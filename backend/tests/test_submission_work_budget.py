"""Scaled clocks and local callbacks only: no real model calls or 3-hour waits."""
import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar

import pytest

from backend.domain.errors import RecognitionError
from backend.llm.providers import ProviderRequestError
from backend.llm.request_timeouts import ResponseDeadlineScope
from backend.services import submission_work_budget as clocks


@pytest.fixture
def install(monkeypatch):
    budgets = []
    monkeypatch.setattr(clocks, "_current", ContextVar("test_submission_work_budget", default=None))
    def apply():
        budget = clocks.SubmissionWorkBudget("job")
        budgets.append(budget)
        clocks._current.set(budget)
        return budget
    yield apply
    for budget in budgets:
        budget.expire()


@pytest.mark.asyncio
async def test_queue_longer_than_budget_does_not_consume_processing_time(install):
    budget = install()
    scope = ResponseDeadlineScope(1, local_wait_seconds=1)
    await scope.wait_local(asyncio.sleep(0.06))
    assert budget.active == 0 and not budget.exhausted.is_set()
    assert await scope.run_response(lambda: asyncio.sleep(0, result="done")) == "done"
    assert budget.active == 0


@pytest.mark.asyncio
async def test_parallel_work_is_counted_once_not_per_request(install):
    budget = install()
    ready, release = asyncio.Event(), asyncio.Event()
    starts = 0
    async def invoke():
        nonlocal starts
        starts += 1
        if starts == 10:
            ready.set()
        await release.wait()
    started = asyncio.get_running_loop().time()
    tasks = [asyncio.create_task(ResponseDeadlineScope(1, local_wait_seconds=1).run_response(invoke)) for _ in range(10)]
    await ready.wait()
    assert budget.active == 10
    await asyncio.sleep(0.02)
    release.set()
    await asyncio.gather(*tasks)
    wall = asyncio.get_running_loop().time() - started
    assert not budget.exhausted.is_set()
    assert budget.active == 0


@pytest.mark.asyncio
async def test_cap_stops_waiters_and_new_requests_but_drains_sent_response(install):
    budget = install()
    sent, release = asyncio.Event(), asyncio.Event()
    first = ResponseDeadlineScope(1, local_wait_seconds=1)
    async def invoke():
        sent.set()
        await release.wait()
        return "saved answer"
    running = asyncio.create_task(first.run_response(invoke))
    await sent.wait()
    waiting = asyncio.create_task(ResponseDeadlineScope(1, local_wait_seconds=1).wait_local(asyncio.Event().wait()))
    budget.expire()
    with pytest.raises(RecognitionError, match=clocks.CODE):
        await asyncio.wait_for(waiting, 1)
    assert not running.done()
    second = ResponseDeadlineScope(1, local_wait_seconds=1, on_send=lambda: pytest.fail("must not dispatch"))
    with pytest.raises(RecognitionError, match=clocks.CODE):
        await second.run_response(lambda: asyncio.sleep(0))
    assert second.attempt_count == 0 and not second.submission_may_exist
    release.set()
    assert await running == "saved answer"
    assert not first.submission_may_exist and budget.active == 0
    assert budget.exhausted.is_set()


@pytest.mark.asyncio
async def test_cap_during_durable_authorization_finishes_admitted_request(install):
    budget = install()
    authorized = []
    async def before_send():
        authorized.append("pending")
        budget.expire()
    scope = ResponseDeadlineScope(1, local_wait_seconds=1, before_send=before_send)
    assert await scope.run_response(lambda: asyncio.sleep(0, result="saved")) == "saved"
    assert authorized == ["pending"] and scope.attempt_count == 1
    assert not scope.submission_may_exist and budget.active == 0
    with pytest.raises(RecognitionError, match=clocks.CODE):
        await ResponseDeadlineScope(1, local_wait_seconds=1).run_response(lambda: asyncio.sleep(0))


@pytest.mark.asyncio
async def test_draining_keeps_original_request_deadline(install):
    budget = install()
    scope = ResponseDeadlineScope(0.04, local_wait_seconds=1)
    with pytest.raises(ProviderRequestError, match="provider_timeout"):
        await scope.run_response(lambda: asyncio.Event().wait())
    assert not budget.exhausted.is_set() and budget.active == 0
    assert scope.submission_may_exist  # An actually sent timeout remains uncertain.


@pytest.mark.asyncio
async def test_cancelled_request_releases_active_clock(install):
    budget = install()
    started = asyncio.Event()
    async def invoke():
        started.set()
        await asyncio.Event().wait()
    task = asyncio.create_task(ResponseDeadlineScope(1, local_wait_seconds=1).run_response(invoke))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert budget.active == 0


@pytest.mark.asyncio
async def test_known_failure_is_reported_immediately_without_spending_deadline(install):
    budget = install()
    async def broken():
        raise ValueError("synthetic immediate failure")
    with pytest.raises(ValueError, match="synthetic"):
        await asyncio.wait_for(ResponseDeadlineScope(10, local_wait_seconds=1).run_response(broken), 0.2)
    assert budget.active == 0 and not budget.exhausted.is_set()


@pytest.mark.asyncio
async def test_admission_retains_task_context_and_closes_lease(install):
    budget = install()
    context = ContextVar("lease", default=None)
    released = []
    @asynccontextmanager
    async def lease():
        token = context.set("same task")
        await asyncio.sleep(0)
        try:
            yield
        finally:
            context.reset(token)
            released.append(True)
    async with ResponseDeadlineScope(1, local_wait_seconds=1).local_admission(lease()):
        assert context.get() == "same task"
    assert context.get() is None and released == [True]
    assert budget.active == 0


@pytest.mark.asyncio
async def test_local_wait_protection_still_reports_capacity_not_model_timeout(install):
    budget = install()
    with pytest.raises(ProviderRequestError, match="provider_capacity_unavailable"):
        await ResponseDeadlineScope(1, local_wait_seconds=0.01).wait_local(asyncio.Event().wait())
    assert budget.active == 0 and not budget.exhausted.is_set()




@pytest.mark.asyncio
async def test_expiry_is_idempotent_while_waiter_is_being_cancelled(install):
    budget = install()
    task = asyncio.create_task(budget.wait(asyncio.Event().wait()))
    await asyncio.sleep(0)
    budget.expire()
    budget.expire()
    with pytest.raises(RecognitionError, match=clocks.CODE):
        await task
    assert not budget.waiters


@pytest.mark.asyncio
async def test_other_user_queued_work_keeps_a_fresh_independent_budget(install):
    released = asyncio.Event()
    async def first_user():
        budget = install()
        scope = ResponseDeadlineScope(1, local_wait_seconds=1)
        budget.expire()
        released.set()
        return budget
    async def second_user():
        budget = install()
        scope = ResponseDeadlineScope(1, local_wait_seconds=1)
        await scope.wait_local(released.wait())
        assert budget.active == 0 and not budget.exhausted.is_set()
        await scope.run_response(lambda: asyncio.sleep(0))
        return budget
    first, second = await asyncio.gather(first_user(), second_user())
    assert first.exhausted.is_set() and not second.exhausted.is_set()
    assert clocks.current_budget() is None


@pytest.mark.asyncio
async def test_submission_wrapper_does_not_leave_clock_in_other_workflows(monkeypatch):
    @clocks.submission_work_budget
    async def run(**kwargs):
        assert clocks.current_budget().job_id == "job"
        return "done"
    assert await run(job_id="job", job_attempt=1) == "done"
    assert clocks.current_budget() is None
