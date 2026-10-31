import os
import stat

import pytest

from image_crawler.crypto import PAD_BLOCK, DecryptionError, Vault, generate_key_file, load_key_file


def test_seal_open_roundtrip():
    vault = Vault(os.urandom(32))
    content = os.urandom(1000)
    meta = {"source_url": "https://example.com/한글.png", "content_type": "image/png"}
    blob = vault.seal(content, meta)
    opened = vault.open(blob)
    assert opened.content == content
    assert opened.metadata == meta


def test_blob_hides_content_and_metadata():
    vault = Vault(os.urandom(32))
    content = b"\x89PNG" + b"A" * 5000
    blob = vault.seal(content, {"source_url": "https://secret.example/cat.png"})
    assert b"secret.example" not in blob
    assert b"AAAAAAAA" not in blob


def test_sizes_are_padded_to_buckets():
    vault = Vault(os.urandom(32))
    small = vault.seal(os.urandom(10), {})
    larger = vault.seal(os.urandom(30_000), {"x": "y"})
    assert len(small) == len(larger)
    assert (len(small) - 4 - 12 - 16) % PAD_BLOCK == 0


def test_wrong_key_is_rejected():
    blob = Vault(os.urandom(32)).seal(b"data", {})
    with pytest.raises(DecryptionError):
        Vault(os.urandom(32)).open(blob)


def test_tampering_is_detected():
    vault = Vault(os.urandom(32))
    blob = bytearray(vault.seal(b"data", {}))
    blob[-1] ^= 1
    with pytest.raises(DecryptionError):
        vault.open(bytes(blob))


def test_object_name_is_keyed_and_deterministic():
    key = os.urandom(32)
    data = os.urandom(100)
    assert Vault(key).object_name(data) == Vault(key).object_name(data)
    assert Vault(key).object_name(data) != Vault(os.urandom(32)).object_name(data)


def test_key_file(tmp_path):
    path = tmp_path / "sub" / "key"
    generate_key_file(path)
    assert len(load_key_file(path)) == 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        generate_key_file(path)
