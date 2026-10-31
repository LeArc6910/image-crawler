from datetime import date, datetime, timezone

import boto3
import pytest
from moto import mock_aws

from image_crawler import layout
from image_crawler.cli import EXIT_QUOTA, main
from image_crawler.cloudflare import GRAPHQL_URL, classify, fetch_r2_usage
from image_crawler.config import QuotaSettings
from image_crawler.quota import (
    FREE_TIER,
    GB,
    GuardedStorage,
    QuotaExceeded,
    Usage,
    billing_period,
    measure,
)
from image_crawler.state import State
from image_crawler.storage.local import LocalStorage

from conftest import make_photo

IMAGE_KEY = layout.image_key("ab" + "0" * 62)
PERIOD = "2026-11-01"


def guarded(tmp_path, baseline=Usage(), state=None, **settings):
    state = state or State(":memory:")
    inner = LocalStorage(tmp_path / "vault")
    return GuardedStorage(inner, QuotaSettings(**settings), state, baseline, PERIOD), state


@pytest.mark.parametrize(
    "today, day, expected",
    [
        (date(2026, 11, 15), 1, (date(2026, 11, 1), date(2026, 12, 1))),
        (date(2026, 11, 3), 5, (date(2026, 10, 5), date(2026, 11, 5))),
        (date(2026, 12, 20), 10, (date(2026, 12, 10), date(2027, 1, 10))),
        (date(2026, 1, 2), 5, (date(2025, 12, 5), date(2026, 1, 5))),
    ],
)
def test_billing_period(today, day, expected):
    assert billing_period(today, day) == expected


def test_billing_day_is_validated():
    with pytest.raises(ValueError):
        billing_period(date(2026, 1, 1), 31)


def test_requests_are_metered_by_class_and_persisted(tmp_path):
    storage, state = guarded(tmp_path)
    storage.put(IMAGE_KEY, b"x" * 100)
    storage.put("index/seg.bin", b"y" * 10)
    assert storage.get(IMAGE_KEY) == b"x" * 100
    assert storage.exists("thumbs/zz/none.bin") is False
    assert sorted(storage.list()) == ["ab/" + IMAGE_KEY.split("/")[1], "index/seg.bin"]

    assert storage.used == Usage(storage_bytes=110, class_a=3, class_b=2)
    target = storage.describe()
    assert state.ops(target, PERIOD) == (3, 2)
    assert state.stored_bytes(target) == 110
    storage.put(IMAGE_KEY, b"x" * 100)  # overwrite: counted once in the local total
    assert state.stored_bytes(target) == 110


def test_listing_is_charged_per_page_of_1000_keys(tmp_path):
    storage, _ = guarded(tmp_path)
    for i in range(2500):
        storage.inner.put(f"index/{i:05d}.bin", b"")
    assert len(list(storage.list("index/"))) == 2500
    assert storage.used.class_a == 3


def test_originals_stop_at_soft_limit_but_thumbnails_and_index_may_finish(tmp_path):
    storage, _ = guarded(tmp_path, storage_limit_gb=150 / GB)
    storage.put(IMAGE_KEY, b"x" * 100)
    with pytest.raises(QuotaExceeded, match="storage"):
        storage.put(layout.image_key("cd" + "0" * 62), b"x" * 100)
    assert not storage.inner.exists(layout.image_key("cd" + "0" * 62))  # refused before upload
    storage.put("thumbs/ab/t.bin", b"t" * 100)  # past the soft limit, under the free tier
    storage.put("index/seg.bin", b"i" * 100)
    assert storage.used.storage_bytes == 300


def test_free_tier_is_a_hard_stop_for_everything(tmp_path):
    storage, _ = guarded(tmp_path, baseline=Usage(storage_bytes=FREE_TIER.storage_bytes - 10))
    with pytest.raises(QuotaExceeded):
        storage.put("index/seg.bin", b"i" * 11)


def test_operation_limits_and_per_run_image_cap(tmp_path):
    storage, _ = guarded(tmp_path, baseline=Usage(class_b=9), class_b_limit=10)
    assert storage.exists("nope") is False
    with pytest.raises(QuotaExceeded, match="Class B"):
        storage.exists("nope")

    storage, _ = guarded(tmp_path, baseline=Usage(class_a=900_000))
    with pytest.raises(QuotaExceeded, match="Class A"):
        storage.put(IMAGE_KEY, b"x")

    storage, _ = guarded(tmp_path, max_images_per_run=2)
    storage.put(layout.image_key("a1" + "0" * 62), b"x")
    storage.put(layout.image_key("a2" + "0" * 62), b"x")
    storage.put("thumbs/a3/t.bin", b"x")  # thumbnails don't count toward the cap
    with pytest.raises(QuotaExceeded, match="max_images_per_run"):
        storage.put(layout.image_key("a3" + "0" * 62), b"x")


def test_classify_actions():
    assert classify("PutObject") == "A"
    assert classify("ListObjectsV2") == "A"
    assert classify("SomethingNew") == "A"  # unknown: assume the pricier class
    assert classify("GetObject") == "B"
    assert classify("HeadObject") == "B"
    assert classify("DeleteObject") is None


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


class FakeSession:
    def __init__(self, ops, storage, errors=None):
        self.ops, self.storage, self.errors = ops, storage, errors
        self.calls = []

    def post(self, url, json, headers, timeout):
        self.calls.append((url, json, headers))
        if self.errors:
            return FakeResponse({"data": None, "errors": [{"message": m} for m in self.errors]})
        key = "r2OperationsAdaptiveGroups" if "r2OperationsAdaptiveGroups" in json["query"] else "r2StorageAdaptiveGroups"
        rows = self.ops if key == "r2OperationsAdaptiveGroups" else self.storage
        return FakeResponse({"data": {"viewer": {"accounts": [{key: rows}]}}, "errors": None})


def op(action, n):
    return {"sum": {"requests": n}, "dimensions": {"actionType": action}}


def sample(bucket, when, payload, metadata=0):
    return {
        "max": {"payloadSize": payload, "metadataSize": metadata, "objectCount": 1},
        "dimensions": {"bucketName": bucket, "datetime": when},
    }


def test_fetch_r2_usage_sums_classes_and_latest_storage_per_bucket():
    session = FakeSession(
        ops=[op("PutObject", 10), op("ListObjects", 2), op("GetObject", 100), op("HeadObject", 5),
             op("DeleteObject", 7), op("PutObject", 1)],
        storage=[  # newest first, as requested with orderBy datetime_DESC
            sample("vault", "2026-11-10T10:00:00Z", 5_000, 100),
            sample("other", "2026-11-10T09:00:00Z", 2_000),
            sample("vault", "2026-11-10T09:00:00Z", 4_000),
        ],
    )
    now = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
    usage = fetch_r2_usage("acct", "tok", datetime(2026, 11, 1, tzinfo=timezone.utc), now, session)
    assert (usage.class_a, usage.class_b) == (13, 105)
    assert usage.storage_by_bucket == {"vault": 5_100, "other": 2_000}
    assert usage.storage_bytes == 7_100

    url, body, headers = session.calls[0]
    assert url == GRAPHQL_URL and headers["Authorization"] == "Bearer tok"
    assert body["variables"] == {
        "accountTag": "acct", "startDate": "2026-11-01T00:00:00Z", "endDate": "2026-11-10T12:00:00Z",
    }
    assert session.calls[1][1]["variables"]["startDate"] == "2026-11-08T12:00:00Z"


def test_fetch_r2_usage_reports_api_errors():
    with pytest.raises(RuntimeError, match="not authorized"):
        fetch_r2_usage("acct", "tok", datetime(2026, 11, 1, tzinfo=timezone.utc),
                       session=FakeSession([], [], errors=["not authorized"]))


def test_measure_uses_the_larger_of_local_and_cloudflare(monkeypatch):
    state = State(":memory:")
    state.add_ops("s3://v/", "2026-11-01", class_a=50, class_b=1)
    state.record_stored("s3://v/", "k", 900)
    settings = QuotaSettings(cloudflare_account_id="acct")

    class Reported:
        storage_bytes, class_a, class_b = 500, 10, 99

    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    report = measure(settings, state, "s3://v/", date(2026, 11, 20), fetch=lambda *a: Reported())
    assert report.local == Usage(900, 50, 1)
    assert report.cloudflare == Usage(500, 10, 99)
    assert report.effective == Usage(900, 50, 99)
    assert report.period_start == date(2026, 11, 1)

    def broken(*a):
        raise RuntimeError("network down")

    report = measure(settings, state, "s3://v/", date(2026, 11, 20), fetch=broken)
    assert report.cloudflare is None and "network down" in report.cloudflare_error
    assert report.effective == Usage(900, 50, 1)

    monkeypatch.delenv("CLOUDFLARE_API_TOKEN")
    report = measure(settings, state, "s3://v/", date(2026, 11, 20), fetch=broken)
    assert report.cloudflare_error == "CLOUDFLARE_API_TOKEN is not set"


@mock_aws
def test_crawl_stops_at_quota_and_keeps_index_consistent(site, tmp_path, key_file, capsys, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="vault")
    photos = [make_photo(200, 200) for _ in range(5)]
    site.page("/", "".join(f'<img src="/{i}.jpg">' for i in range(5)))
    for i, photo in enumerate(photos):
        site.image(f"/{i}.jpg", photo, ctype="image/jpeg")

    config = tmp_path / "config.toml"
    config.write_text(
        f'key_file = "{key_file}"\nstate_db = "state.db"\n'
        "[crawl]\ndelay_seconds = 0\nmin_image_bytes = 100\n"
        '[storage]\nbackend = "s3"\n[storage.s3]\nbucket = "vault"\nregion = "us-east-1"\n'
        "[quota]\nmax_images_per_run = 2\n"
    )
    assert main(["-c", str(config), "crawl", "--site", site.url + "/"]) == EXIT_QUOTA
    out = capsys.readouterr().out
    assert "2 stored" in out and "stopped early: reached max_images_per_run (2)" in out

    # Both stored originals made it into the index before the run stopped.
    assert main(["-c", str(config), "reindex"]) == 0
    assert "2 indexed, 0 missing" in capsys.readouterr().out

    assert main(["-c", str(config), "usage"]) == 0
    usage = capsys.readouterr().out
    assert "storage" in usage and "Class A" in usage and "counts cover this machine only" in usage

    # A tight storage limit is enforced before any crawling starts.
    with config.open("a") as f:
        f.write("storage_limit_gb = 0.000001\n")
    assert main(["-c", str(config), "crawl", "--site", site.url + "/"]) == EXIT_QUOTA
    assert "R2 quota already reached (storage)" in capsys.readouterr().out
    assert main(["-c", str(config), "usage", "--check"]) == 1
