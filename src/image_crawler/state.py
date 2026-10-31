"""Local crawl history (SQLite) so re-runs skip images that are already stored.

This database holds plaintext source URLs. It never leaves this machine, but
keep it on an encrypted disk if that history is sensitive too.
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

    def close(self) -> None:
        self.db.close()
