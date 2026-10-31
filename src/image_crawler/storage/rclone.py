from __future__ import annotations

import shutil
import subprocess
from typing import Iterator

from image_crawler.storage.base import Storage, name_from_path, object_path


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

    def _target(self, name: str) -> str:
        return f"{self.remote}/{object_path(name)}"

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

    def put(self, name: str, data: bytes) -> None:
        self._run("rcat", self._target(name), data=data)

    def get(self, name: str) -> bytes:
        return self._run("cat", self._target(name))

    def exists(self, name: str) -> bool:
        try:
            out = self._run("lsf", "--files-only", self._target(name))
        except RuntimeError:
            return False
        return bool(out.strip())

    def list_names(self) -> Iterator[str]:
        out = self._run("lsf", "-R", "--files-only", self.remote)
        for line in out.decode().splitlines():
            if name := name_from_path(line.strip()):
                yield name

    def describe(self) -> str:
        return f"rclone:{self.remote}"
