from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

from image_crawler.storage.base import Storage


class LocalStorage(Storage):
    """A directory on disk. Useful for testing, or for a folder a sync client uploads."""

    def __init__(self, path: str | Path):
        self.root = Path(path).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.root / key

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def list(self, prefix: str = "") -> Iterator[str]:
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and path.suffix != ".tmp":
                key = path.relative_to(self.root).as_posix()
                if key.startswith(prefix):
                    yield key

    def describe(self) -> str:
        return f"local:{self.root}"
