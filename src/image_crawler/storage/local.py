from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

from image_crawler.storage.base import Storage, name_from_path, object_path


class LocalStorage(Storage):
    """A directory on disk. Useful for testing, or for a folder a sync client uploads."""

    def __init__(self, path: str | Path):
        self.root = Path(path).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        return self.root / object_path(name)

    def put(self, name: str, data: bytes) -> None:
        path = self._path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def get(self, name: str) -> bytes:
        return self._path(name).read_bytes()

    def exists(self, name: str) -> bool:
        return self._path(name).exists()

    def list_names(self) -> Iterator[str]:
        for path in self.root.rglob("*"):
            if path.is_file() and (name := name_from_path(path.name)):
                yield name

    def describe(self) -> str:
        return f"local:{self.root}"
