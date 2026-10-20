"""Call-local response limits without changing shared clients or run control."""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from contextvars import ContextVar
import inspect
import logging
import math
from typing import Awaitable, Callable, Iterator, TypeVar

logger = logging.getLogger(__name__)
T = TypeVar("T")
_LOCAL_STEP_SECONDS = 10.0


_response_seconds: ContextVar[float | None] = ContextVar("provider_response_seconds", default=None)
_response_scope: ContextVar[ResponseDeadlineScope | None] = ContextVar("provider_response_scope", default=None)


def _positive_seconds(seconds: float) -> float:
    if type(seconds) not in {int, float} or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("response timeout must be a positive finite number")
    return float(seconds)


def _local_timeout():
    from backend.llm.providers import ProviderRequestError
    error = ProviderRequestError("provider_capacity_unavailable")
    error.retryable = False
    return error


class ResponseDeadlineScope:
    """One logical OCR call, with separate local waits and response attempts.

    A rejected attempt may be retried by the existing 429 policy. An unknown
    attempt cannot reset its submission marker or authorize another request.
    """

    def __init__(self, seconds, *, local_wait_seconds, before_send=None, on_send=None):
        self.seconds = _positive_seconds(seconds)
        self.local_wait_remaining = _positive_seconds(local_wait_seconds)
        self.before_send, self.on_send = before_send, on_send
        self.attempt_count = 0
        self.submission_may_exist = False
        self.last_attempt_rejected = False
        self._authorized = False
        self._reserved = False

    @property
    def ever_started(self) -> bool:
        return self.attempt_count > 0

    async def wait_local(self, awaitable: Awaitable[T]) -> T:
        """Charge only this local wait, shared across recognition/provider gates."""
        if self.local_wait_remaining <= 0:
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            raise _local_timeout()
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = asyncio.timeout(self.local_wait_remaining)
        try:
            async with deadline:
                return await awaitable
        except TimeoutError as exc:
            if not deadline.expired():
                raise
            raise _local_timeout() from exc
        finally:
            self.local_wait_remaining = max(0.0, self.local_wait_remaining - (loop.time() - started))

    @asynccontextmanager
    async def local_admission(self, lease):
        # Only entering the lease is local waiting. Its body can contain an
        # entire provider response; releasing it must remain cancellation-safe.
        async with AsyncExitStack() as stack:
            await self.wait_local(stack.enter_async_context(lease))
            yield

    async def run_response(
        self, invoke: Callable[[], Awaitable[T]], *, timeout_seconds=None, before_request=None,
    ) -> T:
        from backend.llm.providers import ProviderRequestError
        if self.submission_may_exist:
            raise ProviderRequestError("provider_submit_uncertain")
        if before_request is not None:
            await before_request()
        if not self._authorized:
            if self.before_send is not None:
                await self.before_send()
            self._authorized = True
        if not self._reserved:
            if self.on_send is not None:
                self.on_send()
            self._reserved = True
        seconds = self.seconds if timeout_seconds is None else min(self.seconds, _positive_seconds(timeout_seconds))
        self.attempt_count += 1
        self.submission_may_exist = True
        self.last_attempt_rejected = False
        deadline = asyncio.timeout(seconds)
        try:
            async with deadline:
                result = await invoke()
        except TimeoutError as exc:
            if not deadline.expired():
                raise
            raise ProviderRequestError("provider_timeout") from exc
        # Finish before progress/persistence awaits, which cannot turn a
        # complete response into an unknown upstream request.
        self.submission_may_exist = False
        return result

    def rejected(self) -> None:
        """Called only with evidence of rejection or failure before connection."""
        self.submission_may_exist = False
        self.last_attempt_rejected = True


def current_response_timeout_seconds() -> float | None:
    return _response_seconds.get()


def current_response_scope() -> ResponseDeadlineScope | None:
    return _response_scope.get()


async def provider_local_step(awaitable: Awaitable[T], *, best_effort: bool = False) -> T | None:
    """Bound new-mode control/progress I/O without spending response time."""
    if current_response_scope() is None:
        return await awaitable
    deadline = asyncio.timeout(_LOCAL_STEP_SECONDS)
    try:
        async with deadline:
            return await awaitable
    except Exception as exc:
        if best_effort:
            logger.warning("Provider local status update failed; exception_type=%s", type(exc).__name__)
            return None
        if isinstance(exc, TimeoutError) and deadline.expired():
            raise _local_timeout() from exc
        raise


@contextmanager
def response_timeout_scope(
    seconds: float, *, response_only: bool = False, local_wait_seconds: float = 600,
    before_send=None, on_send=None,
) -> Iterator[ResponseDeadlineScope | None]:
    """Bound only this coroutine's requests; nested scopes cannot widen a cap."""
    seconds = _positive_seconds(seconds)
    previous = _response_seconds.get()
    seconds = seconds if previous is None else min(previous, seconds)
    scope = ResponseDeadlineScope(seconds, local_wait_seconds=local_wait_seconds, before_send=before_send,
                                  on_send=on_send) if response_only else None
    token = _response_seconds.set(seconds)
    scope_token = _response_scope.set(scope)
    try:
        yield scope
    finally:
        _response_scope.reset(scope_token)
        _response_seconds.reset(token)
