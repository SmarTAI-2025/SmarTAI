from __future__ import annotations

from abc import ABC, abstractmethod
from io import BufferedIOBase


class StorageObjectNotFound(FileNotFoundError):
    """The requested object is definitively absent from the backend."""


class StorageUnavailable(OSError):
    """The backend could not determine or return the requested object."""


class StorageBackend(ABC):
    @abstractmethod
    def save(self, key: str, content: bytes) -> None: ...

    @abstractmethod
    def open(self, key: str) -> BufferedIOBase: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    def list_keys(self, prefix: str) -> list[str]:
        """List exact keys below a business prefix for orphan reconciliation.

        Backends must opt in explicitly.  Task deletion retries instead of
        claiming completion when a configured backend cannot prove its prefix
        empty.
        """
        raise StorageUnavailable("storage_prefix_listing_unavailable")

    def ready(self) -> bool:
        return True
