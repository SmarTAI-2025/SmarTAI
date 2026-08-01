from __future__ import annotations

from io import BytesIO
from typing import BinaryIO

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from backend.config import settings
from backend.storage.base import StorageBackend


class S3Storage(StorageBackend):
    """S3-compatible object storage using portable object keys."""

    name = "object"

    def __init__(self) -> None:
        if not settings.storage_s3_bucket:
            raise ValueError("SMARTAI_STORAGE_S3_BUCKET is required for object storage")
        if not settings.storage_s3_access_key or not settings.storage_s3_secret_key:
            raise ValueError("S3 access credentials are required for object storage")
        self.bucket = settings.storage_s3_bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.storage_s3_endpoint or None,
            region_name=settings.storage_s3_region,
            aws_access_key_id=settings.storage_s3_access_key,
            aws_secret_access_key=settings.storage_s3_secret_key,
        )

    def save(self, key: str, content: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=content)

    def open(self, key: str) -> BinaryIO:
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        return BytesIO(response["Body"].read())

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except (ClientError, BotoCoreError):
            return False

    def ready(self) -> bool:
        try:
            self.client.head_bucket(Bucket=self.bucket)
            return True
        except (ClientError, BotoCoreError):
            return False
