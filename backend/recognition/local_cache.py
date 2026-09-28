"""Bounded ephemeral native/media bytes; a cache key is never authorization.

The caller must recheck live business/source access before every lookup, including
hits. This class has no database, disk, provider, or deletion-lifecycle knowledge.
Its byte counters measure payloads only, not Python overhead or process RSS.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import math
import threading
import time
from typing import Callable

from pydantic import ValidationError

from backend.domain.errors import RecognitionError
from backend.recognition.cache_identity import RecognitionCacheIdentityV1
from backend.recognition.models import RecognitionSourceRefV1

MAX_BYTES = 64 * 1024 * 1024
MAX_TTL_SECONDS = 1800
MAX_ENTRIES = 512
_MEDIA_TYPES = {"application/pdf", "image/png", "image/jpeg", "image/webp"}
_SourceKey = tuple[str, str, str, str, str, str]
_CacheKey = tuple[_SourceKey, str]


@dataclass(frozen=True, repr=False)
class _Entry:
    value: bytes
    expires_at: float


def _owner(owner_id, authorized_owner_id):
    if (type(owner_id) is not str or not owner_id.strip() or len(owner_id) > 240
            or type(authorized_owner_id) is not str or owner_id != authorized_owner_id):
        raise RecognitionError("recognition_artifact_invalid") from None


def _source_key(source, authorized_owner_id) -> _SourceKey:
    try:
        if not isinstance(source, RecognitionSourceRefV1):
            raise ValueError
        source = RecognitionSourceRefV1.model_validate(source.model_dump(warnings=False))
        _owner(source.owner_id, authorized_owner_id)
        if not source.stored_file_id or not source.stored_file_id.strip() or source.content_type not in _MEDIA_TYPES:
            raise ValueError
        return (source.owner_id, source.scope, source.business_id, source.stored_file_id,
                source.input_sha256, source.content_type)
    except (ValidationError, ValueError, TypeError, AttributeError, RecursionError):
        raise RecognitionError("recognition_artifact_invalid") from None


def _key(identity, source, authorized_owner_id) -> _CacheKey:
    try:
        source_key = _source_key(source, authorized_owner_id)
        if not isinstance(identity, RecognitionCacheIdentityV1):
            raise ValueError
        identity = RecognitionCacheIdentityV1.model_validate(identity.model_dump(warnings=False))
        if (identity.layer not in {"native", "render"} or identity.owner_id != source_key[0]
                or identity.source_sha256 != source_key[4] or identity.source_content_type != source_key[5]):
            raise ValueError
        return source_key, identity.key
    except (ValidationError, ValueError, TypeError, AttributeError, RecursionError):
        raise RecognitionError("recognition_artifact_invalid") from None


class RecognitionByteCache:
    """Thread-safe instance-wide LRU with absolute, non-renewing entry expiry.

    Invalid or regressing clocks discard all entries and fail closed for that
    operation. Oversized replacements remove their old key but no unrelated key.
    Empty bytes are permitted: the owner of a typed payload decides its validity.
    """

    def __init__(self, *, max_bytes: int = MAX_BYTES, ttl_seconds: float = MAX_TTL_SECONDS,
                 max_entries: int = MAX_ENTRIES, clock: Callable[[], float] = time.monotonic):
        if (type(max_bytes) is not int or not 1 <= max_bytes <= MAX_BYTES
                or type(max_entries) is not int or not 1 <= max_entries <= MAX_ENTRIES
                or type(ttl_seconds) not in {int, float} or not 0 < ttl_seconds <= MAX_TTL_SECONDS
                or not math.isfinite(ttl_seconds) or not callable(clock)):
            raise ValueError("invalid recognition cache limits")
        self._max_bytes, self._max_entries = max_bytes, max_entries
        self._ttl_seconds, self._clock = float(ttl_seconds), clock
        self._entries: OrderedDict[_CacheKey, _Entry] = OrderedDict()
        self._size_bytes = 0
        self._last_now: float | None = None
        self._lock = threading.RLock()

    def _remove(self, key):
        entry = self._entries.pop(key, None)
        if entry is not None:
            self._size_bytes -= len(entry.value)

    def _now(self) -> float | None:
        try:
            now = self._clock()
            if (type(now) not in {int, float} or not math.isfinite(now)
                    or self._last_now is not None and now < self._last_now):
                raise ValueError
            now = float(now)
        except Exception:
            self._entries.clear()
            self._size_bytes, self._last_now = 0, None
            return None
        self._last_now = now
        for key, entry in list(self._entries.items()):
            if entry.expires_at <= now:
                self._remove(key)
        return now

    @property
    def size_bytes(self) -> int:
        with self._lock:
            self._now()
            return self._size_bytes

    @property
    def entry_count(self) -> int:
        with self._lock:
            self._now()
            return len(self._entries)

    def get(self, *, identity: RecognitionCacheIdentityV1, source: RecognitionSourceRefV1,
            authorized_owner_id: str) -> bytes | None:
        key = _key(identity, source, authorized_owner_id)
        with self._lock:
            if self._now() is None:
                return None
            entry = self._entries.get(key)
            if entry is None:
                return None
            self._entries.move_to_end(key)
            return entry.value

    def put(self, *, identity: RecognitionCacheIdentityV1, source: RecognitionSourceRefV1,
            authorized_owner_id: str, value: bytes) -> bool:
        key = _key(identity, source, authorized_owner_id)
        if type(value) is not bytes:
            raise RecognitionError("recognition_artifact_invalid") from None
        with self._lock:
            now = self._now()
            if now is None:
                return False
            self._remove(key)
            expires_at = now + self._ttl_seconds
            if len(value) > self._max_bytes or not math.isfinite(expires_at) or expires_at <= now:
                return False
            while self._entries and (self._size_bytes + len(value) > self._max_bytes
                                     or len(self._entries) >= self._max_entries):
                oldest = next(iter(self._entries))
                self._remove(oldest)
            self._entries[key] = _Entry(value, expires_at)
            self._size_bytes += len(value)
            return True

    def invalidate_source(self, *, source: RecognitionSourceRefV1, authorized_owner_id: str) -> int:
        source_key = _source_key(source, authorized_owner_id)
        with self._lock:
            self._now()
            keys = [key for key in self._entries if key[0] == source_key]
            for key in keys:
                self._remove(key)
            return len(keys)

    def clear_owner(self, *, owner_id: str, authorized_owner_id: str) -> int:
        _owner(owner_id, authorized_owner_id)
        with self._lock:
            self._now()
            keys = [key for key in self._entries if key[0][0] == owner_id]
            for key in keys:
                self._remove(key)
            return len(keys)
