"""Keep R2 usage inside the free tier by metering every storage request before it is made.

Cloudflare has no hard spending cap for R2 (budget alerts only email), so the
guard lives here: GuardedStorage counts Class A/B operations and stored bytes
per billing period, and refuses requests that would cross the limits.

- Originals are held to the configured soft limits (default 9 GB / 900k / 9M).
- Thumbnails and index segments may go up to the free tier itself, so an
  original that was allowed in never ends up without its thumbnail or index entry.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Callable, Iterator

from image_crawler import layout
from image_crawler.cloudflare import fetch_r2_usage
from image_crawler.config import QuotaSettings
from image_crawler.state import State
from image_crawler.storage.base import Storage

log = logging.getLogger(__name__)

GB = 1_000_000_000  # decimal, so limits err on the small side
LIST_PAGE_SIZE = 1000  # one ListObjects request returns up to this many keys


class QuotaExceeded(RuntimeError):
    pass


@dataclass
class Usage:
    storage_bytes: int = 0
    class_a: int = 0
    class_b: int = 0

    def max(self, other: "Usage") -> "Usage":
        return Usage(
            max(self.storage_bytes, other.storage_bytes),
            max(self.class_a, other.class_a),
            max(self.class_b, other.class_b),
        )


FREE_TIER = Usage(storage_bytes=10 * GB, class_a=1_000_000, class_b=10_000_000)


def soft_limits(settings: QuotaSettings) -> Usage:
    return Usage(int(settings.storage_limit_gb * GB), settings.class_a_limit, settings.class_b_limit)


def billing_period(today: date, billing_day: int = 1) -> tuple[date, date]:
    """[start, end) of the billing period containing `today`."""
    if not 1 <= billing_day <= 28:
        raise ValueError("quota.billing_day must be between 1 and 28")

    def shift(d: date, months: int) -> date:
        month = d.month - 1 + months
        return d.replace(year=d.year + month // 12, month=month % 12 + 1, day=billing_day)

    start = shift(today, 0) if today.day >= billing_day else shift(today, -1)
    return start, shift(start, 1)


@dataclass
class UsageReport:
    period_start: date
    period_end: date
    local: Usage
    cloudflare: Usage | None
    cloudflare_error: str | None

    @property
    def effective(self) -> Usage:
        """The larger of what this machine counted and what Cloudflare reports."""
        return self.local.max(self.cloudflare) if self.cloudflare else self.local


def measure(
    settings: QuotaSettings,
    state: State,
    target: str,
    today: date | None = None,
    fetch: Callable = fetch_r2_usage,
) -> UsageReport:
    start, end = billing_period(today or datetime.now(timezone.utc).date(), settings.billing_day)
    local = Usage(state.stored_bytes(target), *state.ops(target, start.isoformat()))
    cloud, error = None, None
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    if settings.cloudflare_account_id and token:
        try:
            r2 = fetch(
                settings.cloudflare_account_id,
                token,
                datetime(start.year, start.month, start.day, tzinfo=timezone.utc),
            )
            cloud = Usage(r2.storage_bytes, r2.class_a, r2.class_b)
        except Exception as e:
            error = str(e)
            log.warning("could not read usage from Cloudflare, using local counts only: %s", e)
    elif settings.cloudflare_account_id:
        error = "CLOUDFLARE_API_TOKEN is not set"
    return UsageReport(start, end, local, cloud, error)


class GuardedStorage(Storage):
    """Wraps a backend; meters and enforces R2 quotas on every request."""

    def __init__(
        self,
        inner: Storage,
        settings: QuotaSettings,
        state: State,
        baseline: Usage,
        period: str,
    ):
        self.inner = inner
        self.settings = settings
        self.state = state
        self.period = period
        self.target = inner.describe()
        self.used = Usage(baseline.storage_bytes, baseline.class_a, baseline.class_b)
        self.limits = soft_limits(settings)
        self.images_this_run = 0

    def _charge(self, *, a: int = 0, b: int = 0, nbytes: int = 0, limits: Usage) -> None:
        """Count a request before it is made; refuse it if it would cross `limits`."""
        if a and self.used.class_a + a > limits.class_a:
            raise QuotaExceeded(f"Class A operations would exceed {limits.class_a:,} this period")
        if b and self.used.class_b + b > limits.class_b:
            raise QuotaExceeded(f"Class B operations would exceed {limits.class_b:,} this period")
        if nbytes and self.used.storage_bytes + nbytes > limits.storage_bytes:
            raise QuotaExceeded(
                f"storage would exceed {limits.storage_bytes / GB:.2f} GB "
                f"(currently {self.used.storage_bytes / GB:.2f} GB)"
            )
        self.used.class_a += a
        self.used.class_b += b
        self.used.storage_bytes += nbytes
        if a or b:
            self.state.add_ops(self.target, self.period, a, b)

    def put(self, key: str, data: bytes) -> None:
        is_image = layout.image_name(key) is not None
        if is_image:
            if self.images_this_run >= self.settings.max_images_per_run:
                raise QuotaExceeded(f"reached max_images_per_run ({self.settings.max_images_per_run})")
            self.images_this_run += 1
        self._charge(a=1, nbytes=len(data), limits=self.limits if is_image else FREE_TIER)
        self.inner.put(key, data)
        self.state.record_stored(self.target, key, len(data))

    def get(self, key: str) -> bytes:
        self._charge(b=1, limits=self.limits)
        return self.inner.get(key)

    def exists(self, key: str) -> bool:
        self._charge(b=1, limits=self.limits)
        return self.inner.exists(key)

    def delete(self, key: str) -> None:
        self.inner.delete(key)  # free on R2
        self.state.forget_stored(self.target, key)

    def list(self, prefix: str = "") -> Iterator[str]:
        self._charge(a=1, limits=self.limits)
        for count, key in enumerate(self.inner.list(prefix), 1):
            if count % LIST_PAGE_SIZE == 0:
                self._charge(a=1, limits=self.limits)
            yield key

    def describe(self) -> str:
        return self.inner.describe()
