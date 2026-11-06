"""Run-local admission guard. Only the owning workflow sets a time limit.

Cancellation closes admission before children drain; no active-work clock,
periodic timer or timing checkpoint is needed here.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from contextlib import asynccontextmanager
from functools import wraps

from backend.domain.errors import RecognitionError

CODE = "submission_wall_time_limit_reached"
_current = ContextVar("submission_work_budget", default=None)


class SubmissionWorkBudget:
    def __init__(self, job_id, *, code=CODE):
        self.job_id = job_id
        self.code = code
        self.active = 0
        self.waiters = set()
        self.blocked = False
        self.exhausted = asyncio.Event()

    def check(self):
        if self.exhausted.is_set():
            self.blocked = True
            raise RecognitionError(self.code)

    def start(self):
        self.check()
        self.active += 1

    def finish(self):
        self.active -= 1

    def expire(self):
        if self.exhausted.is_set():
            return
        self.exhausted.set()
        if self.waiters:
            now = asyncio.get_running_loop().time()
            for deadline in self.waiters:
                deadline.reschedule(now)

    async def wait(self, awaitable):
        """Wake queued admission when the owner stops; do not time the wait."""
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


def current_budget():
    return _current.get()


async def wait_for_admission(awaitable):
    budget = current_budget()
    return await budget.wait(awaitable) if budget is not None else await awaitable



@asynccontextmanager
async def active_work():
    """Refuse new computation after the workflow stops, retaining ownership."""
    budget = current_budget()
    if budget is not None:
        budget.start()
    try:
        yield
    finally:
        if budget is not None:
            budget.finish()


def submission_work_budget(function):
    @wraps(function)
    async def run(*args, **kwargs):
        if current_budget() is not None:
            return await function(*args, **kwargs)
        operation = kwargs.get("leased_operation")
        from backend.services.workflow_deadlines import processing_budget, CHECKPOINT_KEY as WALL_KEY
        checkpoint = operation.checkpoint_data if operation is not None else {}
        async with processing_budget(kwargs["job_id"], kind="submission_recognition",
            started_at=(checkpoint.get(WALL_KEY) or {}).get("started_at"),
            deadline_at=(checkpoint.get(WALL_KEY) or {}).get("deadline_at"), raise_on_blocked=False):
            return await function(*args, **kwargs)
    return run
