"""Workflow deadlines include all work in an attempt and preserve saved units."""
import asyncio
import time

import pytest
from sqlalchemy import update

from backend.db import grading_repository as grades, workflow_repository as flows
from backend.db.models import GradingRunRecord
from backend.db.session import session_scope
from backend.domain.errors import DomainError, LeaseLost
from backend.services import grading_adapter, grading_runs, workflow_deadlines as limits
from backend.services.execution_control import _running
from backend.tests.test_grading_run_lifecycle import setup_assignment  # noqa: F401
from backend.tests.test_task_execution_control import activate, another_task
from backend.tests.test_workflow_worker import _seed_operation, _make_worker, _drain, _expire_lease


@pytest.fixture
def running_deadlines(monkeypatch):
    """Expire the real timer after work starts, independent of machine speed."""
    scopes = []
    original = asyncio.timeout
    def capture(seconds):
        scope = original(seconds)
        if seconds is not None and seconds > 50:
            scopes.append(scope)
        return scope
    monkeypatch.setattr(asyncio, "timeout", capture)
    return scopes

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["question_preparation", "submission_recognition"])
async def test_question_deadline_cancels_children_releases_slot_and_preserves_checkpoint(monkeypatch, running_deadlines, kind):
    monkeypatch.setattr(limits, "QUESTION_WALL_SECONDS", 60)
    owner, task, op = _seed_operation(kind)
    activate(owner, task, op)
    cleaned, started = asyncio.Event(), asyncio.Event()
    async def child():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()
    async def handler(ctx):
        await ctx.checkpoint(stage="reading", checkpoint={"saved_result": "kept", "incremental_inflight_units": ["unit"]})
        await asyncio.gather(child(), asyncio.Event().wait())
    worker = _make_worker({op.operation_type: handler})
    assert await worker.poll_once() == 1
    await asyncio.wait_for(started.wait(), 5)
    running_deadlines[0].reschedule(asyncio.get_running_loop().time())
    await _drain(worker)
    current = flows.get_operation(op.id, owner_id=owner)
    assert started.is_set() and cleaned.is_set()
    assert current.status == "error" and current.error_code == ("submission_wall_time_limit_reached" if kind == "submission_recognition" else "question_preparation_wall_time_limit_reached")
    assert current.lease_owner is None
    assert current.checkpoint["saved_result"] == "kept"
    assert current.checkpoint["incremental_inflight_units"] == ["unit"]
    assert limits.CHECKPOINT_KEY in current.checkpoint
    flow = flows.get_workflow(task, owner_id=owner)
    assert flow.active_job_id is None and flow.last_failed_job_id == op.id
    assert op.id not in _running
    assert worker.in_flight_count == 0 and not worker._heartbeats
    next_task, next_op = another_task(owner, task)
    flows.claim_operation(next_op.id, owner_id=owner, worker_id="next", lease_seconds=60)
    with pytest.raises(LeaseLost):
        flows.save_operation_checkpoint(op.id, owner_id=owner, expected_attempt=op.attempt,
            expected_checkpoint_revision=current.checkpoint_revision, stage="late", checkpoint={},
            expected_lease_token="stale-token")

@pytest.mark.asyncio
async def test_queue_age_is_excluded_but_restart_cannot_reset_question_deadline(monkeypatch):
    monkeypatch.setattr(limits, "QUESTION_WALL_SECONDS", 60)
    owner, task, op = _seed_operation("question_preparation")
    activate(owner, task, op)
    with session_scope() as session:
        session.execute(update(flows.WorkflowOperationRecord).where(flows.WorkflowOperationRecord.id == op.id)
                        .values(created_at=time.time() - 100000))
    claimed = flows.claim_operation(op.id, owner_id=owner, worker_id="first", lease_seconds=60)
    clock = claimed.checkpoint[limits.CHECKPOINT_KEY]
    assert clock["deadline_at"] > time.time() + 50
    with session_scope() as session:
        record = session.get(flows.WorkflowOperationRecord, op.id)
        record.checkpoint = {limits.CHECKPOINT_KEY: {**clock, "deadline_at": time.time() - 1, "started_at": time.time() - 61}}
    _expire_lease(op.id)
    called = []
    async def handler(ctx):
        called.append(True)
    worker = _make_worker({op.operation_type: handler})
    assert await worker.poll_once() == 1
    await _drain(worker)
    current = flows.get_operation(op.id, owner_id=owner)
    assert called == []
    assert current.error_code == "question_preparation_wall_time_limit_reached"
    retried, _ = flows.create_operation(assignment_id=task, owner_id=owner,
        operation_type=op.operation_type, input_hash=op.input_hash)
    assert retried.attempt == op.attempt + 1
    renewed = flows.claim_operation(op.id, owner_id=owner, worker_id="retry", lease_seconds=60)
    assert renewed.checkpoint[limits.CHECKPOINT_KEY]["deadline_at"] > time.time() + 50

@pytest.mark.asyncio
async def test_outer_deadline_does_not_reclassify_provider_timeout():
    with pytest.raises(TimeoutError, match="provider-only"):
        async with limits.execution_deadline(time.time() + 30, "workflow_time_limit_reached"):
            raise TimeoutError("provider-only")


def test_material_import_shares_source_ocr_clock_but_explicit_retry_starts_fresh():
    owner, task, op = _seed_operation("material_import")
    activate(owner, task, op)
    source_started = time.time() - 500
    flows.update_operation(op.id, owner_id=owner, expected_attempt=op.attempt,
                           payload={"preparation_started_at": source_started})
    claimed = flows.claim_operation(op.id, owner_id=owner, worker_id="first", lease_seconds=60)
    clock = claimed.checkpoint[limits.CHECKPOINT_KEY]
    assert clock["started_at"] == source_started
    assert clock["deadline_at"] == source_started + limits.QUESTION_WALL_SECONDS
    flows.fail_timed_out_operation(op.id, owner_id=owner, expected_attempt=op.attempt,
        expected_lease_token=claimed.lease_token, code="question_preparation_wall_time_limit_reached")
    retried, _ = flows.create_operation(assignment_id=task, owner_id=owner,
        operation_type=op.operation_type, input_hash=op.input_hash,
        payload={"preparation_started_at": source_started})
    assert retried.attempt == 2
    renewed = flows.claim_operation(op.id, owner_id=owner, worker_id="retry", lease_seconds=60)
    assert renewed.checkpoint[limits.CHECKPOINT_KEY]["deadline_at"] > time.time() + 8990

@pytest.mark.asyncio
async def test_timeout_after_suppressed_cancellation_cannot_be_success():
    with pytest.raises(DomainError, match="workflow_time_limit_reached"):
        async with limits.execution_deadline(time.time() + .01, "workflow_time_limit_reached"):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass

@pytest.mark.asyncio
async def test_grading_total_limit_is_terminal_and_cleans_heartbeat(monkeypatch, setup_assignment, running_deadlines):
    monkeypatch.setattr(limits, "GRADING_WALL_SECONDS", 60)
    run = grading_runs.create_run(teacher_id=setup_assignment["teacher_id"], assignment_id=setup_assignment["assignment_id"])
    cleaned, started = asyncio.Event(), asyncio.Event()
    class Registry:
        def count(self): return 1
    async def blocked(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()
    monkeypatch.setattr(grading_adapter, "run_grading", blocked)
    mark_failed = grades.mark_failed
    def fail_after_cleanup(*args, **kwargs):
        assert cleaned.is_set(), "Do not release the owner before provider work drains"
        return mark_failed(*args, **kwargs)
    monkeypatch.setattr(grades, "mark_failed", fail_after_cleanup)
    execution = asyncio.create_task(grading_runs.process_run(run_id=run.id, worker_id="deadline-worker", registry=Registry()))
    await asyncio.wait_for(started.wait(), 5)
    running_deadlines[0].reschedule(asyncio.get_running_loop().time())
    with pytest.raises(DomainError, match="grading_wall_time_limit_reached"):
        await execution
    current = grades.get_run(run.id)
    assert cleaned.is_set() and current.status == "failed"
    assert current.error_message == "grading_wall_time_limit_reached" and current.lease_owner is None
    assert run.id not in _running
    assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task() and "_heartbeat" in repr(task.get_coro())]

def test_grading_reclaim_keeps_first_started_at(setup_assignment):
    run = grading_runs.create_run(teacher_id=setup_assignment["teacher_id"], assignment_id=setup_assignment["assignment_id"])
    first = grades.claim_lease(run.id, worker_id="first", lease_seconds=60)
    with session_scope() as session:
        session.execute(update(GradingRunRecord).where(GradingRunRecord.id == run.id).values(lease_expiry=time.time() - 1))
    second = grades.claim_lease(run.id, worker_id="second", lease_seconds=60)
    assert second.started_at == first.started_at

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["question_preparation", "submission_recognition", "grading"])
async def test_shared_active_clock_excludes_queue_counts_parallel_once_and_drains(kind):
    from backend.services.submission_work_budget import current_budget
    from backend.llm.request_timeouts import ResponseDeadlineScope
    with pytest.raises(DomainError, match="time_limit_reached"):
        async with limits.processing_budget("job", kind=kind) as budget:
            assert current_budget() is budget
            await budget.wait(asyncio.sleep(.02))
            assert budget.active == 0
            release, both_started = asyncio.Event(), asyncio.Event()
            entered = 0
            async def call():
                async def response():
                    nonlocal entered
                    entered += 1
                    if entered == 2:
                        both_started.set()
                    await release.wait()
                return await ResponseDeadlineScope(1, local_wait_seconds=1).run_response(response)
            wall_started = asyncio.get_running_loop().time()
            first, second = asyncio.create_task(call()), asyncio.create_task(call())
            await both_started.wait()
            await asyncio.sleep(.01)
            assert budget.active == 2
            budget.expire()
            with pytest.raises(DomainError):
                budget.check()
            assert not first.done() and not second.done()
            release.set()
            await asyncio.gather(first, second)
            assert budget.active == 0
    assert budget.exhausted.is_set()
    assert current_budget() is None







@pytest.mark.asyncio
async def test_total_expiry_closes_admission_and_preserves_final_active_clock():
    with pytest.raises(DomainError, match="question_preparation_wall_time_limit_reached"):
        async with limits.processing_budget("job", kind="question_preparation",
                deadline_at=time.time() + .02) as budget:
            budget.start()
            try:
                await asyncio.Event().wait()
            finally:
                budget.finish()
    assert budget.exhausted.is_set() and budget.active == 0
    with pytest.raises(DomainError, match="question_preparation_wall_time_limit_reached"):
        budget.start()


@pytest.mark.parametrize("kind", ["question_preparation", "submission_recognition"])
def test_stage_checkpoint_cannot_replace_first_claim_deadline(kind):
    owner, task, op = _seed_operation(kind)
    first = flows.claim_operation(op.id, owner_id=owner, worker_id="first", lease_seconds=60)
    original = first.checkpoint[limits.CHECKPOINT_KEY]
    saved = flows.save_operation_checkpoint(op.id, owner_id=owner, expected_attempt=first.attempt,
        expected_checkpoint_revision=first.checkpoint_revision, stage="next",
        checkpoint={limits.CHECKPOINT_KEY: {"attempt": first.attempt, "started_at": 10**12, "deadline_at": 10**12}},
        expected_lease_token=first.lease_token)
    assert saved.checkpoint[limits.CHECKPOINT_KEY] == original


@pytest.mark.asyncio
async def test_noncooperative_child_is_fenced_without_releasing_its_resources(monkeypatch, running_deadlines):
    monkeypatch.setattr(limits, "TASK_CLEANUP_SECONDS", .02)
    owner, task, op = _seed_operation("question_preparation")
    activate(owner, task, op)
    started, release, late_write = asyncio.Event(), asyncio.Event(), asyncio.Event()
    resource = asyncio.Semaphore(1)
    async def handler(ctx):
        async with resource:
            await ctx.checkpoint(stage="saved", checkpoint={"saved_result": "kept"})
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()  # Models a shielded process reaper.
            with pytest.raises(LeaseLost):
                await ctx.checkpoint(stage="late", checkpoint={"unsafe": True})
            late_write.set()
    worker = _make_worker({op.operation_type: handler})
    try:
        await worker.poll_once()
        await asyncio.wait_for(started.wait(), 5)
        running_deadlines[0].reschedule(asyncio.get_running_loop().time())
        await _drain(worker)
        assert resource.locked() and limits._quarantined_tasks
        current = flows.get_operation(op.id, owner_id=owner)
        assert current.error_code == "question_preparation_wall_time_limit_reached"
        assert current.checkpoint["saved_result"] == "kept"
        assert flows.get_workflow(task, owner_id=owner).active_job_id is None
    finally:
        release.set()
        await asyncio.wait_for(late_write.wait(), 2)
        for _ in range(10):
            await asyncio.sleep(0)
    assert not resource.locked() and not limits._quarantined_tasks


def test_total_limit_errors_keep_existing_retry_and_manual_projection():
    from backend.services.background_errors import is_retryable_background_error, safe_background_error_code
    for code in limits.LIMIT_CODES:
        assert is_retryable_background_error(code)
        assert safe_background_error_code(code, "workflow_failed") == code






@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["question_preparation", "submission_recognition", "grading"])
async def test_only_processing_budget_selects_workflow_cancellation_transport(kind):
    from backend.llm.request_timeouts import current_workflow_cancellation_scope
    assert not current_workflow_cancellation_scope()
    async with limits.processing_budget("job", kind=kind):
        assert current_workflow_cancellation_scope()
    assert not current_workflow_cancellation_scope()


@pytest.mark.asyncio
async def test_grading_final_persistence_cannot_publish_success_after_total_deadline(monkeypatch, setup_assignment):
    from types import SimpleNamespace
    run = grading_runs.create_run(teacher_id=setup_assignment["teacher_id"], assignment_id=setup_assignment["assignment_id"])
    class Registry:
        def count(self):
            return 1
    async def complete(**kwargs):
        return []
    def persisted(run_id):
        claimed = grades.get_run(run_id)
        monkeypatch.setattr(grading_runs, "time", SimpleNamespace(
            time=lambda: claimed.started_at + limits.GRADING_WALL_SECONDS + 1))
        return 0, 0
    def forbidden(**kwargs):
        pytest.fail("the completed terminal state must not be published after the deadline")
    monkeypatch.setattr(grading_adapter, "run_grading", complete)
    monkeypatch.setattr(grades, "persisted_result_counters", persisted)
    monkeypatch.setattr(grades, "mark_completed", forbidden)
    with pytest.raises(DomainError, match="grading_wall_time_limit_reached"):
        await grading_runs.process_run(run_id=run.id, worker_id="deadline-worker", registry=Registry())
    assert grades.get_run(run.id).error_message == "grading_wall_time_limit_reached"


def test_whole_task_policies_cover_auxiliary_question_entrypoints():
    for kind in limits.QUESTION_TYPES:
        assert limits.policy(kind) == (9000, "question_preparation")
    assert limits.policy("submission_recognition") == (12600, "submission")
    assert limits.policy("grading") == (12600, "grading")
    assert limits.policy("knowledge_ingestion") == (7200, "knowledge")
    with pytest.raises(ValueError):
        limits.policy("source_cleanup")
