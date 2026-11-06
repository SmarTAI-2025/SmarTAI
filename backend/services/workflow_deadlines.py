"""Whole-workflow time limits with cancellation-safe admission and cleanup.

Initial queueing is excluded. Reclaim keeps the first claim's wall deadline;
explicit retry starts a new attempt. No active-work cutoff or periodic timing
writes: the stored start/end timestamps measure the whole attempt.
"""
import asyncio
from contextlib import asynccontextmanager
import logging
import time

from backend.domain.errors import DomainError, RecognitionError
from backend.services import submission_work_budget as clocks

logger = logging.getLogger(__name__)
TASK_CLEANUP_SECONDS = 30.0
_quarantined_tasks = set()


async def cancel_and_drain(task, *, deadline=None):
    """Bound cooperative shutdown; never abandon ownership of stuck children.

    Provider/process gates remain owned by the child until its cleanup finishes.
    Durable writes are independently lease fenced before another attempt runs.
    """
    if task is None:
        return True
    if task in _quarantined_tasks:
        return False
    if not task.done():
        task.cancel()
    loop = asyncio.get_running_loop()
    end = loop.time() + TASK_CLEANUP_SECONDS if deadline is None else deadline
    cancelled = False
    while not task.done() and loop.time() < end:
        try:
            await asyncio.wait({task}, timeout=max(0, end - loop.time()))
        except asyncio.CancelledError:
            # Repeated stop signals must not restart the cleanup allowance.
            cancelled = True
    if task.done():
        if not task.cancelled():
            task.exception()
        if cancelled:
            raise asyncio.CancelledError
        return True
    _quarantined_tasks.add(task)
    def finished(done):
        _quarantined_tasks.discard(done)
        if not done.cancelled():
            done.exception()
    task.add_done_callback(finished)
    logger.error("Execution cleanup exceeded its bound; child resources remain quarantined")
    if cancelled:
        raise asyncio.CancelledError
    return False

QUESTION_WALL_SECONDS = 150 * 60
SUBMISSION_WALL_SECONDS = 210 * 60
GRADING_WALL_SECONDS = 210 * 60
KNOWLEDGE_WALL_SECONDS = 120 * 60
QUESTION_TYPES = frozenset({"question_preparation", "problem_extraction", "material_import", "ai_completion"})
BUDGET_TYPES = QUESTION_TYPES | {"submission_recognition"}
CHECKPOINT_KEY = "execution_deadline"
LIMIT_CODES = frozenset(f"{kind}_wall_time_limit_reached"
    for kind in ("question_preparation", "submission", "grading", "knowledge"))


def policy(kind):
    if kind in QUESTION_TYPES:
        return QUESTION_WALL_SECONDS, "question_preparation"
    if kind == "submission_recognition":
        return SUBMISSION_WALL_SECONDS, "submission"
    if kind == "grading":
        return GRADING_WALL_SECONDS, "grading"
    if kind == "knowledge_ingestion":
        return KNOWLEDGE_WALL_SECONDS, "knowledge"
    raise ValueError("Unsupported timed workflow")


@asynccontextmanager
async def execution_deadline(deadline_at, code):
    remaining = deadline_at - time.time()
    if remaining <= 0:
        raise DomainError(code, code=code, status_code=504)
    deadline = asyncio.timeout(remaining)
    try:
        async with deadline:
            yield
    except (Exception, asyncio.CancelledError):
        if not deadline.expired():
            raise
        raise DomainError(code, code=code, status_code=504) from None
    if deadline.expired():
        raise DomainError(code, code=code, status_code=504)


@asynccontextmanager
async def processing_budget(job_id, *, kind, started_at=None, deadline_at=None, raise_on_blocked=True):
    wall_seconds, prefix = policy(kind)
    budget = clocks.SubmissionWorkBudget(job_id, code=f"{prefix}_wall_time_limit_reached")
    token = clocks._current.set(budget)
    started_at = time.time() if started_at is None else started_at
    deadline_at = started_at + wall_seconds if deadline_at is None else min(deadline_at, started_at + wall_seconds)

    completed = False
    try:
        async with execution_deadline(deadline_at, f"{prefix}_wall_time_limit_reached"):
            yield budget
            if budget.blocked and raise_on_blocked:
                raise RecognitionError(budget.code)
            completed = True
    finally:
        if not completed:
            budget.expire()  # Cancelled children cannot admit replacement work.
        clocks._current.reset(token)


@asynccontextmanager
async def operation_budget(ctx):
    if ctx.operation_type not in BUDGET_TYPES:
        yield
        return
    clock = ctx.checkpoint_data[CHECKPOINT_KEY]
    async with processing_budget(ctx.operation_id, kind=ctx.operation_type,
        started_at=clock["started_at"], deadline_at=clock["deadline_at"]):
        yield


async def run_owned_work(awaitable):
    """Bound only coordinator shutdown, retaining ownership of undrained work."""
    child = asyncio.create_task(awaitable)
    try:
        return await asyncio.shield(child)
    finally:
        budget = clocks.current_budget()
        if not child.done() and budget is not None:
            budget.expire()
            budget.cleanup_deadline = asyncio.get_running_loop().time() + TASK_CLEANUP_SECONDS
        await cancel_and_drain(child, deadline=getattr(budget, "cleanup_deadline", None))
