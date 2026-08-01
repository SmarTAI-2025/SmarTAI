from __future__ import annotations

from io import BufferedReader
from pathlib import Path

from backend.config import settings
from backend.storage.base import StorageBackend


class LocalStorage(StorageBackend):
    """Filesystem backend with strict root containment checks."""

    name = "local"

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or settings.storage_root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        candidate = (self.root / key).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError("storage key escapes storage root")
        return candidate

    def save(self, key: str, content: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def open(self, key: str) -> BufferedReader:
        return self._path(key).open("rb")

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def ready(self) -> bool:
        return self.root.is_dir()
