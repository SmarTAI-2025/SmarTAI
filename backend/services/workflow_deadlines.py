"""Durable execution limits. Initial queue time is excluded; requeues are not.

Active time is the union of admitted PDF/model/tool work (parallel calls count
once). Its cap closes admission and drains sent calls under their request caps.
The independent wall cap cancels work, including all waits, from first claim.
Both clocks survive reclaim; an explicit teacher retry starts a fresh attempt.
"""
from contextlib import asynccontextmanager
import asyncio
import logging
import time

from backend.config import settings
from backend.domain.errors import DomainError, LeaseLost, RecognitionError
from backend.domain.source_storage import DELAYED_SOURCE_OPERATION_TYPES
from backend.services import submission_work_budget as clocks

logger = logging.getLogger(__name__)
TASK_CLEANUP_SECONDS = 30.0
_quarantined_tasks = set()


async def cancel_and_drain(task):
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
    end = loop.time() + TASK_CLEANUP_SECONDS
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

GRADING_SECONDS = SUBMISSION_SECONDS = 150 * 60
QUESTION_SECONDS = 90 * 60
GRADING_WALL_SECONDS = SUBMISSION_WALL_SECONDS = 210 * 60
QUESTION_WALL_SECONDS = 150 * 60
QUESTION_TYPES = frozenset({"problem_extraction", "question_preparation", "material_import", "ai_completion"})
BUDGET_TYPES = QUESTION_TYPES | {"submission_recognition"}
CHECKPOINT_KEY = "execution_deadline"
WORK_CHECKPOINT_KEY = "workflow_work_clock"
LIMIT_CODES = frozenset({"grading_time_limit_reached", "question_preparation_time_limit_reached",
    "submission_time_limit_reached", "workflow_time_limit_reached", "grading_wall_time_limit_reached",
    "question_preparation_wall_time_limit_reached", "submission_wall_time_limit_reached"})


def workflow_limit(operation_type):
    if operation_type in QUESTION_TYPES:
        return QUESTION_WALL_SECONDS
    if operation_type == "submission_recognition":
        return SUBMISSION_WALL_SECONDS
    if operation_type in DELAYED_SOURCE_OPERATION_TYPES:
        return max(.001, float(settings.background_pass_timeout_seconds))
    return None


def policy(kind):
    if kind in QUESTION_TYPES:
        return QUESTION_SECONDS, QUESTION_WALL_SECONDS, "question_preparation"
    if kind == "submission_recognition":
        return SUBMISSION_SECONDS, SUBMISSION_WALL_SECONDS, "submission"
    if kind == "grading":
        return GRADING_SECONDS, GRADING_WALL_SECONDS, "grading"
    raise ValueError("Workload must choose an explicit time policy")


@asynccontextmanager
async def execution_deadline(deadline_at, code):
    if deadline_at is None:
        yield
        return
    remaining = float(deadline_at) - time.time()
    if remaining <= 0:
        raise DomainError(code, code=code)
    deadline = asyncio.timeout(remaining)
    try:
        async with deadline:
            yield
    except (Exception, asyncio.CancelledError):
        if not deadline.expired():
            raise
        # Cleanup can raise its own exception after cancellation. The owner
        # deadline remains the reason this run stopped, not a retryable IO error.
        raise DomainError(code, code=code) from None
    if deadline.expired():
        raise DomainError(code, code=code)


@asynccontextmanager
async def processing_budget(job_id, *, kind, attempt=1, saved=None, started_at=None, deadline_at=None, persist=None,
                            raise_on_blocked=True):
    active_seconds, wall_seconds, prefix = policy(kind)
    budget = clocks.SubmissionWorkBudget(job_id, attempt=attempt, saved=saved,
        limit_seconds=active_seconds, code=f"{prefix}_time_limit_reached",
        profile=f"{prefix}-active-v2", checkpoint_key=WORK_CHECKPOINT_KEY)
    owner = asyncio.current_task()
    token = clocks._current.set(budget)
    started_at = time.time() if started_at is None else started_at
    deadline_at = started_at + wall_seconds if deadline_at is None else min(deadline_at, started_at + wall_seconds)

    async def save():
        if persist is not None:
            await persist(budget.snapshot())

    async def watch():
        reported = False
        while True:
            await asyncio.sleep(5)
            try:
                await save()
            except LeaseLost:
                owner.cancel()
                return
            except Exception as exc:
                logger.warning("Work clock checkpoint failed; exception_type=%s", type(exc).__name__)
            if budget.exhausted.is_set() and not reported:
                from backend.progress.tracker import get_reporter
                if (reporter := get_reporter(job_id)) is not None:
                    await reporter._emit_message(budget.code, level="warn")
                reported = True

    watcher = asyncio.create_task(watch())
    completed = False
    try:
        async with execution_deadline(deadline_at, f"{prefix}_wall_time_limit_reached"):
            yield budget
            if budget.blocked and raise_on_blocked:
                raise RecognitionError(budget.code)
            completed = True
    finally:
        if not completed:
            budget.expire()  # Fenced/quarantined children must never submit new paid work.
        watcher.cancel()
        try:
            await asyncio.gather(watcher, return_exceptions=True)
            # Terminal handlers may already have released their lease.
            try:
                await save()
            except Exception:
                pass
        finally:
            budget.close()
            clocks._current.reset(token)


@asynccontextmanager
async def operation_budget(ctx):
    if ctx.operation_type not in BUDGET_TYPES:
        limit = workflow_limit(ctx.operation_type)
        async with execution_deadline(time.time() + limit if limit else None, "workflow_time_limit_reached"):
            yield
        return
    clock = ctx.checkpoint_data[CHECKPOINT_KEY]
    saved = ctx.checkpoint_data.get(WORK_CHECKPOINT_KEY)
    if saved is None and ctx.operation_type == "submission_recognition":
        # Retain the old deployed active ledger when adopting the lower cap.
        legacy = ctx.checkpoint_data.get("submission_work_clock")
        if isinstance(legacy, dict) and legacy.get("profile") == "submission-active-3h-v1":
            saved = {**legacy, "profile": "submission-active-v2"}
    async def persist(snapshot):
        await ctx.checkpoint(stage=ctx.checkpoint_stage,
            checkpoint={**ctx.checkpoint_data, WORK_CHECKPOINT_KEY: snapshot}, artifact_refs=ctx.artifact_refs)
    async with processing_budget(ctx.operation_id, kind=ctx.operation_type, attempt=ctx.attempt,
        saved=saved, started_at=clock["started_at"], deadline_at=clock.get("deadline_at"), persist=persist):
        yield
