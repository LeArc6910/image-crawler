"""The encrypted archive: originals, thumbnails and the index, laid out in one storage root."""

from __future__ import annotations

import json
import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Iterator

from image_crawler import layout
from image_crawler.crypto import (
    KIND_IMAGE,
    KIND_INDEX,
    KIND_THUMB,
    SMALL_PAD_BLOCK,
    Opened,
    Vault,
)
from image_crawler.storage import Storage
from image_crawler.thumbs import make_preview

log = logging.getLogger(__name__)

INDEX_VERSION = 1
FLUSH_EVERY = 100
COMPACT_SEGMENT_SIZE = 5_000  # entries per segment written by compact()
AUTO_COMPACT_SEGMENTS = 30  # crawl compacts once the index has more segments than this
_ENTRY_FIELDS = ("site", "page_url", "album", "position", "source_url", "filename", "content_type", "fetched_at")


_last_segment_ns = 0


def new_segment_id() -> str:
    """Sorts by creation time (strictly increasing within a process); the random tail keeps
    concurrent runs from colliding."""
    global _last_segment_ns
    _last_segment_ns = max(time.time_ns(), _last_segment_ns + 1)
    seconds, nanos = divmod(_last_segment_ns, 1_000_000_000)
    stamp = datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{stamp}{nanos:09d}Z-{secrets.token_hex(4)}"


class Archive:
    def __init__(self, vault: Vault, storage: Storage, flush_every: int = FLUSH_EVERY):
        self.vault = vault
        self.storage = storage
        self.flush_every = flush_every
        self._pending: list[dict] = []

    # --- writing ---------------------------------------------------------

    def has_image(self, name: str) -> bool:
        return self.storage.exists(layout.image_key(name))

    def store(self, content: bytes, metadata: dict) -> str:
        """Upload an image with its thumbnail and queue its index entry. Returns the object name."""
        name = self.vault.object_name(content)
        blob = self.vault.seal(content, metadata, kind=KIND_IMAGE, name=name)
        self.storage.put(layout.image_key(name), blob)
        self.add_to_index(name, content, metadata)
        return name

    def add_to_index(self, name: str, content: bytes, metadata: dict) -> None:
        """Upload the thumbnail for an already-stored image and queue its index entry."""
        preview = make_preview(content)
        thumb = None
        if preview.thumbnail is not None:
            thumb = self.vault.thumb_name(name)
            blob = self.vault.seal(
                preview.thumbnail,
                {"content_type": "image/webp"},
                kind=KIND_THUMB,
                name=thumb,
                pad_block=SMALL_PAD_BLOCK,
            )
            self.storage.put(layout.thumb_key(thumb), blob)
        entry = {"object": name, "thumb": thumb, "size": len(content)}
        entry.update({k: metadata.get(k) for k in _ENTRY_FIELDS})
        entry.update({"width": preview.width, "height": preview.height})
        self._pending.append(entry)
        if len(self._pending) >= self.flush_every:
            self.flush()

    def flush(self) -> str | None:
        """Write queued entries as a new index segment."""
        if not self._pending:
            return None
        segment = self._write_segment(self._pending)
        self._pending = []
        return segment

    def _write_segment(self, entries: list[dict]) -> str:
        segment = new_segment_id()
        payload = json.dumps({"v": INDEX_VERSION, "entries": entries}, ensure_ascii=False)
        blob = self.vault.seal(
            payload.encode(),
            {"v": INDEX_VERSION, "count": len(entries)},
            kind=KIND_INDEX,
            name=segment,
            pad_block=SMALL_PAD_BLOCK,
        )
        self.storage.put(layout.index_key(segment), blob)
        log.info("wrote index segment %s (%d entries)", segment, len(entries))
        return segment

    def compact(self) -> tuple[int, int]:
        """Merge all index segments into as few as possible. Returns (segments before, after).

        New segments are written before old ones are deleted, so a crash in between only
        leaves duplicate entries (readers keep one per object). Segments that appear while this runs
        (a concurrent crawl) are left alone.
        """
        self.flush()
        keys = self.segment_keys()
        if len(keys) <= 1:
            return len(keys), len(keys)
        entries = self._read_segments(keys)
        written = [
            self._write_segment(entries[i : i + COMPACT_SEGMENT_SIZE])
            for i in range(0, len(entries), COMPACT_SEGMENT_SIZE)
        ]
        for key in keys:
            self.storage.delete(key)
        return len(keys), len(written)

    # --- reading ---------------------------------------------------------

    def image_names(self) -> Iterator[str]:
        for key in self.storage.list():
            if name := layout.image_name(key):
                yield name

    def open_image(self, name: str) -> Opened:
        return self.vault.open(self.storage.get(layout.image_key(name)), kind=KIND_IMAGE, name=name)

    def segment_keys(self) -> list[str]:
        return sorted(k for k in self.storage.list(layout.INDEX) if layout.index_segment_id(k))

    def read_index(self) -> list[dict]:
        """All index entries, oldest segment first. When an object appears more than once, the
        latest entry wins (it keeps the first one's place): that is how an image moves folders."""
        return self._read_segments(self.segment_keys())

    def _read_segments(self, keys: list[str]) -> list[dict]:
        entries, where = [], {}
        for key in keys:
            segment = layout.index_segment_id(key)
            opened = self.vault.open(self.storage.get(key), kind=KIND_INDEX, name=segment)
            for entry in json.loads(opened.content)["entries"]:
                if entry["object"] in where:
                    entries[where[entry["object"]]] = entry
                else:
                    where[entry["object"]] = len(entries)
                    entries.append(entry)
        return entries
