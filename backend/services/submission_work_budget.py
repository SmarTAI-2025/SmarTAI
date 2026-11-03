"""Attempt-local submission work clock; queue waits do not consume its budget.

Only submission parsing installs this context. Concurrent PDF/model work is
counted once in elapsed seconds. Exhaustion closes admission, not running calls:
their existing request deadlines still bound draining and result persistence.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from functools import wraps
import math
import logging

from backend.domain.errors import RecognitionError, LeaseLost

logger = logging.getLogger(__name__)

LIMIT_SECONDS = 3 * 60 * 60
CODE = "submission_time_limit_reached"
PROFILE = "submission-active-3h-v1"
CHECKPOINT_KEY = "submission_work_clock"
_current = ContextVar("submission_work_budget", default=None)


class SubmissionWorkBudget:
    def __init__(self, job_id, *, attempt=1, saved=None, limit_seconds=None):
        self.job_id = job_id
        self.attempt = attempt
        self.limit = LIMIT_SECONDS if limit_seconds is None else limit_seconds
        saved = saved if isinstance(saved, dict) and saved.get("profile") == PROFILE and saved.get("attempt") == attempt else {}
        spent = saved.get("active_seconds", 0)
        self.spent = float(spent) if type(spent) in (float, int) and math.isfinite(spent) and spent >= 0 else 0.0
        self.active = 0
        self.started = None
        self.timer = None
        self.waiters = set()
        self.blocked = saved.get("blocked") is True
        self.exhausted = asyncio.Event()
        if self.spent >= self.limit or saved.get("draining") is True:
            self.exhausted.set()

    @property
    def elapsed(self):
        return self.spent + (asyncio.get_running_loop().time() - self.started if self.started is not None else 0)

    def check(self):
        if self.elapsed >= self.limit:
            self.expire()
        if self.exhausted.is_set():
            self.blocked = True
            raise RecognitionError(CODE)

    def start(self):
        self.check()
        if self.active == 0:
            loop = asyncio.get_running_loop()
            self.started = loop.time()
            self.timer = loop.call_later(max(0, self.limit - self.spent), self.expire)
        self.active += 1

    def finish(self):
        self.active -= 1
        if self.active == 0:
            self.spent = self.elapsed
            self.started = None
            self.timer.cancel()
            self.timer = None
            if self.spent >= self.limit:
                self.expire()

    def expire(self):
        if self.exhausted.is_set():
            return
        self.exhausted.set()
        now = asyncio.get_running_loop().time()
        for deadline in self.waiters:
            deadline.reschedule(now)

    def snapshot(self):
        return {"profile": PROFILE, "attempt": self.attempt, "active_seconds": self.elapsed,
                "draining": self.exhausted.is_set(), "blocked": self.blocked}

    async def wait(self, awaitable):
        """Wake queued admission promptly at the cap; do not time the wait."""
        deadline = asyncio.timeout(None)
        try:
            async with deadline:
                self.waiters.add(deadline)
                if self.exhausted.is_set():
                    deadline.reschedule(asyncio.get_running_loop().time())
                return await awaitable
        except TimeoutError:
            if not deadline.expired():
                raise
            self.blocked = True
            raise RecognitionError(CODE)
        finally:
            self.waiters.discard(deadline)

    def close(self):
        if self.timer is not None:
            self.timer.cancel()


def current_budget():
    return _current.get()


def with_work_checkpoint(job_id, checkpoint):
    budget = current_budget()
    return {**checkpoint, CHECKPOINT_KEY: budget.snapshot()} if budget is not None and budget.job_id == job_id else checkpoint


async def wait_for_admission(awaitable):
    budget = current_budget()
    return await budget.wait(awaitable) if budget is not None else await awaitable


def submission_work_budget(function):
    @wraps(function)
    async def run(*args, **kwargs):
        operation = kwargs.get("leased_operation")
        saved = operation.checkpoint_data.get(CHECKPOINT_KEY) if operation is not None else None
        budget = SubmissionWorkBudget(kwargs["job_id"], attempt=kwargs["job_attempt"], saved=saved)
        token = _current.set(budget)
        owner_task = asyncio.current_task()

        async def persist_clock():
            reported = False
            while True:
                await asyncio.sleep(5)
                if operation is not None:
                    try:
                        await operation.checkpoint(stage=operation.checkpoint_stage,
                            checkpoint=operation.checkpoint_data, artifact_refs=operation.artifact_refs)
                    except LeaseLost:
                        owner_task.cancel()
                        return
                    except Exception as exc:
                        logger.warning("Submission work clock checkpoint failed; exception_type=%s", type(exc).__name__)
                if budget.exhausted.is_set() and not reported:
                    from backend.progress.tracker import get_reporter
                    if (reporter := get_reporter(budget.job_id)) is not None:
                        await reporter._emit_message(CODE, level="warn")
                    reported = True

        watcher = asyncio.create_task(persist_clock())
        try:
            return await function(*args, **kwargs)
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            budget.close()
            _current.reset(token)
    return run
