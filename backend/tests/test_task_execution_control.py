import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest

from backend.config import settings
from backend.db import grading_repository as grades, workflow_repository as flows
from backend.db.models import AssignmentRecord, GradingRunRecord
from backend.db.session import session_scope
from backend.domain.errors import InvalidTransition, LeaseLost, NotFound, VersionConflict
from backend.llm.providers import ProviderRequestError
from backend.progress.tracker import get_or_create_reporter, remove_reporter
from backend.services.execution_control import cancel_local, run_controlled
from backend.services.task_execution import continue_auxiliary_run, stop_task_run
from backend.services.task_facade import task_state
from backend.tests.test_workflow_worker import _seed_operation, _make_worker, _drain
from backend.tools import structured_llm as llm


def another_task(owner, original, kind="submission_recognition"):
    task_id = uuid.uuid4().hex
    with session_scope() as session:
        course_id = session.get(AssignmentRecord, original).course_id
        session.add(AssignmentRecord(id=task_id, course_id=course_id, teacher_id=owner,
                                     name="Queued task", status="draft", version=1))
    flows.ensure_workflow(assignment_id=task_id, owner_id=owner)
    op, _ = flows.create_operation(assignment_id=task_id, owner_id=owner,
                                   operation_type=kind, input_hash=uuid.uuid4().hex)
    return task_id, op


def activate(owner, task, op):
    return flows.update_workflow(task, owner_id=owner, active_job_id=op.id,
                                active_operation=op.operation_type, presentation_status="extracting_problems")


def claim(owner, op):
    return flows.claim_operation(op.id, owner_id=owner, worker_id="execution-test", lease_seconds=60)


def seed_grade(owner, task):
    run_id = uuid.uuid4().hex
    with session_scope() as session:
        session.add(GradingRunRecord(id=run_id, assignment_id=task, teacher_id=owner, status="queued"))
    return run_id


def test_one_user_cannot_overlap_recognition_and_grading_but_other_users_can(monkeypatch):
    monkeypatch.setattr(settings, "workload_max_in_flight", 2)
    owner, task, op = _seed_operation()
    other, other_task, other_op = _seed_operation()
    activate(owner, task, op)
    claim(owner, op)
    queued_task, queued = another_task(owner, task)
    activate(owner, queued_task, queued)
    run_id = seed_grade(owner, queued_task)
    with pytest.raises(LeaseLost, match="Another task"):
        grades.claim_lease(run_id, worker_id="grader", lease_seconds=60)
    with pytest.raises(LeaseLost, match="Another task"):
        claim(owner, queued)
    assert task_state(task_id=queued_task, owner_id=owner)["queue_reason"] == "user_busy"
    assert queued.id not in {row.id for row in flows.list_claimable_operations(["submission_recognition"])}
    claim(other, other_op)
    third, third_task, third_op = _seed_operation()
    activate(third, third_task, third_op)
    assert task_state(task_id=third_task, owner_id=third)["queue_reason"] == "server_busy"
    with pytest.raises(LeaseLost, match="slots"):
        claim(third, third_op)


@pytest.mark.parametrize("running", [False, True])
def test_stop_preserves_inputs_fences_late_writes_and_continues_at_end_of_queue(running):
    owner, task, op = _seed_operation("material_import")
    flows.update_operation(op.id, owner_id=owner, expected_attempt=op.attempt,
                           payload={"source_token": "saved-source", "provider_id": "saved-provider"})
    workflow = activate(owner, task, op)
    leased = claim(owner, op) if running else None
    result = stop_task_run(task_id=task, owner_id=owner, job_id=op.id, expected_revision=workflow.workflow_revision)
    assert result["status"] == "stopped"
    stopped = flows.get_operation(op.id, owner_id=owner)
    assert stopped.payload["source_token"] == "saved-source"
    assert stopped.error_code == "operation_cancelled" and stopped.lease_owner is None
    if leased:
        with pytest.raises((LeaseLost, InvalidTransition)):
            flows.update_operation(op.id, owner_id=owner, expected_attempt=leased.attempt,
                                   expected_lease_token=leased.lease_token, status="done")
    _, queued = another_task(owner, task, "material_import")
    revision = flows.get_workflow(task, owner_id=owner).workflow_revision
    assert continue_auxiliary_run(task_id=task, owner_id=owner, job_id=op.id, expected_revision=revision)["status"] == "started"
    resumed = flows.get_operation(op.id, owner_id=owner)
    assert resumed.attempt == op.attempt + 1
    assert resumed.payload["provider_id"] == "saved-provider"
    order = [row.id for row in flows.list_claimable_operations(["material_import"])]
    assert order.index(queued.id) < order.index(op.id)
    resumed_claim = claim(owner, resumed)
    flows.update_operation(op.id, owner_id=owner, expected_attempt=resumed_claim.attempt,
                           expected_lease_token=resumed_claim.lease_token, progress={"phase": "running"})
    # An old page cannot stop the resumed attempt using its stale revision.
    with pytest.raises(VersionConflict):
        stop_task_run(task_id=task, owner_id=owner, job_id=op.id, expected_revision=workflow.workflow_revision)


def test_stop_is_owner_scoped_and_idempotent():
    owner, task, op = _seed_operation()
    workflow = activate(owner, task, op)
    with pytest.raises(NotFound):
        stop_task_run(task_id=task, owner_id="someone-else", job_id=op.id, expected_revision=workflow.workflow_revision)
    stop_task_run(task_id=task, owner_id=owner, job_id=op.id, expected_revision=workflow.workflow_revision)
    assert stop_task_run(task_id=task, owner_id=owner, job_id=op.id, expected_revision=workflow.workflow_revision)["status"] == "stopped"


def test_workload_limit_is_configurable_from_aws_environment(monkeypatch):
    from backend.config import Settings
    monkeypatch.setenv("SMARTAI_WORKLOAD_MAX_IN_FLIGHT", "5")
    assert Settings(_env_file=None).workload_max_in_flight == 5


def test_continue_reuses_saved_auxiliary_results_from_before_stop(monkeypatch):
    from types import SimpleNamespace
    from backend.api import task_preparation
    owner, task, op = _seed_operation("ai_completion")
    workflow = activate(owner, task, op)
    stop_task_run(task_id=task, owner_id=owner, job_id=op.id, expected_revision=workflow.workflow_revision)
    revision = flows.get_workflow(task, owner_id=owner).workflow_revision
    continue_auxiliary_run(task_id=task, owner_id=owner, job_id=op.id, expected_revision=revision)
    resumed = flows.get_operation(op.id, owner_id=owner)
    artifact = SimpleNamespace(kind="ai_completion_result", created_at=1,
                               original_name=f"{op.id}-attempt-{op.attempt}-ai-candidates.json")
    monkeypatch.setattr(task_preparation.file_repository, "list_files", lambda **kwargs: [artifact])
    context = SimpleNamespace(operation_id=resumed.id, owner_id=owner, assignment_id=task,
                              attempt=resumed.attempt, payload=resumed.payload)
    assert task_preparation._find_auxiliary_result_artifact(operation=context, kind="ai_completion_result", stage="ai-candidates") is artifact


def test_upstream_outage_is_not_a_rate_limit_and_retry_after_headers_are_honored():
    from types import SimpleNamespace
    error = ProviderRequestError("provider_upstream_unavailable", status_code=503)
    assert type(llm._classify_exception(error)) is llm.TransientLLMError
    error = ProviderRequestError("provider_rate_limited", status_code=429)
    error.response = SimpleNamespace(headers={"retry-after": "120"})
    assert llm._classify_exception(error).retry_after == 120


def test_cancelled_grading_is_terminal_and_releases_owner():
    owner, task, op = _seed_operation()
    run_id = seed_grade(owner, task)
    workflow = flows.update_workflow(task, owner_id=owner, active_job_id=run_id,
        active_operation="grading", grading_job_id=run_id, presentation_status="grading")
    grades.claim_lease(run_id, worker_id="grader", lease_seconds=60)
    with pytest.raises(LeaseLost):
        claim(owner, op)
    stop_task_run(task_id=task, owner_id=owner, job_id=run_id, expected_revision=workflow.workflow_revision)
    assert grades.get_run(run_id).status == "cancelled"
    assert task_state(task_id=task, owner_id=owner)["error"] == "operation_cancelled"
    claim(owner, op)


class LimitedProvider:
    model = "test-model"
    provider_id = "test-provider"
    def __init__(self, error=None):
        self.calls = 0
        self.error = error or ProviderRequestError("provider_rate_limited", status_code=429)
    async def ainvoke(self, messages):
        self.calls += 1
        raise self.error


@pytest.mark.asyncio
async def test_stale_cancelled_handler_cannot_remove_resumed_progress():
    from backend.progress.tracker import get_reporter
    ready, finish = asyncio.Event(), asyncio.Event()
    async def old_handler():
        get_or_create_reporter("same-job")
        ready.set()
        await finish.wait()
        remove_reporter("same-job")
    old = asyncio.create_task(run_controlled("same-job", lambda: False, old_handler()))
    await ready.wait()
    async def resumed_handler():
        remove_reporter("same-job")
        replacement = get_or_create_reporter("same-job")
        finish.set()
        await old
        assert get_reporter("same-job") is replacement
        remove_reporter("same-job")
    await run_controlled("same-job", lambda: False, resumed_handler())


@pytest.mark.asyncio
async def test_three_attempts_even_with_old_env_then_block_later_stages_and_reset_on_manual_retry(monkeypatch):
    monkeypatch.setattr(settings, "llm_max_retries", 9)
    monkeypatch.setattr(settings, "llm_rate_limit_max_retries", 6)
    sleep = AsyncMock()
    monkeypatch.setattr(llm._ainvoke_with_retry.retry, "sleep", sleep)
    provider = LimitedProvider()
    async def job():
        with pytest.raises(llm.RateLimitError):
            await llm.ainvoke_with_retry(provider, [])
        with pytest.raises(ProviderRequestError):
            await llm.ainvoke_with_retry(provider, [])
    await run_controlled("first", lambda: False, job())
    assert provider.calls == 3 and sleep.await_count == 2
    assert all(65 <= float(call.args[0]) <= 67 for call in sleep.await_args_list)
    await run_controlled("manual-retry", lambda: False, job())
    assert provider.calls == 6


@pytest.mark.asyncio
@pytest.mark.parametrize("detail,code", [("insufficient_quota", "provider_quota_exceeded"),
                                       ("requests per day quota exceeded", "provider_daily_quota_exceeded")])
async def test_explicit_exhaustion_never_sleeps(monkeypatch, detail, code):
    sleep = AsyncMock()
    monkeypatch.setattr(llm._ainvoke_with_retry.retry, "sleep", sleep)
    error = ProviderRequestError(detail, status_code=429)
    provider = LimitedProvider(error)
    with pytest.raises(llm.PermanentLLMError, match=code):
        await llm.ainvoke_with_retry(provider, [])
    assert provider.calls == 1 and sleep.await_count == 0


@pytest.mark.asyncio
async def test_retry_after_beyond_budget_stops_without_early_retry(monkeypatch):
    error = ProviderRequestError("provider_rate_limited", status_code=429)
    error.retry_after = 86400
    provider = LimitedProvider(error)
    sleep = AsyncMock()
    monkeypatch.setattr(llm._ainvoke_with_retry.retry, "sleep", sleep)
    with pytest.raises(llm.RateLimitError):
        await llm.ainvoke_with_retry(provider, [])
    assert provider.calls == 1 and not sleep.called


@pytest.mark.asyncio
async def test_stop_during_backoff_persists_wait_and_never_sends_second_attempt(monkeypatch):
    owner, task, op = _seed_operation()
    workflow = activate(owner, task, op)
    waiting = asyncio.Event()
    provider = LimitedProvider()
    async def sleep(_seconds):
        waiting.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(llm._ainvoke_with_retry.retry, "sleep", sleep)
    async def handler(ctx):
        get_or_create_reporter(ctx.operation_id)
        await llm.ainvoke_with_retry(provider, [])
    worker = _make_worker({"problem_extraction": handler})
    try:
        assert await worker.poll_once() == 1
        await asyncio.wait_for(waiting.wait(), 2)
        durable = flows.get_operation(op.id, owner_id=owner).progress
        assert durable["model_waits"][0]["attempt"] == 2
        assert durable["model_waits"][0]["max_attempts"] == 3
        stop_task_run(task_id=task, owner_id=owner, job_id=op.id, expected_revision=workflow.workflow_revision)
        cancel_local(op.id)
        await _drain(worker)
        assert provider.calls == 1
    finally:
        await worker.shutdown()
        remove_reporter(op.id)


@pytest.mark.asyncio
async def test_grading_worker_dispatches_two_runs_and_keeps_running_after_one_stop(monkeypatch):
    from backend.services import grading_runs
    monkeypatch.setattr(settings, "max_concurrent_jobs", 2)
    monkeypatch.setattr(settings, "workload_max_in_flight", 2)
    started, released = [], set()
    monkeypatch.setattr(grading_runs, "poll_queued_runs", lambda: [r for r in ("a", "b", "c") if r not in released])
    async def process_run(*, run_id, **kwargs):
        started.append(run_id)
        while run_id not in released:
            await asyncio.sleep(.01)
    monkeypatch.setattr(grading_runs, "process_run", process_run)
    worker = asyncio.create_task(grading_runs.worker_loop(worker_id="test", poll_seconds=.01))
    try:
        for _ in range(100):
            if len(started) == 2: break
            await asyncio.sleep(.01)
        assert started == ["a", "b"]
        released.add("a")
        for _ in range(100):
            if "c" in started: break
            await asyncio.sleep(.01)
        assert started == ["a", "b", "c"] and not worker.done()
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
