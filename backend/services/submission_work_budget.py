"""Attempt-local submission work clock; queue waits do not consume its budget.

Durable recognition and grading install this shared context. Concurrent PDF/model work is
counted once in elapsed seconds. Exhaustion closes admission, not running calls:
their existing request deadlines still bound draining and result persistence.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from functools import wraps
import math

from backend.domain.errors import RecognitionError


LIMIT_SECONDS = 150 * 60
CODE = "submission_time_limit_reached"
PROFILE = "submission-active-v2"
CHECKPOINT_KEY = "submission_work_clock"
_current = ContextVar("submission_work_budget", default=None)


class SubmissionWorkBudget:
    def __init__(self, job_id, *, attempt=1, saved=None, limit_seconds=None,
                 code=CODE, profile=PROFILE, checkpoint_key=CHECKPOINT_KEY):
        self.job_id = job_id
        self.attempt = attempt
        self.limit = LIMIT_SECONDS if limit_seconds is None else limit_seconds
        self.code, self.profile, self.checkpoint_key = code, profile, checkpoint_key
        saved = saved if isinstance(saved, dict) and saved.get("profile") == profile and saved.get("attempt") == attempt else {}
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
            raise RecognitionError(self.code)

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
        return {"profile": self.profile, "attempt": self.attempt, "active_seconds": self.elapsed,
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
            raise RecognitionError(self.code)
        finally:
            self.waiters.discard(deadline)

    def close(self):
        if self.timer is not None:
            self.timer.cancel()


def current_budget():
    return _current.get()


def with_work_checkpoint(job_id, checkpoint):
    budget = current_budget()
    return {**checkpoint, budget.checkpoint_key: budget.snapshot()} if budget is not None and budget.job_id == job_id else checkpoint


async def wait_for_admission(awaitable):
    budget = current_budget()
    return await budget.wait(awaitable) if budget is not None else await awaitable


def submission_work_budget(function):
    @wraps(function)
    async def run(*args, **kwargs):
        if current_budget() is not None:
            return await function(*args, **kwargs)
        operation = kwargs.get("leased_operation")
        from backend.services.workflow_deadlines import processing_budget, CHECKPOINT_KEY as WALL_KEY, WORK_CHECKPOINT_KEY
        saved = operation.checkpoint_data.get(WORK_CHECKPOINT_KEY) if operation is not None else None
        started_at = (operation.checkpoint_data.get(WALL_KEY) or {}).get("started_at") if operation is not None else None
        async def persist(snapshot):
            if operation is not None:
                await operation.checkpoint(stage=operation.checkpoint_stage,
                    checkpoint={**operation.checkpoint_data, WORK_CHECKPOINT_KEY: snapshot},
                    artifact_refs=operation.artifact_refs)
        async with processing_budget(kwargs["job_id"], kind="submission_recognition", attempt=kwargs["job_attempt"],
                                     saved=saved, started_at=started_at, persist=persist, raise_on_blocked=False):
            return await function(*args, **kwargs)
    return run
