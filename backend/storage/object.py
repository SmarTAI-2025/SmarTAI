from __future__ import annotations

from io import BytesIO
from typing import BinaryIO

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from backend.config import settings
from backend.storage.base import (
    StorageBackend,
    StorageObjectNotFound,
    StorageUnavailable,
)


class S3Storage(StorageBackend):
    """S3-compatible object storage using portable object keys."""

    name = "object"
    _MAX_DELETE_VERIFICATION_PASSES = 100

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
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            return BytesIO(response["Body"].read())
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound"}:
                raise StorageObjectNotFound("storage_object_not_found") from None
            raise StorageUnavailable("storage_unavailable") from None
        except BotoCoreError:
            raise StorageUnavailable("storage_unavailable") from None

    def delete(self, key: str) -> None:
        """Permanently remove every physical version of one exact key.

        A plain ``DeleteObject`` call against a versioned bucket only creates
        a delete marker and leaves the previous bytes stored (and billable).
        Source-file cleanup therefore enumerates and deletes both object
        versions and delete markers, then verifies the exact key is empty.
        ``ListBucketVersions`` and ``DeleteObjectVersion`` permissions are a
        deliberate part of the storage contract: inability to prove deletion
        is reported as unavailable so the durable cleanup worker retries.
        """

        try:
            for _ in range(self._MAX_DELETE_VERIFICATION_PASSES):
                versions = [
                    (version_key, version_id)
                    for version_key, version_id in self._list_physical_versions(
                        prefix=key
                    )
                    if version_key == key
                ]
                if not versions:
                    return
                for version_key, version_id in versions:
                    self.client.delete_object(
                        Bucket=self.bucket,
                        Key=version_key,
                        VersionId=version_id,
                    )
        except StorageUnavailable:
            raise
        except (ClientError, BotoCoreError):
            raise StorageUnavailable("storage_unavailable") from None
        # A fenced source key should not be recreated while cleanup owns it.
        # Still fail closed if an unexpected writer keeps racing deletion.
        raise StorageUnavailable("storage_delete_verification_failed")

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except (ClientError, BotoCoreError):
            return False

    def list_keys(self, prefix: str) -> list[str]:
        """List keys with any stored version or delete marker below prefix."""

        try:
            return sorted(
                {
                    key
                    for key, _version_id in self._list_physical_versions(
                        prefix=prefix
                    )
                    if key.startswith(prefix)
                }
            )
        except StorageUnavailable:
            raise
        except (ClientError, BotoCoreError):
            raise StorageUnavailable("storage_unavailable") from None

    def _list_physical_versions(self, *, prefix: str) -> list[tuple[str, str]]:
        """Return every physical object version and delete marker.

        The low-level API is used instead of ``list_objects_v2`` because the
        latter intentionally hides non-current versions and keys whose latest
        state is a delete marker. Pagination markers are validated so a broken
        or partially compatible backend cannot make cleanup falsely succeed.
        """

        request: dict[str, str] = {
            "Bucket": self.bucket,
            "Prefix": prefix,
        }
        seen_markers: set[tuple[str, str | None]] = set()
        versions: list[tuple[str, str]] = []
        try:
            while True:
                page = self.client.list_object_versions(**request)
                for collection_name in ("Versions", "DeleteMarkers"):
                    for item in page.get(collection_name) or []:
                        key = item.get("Key")
                        version_id = item.get("VersionId")
                        if not isinstance(key, str) or not isinstance(
                            version_id, str
                        ):
                            raise StorageUnavailable(
                                "storage_version_listing_invalid"
                            )
                        if key.startswith(prefix):
                            versions.append((key, version_id))
                if not page.get("IsTruncated"):
                    return versions

                next_key = page.get("NextKeyMarker")
                next_version = page.get("NextVersionIdMarker")
                if not isinstance(next_key, str) or (
                    next_version is not None
                    and not isinstance(next_version, str)
                ):
                    raise StorageUnavailable("storage_version_listing_invalid")
                marker = (next_key, next_version)
                if marker in seen_markers:
                    raise StorageUnavailable("storage_version_listing_invalid")
                seen_markers.add(marker)
                request["KeyMarker"] = next_key
                if next_version is None:
                    request.pop("VersionIdMarker", None)
                else:
                    request["VersionIdMarker"] = next_version
        except StorageUnavailable:
            raise
        except (ClientError, BotoCoreError):
            raise StorageUnavailable("storage_unavailable") from None

    def ready(self) -> bool:
        try:
            self.client.head_bucket(Bucket=self.bucket)
            return True
        except (ClientError, BotoCoreError):
            return False
