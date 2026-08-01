from backend.storage.base import StorageBackend
from backend.storage.local import LocalStorage
from backend.config import settings
from functools import lru_cache


def build_storage() -> StorageBackend:
    if settings.storage_backend == "local":
        return LocalStorage()
    if settings.storage_backend == "object":
        # boto3 is an object-storage-only dependency. Keep local development,
        # SQLite tests, and CLI imports usable when that optional package is not
        # installed; production object mode still imports it eagerly here.
        from backend.storage.object import S3Storage
        return S3Storage()
    raise ValueError(f"Unsupported SMARTAI_STORAGE_BACKEND: {settings.storage_backend}")


@lru_cache(maxsize=1)
def get_storage() -> StorageBackend:
    return build_storage()


def __getattr__(name: str):
    """Keep the optional object-storage dependency lazy while preserving API."""
    if name == "S3Storage":
        from backend.storage.object import S3Storage

        return S3Storage
    raise AttributeError(name)


__all__ = [
    "StorageBackend", "LocalStorage", "S3Storage", "build_storage", "get_storage",
]
