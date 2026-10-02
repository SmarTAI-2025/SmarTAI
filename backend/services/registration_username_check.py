"""Bound anonymous username probes without storing identities or adding a schema.

This process-local guard supplements the existing persistent mail-send limits.
Fixed hash buckets bound memory; collisions share a budget conservatively.
"""
from __future__ import annotations

import hashlib
import math
import time
from collections import deque
from threading import Lock

from backend.services.email_registration import RegistrationError

_WINDOW_SECONDS = 60
_MAX_ATTEMPTS = 30
_ATTEMPTS: tuple[deque[float], ...] = tuple(deque() for _ in range(1024))
_LOCK = Lock()


def check_username_rate_limit(source_ip: str | None) -> None:
    key = hashlib.sha256((source_ip or "unknown").encode()).digest()
    attempts = _ATTEMPTS[int.from_bytes(key[:4], "big") % len(_ATTEMPTS)]
    now = time.monotonic()
    with _LOCK:
        while attempts and attempts[0] <= now - _WINDOW_SECONDS:
            attempts.popleft()
        if len(attempts) >= _MAX_ATTEMPTS:
            raise RegistrationError(
                "registration_rate_limited",
                status_code=429,
                retry_after=max(1, math.ceil(attempts[0] + _WINDOW_SECONDS - now)),
            )
        attempts.append(now)
