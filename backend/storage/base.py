from __future__ import annotations

from abc import ABC, abstractmethod
from io import BufferedIOBase


class StorageBackend(ABC):
    @abstractmethod
    def save(self, key: str, content: bytes) -> None: ...

    @abstractmethod
    def open(self, key: str) -> BufferedIOBase: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    def ready(self) -> bool:
        return True
