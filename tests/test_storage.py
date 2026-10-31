import os

import boto3
import pytest
from moto import mock_aws

from image_crawler.sites import add_sites, read_sites, remove_sites
from image_crawler.storage import build_storage
from image_crawler.storage.local import LocalStorage
from image_crawler.storage.s3 import S3Storage


def exercise(storage):
    key = "ab/" + "ab" + "0" * 62 + ".bin"
    assert not storage.exists(key)
    storage.put(key, b"blob")
    storage.put("index/seg-1.bin", b"idx")
    assert storage.exists(key)
    assert storage.get(key) == b"blob"
    assert sorted(storage.list()) == [key, "index/seg-1.bin"]
    assert list(storage.list("index/")) == ["index/seg-1.bin"]
    assert list(storage.list("thumbs/")) == []
    storage.delete("index/seg-1.bin")
    storage.delete("index/missing.bin")
    assert list(storage.list("index/")) == []


def test_local_storage(tmp_path):
    exercise(LocalStorage(tmp_path))
    assert (tmp_path / "ab" / ("ab" + "0" * 62 + ".bin")).exists()


@mock_aws
def test_s3_storage_and_cors():
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="vault")
    storage = S3Storage("vault", prefix="images", client=client)
    exercise(storage)
    keys = sorted(o["Key"] for o in client.list_objects_v2(Bucket="vault")["Contents"])
    assert keys == ["images/ab/ab" + "0" * 62 + ".bin"]

    storage.set_cors(["https://me.github.io"])
    rule = client.get_bucket_cors(Bucket="vault")["CORSRules"][0]
    assert rule["AllowedOrigins"] == ["https://me.github.io"]
    assert set(rule["AllowedMethods"]) == {"GET", "HEAD"}


def test_build_storage_validates():
    with pytest.raises(ValueError):
        build_storage({"backend": "s3", "s3": {}})
    with pytest.raises(ValueError):
        build_storage({"backend": "nope"})


def test_sites_file(tmp_path):
    path = tmp_path / "sites.txt"
    path.write_text("# my sites\nhttps://a.example/  # comment")
    assert add_sites(path, ["b.example", "https://a.example/"]) == ["https://b.example"]
    assert read_sites(path) == ["https://a.example/", "https://b.example"]
    assert remove_sites(path, ["https://a.example/"]) == ["https://a.example/"]
    assert read_sites(path) == ["https://b.example"]
    assert path.read_text().startswith("# my sites\n")
