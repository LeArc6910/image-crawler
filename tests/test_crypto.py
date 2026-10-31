import os
import stat

import pytest

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from image_crawler.crypto import (
    KIND_IMAGE,
    KIND_THUMB,
    SMALL_PAD_BLOCK,
    DecryptionError,
    Vault,
    generate_key_file,
    load_key_file,
    padme,
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


OVERHEAD = 4 + 12 + 16  # magic, nonce, GCM tag


def test_sizes_are_padded():
    vault = Vault(os.urandom(32))
    # Tiny images and thumbnails all fill the 4 KiB minimum.
    small = vault.seal(os.urandom(10), {}, **IMG)
    thumb = vault.seal(os.urandom(1000), {"x": "y"}, kind=KIND_THUMB, name="t")
    assert len(small) - OVERHEAD == len(thumb) - OVERHEAD == SMALL_PAD_BLOCK
    # Nearby sizes land in the same Padmé bucket; the overhead stays small.
    a = vault.seal(os.urandom(37_000), {}, **IMG)
    b = vault.seal(os.urandom(38_500), {}, **IMG)
    assert len(a) == len(b) and len(a) - OVERHEAD == 38_912
    index = vault.seal(os.urandom(10), {}, kind="index", name="i", pad_block=SMALL_PAD_BLOCK)
    assert len(index) - OVERHEAD == SMALL_PAD_BLOCK


def test_padme_bounds():
    previous = 0
    for length in [*range(2, 5000), *range(5000, 10_000_000, 9_973)]:
        padded = padme(length)
        assert padded >= length and padded >= previous  # never shrinks, never reorders
        assert padded - length <= length * 0.12          # Padmé's overhead bound
        previous = padded
    assert len({padme(n) for n in range(32_768, 65_536)}) == 17  # 2 KiB steps at 32-64 KB


def test_blobs_padded_the_old_way_still_open():
    vault = Vault(os.urandom(32))
    content = os.urandom(30_000)
    old = vault.seal(content, {"a": 1}, **IMG, pad_block=64 * 1024)  # how images were padded before
    assert len(old) - OVERHEAD == 64 * 1024
    assert vault.open(old, **IMG).content == content


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
