"""Three durable workflows share the existing active clock and one total cap.

Initial queueing is excluded. Reclaim keeps the first claim's wall deadline and
saved active work; explicit retry starts a new attempt. No stage/request limits
are changed here. Active expiry drains admitted work; total expiry cancels it.
"""
import asyncio
from contextlib import asynccontextmanager
import logging
import time

from backend.domain.errors import DomainError, LeaseLost, RecognitionError
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

QUESTION_SECONDS, QUESTION_WALL_SECONDS = 90 * 60, 150 * 60
SUBMISSION_SECONDS, SUBMISSION_WALL_SECONDS = 150 * 60, 210 * 60
GRADING_SECONDS, GRADING_WALL_SECONDS = 150 * 60, 210 * 60
BUDGET_TYPES = frozenset({"question_preparation", "submission_recognition"})
CHECKPOINT_KEY = "execution_deadline"
WORK_CHECKPOINT_KEY = "workflow_work_clock"
LIMIT_CODES = frozenset(f"{kind}_{limit}_limit_reached"
    for kind in ("question_preparation", "submission", "grading") for limit in ("time", "wall_time"))


def policy(kind):
    if kind == "question_preparation":
        return QUESTION_SECONDS, QUESTION_WALL_SECONDS, "question_preparation"
    if kind == "submission_recognition":
        return clocks.LIMIT_SECONDS, SUBMISSION_WALL_SECONDS, "submission"
    if kind == "grading":
        return GRADING_SECONDS, GRADING_WALL_SECONDS, "grading"
    raise ValueError("Only question preparation, submission recognition and grading have this budget")


@asynccontextmanager
async def execution_deadline(deadline_at, code):
    remaining = deadline_at - time.time()
    if remaining <= 0:
        raise DomainError(code, code=code)
    deadline = asyncio.timeout(remaining)
    try:
        async with deadline:
            yield
    except (Exception, asyncio.CancelledError):
        if not deadline.expired():
            raise
        raise DomainError(code, code=code) from None
    if deadline.expired():
        raise DomainError(code, code=code)


@asynccontextmanager
async def processing_budget(job_id, *, kind, attempt=1, saved=None, started_at=None, deadline_at=None,
                            persist=None, raise_on_blocked=True):
    active_seconds, wall_seconds, prefix = policy(kind)
    submission = kind == "submission_recognition"
    budget = clocks.SubmissionWorkBudget(job_id, attempt=attempt, saved=saved,
        limit_seconds=active_seconds, code=f"{prefix}_time_limit_reached",
        profile=clocks.PROFILE if submission else f"{prefix}-active-v1",
        checkpoint_key=clocks.CHECKPOINT_KEY if submission else WORK_CHECKPOINT_KEY)
    owner = asyncio.current_task()
    token = clocks._current.set(budget)
    started_at = time.time() if started_at is None else started_at
    deadline_at = started_at + wall_seconds if deadline_at is None else min(deadline_at, started_at + wall_seconds)

    async def save():
        if persist is not None:
            await persist(budget.snapshot())

    closing = False
    async def watch():
        reported = False
        while not closing:
            await asyncio.sleep(5)
            try:
                await save()
            except LeaseLost:
                owner.cancel()
                return
            except Exception as exc:
                logger.warning("Work clock checkpoint failed; exception_type=%s", type(exc).__name__)
            if closing:
                return
            if budget.exhausted.is_set() and not reported:
                from backend.progress.tracker import get_reporter
                if (reporter := get_reporter(job_id)) is not None:
                    await reporter._emit_message(budget.code, level="warn")
                reported = True

    watcher = asyncio.create_task(watch())
    completed = False
    try:
        from backend.llm.request_timeouts import workflow_cancellation_scope
        with workflow_cancellation_scope():
            async with execution_deadline(deadline_at, f"{prefix}_wall_time_limit_reached"):
                yield budget
                if budget.blocked and raise_on_blocked:
                    raise RecognitionError(budget.code)
                completed = True
    finally:
        closing = True
        if not completed:
            budget.expire()  # Cancelled children cannot admit replacement work.
        cleanup_until = getattr(budget, "cleanup_deadline", asyncio.get_running_loop().time() + TASK_CLEANUP_SECONDS)
        try:
            await cancel_and_drain(watcher, deadline=cleanup_until)
            # The last checkpoint shares the coordinator's cleanup allowance.
            # Its lease/attempt fence still rejects a late write after recovery.
            remaining = cleanup_until - asyncio.get_running_loop().time()
            if persist is not None and remaining > 0:
                saving = asyncio.create_task(save())
                try:
                    await asyncio.wait({saving}, timeout=remaining)
                finally:
                    await cancel_and_drain(saving, deadline=cleanup_until)
        finally:
            budget.close()
            clocks._current.reset(token)


@asynccontextmanager
async def operation_budget(ctx):
    if ctx.operation_type not in BUDGET_TYPES:
        yield
        return
    clock = ctx.checkpoint_data[CHECKPOINT_KEY]
    key = clocks.CHECKPOINT_KEY if ctx.operation_type == "submission_recognition" else WORK_CHECKPOINT_KEY
    async def persist(snapshot):
        await ctx.checkpoint(stage=ctx.checkpoint_stage,
            checkpoint={**ctx.checkpoint_data, key: snapshot}, artifact_refs=ctx.artifact_refs)
    async with processing_budget(ctx.operation_id, kind=ctx.operation_type, attempt=ctx.attempt,
        saved=ctx.checkpoint_data.get(key), started_at=clock["started_at"],
        deadline_at=clock["deadline_at"], persist=persist):
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
