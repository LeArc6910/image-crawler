from __future__ import annotations

import shutil
import subprocess
from typing import Iterator

from image_crawler.storage.base import Storage


class RcloneStorage(Storage):
    """Upload through an rclone remote (MEGA, Filen, pCloud, Proton Drive, SFTP, ...).

    Configure the remote once with `rclone config`; this backend only shells
    out to `rclone`, so every provider rclone supports works here.
    """

    def __init__(self, remote: str, rclone_bin: str = "rclone"):
        if ":" not in remote:
            raise ValueError(f"rclone remote must look like 'name:path', got {remote!r}")
        self.binary = shutil.which(rclone_bin)
        if self.binary is None:
            raise RuntimeError(f"rclone binary not found: {rclone_bin}")
        self.remote = remote.rstrip("/")

    def _target(self, key: str) -> str:
        return f"{self.remote}/{key}"

    def _run(self, *args: str, data: bytes | None = None) -> bytes:
        result = subprocess.run(
            [self.binary, *args], input=data, capture_output=True, check=False
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"rclone {args[0]} failed ({result.returncode}): "
                f"{result.stderr.decode(errors='replace').strip()}"
            )
        return result.stdout

    def put(self, key: str, data: bytes) -> None:
        self._run("rcat", self._target(key), data=data)

    def get(self, key: str) -> bytes:
        return self._run("cat", self._target(key))

    def exists(self, key: str) -> bool:
        try:
            out = self._run("lsf", "--files-only", self._target(key))
        except RuntimeError:
            return False
        return bool(out.strip())

    def delete(self, key: str) -> None:
        if self.exists(key):
            self._run("deletefile", self._target(key))

    def list(self, prefix: str = "") -> Iterator[str]:
        # List the deepest directory the prefix names, then filter on the remainder.
        directory = prefix.rsplit("/", 1)[0] + "/" if "/" in prefix else ""
        try:
            out = self._run("lsf", "-R", "--files-only", self._target(directory))
        except RuntimeError:
            return
        for line in out.decode().splitlines():
            key = directory + line.strip()
            if key.startswith(prefix):
                yield key

    def describe(self) -> str:
        return f"rclone:{self.remote}"
