"""Per-run cancellation and provider retry state, inherited by child coroutines.

Durable repositories remain authoritative. This small cache wakes local work
immediately and observes cancellation from other processes without model calls.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import time
from typing import Awaitable, Callable

_current: ContextVar[ExecutionControl | None] = ContextVar("execution_control", default=None)
_running: dict[str, ExecutionControl] = {}


def provider_key(provider) -> str:
    config = getattr(provider, "config", None)
    value = "\0".join(str(getattr(config, key, "")) for key in (
        "provider_type", "model", "base_url", "wire_protocol", "api_key",
    )) if config is not None else str(getattr(provider, "provider_id", id(provider)))
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass
class ExecutionControl:
    job_id: str
    is_cancelled: Callable[[], bool]
    task: asyncio.Task
    persist_progress: Callable[[dict], Awaitable[None]] | None = None
    blocked: dict[str, str] = field(default_factory=dict)
    rate_failures: dict[str, int] = field(default_factory=dict)
    waits: dict[str, dict] = field(default_factory=dict)
    stopped: bool = False

    def stop(self) -> None:
        self.stopped = True
        self.task.cancel()

    async def check(self, provider) -> None:
        if self.stopped or await asyncio.to_thread(self.is_cancelled):
            raise asyncio.CancelledError()
        code = self.blocked.get(provider_key(provider))
        if code:
            from backend.llm.providers import ProviderRequestError
            error = ProviderRequestError(code)
            error.retryable = False
            raise error

    def block(self, provider, code: str) -> None:
        self.blocked[provider_key(provider)] = code

    async def publish(self) -> None:
        if not owns_current_execution(self.job_id):
            return
        from backend.progress.tracker import get_reporter
        reporter = get_reporter(self.job_id)
        if reporter is not None:
            await reporter.set_model_waits(list(self.waits.values()))
            if self.persist_progress is not None:
                await self.persist_progress((await reporter.snapshot()).model_dump(mode="json"))

    async def waiting(self, provider, attempt: int, limit: int, seconds: float) -> None:
        self.waits[provider_key(provider)] = {
            "model": str(getattr(provider, "model", ""))[:120],
            "reason": "provider_rate_limited",
            "attempt": attempt, "max_attempts": limit,
            "retry_at": time.time() + seconds,
        }
        await self.publish()

    async def succeeded(self, provider) -> None:
        self.rate_failures.pop(provider_key(provider), None)
        if self.waits.pop(provider_key(provider), None) is not None:
            await self.publish()


def current_execution() -> ExecutionControl | None:
    return _current.get()


def owns_current_execution(job_id: str) -> bool:
    control = _current.get()
    return control is None or control.job_id != job_id or _running.get(job_id) is control


def cancel_local(job_id: str) -> None:
    control = _running.get(job_id)
    if control is not None:
        control.stop()


async def run_controlled(job_id: str, is_cancelled: Callable[[], bool], awaitable, *, persist_progress=None):
    control = ExecutionControl(job_id, is_cancelled, asyncio.current_task(), persist_progress)
    token = _current.set(control)
    _running[job_id] = control

    async def watch():
        while True:
            await asyncio.sleep(1)
            if await asyncio.to_thread(is_cancelled):
                control.stop()
                return

    watcher = asyncio.create_task(watch())
    try:
        return await awaitable
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        if _running.get(job_id) is control:
            _running.pop(job_id, None)
        _current.reset(token)
