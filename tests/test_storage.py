import os

import boto3
import pytest
from moto import mock_aws

from image_crawler.sites import add_sites, read_sites, remove_sites
from image_crawler.storage import build_storage
from image_crawler.storage.local import LocalStorage
from image_crawler.storage.s3 import S3Storage


def exercise(storage):
    name = "ab" + "0" * 62
    assert not storage.exists(name)
    storage.put(name, b"blob")
    assert storage.exists(name)
    assert storage.get(name) == b"blob"
    assert list(storage.list_names()) == [name]


def test_local_storage(tmp_path):
    exercise(LocalStorage(tmp_path))
    assert (tmp_path / "ab" / ("ab" + "0" * 62 + ".bin")).exists()


@mock_aws
def test_s3_storage():
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="vault")
    exercise(S3Storage("vault", prefix="images", client=client))
    keys = [o["Key"] for o in client.list_objects_v2(Bucket="vault")["Contents"]]
    assert keys == ["images/ab/ab" + "0" * 62 + ".bin"]


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
