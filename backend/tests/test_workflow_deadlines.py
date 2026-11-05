"""Whole-operation limits, independent of model response and admission clocks."""
import asyncio
import time

import pytest
from sqlalchemy import update

from backend.config import settings
from backend.db import grading_repository as grades, workflow_repository as flows
from backend.db.models import GradingRunRecord
from backend.db.session import session_scope
from backend.domain.errors import DomainError, LeaseLost
from backend.services import grading_adapter, grading_runs, workflow_deadlines as limits
from backend.services.execution_control import _running
from backend.services.task_execution import stop_task_run, continue_auxiliary_run
from backend.tests.test_grading_run_lifecycle import setup_assignment
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


def test_policy_separates_question_grading_submission_and_cleanup():
    assert limits.GRADING_SECONDS == 9000
    assert limits.GRADING_WALL_SECONDS == 12600
    assert limits.QUESTION_SECONDS == 5400
    for kind in ("problem_extraction", "question_preparation", "material_import", "ai_completion"):
        assert limits.workflow_limit(kind) == 9000
    assert limits.workflow_limit("submission_recognition") == 12600
    assert limits.SUBMISSION_SECONDS == 9000
    assert limits.workflow_limit("source_cleanup") == settings.background_pass_timeout_seconds == 3600


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["question_preparation", "problem_extraction", "material_import", "ai_completion", "submission_recognition"])
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
    owner, task, op = _seed_operation()
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


@pytest.mark.asyncio
async def test_timeout_after_suppressed_cancellation_cannot_be_success():
    with pytest.raises(DomainError, match="workflow_time_limit_reached"):
        async with limits.execution_deadline(time.time() + .01, "workflow_time_limit_reached"):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass


@pytest.mark.asyncio
async def test_cleanup_timeout_keeps_idempotent_retry_without_expired_lifetime(monkeypatch, running_deadlines):
    monkeypatch.setattr(settings, "background_pass_timeout_seconds", 60)
    owner, task, op = _seed_operation("source_cleanup")
    cancelled, started = asyncio.Event(), asyncio.Event()
    async def handler(ctx):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    worker = _make_worker({op.operation_type: handler})
    await worker.poll_once()
    await asyncio.wait_for(started.wait(), 5)
    running_deadlines[0].reschedule(asyncio.get_running_loop().time())
    await _drain(worker)
    current = flows.get_operation(op.id, owner_id=owner)
    assert cancelled.is_set() and current.status == "pending"
    assert current.error_code == "workflow_time_limit_reached"
    assert current.lease_owner is None and current.expires_at > time.time()
    assert limits.CHECKPOINT_KEY not in current.checkpoint


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


def test_auxiliary_continue_preserves_results_but_starts_a_new_attempt_clock():
    owner, task, op = _seed_operation("material_import")
    flow = activate(owner, task, op)
    first = flows.claim_operation(op.id, owner_id=owner, worker_id="first", lease_seconds=60)
    old_clock = first.checkpoint[limits.CHECKPOINT_KEY]
    flows.save_operation_checkpoint(op.id, owner_id=owner, expected_attempt=first.attempt,
        expected_checkpoint_revision=first.checkpoint_revision, stage="result", checkpoint={"saved_result": "kept"},
        expected_lease_token=first.lease_token)
    stop_task_run(task_id=task, owner_id=owner, job_id=op.id, expected_revision=flow.workflow_revision)
    revision = flows.get_workflow(task, owner_id=owner).workflow_revision
    continue_auxiliary_run(task_id=task, owner_id=owner, job_id=op.id, expected_revision=revision)
    second = flows.claim_operation(op.id, owner_id=owner, worker_id="second", lease_seconds=60)
    assert second.checkpoint["saved_result"] == "kept"
    assert second.checkpoint[limits.CHECKPOINT_KEY]["attempt"] == old_clock["attempt"] + 1
    assert second.checkpoint[limits.CHECKPOINT_KEY]["started_at"] > old_clock["started_at"]


def test_old_timeout_cannot_stop_new_attempt_or_clear_active_marker():
    owner, task, op = _seed_operation("ai_completion")
    flow = activate(owner, task, op)
    first = flows.claim_operation(op.id, owner_id=owner, worker_id="first", lease_seconds=60)
    stop_task_run(task_id=task, owner_id=owner, job_id=op.id, expected_revision=flow.workflow_revision)
    revision = flows.get_workflow(task, owner_id=owner).workflow_revision
    continue_auxiliary_run(task_id=task, owner_id=owner, job_id=op.id, expected_revision=revision)
    second = flows.claim_operation(op.id, owner_id=owner, worker_id="second", lease_seconds=60)
    assert not flows.fail_timed_out_operation(op.id, owner_id=owner, expected_attempt=first.attempt,
                                            expected_lease_token=first.lease_token)
    assert flows.get_operation(op.id, owner_id=owner).lease_token == second.lease_token
    assert flows.get_workflow(task, owner_id=owner).active_job_id == op.id


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["question_preparation", "submission_recognition", "grading"])
async def test_shared_active_clock_excludes_queue_counts_parallel_once_and_drains(kind):
    from backend.services.submission_work_budget import current_budget
    from backend.llm.request_timeouts import ResponseDeadlineScope
    saved = []
    async def persist(snapshot): saved.append(snapshot)
    with pytest.raises(DomainError, match="time_limit_reached"):
        async with limits.processing_budget("job", kind=kind, persist=persist) as budget:
            assert current_budget() is budget
            await budget.wait(asyncio.sleep(.02))
            assert budget.elapsed == 0
            release = asyncio.Event()
            async def call():
                return await ResponseDeadlineScope(1, local_wait_seconds=1).run_response(release.wait)
            wall_started = asyncio.get_running_loop().time()
            first, second = asyncio.create_task(call()), asyncio.create_task(call())
            await asyncio.sleep(.01)
            assert budget.active == 2
            budget.expire()
            with pytest.raises(DomainError):
                budget.check()
            assert not first.done() and not second.done()
            release.set()
            await asyncio.gather(first, second)
            assert .01 <= budget.elapsed <= asyncio.get_running_loop().time() - wall_started
    assert saved[-1]["blocked"] and saved[-1]["draining"]
    assert current_budget() is None


@pytest.mark.asyncio
async def test_active_clock_resumes_saved_elapsed_without_reset():
    saved = {"profile": "question_preparation-active-v2", "attempt": 1,
             "active_seconds": 5399, "draining": False, "blocked": False}
    async with limits.processing_budget("job", kind="question_preparation", saved=saved) as budget:
        assert budget.elapsed == 5399 and budget.limit == 5400
    async with limits.processing_budget("job", kind="question_preparation", attempt=2, saved=saved) as budget:
        assert budget.elapsed == 0


@pytest.mark.parametrize("code", ["question_preparation_time_limit_reached", "question_preparation_wall_time_limit_reached"])
def test_timed_out_auxiliary_continues_saved_results_with_new_attempt(code):
    owner, task, op = _seed_operation("ai_completion")
    activate(owner, task, op)
    first = flows.claim_operation(op.id, owner_id=owner, worker_id="first", lease_seconds=60)
    flows.save_operation_checkpoint(op.id, owner_id=owner, expected_attempt=first.attempt,
        expected_checkpoint_revision=first.checkpoint_revision, stage="saved", checkpoint={"result_artifact_id": "kept"},
        expected_lease_token=first.lease_token)
    assert flows.fail_timed_out_operation(op.id, owner_id=owner, expected_attempt=first.attempt,
                                         expected_lease_token=first.lease_token, code=code)
    revision = flows.get_workflow(task, owner_id=owner).workflow_revision
    continue_auxiliary_run(task_id=task, owner_id=owner, job_id=op.id, expected_revision=revision)
    next_run = flows.claim_operation(op.id, owner_id=owner, worker_id="next", lease_seconds=60)
    assert next_run.checkpoint["result_artifact_id"] == "kept"
    assert next_run.payload["resume_saved_results"] is True
    assert next_run.attempt == first.attempt + 1


def test_auxiliary_unknown_request_requires_explicit_acknowledgment():
    owner, task, op = _seed_operation("ai_completion")
    activate(owner, task, op)
    first = flows.claim_operation(op.id, owner_id=owner, worker_id="first", lease_seconds=60)
    flows.save_operation_checkpoint(op.id, owner_id=owner, expected_attempt=first.attempt,
        expected_checkpoint_revision=first.checkpoint_revision, stage="sent", checkpoint={"auxiliary_provider_inflight": True},
        expected_lease_token=first.lease_token)
    flows.fail_timed_out_operation(op.id, owner_id=owner, expected_attempt=first.attempt,
        expected_lease_token=first.lease_token, code="question_preparation_wall_time_limit_reached")
    revision = flows.get_workflow(task, owner_id=owner).workflow_revision
    with pytest.raises(DomainError, match="previous provider request"):
        continue_auxiliary_run(task_id=task, owner_id=owner, job_id=op.id, expected_revision=revision)
    continue_auxiliary_run(task_id=task, owner_id=owner, job_id=op.id, expected_revision=revision,
                          acknowledge_possible_duplicate_call=True)
    resumed = flows.get_operation(op.id, owner_id=owner)
    assert resumed.checkpoint["auxiliary_provider_inflight"] is False
    assert resumed.checkpoint["acknowledged_inflight_attempt"] == first.attempt


@pytest.mark.asyncio
async def test_wall_deadline_remains_reason_when_cancel_cleanup_fails():
    with pytest.raises(DomainError, match="grading_wall_time_limit_reached"):
        async with limits.execution_deadline(time.time() + .01, "grading_wall_time_limit_reached"):
            try:
                await asyncio.Event().wait()
            finally:
                raise RuntimeError("cleanup quarantined its slot")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["question_preparation", "submission_recognition", "material_import", "ai_completion"])
async def test_active_expiry_preserves_drained_result_and_rejects_new_call(kind):
    from backend.services.submission_work_budget import current_budget
    from backend.llm.request_timeouts import ResponseDeadlineScope
    owner, task, op = _seed_operation(kind)
    activate(owner, task, op)
    dispatched = []
    async def handler(ctx):
        async def invoke():
            dispatched.append(1)
            current_budget().expire()
            await asyncio.sleep(.01)
            return "verified result"
        answer = await ResponseDeadlineScope(1, local_wait_seconds=1).run_response(invoke)
        await ctx.checkpoint(stage="saved", checkpoint={"saved_result": answer})
        await ResponseDeadlineScope(1, local_wait_seconds=1).run_response(invoke)
    worker = _make_worker({kind: handler})
    await worker.poll_once()
    await _drain(worker)
    row = flows.get_operation(op.id, owner_id=owner)
    assert dispatched == [1]
    assert row.status == "error" and row.error_code == ("submission_time_limit_reached" if kind == "submission_recognition" else "question_preparation_time_limit_reached")
    assert row.checkpoint["saved_result"] == "verified result"
    assert row.checkpoint[limits.WORK_CHECKPOINT_KEY]["draining"]
    assert row.lease_owner is None


@pytest.mark.asyncio
async def test_wall_cutoff_quarantines_noncooperative_child_and_fences_late_work(monkeypatch, running_deadlines):
    from backend.services.submission_work_budget import current_budget
    monkeypatch.setattr(limits, "TASK_CLEANUP_SECONDS", .01)
    owner, task, op = _seed_operation("question_preparation")
    activate(owner, task, op)
    started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    rejected = []
    async def handler(ctx):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()  # Simulate an SDK ignoring cancellation.
        try:
            current_budget().start()
        except DomainError:
            rejected.append("no new model request")
        try:
            await ctx.checkpoint(stage="late", checkpoint={"must_not_save": True})
        except (LeaseLost, DomainError):
            rejected.append("no late writes")
        finished.set()
    worker = _make_worker({op.operation_type: handler})
    await worker.poll_once()
    await started.wait()
    running_deadlines[0].reschedule(asyncio.get_running_loop().time())
    await _drain(worker)
    row = flows.get_operation(op.id, owner_id=owner)
    assert row.error_code == "question_preparation_wall_time_limit_reached"
    assert row.lease_owner is None and limits._quarantined_tasks
    quarantined = list(limits._quarantined_tasks)
    release.set()
    await finished.wait()
    await asyncio.gather(*quarantined, return_exceptions=True)
    await asyncio.sleep(0)
    assert rejected == ["no new model request", "no late writes"]
    assert not limits._quarantined_tasks
    assert "must_not_save" not in flows.get_operation(op.id, owner_id=owner).checkpoint
