"""Read account-wide R2 usage from Cloudflare's GraphQL Analytics API.

Cloudflare notes these analytics are not the billing source of truth, so the
quota guard treats them as one input and keeps its own count too.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import requests

GRAPHQL_URL = "https://api.cloudflare.com/client/v4/graphql"

# https://developers.cloudflare.com/r2/pricing/ — everything not listed as Class B or free
# (including unknown/new actions) is counted as the pricier Class A, to err on the safe side.
CLASS_B_ACTIONS = {
    "HeadBucket", "HeadObject", "GetObject", "UsageSummary", "GetBucketEncryption",
    "GetBucketLocation", "GetBucketCors", "GetBucketLifecycleConfiguration",
}
FREE_ACTIONS = {"DeleteObject", "DeleteObjects", "DeleteBucket", "AbortMultipartUpload"}

OPERATIONS_QUERY = """
query R2Operations($accountTag: string!, $startDate: Time, $endDate: Time) {
  viewer {
    accounts(filter: { accountTag: $accountTag }) {
      r2OperationsAdaptiveGroups(
        limit: 10000
        filter: { datetime_geq: $startDate, datetime_leq: $endDate }
      ) {
        sum { requests }
        dimensions { actionType }
      }
    }
  }
}
"""

STORAGE_QUERY = """
query R2Storage($accountTag: string!, $startDate: Time, $endDate: Time) {
  viewer {
    accounts(filter: { accountTag: $accountTag }) {
      r2StorageAdaptiveGroups(
        limit: 10000
        filter: { datetime_geq: $startDate, datetime_leq: $endDate }
        orderBy: [datetime_DESC]
      ) {
        max { payloadSize metadataSize objectCount }
        dimensions { bucketName datetime }
      }
    }
  }
}
"""


def classify(action: str) -> str | None:
    """'A', 'B', or None for free operations."""
    if action in FREE_ACTIONS:
        return None
    return "B" if action in CLASS_B_ACTIONS else "A"


@dataclass
class R2Usage:
    storage_bytes: int
    class_a: int
    class_b: int
    by_action: dict[str, int] = field(default_factory=dict)
    storage_by_bucket: dict[str, int] = field(default_factory=dict)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _query(session: requests.Session, token: str, query: str, variables: dict) -> dict:
    resp = session.post(
        GRAPHQL_URL,
        json={"query": query, "variables": variables},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("errors"):
        raise RuntimeError("Cloudflare GraphQL error: " + "; ".join(e.get("message", "?") for e in body["errors"]))
    accounts = (body.get("data") or {}).get("viewer", {}).get("accounts") or []
    if not accounts:
        raise RuntimeError("Cloudflare returned no account data (check the account id and token permissions)")
    return accounts[0]


def fetch_r2_usage(
    account_id: str,
    token: str,
    period_start: datetime,
    now: datetime | None = None,
    session: requests.Session | None = None,
) -> R2Usage:
    """Month-to-date operations since `period_start`, plus the latest stored size of every bucket."""
    session = session or requests.Session()
    now = now or datetime.now(timezone.utc)

    ops = _query(session, token, OPERATIONS_QUERY, {
        "accountTag": account_id, "startDate": _iso(period_start), "endDate": _iso(now),
    })
    by_action: dict[str, int] = {}
    for group in ops.get("r2OperationsAdaptiveGroups") or []:
        action = group["dimensions"]["actionType"]
        by_action[action] = by_action.get(action, 0) + int(group["sum"]["requests"])
    class_a = sum(n for a, n in by_action.items() if classify(a) == "A")
    class_b = sum(n for a, n in by_action.items() if classify(a) == "B")

    # Storage is sampled periodically; the newest sample per bucket is the current size.
    storage = _query(session, token, STORAGE_QUERY, {
        "accountTag": account_id, "startDate": _iso(now - timedelta(days=2)), "endDate": _iso(now),
    })
    by_bucket: dict[str, int] = {}
    for group in storage.get("r2StorageAdaptiveGroups") or []:
        bucket = group["dimensions"]["bucketName"]
        if bucket not in by_bucket:  # rows are newest first
            by_bucket[bucket] = int(group["max"]["payloadSize"] or 0) + int(group["max"]["metadataSize"] or 0)

    return R2Usage(
        storage_bytes=sum(by_bucket.values()),
        class_a=class_a,
        class_b=class_b,
        by_action=by_action,
        storage_by_bucket=by_bucket,
    )
