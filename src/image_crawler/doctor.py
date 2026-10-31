"""`image-crawler check`: verify config, key, credentials and bucket access step by step."""

from __future__ import annotations

import secrets
from typing import Callable

from image_crawler.crypto import load_key_file
from image_crawler.state import State
from image_crawler.storage import Storage
from image_crawler.storage.s3 import S3Storage

HINTS = {
    "InvalidAccessKeyId": "the access key id is wrong — check AWS_ACCESS_KEY_ID",
    "SignatureDoesNotMatch": "the secret is wrong — check AWS_SECRET_ACCESS_KEY",
    "NoSuchBucket": "the bucket doesn't exist — check [storage.s3] bucket",
    "AccessDenied": "the token lacks permission — the crawler needs Object Read & Write on this bucket",
    "Unauthorized": "the token was rejected — check the token and that it covers this bucket",
    "EndpointConnectionError": "cannot reach the endpoint — check [storage.s3] endpoint_url (and network access)",
    "NoCredentialsError": "no credentials — set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY",
}


def explain(error: Exception) -> str:
    response = getattr(error, "response", None) or {}
    code = response.get("Error", {}).get("Code")
    for name in (code, type(error).__name__):
        if name in HINTS:
            return f"{error} → {HINTS[name]}"
    return str(error)


class Report:
    def __init__(self, out: Callable[[str], None] = print):
        self.out = out
        self.failed = 0

    def step(self, label: str, fn: Callable[[], str | None]) -> bool:
        try:
            detail = fn()
        except Exception as e:
            self.failed += 1
            self.out(f"  ✗ {label}: {explain(e)}")
            return False
        self.out(f"  ✓ {label}" + (f": {detail}" if detail else ""))
        return True


def run_checks(cfg, open_storage: Callable[[State], Storage], out: Callable[[str], None] = print) -> bool:
    report = Report(out)
    out("Checking image-crawler setup")

    def key() -> str:
        load_key_file(cfg.key_file)
        return str(cfg.key_file)

    report.step("encryption key", key)

    state = State(cfg.state_db)
    storage: Storage | None = None

    def connect() -> str:
        nonlocal storage
        storage = open_storage(state)
        return storage.describe()

    try:
        if not report.step("storage config", connect):
            return False
        report.step("list objects", lambda: _count_some(storage))
        report.step("write / read / delete a test object", lambda: _roundtrip(storage))
        inner = getattr(storage, "inner", storage)
        if isinstance(inner, S3Storage):
            report.step("CORS for the phone viewer", lambda: _cors(inner))
    finally:
        state.close()
    out("All checks passed." if not report.failed else f"{report.failed} check(s) failed.")
    return not report.failed


def _count_some(storage: Storage) -> str:
    count = 0
    for count, _ in enumerate(storage.list(), 1):
        if count >= 1000:
            return "1000+ objects"
    return f"{count} object(s)"


def _roundtrip(storage: Storage) -> str:
    key = f"check/{secrets.token_hex(8)}.bin"
    payload = secrets.token_bytes(64)
    storage.put(key, payload)
    try:
        if storage.get(key) != payload:
            raise RuntimeError("read back different bytes than written")
    finally:
        storage.delete(key)
    if storage.exists(key):
        raise RuntimeError("test object still exists after delete")
    return "ok"


def _cors(storage: S3Storage) -> str:
    origins = storage.cors_origins()
    if not origins:
        return "not set yet (run `image-crawler cors --origin <viewer URL>` once the viewer is hosted)"
    return ", ".join(origins)
