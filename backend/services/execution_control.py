"""Per-run cancellation and provider retry state, inherited by child coroutines.

Durable repositories remain authoritative. This small cache wakes local work
immediately and observes cancellation from other processes without model calls.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import logging
import time
from typing import Awaitable, Callable
from weakref import WeakKeyDictionary

_current: ContextVar[ExecutionControl | None] = ContextVar("execution_control", default=None)
_running: dict[str, ExecutionControl] = {}
logger = logging.getLogger(__name__)


def provider_key(provider) -> str:
    config = getattr(provider, "config", None)
    value = "\0".join(str(getattr(config, key, "")) for key in (
        "provider_type", "model", "base_url", "wire_protocol", "api_key",
    )) if config is not None else str(getattr(provider, "provider_id", id(provider)))
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass
class _RateWait:
    key: str
    progress: dict
    wake: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class ExecutionControl:
    job_id: str
    is_cancelled: Callable[[], bool]
    task: asyncio.Task
    persist_progress: Callable[[dict], Awaitable[None]] | None = None
    response_timeout_seconds: float | None = None
    blocked: dict[str, str] = field(default_factory=dict)
    waits: WeakKeyDictionary[asyncio.Task, _RateWait] = field(default_factory=WeakKeyDictionary)
    active_calls: dict[str, set[asyncio.Timeout]] = field(default_factory=dict)
    stopped: bool = False

    def stop(self) -> None:
        self.stopped = True
        self.task.cancel()

    async def check(self, provider) -> None:
        await self.finish_wait(provider)
        from backend.llm.request_timeouts import provider_local_step
        if self.stopped or await provider_local_step(asyncio.to_thread(self.is_cancelled)):
            raise asyncio.CancelledError()
        code = self.blocked.get(provider_key(provider))
        if code:
            from backend.llm.providers import ProviderRequestError
            error = ProviderRequestError(code)
            error.retryable = False
            raise error

    def block(self, provider, code: str) -> None:
        key = provider_key(provider)
        # A late in-flight 429 must preserve a more specific quota verdict.
        if key not in self.blocked or code != "provider_rate_limited":
            self.blocked[key] = code
        for wait in self.waits.values():
            if wait.key == key:
                wait.wake.set()
        if code == "provider_rate_limited":
            for timer in self.active_calls.get(key, ()):
                if not timer.expired():
                    timer.reschedule(asyncio.get_running_loop().time())

    @asynccontextmanager
    async def interruptible_call(self, provider, may_have_sent: Callable[[], bool]):
        """Interrupt only this model's calls, without cancelling the workflow task."""
        from backend.llm.providers import ProviderRequestError
        key = provider_key(provider)
        calls = self.active_calls.setdefault(key, set())
        timer = asyncio.timeout(None)
        try:
            async with timer:
                calls.add(timer)
                yield
        except TimeoutError:
            if not timer.expired():
                raise
            error = ProviderRequestError(self.blocked[key])
            error.retryable = False
            error.submission_may_exist = may_have_sent()
            raise error from None
        finally:
            calls.discard(timer)
            if not calls:
                del self.active_calls[key]

    async def publish(self) -> None:
        if not owns_current_execution(self.job_id):
            return
        from backend.progress.tracker import get_reporter
        reporter = get_reporter(self.job_id)
        if reporter is not None:
            # Parallel calls can wait independently. Show the next retry per model.
            upcoming = {}
            for task, wait in self.waits.items():
                if task.done() or wait.key in self.blocked:
                    continue
                if wait.key not in upcoming or wait.progress["retry_at"] < upcoming[wait.key]["retry_at"]:
                    upcoming[wait.key] = wait.progress
            await reporter.set_model_waits(list(upcoming.values()))
            if self.persist_progress is not None:
                from backend.llm.request_timeouts import provider_local_step
                await provider_local_step(
                    self.persist_progress((await reporter.snapshot()).model_dump(mode="json")),
                    best_effort=True,
                )

    async def waiting(self, provider, attempt: int, limit: int, seconds: float) -> None:
        self.waits[asyncio.current_task()] = _RateWait(provider_key(provider), {
            "model": str(getattr(provider, "model", ""))[:120],
            "reason": "provider_rate_limited",
            "attempt": attempt, "max_attempts": limit,
            "retry_at": time.time() + seconds,
        })
        await self.publish()

    async def finish_wait(self, provider) -> bool:
        task = asyncio.current_task()
        wait = self.waits.get(task)
        if wait is None or wait.key != provider_key(provider):
            return False
        del self.waits[task]
        try:
            await self.publish()
        except Exception:
            logger.warning("Could not publish the end of a rate-limit wait")
        return True

    async def sleep_before_retry(self, seconds: float) -> None:
        wait = self.waits.get(asyncio.current_task())
        if wait is None:
            await asyncio.sleep(seconds)
        elif wait.key not in self.blocked:
            # A sibling exhausting this configuration wakes queued retries;
            # check() enforces the block before the next provider send.
            try:
                async with asyncio.timeout(seconds):
                    await wait.wake.wait()
            except TimeoutError:
                pass


def current_execution() -> ExecutionControl | None:
    return _current.get()


def owns_current_execution(job_id: str) -> bool:
    control = _current.get()
    return control is None or control.job_id != job_id or _running.get(job_id) is control


def cancel_local(job_id: str) -> None:
    control = _running.get(job_id)
    if control is not None:
        control.stop()


async def run_controlled(
    job_id: str, is_cancelled: Callable[[], bool], awaitable, *,
    persist_progress=None, response_timeout_seconds: float | None = None,
):
    control = ExecutionControl(
        job_id, is_cancelled, asyncio.current_task(), persist_progress,
        response_timeout_seconds=response_timeout_seconds,
    )
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
