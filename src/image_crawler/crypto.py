"""Client-side encryption so the storage provider only ever sees opaque blobs.

Every image is sealed with AES-256-GCM before it leaves this machine. The
original URL, file name and content type travel *inside* the ciphertext, and
the object name is a keyed HMAC of the content, so the provider learns neither
what is stored nor where it came from. Plaintext is padded to 64 KiB buckets
to blur exact file sizes.

Blob layout:  MAGIC(4) | nonce(12) | AES-GCM( header | metadata | content | padding )
Header:       metadata length (u32, big endian) | content length (u64, big endian)
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

MAGIC = b"ICV1"
NONCE_SIZE = 12
KEY_SIZE = 32
PAD_BLOCK = 64 * 1024
_HEADER = struct.Struct(">IQ")


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
        key = base64.b64decode(path.read_text().strip(), validate=True)
    except FileNotFoundError:
        raise FileNotFoundError(
            f"key file not found: {path} (create one with `image-crawler keygen`)"
        ) from None
    if len(key) != KEY_SIZE:
        raise ValueError(f"key file {path} must contain {KEY_SIZE} base64-encoded bytes")
    return key


def _derive(master_key: bytes, label: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=KEY_SIZE, salt=None, info=label).derive(
        master_key
    )


@dataclass
class Opened:
    metadata: dict
    content: bytes


class Vault:
    def __init__(self, master_key: bytes):
        self._aead = AESGCM(_derive(master_key, b"image-crawler/v1/encrypt"))
        self._name_key = _derive(master_key, b"image-crawler/v1/object-name")

    @classmethod
    def from_key_file(cls, path: Path) -> "Vault":
        return cls(load_key_file(path))

    def object_name(self, content: bytes) -> str:
        """Deterministic, keyed name: identical images dedupe, but the name reveals nothing."""
        return hmac.new(self._name_key, content, hashlib.sha256).hexdigest()

    def seal(self, content: bytes, metadata: dict) -> bytes:
        meta = json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode()
        body = _HEADER.pack(len(meta), len(content)) + meta + content
        body += b"\0" * (-len(body) % PAD_BLOCK)
        nonce = os.urandom(NONCE_SIZE)
        return MAGIC + nonce + self._aead.encrypt(nonce, body, MAGIC)

    def open(self, blob: bytes) -> Opened:
        if not blob.startswith(MAGIC) or len(blob) < len(MAGIC) + NONCE_SIZE:
            raise DecryptionError("not an image-crawler blob")
        nonce = blob[len(MAGIC) : len(MAGIC) + NONCE_SIZE]
        try:
            body = self._aead.decrypt(nonce, blob[len(MAGIC) + NONCE_SIZE :], MAGIC)
        except Exception as e:  # InvalidTag
            raise DecryptionError("wrong key or corrupted blob") from e
        meta_len, content_len = _HEADER.unpack_from(body)
        start = _HEADER.size
        metadata = json.loads(body[start : start + meta_len])
        content = body[start + meta_len : start + meta_len + content_len]
        return Opened(metadata=metadata, content=content)
