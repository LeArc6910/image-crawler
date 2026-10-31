import hashlib
import json
import shutil
import stat
import subprocess
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from image_crawler.archive import Archive
from image_crawler.cli import main, verify_read_only
from image_crawler.crypto import Vault
from image_crawler.storage.s3 import S3Storage
from image_crawler.viewer import make_bundle, open_bundle, seal_bundle

from conftest import make_photo

NODE = shutil.which("node")
SCRIPT = Path(__file__).parent / "js" / "viewer_e2e.mjs"
PASS = "correct horse battery"
FAST = 1_000  # PBKDF2 iterations for tests; the real default is 600k


def bundle(**overrides):
    fields = dict(
        endpoint="https://acct.r2.cloudflarestorage.com/", region="auto", bucket="vault", prefix="/imgs/",
        access_key_id="viewer-id", secret_access_key="viewer-secret", encryption_key=b"\x01" * 32,
    )
    fields.update(overrides)
    return make_bundle(**fields)


def test_bundle_roundtrip_and_wrong_passphrase():
    b = bundle()
    assert b["e"] == "https://acct.r2.cloudflarestorage.com" and b["p"] == "imgs/"
    text = seal_bundle(b, PASS, iterations=FAST)
    assert text.startswith("ICVIEW1.") and "viewer-secret" not in text
    assert open_bundle(text[:40] + "\n " + text[40:], PASS) == b  # whitespace from copy/paste is fine
    with pytest.raises(ValueError, match="wrong passphrase"):
        open_bundle(text, PASS + "x")
    with pytest.raises(ValueError, match="at least"):
        seal_bundle(b, "short")


def denying_client(write_allowed: bool):
    class Client:
        deleted = []

        def list_objects_v2(self, **kw):
            return {}

        def put_object(self, **kw):
            if not write_allowed:
                raise ClientError({"Error": {"Code": "AccessDenied", "Message": "nope"}}, "PutObject")

        def delete_object(self, Bucket, Key):
            self.deleted.append(Key)

    return Client()


def test_verify_read_only():
    verify_read_only(denying_client(write_allowed=False), "vault", "")
    writable = denying_client(write_allowed=True)
    with pytest.raises(ValueError, match="can WRITE"):
        verify_read_only(writable, "vault", "p/")
    assert writable.deleted and writable.deleted[0].startswith("p/check/viewer-write-probe-")


def r2_config(tmp_path, key_file, endpoint="https://acct.r2.cloudflarestorage.com"):
    config = tmp_path / "config.toml"
    config.write_text(
        f'key_file = "{key_file}"\nstate_db = "state.db"\n'
        f'[storage]\nbackend = "s3"\n[storage.s3]\nbucket = "vault"\nendpoint_url = "{endpoint}"\nregion = "us-east-1"\n'
    )
    return config


def test_viewer_export_writes_a_private_bundle(tmp_path, key_file, monkeypatch, capsys):
    monkeypatch.setenv("IMAGE_CRAWLER_VIEWER_KEY_ID", "ro-id")
    monkeypatch.setenv("IMAGE_CRAWLER_VIEWER_SECRET", "ro-secret")
    monkeypatch.setenv("IMAGE_CRAWLER_VIEWER_PASSPHRASE", PASS)
    out, png = tmp_path / "bundle.txt", tmp_path / "bundle.png"
    config = r2_config(tmp_path, key_file)
    assert main(["-c", str(config), "viewer-export", "--no-verify", "--out", str(out), "--qr-png", str(png)]) == 0
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert png.read_bytes().startswith(b"\x89PNG")
    opened = open_bundle(out.read_text(), PASS)
    assert opened["a"] == "ro-id" and opened["b"] == "vault"
    assert opened["k"] == __import__("base64").b64encode(Vault.from_key_file(key_file).encryption_key).decode()
    assert "ro-secret" not in capsys.readouterr().out


def test_viewer_export_requires_s3_endpoint(tmp_path, key_file):
    config = tmp_path / "config.toml"
    config.write_text(f'key_file = "{key_file}"\n[storage.local]\npath = "v"\n')
    assert main(["-c", str(config), "viewer-export", "--no-verify"]) == 2


@mock_aws
def test_check_command(tmp_path, key_file, monkeypatch, capsys):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="vault")
    config = r2_config(tmp_path, key_file, endpoint="https://s3.amazonaws.com")
    assert main(["-c", str(config), "check"]) == 0
    out = capsys.readouterr().out
    assert "✓ write / read / delete a test object" in out and "CORS for the phone viewer: not set yet" in out
    assert boto3.client("s3", region_name="us-east-1").list_objects_v2(Bucket="vault").get("KeyCount") == 0

    def denied(self):
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "Access Denied"}}, "GetBucketCors")

    monkeypatch.setattr(S3Storage, "cors_origins", denied)
    assert main(["-c", str(config), "check"]) == 0  # an Object Read & Write token can't read CORS: warn only
    out = capsys.readouterr().out
    assert "! CORS for the phone viewer: can't read the bucket's CORS policy" in out
    assert "All checks passed (1 warning(s))." in out
    monkeypatch.undo()
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")

    config.write_text(config.read_text().replace('bucket = "vault"', 'bucket = "missing"'))
    assert main(["-c", str(config), "check"]) == 1
    assert "check [storage.s3] bucket" in capsys.readouterr().out


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_js_signer_matches_aws_reference_signatures():
    out = json.loads(subprocess.run([NODE, str(SCRIPT), "sigv4"], capture_output=True, text=True, check=True).stdout)
    # From AWS's Signature V4 documentation examples (also reproduced with botocore).
    assert out == {
        "getObject": "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41",
        "listObjects": "34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7",
    }


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_js_request_budget_blocks_runaway_loops():
    out = json.loads(subprocess.run([NODE, str(SCRIPT), "budget"], capture_output=True, text=True, check=True).stdout)
    # 3 requests per rolling second: the 4th and 5th wait, slots free up as old requests age out.
    assert out["log"] == [
        "0:ok", "100:ok", "200:ok", "300:blocked 700", "999:blocked 1", "1000:ok", "1100:ok", "1150:blocked 50",
    ]
    assert out == {"log": out["log"], "refused": True, "fetched": 0}


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_viewer_reads_archive_over_http(moto_server, tmp_path, key_file, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    client = boto3.client("s3", endpoint_url=moto_server, region_name="us-east-1")
    client.create_bucket(Bucket="vault")
    archive = Archive(Vault.from_key_file(key_file), S3Storage("vault", prefix="my photos", client=client),
                      flush_every=2)
    photos = [make_photo(120, 80), make_photo(30, 30, "PNG"), make_photo(64, 64)]
    for i, photo in enumerate(photos):
        archive.store(photo, {"source_url": f"https://example.com/{i}.jpg", "site": "https://example.com/"})
    archive.flush()

    vault = Vault.from_key_file(key_file)
    text = seal_bundle(
        make_bundle(endpoint=moto_server, region="us-east-1", bucket="vault", prefix="my photos",
                    access_key_id="test", secret_access_key="test", encryption_key=vault.encryption_key),
        PASS, iterations=FAST,
    )
    result = subprocess.run([NODE, str(SCRIPT), "open", text, PASS], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)

    assert out["config"] == {"endpoint": moto_server, "region": "us-east-1", "bucket": "vault", "prefix": "my photos/"}
    assert out["entries"] == archive.read_index()
    assert [i["sha256"] for i in out["images"]] == [hashlib.sha256(p).hexdigest() for p in photos]
    assert all(i["thumb"] for i in out["images"])
    assert out["keyCount"] == 3 + 3 + 2  # originals, thumbnails, index segments
    assert out["missing"] == {"status": 404, "code": "NoSuchKey"}
