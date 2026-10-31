"""Client-side encryption so the storage provider only ever sees opaque blobs.

Every image, thumbnail and index segment is sealed with AES-256-GCM before it
leaves this machine. The original URL, file name and content type travel
*inside* the ciphertext, and object names are keyed HMACs, so the provider
learns neither what is stored nor where it came from. Plaintext is padded to
fixed-size buckets to blur exact sizes.

Blob layout:  MAGIC(4) | nonce(12) | AES-GCM( header | metadata | content | padding )
Header:       metadata length (u32, big endian) | content length (u64, big endian)
AAD (v2):     b"ICV2|<kind>|<name>" — binds a blob to its slot, so a malicious
              server cannot swap one valid blob for another.
AAD (v1):     b"ICV1" — still accepted when opening.

pwa/js/vault.js implements the same format for the browser; keep them in sync.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC_V1 = b"ICV1"
MAGIC = b"ICV2"
NONCE_SIZE = 12
KEY_SIZE = 32
PAD_BLOCK = 64 * 1024
SMALL_PAD_BLOCK = 4 * 1024
_HEADER = struct.Struct(">IQ")

KIND_IMAGE = "image"
KIND_THUMB = "thumb"
KIND_INDEX = "index"
KIND_STATE = "state"

# Where a machine without a key file (e.g. a cloud runner) takes the master key from:
# the same single base64 line that `image-crawler keygen` writes to the key file.
KEY_ENV = "IMAGE_CRAWLER_KEY"


class DecryptionError(Exception):
    pass


def generate_key_file(path: Path, overwrite: bool = False) -> None:
    path = path.expanduser()
    if path.exists() and not overwrite:
        raise FileExistsError(f"key file already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(base64.b64encode(os.urandom(KEY_SIZE)).decode() + "\n")


def load_key_file(path: Path) -> bytes:
    path = path.expanduser()
    try:
        text = path.read_text()
    except FileNotFoundError:
        raise FileNotFoundError(
            f"key file not found: {path} (create one with `image-crawler keygen`, "
            f"or set {KEY_ENV})"
        ) from None
    return _decode_key(text, f"key file {path}")


def _decode_key(text: str, source: str) -> bytes:
    try:
        key = base64.b64decode(text.strip(), validate=True)
    except ValueError:
        key = b""
    if len(key) != KEY_SIZE:
        raise ValueError(f"{source} must contain {KEY_SIZE} base64-encoded bytes")
    return key


def key_source(key_file: Path) -> str:
    """Where load_master_key() will read the key from, for messages."""
    return f"{KEY_ENV} (environment)" if os.environ.get(KEY_ENV, "").strip() else str(key_file)


def load_master_key(key_file: Path) -> bytes:
    """The master key from $IMAGE_CRAWLER_KEY if it is set, otherwise from the key file."""
    if text := os.environ.get(KEY_ENV, "").strip():
        return _decode_key(text, KEY_ENV)
    return load_key_file(key_file)


def _derive(master_key: bytes, label: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=KEY_SIZE, salt=None, info=label).derive(
        master_key
    )


def _aad(kind: str, name: str) -> bytes:
    return b"|".join((MAGIC, kind.encode(), name.encode()))


@dataclass
class Opened:
    metadata: dict
    content: bytes


class Vault:
    def __init__(self, master_key: bytes):
        self.encryption_key = _derive(master_key, b"image-crawler/v1/encrypt")
        self._aead = AESGCM(self.encryption_key)
        self._name_key = _derive(master_key, b"image-crawler/v1/object-name")

    @classmethod
    def from_key_file(cls, path: Path) -> "Vault":
        return cls(load_key_file(path))

    @classmethod
    def load(cls, key_file: Path) -> "Vault":
        """$IMAGE_CRAWLER_KEY if set, else the key file (see load_master_key)."""
        return cls(load_master_key(key_file))

    def object_name(self, content: bytes) -> str:
        """Deterministic, keyed name: identical images dedupe, but the name reveals nothing."""
        return hmac.new(self._name_key, content, hashlib.sha256).hexdigest()

    def thumb_name(self, object_name: str) -> str:
        """Unlinkable to its image for anyone without the key."""
        return hmac.new(self._name_key, b"thumb:" + object_name.encode(), hashlib.sha256).hexdigest()

    def seal(
        self, content: bytes, metadata: dict, *, kind: str, name: str, pad_block: int = PAD_BLOCK
    ) -> bytes:
        meta = json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode()
        body = _HEADER.pack(len(meta), len(content)) + meta + content
        body += b"\0" * (-len(body) % pad_block)
        nonce = os.urandom(NONCE_SIZE)
        return MAGIC + nonce + self._aead.encrypt(nonce, body, _aad(kind, name))

    def open(self, blob: bytes, *, kind: str, name: str) -> Opened:
        magic = blob[: len(MAGIC)]
        if magic == MAGIC:
            aad = _aad(kind, name)
        elif magic == MAGIC_V1 and kind == KIND_IMAGE:
            aad = MAGIC_V1
        else:
            raise DecryptionError("not an image-crawler blob")
        if len(blob) < len(MAGIC) + NONCE_SIZE:
            raise DecryptionError("truncated blob")
        nonce = blob[len(MAGIC) : len(MAGIC) + NONCE_SIZE]
        try:
            body = self._aead.decrypt(nonce, blob[len(MAGIC) + NONCE_SIZE :], aad)
        except Exception as e:  # InvalidTag
            raise DecryptionError("wrong key, wrong slot, or corrupted blob") from e
        meta_len, content_len = _HEADER.unpack_from(body)
        start = _HEADER.size
        metadata = json.loads(body[start : start + meta_len])
        content = body[start + meta_len : start + meta_len + content_len]
        return Opened(metadata=metadata, content=content)
