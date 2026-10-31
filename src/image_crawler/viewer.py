"""The bundle that hands the phone viewer what it needs: where the bucket is, a
read-only R2 token, and the decryption key — sealed with a passphrase.

The bundle never contains the master key or the object-name key, so a leaked
bundle cannot be used to add or forge objects, and the R2 token in it must be
read-only (`viewer-export` verifies that).

Text form:  "ICVIEW1." + base64url( salt(16) | iterations(u32 BE) | nonce(12) | AES-GCM(json) )
Key:        PBKDF2-HMAC-SHA256(passphrase, salt, iterations) -> 32 bytes;  AAD = b"ICVIEW1"

pwa/js/keystore.js implements the reverse; keep them in sync.
"""

from __future__ import annotations

import base64
import json
import os
import struct

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

PREFIX = "ICVIEW1."
AAD = b"ICVIEW1"
ITERATIONS = 600_000  # OWASP 2023 guidance for PBKDF2-HMAC-SHA256
SALT_SIZE = 16
NONCE_SIZE = 12
MIN_PASSPHRASE = 10


def _derive(passphrase: str, salt: bytes, iterations: int) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
    return kdf.derive(passphrase.encode())


def make_bundle(
    *,
    endpoint: str,
    region: str,
    bucket: str,
    prefix: str,
    access_key_id: str,
    secret_access_key: str,
    encryption_key: bytes,
) -> dict:
    # Short field names keep the QR code small.
    return {
        "v": 1,
        "e": endpoint.rstrip("/"),
        "r": region,
        "b": bucket,
        "p": prefix.strip("/") + "/" if prefix.strip("/") else "",
        "a": access_key_id,
        "s": secret_access_key,
        "k": base64.b64encode(encryption_key).decode(),
    }


def seal_bundle(bundle: dict, passphrase: str, iterations: int = ITERATIONS) -> str:
    if len(passphrase) < MIN_PASSPHRASE:
        raise ValueError(f"passphrase must be at least {MIN_PASSPHRASE} characters")
    salt, nonce = os.urandom(SALT_SIZE), os.urandom(NONCE_SIZE)
    key = _derive(passphrase, salt, iterations)
    plaintext = json.dumps(bundle, separators=(",", ":")).encode()
    blob = salt + struct.pack(">I", iterations) + nonce + AESGCM(key).encrypt(nonce, plaintext, AAD)
    return PREFIX + base64.urlsafe_b64encode(blob).decode().rstrip("=")


def open_bundle(text: str, passphrase: str) -> dict:
    text = "".join(text.split())
    if not text.startswith(PREFIX):
        raise ValueError("not a viewer bundle")
    body = text[len(PREFIX) :]
    blob = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    salt = blob[:SALT_SIZE]
    (iterations,) = struct.unpack(">I", blob[SALT_SIZE : SALT_SIZE + 4])
    nonce = blob[SALT_SIZE + 4 : SALT_SIZE + 4 + NONCE_SIZE]
    key = _derive(passphrase, salt, iterations)
    try:
        plaintext = AESGCM(key).decrypt(nonce, blob[SALT_SIZE + 4 + NONCE_SIZE :], AAD)
    except Exception:
        raise ValueError("wrong passphrase or damaged bundle") from None
    return json.loads(plaintext)
