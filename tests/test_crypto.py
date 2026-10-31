import os
import stat

import pytest

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from image_crawler.crypto import (
    KIND_IMAGE,
    KIND_THUMB,
    PAD_BLOCK,
    SMALL_PAD_BLOCK,
    DecryptionError,
    Vault,
    generate_key_file,
    load_key_file,
)

IMG = {"kind": KIND_IMAGE, "name": "a" * 64}


def test_seal_open_roundtrip():
    vault = Vault(os.urandom(32))
    content = os.urandom(1000)
    meta = {"source_url": "https://example.com/한글.png", "content_type": "image/png"}
    blob = vault.seal(content, meta, **IMG)
    opened = vault.open(blob, **IMG)
    assert opened.content == content
    assert opened.metadata == meta


def test_blob_hides_content_and_metadata():
    vault = Vault(os.urandom(32))
    content = b"\x89PNG" + b"A" * 5000
    blob = vault.seal(content, {"source_url": "https://secret.example/cat.png"}, **IMG)
    assert b"secret.example" not in blob
    assert b"AAAAAAAA" not in blob


def test_sizes_are_padded_to_buckets():
    vault = Vault(os.urandom(32))
    small = vault.seal(os.urandom(10), {}, **IMG)
    larger = vault.seal(os.urandom(30_000), {"x": "y"}, **IMG)
    assert len(small) == len(larger)
    assert (len(small) - 4 - 12 - 16) % PAD_BLOCK == 0
    thumb = vault.seal(os.urandom(10), {}, kind=KIND_THUMB, name="t", pad_block=SMALL_PAD_BLOCK)
    assert len(thumb) - 4 - 12 - 16 == SMALL_PAD_BLOCK


def test_wrong_key_is_rejected():
    blob = Vault(os.urandom(32)).seal(b"data", {}, **IMG)
    with pytest.raises(DecryptionError):
        Vault(os.urandom(32)).open(blob, **IMG)


def test_tampering_is_detected():
    vault = Vault(os.urandom(32))
    blob = bytearray(vault.seal(b"data", {}, **IMG))
    blob[-1] ^= 1
    with pytest.raises(DecryptionError):
        vault.open(bytes(blob), **IMG)


def test_blob_is_bound_to_its_slot():
    vault = Vault(os.urandom(32))
    blob = vault.seal(b"data", {}, **IMG)
    with pytest.raises(DecryptionError):
        vault.open(blob, kind=KIND_IMAGE, name="b" * 64)  # swapped with another image
    with pytest.raises(DecryptionError):
        vault.open(blob, kind=KIND_THUMB, name="a" * 64)  # served as a thumbnail


def test_v1_image_blobs_still_open():
    vault = Vault(os.urandom(32))
    body = (4).to_bytes(4, "big") + (3).to_bytes(8, "big") + b"{}  " + b"abc"
    nonce = os.urandom(12)
    blob = b"ICV1" + nonce + AESGCM(vault.encryption_key).encrypt(nonce, body, b"ICV1")
    assert vault.open(blob, **IMG).content == b"abc"
    with pytest.raises(DecryptionError):
        vault.open(blob, kind=KIND_THUMB, name="x")


def test_object_name_is_keyed_and_deterministic():
    key = os.urandom(32)
    data = os.urandom(100)
    assert Vault(key).object_name(data) == Vault(key).object_name(data)
    assert Vault(key).object_name(data) != Vault(os.urandom(32)).object_name(data)
    name = Vault(key).object_name(data)
    assert Vault(key).thumb_name(name) not in (name, Vault(os.urandom(32)).thumb_name(name))


def test_key_file(tmp_path):
    path = tmp_path / "sub" / "key"
    generate_key_file(path)
    assert len(load_key_file(path)) == 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        generate_key_file(path)
