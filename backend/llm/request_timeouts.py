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
LOCAL_PREPARATION_SECONDS = 60.0


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

    Rejections and explicitly authorized timeout retries share the caller's
    attempt limit. A timeout retry never proves the previous request unbilled.
    """

    def __init__(self, seconds, *, local_wait_seconds, before_send=None, on_send=None):
        self.seconds = _positive_seconds(seconds)
        self.local_wait_remaining = _positive_seconds(local_wait_seconds)
        self.before_send, self.on_send = before_send, on_send
        self.attempt_count = 0
        self.submission_may_exist = False
        self.last_attempt_rejected = False
        self.had_uncertain_timeout = False
        self._timeout_retry_authorized = False
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
                from backend.services.submission_work_budget import wait_for_admission
                return await wait_for_admission(awaitable)
        except TimeoutError as exc:
            if not deadline.expired():
                raise
            raise _local_timeout() from exc
        finally:
            self.local_wait_remaining = max(0.0, self.local_wait_remaining - (loop.time() - started))

    @asynccontextmanager
    async def local_admission(self, lease):
        # Waiting for an internal slot consumes neither the local preparation
        # allowance nor response time. Workflow limits/cancellation still apply;
        # releasing an acquired lease must remain cancellation-safe.
        from backend.services.submission_work_budget import wait_for_admission
        async with AsyncExitStack() as stack:
            await wait_for_admission(stack.enter_async_context(lease))
            yield

    async def run_response(
        self, invoke: Callable[[], Awaitable[T]], *, timeout_seconds=None, before_request=None,
    ) -> T:
        from backend.llm.providers import ProviderRequestError
        if self.submission_may_exist and not (
            self._timeout_retry_authorized or self.last_attempt_rejected
        ):
            raise ProviderRequestError("provider_submit_uncertain")
        from backend.services.submission_work_budget import current_budget
        budget = current_budget()
        if budget is not None:
            budget.check()
        if before_request is not None:
            await before_request()
        seconds = self.seconds if timeout_seconds is None else min(self.seconds, _positive_seconds(timeout_seconds))
        if budget is not None:
            budget.start()
        deadline = None
        try:
            # Admission precedes the durable pending marker. Once admitted,
            # finish this bounded authorization/send even if the batch drains;
            # otherwise a never-sent request could be left marked uncertain.
            if not self._authorized:
                if self.before_send is not None:
                    await self.before_send()
                self._authorized = True
            if not self._reserved:
                if self.on_send is not None:
                    self.on_send()
                self._reserved = True
            self.attempt_count += 1
            self._timeout_retry_authorized = False
            self.submission_may_exist = True
            self.last_attempt_rejected = False
            deadline = asyncio.timeout(seconds)
            async with deadline:
                result = await invoke()
        except TimeoutError as exc:
            if deadline is None or not deadline.expired():
                raise
            raise ProviderRequestError("provider_timeout") from exc
        finally:
            if budget is not None:
                budget.finish()
        # Finish before progress/persistence awaits, which cannot turn a
        # complete response into an unknown upstream request.
        self.submission_may_exist = False
        return result

    def rejected(self) -> None:
        """Called only with evidence of rejection or failure before connection."""
        # A later rejection cannot disprove an earlier timed-out submission.
        self.submission_may_exist = self.had_uncertain_timeout
        self.last_attempt_rejected = True

    def authorize_timeout_retry(self) -> None:
        """Permit one bounded retry without clearing uncertain-submission evidence."""
        self.had_uncertain_timeout |= self.submission_may_exist
        self._timeout_retry_authorized = True


def is_response_timeout(exc: BaseException) -> bool:
    """Recognize response/transport timeouts, never a stale exception context.

    Workflow deadlines and local admission/preparation failures have their own
    codes and must not gain model retry authority through a timeout cause.
    """
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        code = getattr(exc, "code", None)
        if isinstance(code, str) and code not in {
            "provider_upstream_unavailable", "provider_request_failed", "provider_request_rejected",
        }:
            return code == "provider_timeout"
        status = getattr(exc, "status_code", None)
        if status is None:
            status = getattr(getattr(exc, "response", None), "status_code", None)
        if status is None and type(code) is int:  # Google SDK HTTP errors.
            status = code
        if isinstance(status, int):
            return status in {408, 504}
        if isinstance(exc, TimeoutError) or type(exc).__name__ in {
            "APITimeoutError", "TimeoutException", "ConnectTimeout", "ReadTimeout",
            "WriteTimeout", "PoolTimeout", "DeadlineExceeded",
        }:
            return True
        exc = exc.__cause__
    return False


def current_response_timeout_seconds() -> float | None:
    return _response_seconds.get()


def current_response_scope() -> ResponseDeadlineScope | None:
    return _response_scope.get()


async def provider_local_step(awaitable: Awaitable[T], *, best_effort: bool = False) -> T | None:
    """Bound advisory I/O in every mode; preserve required control failures."""
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
    seconds: float, *, response_only: bool = False, local_wait_seconds: float | None = None,
    before_send=None, on_send=None,
) -> Iterator[ResponseDeadlineScope | None]:
    """Bound only this coroutine's requests; nested scopes cannot widen a cap."""
    seconds = _positive_seconds(seconds)
    previous = _response_seconds.get()
    seconds = seconds if previous is None else min(previous, seconds)
    scope = ResponseDeadlineScope(seconds, local_wait_seconds=LOCAL_PREPARATION_SECONDS if local_wait_seconds is None else local_wait_seconds, before_send=before_send,
                                  on_send=on_send) if response_only else None
    token = _response_seconds.set(seconds)
    scope_token = _response_scope.set(scope)
    try:
        yield scope
    finally:
        _response_scope.reset(scope_token)
        _response_seconds.reset(token)
