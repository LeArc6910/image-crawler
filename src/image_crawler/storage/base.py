from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterator

SUFFIX = ".bin"


def object_path(name: str) -> str:
    """Spread objects over 256 prefixes so no single directory grows huge."""
    return f"{name[:2]}/{name}{SUFFIX}"


def name_from_path(path: str) -> str | None:
    base = path.rsplit("/", 1)[-1]
    return base[: -len(SUFFIX)] if base.endswith(SUFFIX) else None


class Storage(ABC):
    """Stores opaque encrypted blobs under content-derived names."""

    @abstractmethod
    def put(self, name: str, data: bytes) -> None: ...

    @abstractmethod
    def get(self, name: str) -> bytes: ...

    @abstractmethod
    def exists(self, name: str) -> bool: ...

    @abstractmethod
    def list_names(self) -> Iterator[str]: ...

    def describe(self) -> str:
        return type(self).__name__
