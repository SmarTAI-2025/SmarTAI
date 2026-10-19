"""Call-local response limits without changing shared clients or run control."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import math
from typing import Iterator


_response_seconds: ContextVar[float | None] = ContextVar("provider_response_seconds", default=None)


def current_response_timeout_seconds() -> float | None:
    return _response_seconds.get()


@contextmanager
def response_timeout_scope(seconds: float) -> Iterator[None]:
    """Bound only this coroutine's requests; nested scopes cannot widen a cap."""
    if type(seconds) not in {int, float} or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("response timeout must be a positive finite number")
    previous = _response_seconds.get()
    token = _response_seconds.set(float(seconds) if previous is None else min(previous, float(seconds)))
    try:
        yield
    finally:
        _response_seconds.reset(token)
