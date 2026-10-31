"""Local crawl history (SQLite) so re-runs skip images that are already stored.

This database holds plaintext source URLs. It only leaves this machine encrypted,
when `state_sync` is on (see image_crawler.statesync); keep it on an encrypted
disk if that history is sensitive too.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    url         TEXT PRIMARY KEY,
    object_name TEXT,
    page_url    TEXT,
    site        TEXT,
    status      TEXT NOT NULL,
    fetched_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS objects (
    name      TEXT PRIMARY KEY,
    stored_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_ops (
    target  TEXT NOT NULL,
    period  TEXT NOT NULL,
    class_a INTEGER NOT NULL DEFAULT 0,
    class_b INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (target, period)
);
CREATE TABLE IF NOT EXISTS stored (
    target TEXT NOT NULL,
    key    TEXT NOT NULL,
    bytes  INTEGER NOT NULL,
    PRIMARY KEY (target, key)
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class State:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            path = Path(path).expanduser()
        self.db = sqlite3.connect(str(path))
        self.db.executescript(_SCHEMA)

    def seen_url(self, url: str) -> bool:
        return self.db.execute("SELECT 1 FROM images WHERE url = ?", (url,)).fetchone() is not None

    def record_url(
        self, url: str, status: str, object_name: str | None, page_url: str, site: str
    ) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO images VALUES (?, ?, ?, ?, ?, ?)",
                (url, object_name, page_url, site, status, time.time()),
            )

    def has_object(self, name: str) -> bool:
        return self.db.execute("SELECT 1 FROM objects WHERE name = ?", (name,)).fetchone() is not None

    def add_object(self, name: str) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO objects VALUES (?, ?)", (name, time.time()))

    # --- quota metering (see image_crawler.quota) ---

    def add_ops(self, target: str, period: str, class_a: int = 0, class_b: int = 0) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO usage_ops VALUES (?, ?, ?, ?) ON CONFLICT (target, period) "
                "DO UPDATE SET class_a = class_a + excluded.class_a, class_b = class_b + excluded.class_b",
                (target, period, class_a, class_b),
            )

    def ops(self, target: str, period: str) -> tuple[int, int]:
        row = self.db.execute(
            "SELECT class_a, class_b FROM usage_ops WHERE target = ? AND period = ?", (target, period)
        ).fetchone()
        return row or (0, 0)

    def record_stored(self, target: str, key: str, size: int) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO stored VALUES (?, ?, ?)", (target, key, size))

    def forget_stored(self, target: str, key: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM stored WHERE target = ? AND key = ?", (target, key))

    def stored_size(self, target: str, key: str) -> int | None:
        row = self.db.execute("SELECT bytes FROM stored WHERE target = ? AND key = ?", (target, key)).fetchone()
        return row[0] if row else None

    def stored_bytes(self, target: str) -> int:
        row = self.db.execute("SELECT SUM(bytes) FROM stored WHERE target = ?", (target,)).fetchone()
        return row[0] or 0

    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))

    def close(self) -> None:
        self.db.close()
