"""Global sandbox runtime — caps concurrent subprocess count to prevent fork bombs.

Without this, the grading pipeline fans out N students × M questions × K test
cases, all running concurrently via ``asyncio.gather`` in
:func:`backend.tools.code_interpreter.run_sandbox`. With per-process rlimits of
256 MB, a few dozen subprocesses can exhaust system RAM (the previous default
ran 400+ subprocesses on a typical class of 10 students × 5 programming
problems × 8 test cases).

This module exposes a single global :class:`asyncio.Semaphore` that gates every
sandbox invocation, regardless of how deep in the gather tree the call sits.

Usage:

    # Once at process startup (e.g. FastAPI lifespan):
    from backend.tools.sandbox_runtime import init_sandbox_semaphore
    init_sandbox_semaphore(limit=8)

    # In every code path that spawns a sandbox subprocess:
    from backend.tools.sandbox_runtime import get_sandbox_semaphore
    sem = get_sandbox_semaphore()
    async with sem:
        ...  # spawn subprocess
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Default concurrency cap. Tunable via init_sandbox_semaphore(limit=...).
DEFAULT_SANDBOX_CONCURRENCY = 8

_SEMAPHORE: Optional[asyncio.Semaphore] = None
_LIMIT: int = DEFAULT_SANDBOX_CONCURRENCY


def init_sandbox_semaphore(limit: int = DEFAULT_SANDBOX_CONCURRENCY) -> asyncio.Semaphore:
    """Create (or replace) the global sandbox semaphore.

    Call once at process startup. Re-calling resets the semaphore — only safe
    when no sandbox calls are in flight.
    """
    global _SEMAPHORE, _LIMIT
    _LIMIT = max(1, int(limit))
    _SEMAPHORE = asyncio.Semaphore(_LIMIT)
    logger.info("Initialized sandbox semaphore with concurrency limit = %d", _LIMIT)
    return _SEMAPHORE


def get_sandbox_semaphore() -> asyncio.Semaphore:
    """Return the global semaphore.

    Lazily initializes with :data:`DEFAULT_SANDBOX_CONCURRENCY` if
    :func:`init_sandbox_semaphore` has not been called yet — necessary for
    pytest / CLI usage that bypasses the FastAPI lifespan hook.
    """
    global _SEMAPHORE
    if _SEMAPHORE is None:
        return init_sandbox_semaphore()
    return _SEMAPHORE


def get_sandbox_limit() -> int:
    """Return the configured concurrency limit (for diagnostics / tests)."""
    return _LIMIT
