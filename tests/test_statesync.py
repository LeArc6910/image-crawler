"""Key from the environment and state_sync: a fresh machine (cloud runner) picks up where the last left off."""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from image_crawler import layout
from image_crawler.cli import main
from image_crawler.crypto import KEY_ENV, Vault, generate_key_file
from image_crawler.state import State
from image_crawler.statesync import StateSync, local_generation, read_snapshot
from image_crawler.storage.local import LocalStorage

from conftest import make_png, make_photo


def machine(tmp_path, name: str, storage: str, *, sync: bool = True) -> str:
    """A working directory with its own config/state/sites, sharing the storage. Returns the config path."""
    home = tmp_path / name
    home.mkdir()
    config = home / "config.toml"
    config.write_text(
        'key_file = "no-such-key"\nstate_db = "state.db"\nsites_file = "sites.txt"\n'
        f"state_sync = {'true' if sync else 'false'}\n"
        "[crawl]\ndelay_seconds = 0\nmin_image_bytes = 100\n" + storage
    )
    return str(config)


def local_storage(tmp_path) -> str:
    return f'[storage]\nbackend = "local"\n[storage.local]\npath = "{tmp_path / "vault"}"\n'


@pytest.fixture
def env_key(key_file, monkeypatch):
    """The master key only in $IMAGE_CRAWLER_KEY (no key file on the machines)."""
    monkeypatch.setenv(KEY_ENV, key_file.read_text().strip())
    return key_file


def test_key_from_environment(tmp_path, key_file, site, monkeypatch, capsys):
    site.page("/", '<img src="/a.png">')
    site.image("/a.png", make_png(2000))
    config = machine(tmp_path, "pc", local_storage(tmp_path), sync=False)

    assert main(["-c", config, "crawl", "--site", site.url + "/"]) == 2
    assert "key file not found" in capsys.readouterr().err

    monkeypatch.setenv(KEY_ENV, key_file.read_text().strip())
    assert main(["-c", config, "crawl", "--site", site.url + "/"]) == 0
    assert "1 stored" in capsys.readouterr().out
    # Same key either way: the key file opens what the environment key stored.
    vault = Vault.from_key_file(key_file)
    (blob,) = [p for p in (tmp_path / "vault").glob("??/*.bin")]
    assert vault.open(blob.read_bytes(), kind="image", name=blob.stem).metadata["source_url"].endswith("/a.png")

    assert main(["-c", config, "check"]) == 0
    assert f"encryption key: {KEY_ENV} (environment)" in capsys.readouterr().out

    monkeypatch.setenv(KEY_ENV, "not-a-key")
    assert main(["-c", config, "crawl", "--site", site.url + "/"]) == 2
    assert f"{KEY_ENV} must contain 32 base64-encoded bytes" in capsys.readouterr().err


def test_fresh_machine_continues_from_backup(tmp_path, env_key, site, capsys):
    photos = [make_png(2000 + i) for i in range(3)]
    site.page("/", "".join(f'<img src="/{i}.png">' for i in range(3)))
    for i, photo in enumerate(photos):
        site.image(f"/{i}.png", photo)
    storage = local_storage(tmp_path)

    first = machine(tmp_path, "run1", storage)
    assert main(["-c", first, "add", site.url + "/"]) == 0
    assert "crawl state backed up to storage (generation 1)" in capsys.readouterr().out
    assert main(["-c", first, "crawl"]) == 0
    out = capsys.readouterr().out
    assert "3 stored" in out and "(generation 2)" in out

    # The backup is an ordinary sealed blob: neither the site URL nor the image URLs show.
    blob = (tmp_path / "vault" / layout.STATE_KEY).read_bytes()
    assert site.url.encode() not in blob and b"/0.png" not in blob
    assert layout.image_name(layout.STATE_KEY) is None  # never mistaken for an image

    # A new, empty machine: the site list and history come back, so nothing is fetched again.
    site.hits.clear()
    second = machine(tmp_path, "run2", storage)
    assert main(["-c", second, "sites"]) == 0
    out = capsys.readouterr().out
    assert site.url + "/" in out and "backed up" not in out  # reading changes nothing: no upload
    assert main(["-c", second, "crawl"]) == 0
    out = capsys.readouterr().out
    assert "0 stored" in out and "3 already seen" in out
    assert not any(h.endswith(".png") for h in site.hits)

    # The index and restore are unaffected by the extra object.
    assert main(["-c", second, "reindex"]) == 0
    assert "3 indexed, 0 missing" in capsys.readouterr().out


def test_stale_local_state_is_replaced_not_pushed(tmp_path, env_key, site, capsys):
    site.page("/", '<img src="/a.png">')
    site.image("/a.png", make_png(2000))
    storage = local_storage(tmp_path)
    pc, cloud = machine(tmp_path, "pc", storage), machine(tmp_path, "cloud", storage)

    assert main(["-c", pc, "add", site.url + "/"]) == 0  # generation 1, PC has it locally
    assert main(["-c", cloud, "crawl"]) == 0  # cloud pulls 1, pushes 2
    capsys.readouterr()
    pc_db = tmp_path / "pc" / "state.db"
    assert local_generation(pc_db) == 1

    # The PC's next command takes the newer history first (keeping its old copy aside)...
    assert main(["-c", pc, "crawl"]) == 0
    assert "1 already seen" in capsys.readouterr().out
    assert (tmp_path / "pc" / "state.db.bak").exists()
    # ...and since that run changed nothing, nothing was uploaded: the backup is still the cloud's.
    vault = Vault.from_key_file(env_key)
    snapshot = read_snapshot(vault, LocalStorage(tmp_path / "vault"))
    assert snapshot.generation == 2 and local_generation(pc_db) == 2

    # A change on the PC now builds on generation 2.
    assert main(["-c", pc, "remove", site.url + "/"]) == 0
    assert "(generation 3)" in capsys.readouterr().out
    assert read_snapshot(vault, LocalStorage(tmp_path / "vault")).sites == ""


def test_push_refuses_to_overwrite_a_newer_backup(tmp_path, key_file):
    vault = Vault.from_key_file(key_file)
    storage = LocalStorage(tmp_path / "vault")

    def open_sync(name):
        sync = StateSync(vault, storage, tmp_path / name / "state.db", tmp_path / name / "sites.txt")
        sync.pull()
        state = State(sync.state_db)
        sync.begin(state)
        return sync, state

    a, a_state = open_sync("a")
    b, b_state = open_sync("b")
    a_state.add_object("x")
    assert a.push(a_state, storage) == 1
    b_state.add_object("y")
    with pytest.raises(RuntimeError, match="changed while this command ran"):
        b.push(b_state, storage)
    assert read_snapshot(vault, storage).generation == 1


def test_wrong_key_cannot_read_backup(tmp_path, env_key, site, monkeypatch, capsys):
    site.page("/", '<img src="/a.png">')
    site.image("/a.png", make_png(2000))
    storage = local_storage(tmp_path)
    assert main(["-c", machine(tmp_path, "a", storage), "add", site.url + "/"]) == 0

    other = tmp_path / "other-key"
    generate_key_file(other)
    monkeypatch.setenv(KEY_ENV, other.read_text().strip())
    capsys.readouterr()
    assert main(["-c", machine(tmp_path, "b", storage), "crawl"]) == 2
    assert "cannot decrypt the crawl state backup" in capsys.readouterr().err
    assert not (tmp_path / "b" / "state.db").exists()  # nothing ran on an empty history


@mock_aws
def test_quota_meter_survives_fresh_machines(tmp_path, env_key, site, capsys, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="vault")
    site.page("/", "".join(f'<img src="/{i}.jpg">' for i in range(3)))
    for i in range(3):
        site.image(f"/{i}.jpg", make_photo(200, 200), ctype="image/jpeg")
    s3 = '[storage]\nbackend = "s3"\n[storage.s3]\nbucket = "vault"\nregion = "us-east-1"\n'

    first = machine(tmp_path, "run1", s3)
    assert main(["-c", first, "crawl", "--site", site.url + "/"]) == 0
    assert "not backed up" not in capsys.readouterr().err
    stored = State(tmp_path / "run1" / "state.db").stored_bytes("s3://vault/")
    assert stored > 0

    second = machine(tmp_path, "run2", s3)
    assert main(["-c", second, "check"]) == 0
    out = capsys.readouterr().out
    assert "crawl state backup (state_sync): generation 1" in out and "3 image URL(s)" in out

    # A fresh machine sees this period's usage (stored bytes and requests), not zero.
    third = machine(tmp_path, "run3", s3)
    assert main(["-c", third, "usage"]) == 0
    assert " 0.0 MB" not in capsys.readouterr().out
    # Exact, including the backup blob itself (recorded before the snapshot is taken).
    assert State(tmp_path / "run3" / "state.db").stored_bytes("s3://vault/") == stored
