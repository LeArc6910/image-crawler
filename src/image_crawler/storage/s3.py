from __future__ import annotations

from typing import Iterator

from image_crawler.storage.base import Storage, name_from_path, object_path


class S3Storage(Storage):
    """Any S3-compatible object store: Backblaze B2, Wasabi, Cloudflare R2, AWS S3, Filen S3 gateway...

    Credentials come from the standard AWS chain (AWS_ACCESS_KEY_ID /
    AWS_SECRET_ACCESS_KEY environment variables, ~/.aws/credentials, ...), never
    from the config file.
    """

    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        endpoint_url: str | None = None,
        region: str | None = None,
        force_path_style: bool = False,
        client=None,
    ):
        if client is None:
            try:
                import boto3
            except ImportError:
                raise RuntimeError(
                    "the s3 backend needs boto3: pip install 'image-crawler[s3]'"
                ) from None
            from botocore.config import Config

            client = boto3.client(
                "s3",
                endpoint_url=endpoint_url or None,
                region_name=region,
                config=Config(s3={"addressing_style": "path" if force_path_style else "auto"}),
            )
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""

    def _key(self, name: str) -> str:
        return self.prefix + object_path(name)

    def put(self, name: str, data: bytes) -> None:
        self.client.put_object(
            Bucket=self.bucket,
            Key=self._key(name),
            Body=data,
            ContentType="application/octet-stream",
        )

    def get(self, name: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=self._key(name))["Body"].read()

    def exists(self, name: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(name))
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def list_names(self) -> Iterator[str]:
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix):
            for obj in page.get("Contents", []):
                if name := name_from_path(obj["Key"]):
                    yield name

    def describe(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}"
