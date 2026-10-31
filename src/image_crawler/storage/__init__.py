from __future__ import annotations

from image_crawler.storage.base import Storage


def build_storage(cfg: dict) -> Storage:
    """Create the backend selected by the [storage] section of the config."""
    backend = cfg.get("backend", "local")
    options = cfg.get(backend, {})
    if backend == "local":
        from image_crawler.storage.local import LocalStorage

        return LocalStorage(options.get("path", "./vault"))
    if backend == "s3":
        from image_crawler.storage.s3 import S3Storage

        if "bucket" not in options:
            raise ValueError("[storage.s3] needs a bucket")
        return S3Storage(
            bucket=options["bucket"],
            prefix=options.get("prefix", ""),
            endpoint_url=options.get("endpoint_url"),
            region=options.get("region"),
            force_path_style=options.get("force_path_style", False),
        )
    if backend == "rclone":
        from image_crawler.storage.rclone import RcloneStorage

        if "remote" not in options:
            raise ValueError("[storage.rclone] needs a remote, e.g. remote = \"mega:image-vault\"")
        return RcloneStorage(options["remote"], options.get("binary", "rclone"))
    raise ValueError(f"unknown storage backend: {backend!r} (use local, s3 or rclone)")


__all__ = ["Storage", "build_storage"]
