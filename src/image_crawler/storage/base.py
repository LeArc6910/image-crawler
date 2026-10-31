from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterator


class Storage(ABC):
    """A flat key -> bytes store. Keys are '/'-separated paths relative to the storage root.

    Backends only ever see encrypted blobs; image_crawler.layout decides the keys.
    """

    @abstractmethod
    def put(self, key: str, data: bytes) -> None: ...

    @abstractmethod
    def get(self, key: str) -> bytes: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    @abstractmethod
    def delete(self, key: str) -> None:
        """Remove a key; deleting a missing key is not an error."""

    @abstractmethod
    def list(self, prefix: str = "") -> Iterator[str]:
        """Yield every key under `prefix` (keys are returned relative to the storage root)."""

    def describe(self) -> str:
        return type(self).__name__
