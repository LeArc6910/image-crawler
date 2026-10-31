from __future__ import annotations

from typing import Iterator

from image_crawler.storage.base import Storage

CORS_HEADERS = ["authorization", "x-amz-date", "x-amz-content-sha256", "range"]


class S3Storage(Storage):
    """Any S3-compatible object store: Cloudflare R2, Backblaze B2, Wasabi, AWS S3, ...

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

    def put(self, key: str, data: bytes) -> None:
        self.client.put_object(
            Bucket=self.bucket,
            Key=self.prefix + key,
            Body=data,
            ContentType="application/octet-stream",
        )

    def get(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=self.prefix + key)["Body"].read()

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=self.prefix + key)
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def list(self, prefix: str = "") -> Iterator[str]:
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix + prefix):
            for obj in page.get("Contents", []):
                yield obj["Key"][len(self.prefix) :]

    def set_cors(self, origins: list[str]) -> None:
        """Let the PWA (served from `origins`) read objects with signed requests."""
        self.client.put_bucket_cors(
            Bucket=self.bucket,
            CORSConfiguration={
                "CORSRules": [
                    {
                        "AllowedOrigins": origins,
                        "AllowedMethods": ["GET", "HEAD"],
                        "AllowedHeaders": CORS_HEADERS,
                        "ExposeHeaders": ["ETag", "Content-Length"],
                        "MaxAgeSeconds": 3600,
                    }
                ]
            },
        )

    def describe(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}"
