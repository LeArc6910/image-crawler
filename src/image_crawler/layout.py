"""Where each kind of object lives inside the storage root.

    ab/<name>.bin            encrypted original image (v1-compatible location)
    thumbs/cd/<thumb>.bin    encrypted thumbnail
    index/<segment>.bin      encrypted index segment (append-only list of images)
    state/crawl-state.bin    encrypted crawl history + site list (only with state_sync)

pwa/js/vault.js mirrors these paths; keep them in sync.
"""

from __future__ import annotations

import re

SUFFIX = ".bin"
THUMBS = "thumbs/"
INDEX = "index/"
STATE_KEY = "state/crawl-state.bin"

_IMAGE_KEY = re.compile(r"^[0-9a-f]{2}/([0-9a-f]{64})\.bin$")
_INDEX_KEY = re.compile(r"^index/([0-9A-Za-z_-]+)\.bin$")


def image_key(name: str) -> str:
    return f"{name[:2]}/{name}{SUFFIX}"


def thumb_key(name: str) -> str:
    return f"{THUMBS}{name[:2]}/{name}{SUFFIX}"


def index_key(segment_id: str) -> str:
    return f"{INDEX}{segment_id}{SUFFIX}"


def image_name(key: str) -> str | None:
    m = _IMAGE_KEY.match(key)
    return m.group(1) if m else None


def index_segment_id(key: str) -> str | None:
    m = _INDEX_KEY.match(key)
    return m.group(1) if m else None
