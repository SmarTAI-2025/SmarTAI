"""Short renewable OCR leases preserve both long work and uncertain-call fences."""
import asyncio
import threading
import time

import pytest
from sqlalchemy import update

from backend.db import workflow_repository
from backend.db.session import session_scope
from backend.domain.errors import LeaseLost, RecognitionError
from backend.services import recognition_runs
from backend.tests.test_recognition_durable_runs import run, runner


def extended(tmp_path, *, total=6060):
    service, request, data, engine = runner(tmp_path, initial=["literal 1 + 1 = 3"])
    request = request.model_copy(update={"policy": request.policy.model_copy(update={
        "per_call_seconds": 300, "read_seconds": 6000, "total_seconds": total,
    })})
    return service, request, data, engine


def expire(operation_id):
    with session_scope() as session:
        session.execute(update(workflow_repository.WorkflowOperationRecord).where(
            workflow_repository.WorkflowOperationRecord.id == operation_id,
        ).values(lease_expires_at=time.time() - 1))


@pytest.fixture
def heartbeats(monkeypatch):
    observed = []
    original = recognition_runs._RecognitionLeaseHeartbeat.start

    def start(self):
        observed.append(self)
        original(self)

    monkeypatch.setattr(recognition_runs._RecognitionLeaseHeartbeat, "start", start)
    yield observed
    assert all(item.task.done() for item in observed), "An OCR heartbeat outlived its run"


@pytest.fixture
def claims(monkeypatch):
    observed = []
    original = workflow_repository.claim_operation

    def claim(*args, **kwargs):
        row = original(*args, **kwargs)
        observed.append((row, kwargs["lease_seconds"]))
        return row

    monkeypatch.setattr(workflow_repository, "claim_operation", claim)
    return observed


@pytest.mark.asyncio
async def test_long_work_renews_beyond_original_lease_without_duplicate_dispatch(
    tmp_path, monkeypatch, heartbeats, claims,
):
    monkeypatch.setattr(recognition_runs, "_RENEWABLE_LEASE_SECONDS", 1.5)
    monkeypatch.setattr(recognition_runs, "_RENEWABLE_HEARTBEAT_SECONDS", 0.2)
    service, request, data, engine = extended(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    ticks = asyncio.Queue()
    loop = asyncio.get_running_loop()
    original_read = engine.recognize
    original_heartbeat = workflow_repository.heartbeat_operation
    calls = 0

    async def slow_read(unit):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return await original_read(unit)

    def heartbeat(*args, **kwargs):
        result = original_heartbeat(*args, **kwargs)
        loop.call_soon_threadsafe(ticks.put_nowait, time.time())
        return result

    engine.recognize = slow_read
    monkeypatch.setattr(workflow_repository, "heartbeat_operation", heartbeat)
    task = asyncio.create_task(run(service, request, data, engine))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        first, seconds = claims[0]
        assert seconds == 1.5
        # Let two original lease windows pass while one request remains active.
        while await asyncio.wait_for(ticks.get(), 5) < first.lease_expires_at + 1.5:
            pass
        current = workflow_repository.get_operation(first.id, owner_id=request.source.owner_id)
        assert current.lease_expires_at > first.lease_expires_at + 1.5
        assert current.checkpoint_revision >= first.checkpoint_revision
        concurrent = await run(service, request, data, engine)
        assert concurrent.status == "already_running" and calls == 1
        release.set()
        result = await asyncio.wait_for(task, 10)
        assert result.status == "completed", result.safe_error_code
        assert result.operation_usage.initial_calls == calls == 1
        assert (await run(service, request, None, engine)).status == "already_done"
        assert calls == 1 and heartbeats[0].failure is None
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("total", [6060, 10020])
async def test_hard_crash_lease_is_300_seconds_and_pending_never_replays(
    tmp_path, monkeypatch, heartbeats, claims, total,
):
    service, request, data, engine = extended(tmp_path, total=total)
    entered = asyncio.Event()
    calls = 0

    async def blocked(_):
        nonlocal calls
        calls += 1
        entered.set()
        await asyncio.Event().wait()

    engine.recognize = blocked
    task = asyncio.create_task(run(service, request, data, engine))
    await asyncio.wait_for(entered.wait(), 10)
    claimed, seconds = claims[0]
    assert seconds == 300
    assert 299 <= claimed.lease_expires_at - claimed.lease_heartbeat_at <= 300

    # Reproduce the persisted state of a killed process: no final release ran.
    original_release = workflow_repository.release_operation
    monkeypatch.setattr(workflow_repository, "release_operation", lambda *a, **k: None)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    monkeypatch.setattr(workflow_repository, "release_operation", original_release)
    assert (await run(service, request, data, engine)).status == "already_running"
    assert calls == 1

    expire(claimed.id)
    resumed = await run(service, request, data, engine)
    assert resumed.status == "needs_review"
    assert resumed.safe_error_code == "provider_submit_uncertain"
    assert calls == 1
    persisted = workflow_repository.get_operation(claimed.id, owner_id=request.source.owner_id)
    assert persisted.checkpoint["calls"][0]["state"] == "pending"
    assert persisted.lease_token is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["fenced", "foreign_terminal", "database_error", "database_hung", "release_hung"])
async def test_heartbeat_failure_stops_provider_and_preserves_pending(
    tmp_path, monkeypatch, heartbeats, claims, failure,
):
    monkeypatch.setattr(recognition_runs, "_RENEWABLE_HEARTBEAT_SECONDS", 0.2)
    service, request, data, engine = extended(tmp_path)
    entered = threading.Event()
    delayed_release, delayed_finished = threading.Event(), threading.Event()
    cancelled = asyncio.Event()
    original_heartbeat = workflow_repository.heartbeat_operation
    original_release = workflow_repository.release_operation
    late_errors = []
    calls = 0

    async def blocked(_):
        nonlocal calls
        calls += 1
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    def heartbeat(operation_id, **kwargs):
        if not entered.is_set():
            return original_heartbeat(operation_id, **kwargs)
        if failure in {"fenced", "foreign_terminal"}:
            original_release(operation_id, owner_id=kwargs["owner_id"],
                             worker_id=kwargs["worker_id"], lease_token=kwargs["lease_token"])
            replacement = workflow_repository.claim_operation(operation_id, owner_id=kwargs["owner_id"],
                                                               worker_id="replacement-worker", lease_seconds=300)
            if failure == "foreign_terminal":
                workflow_repository.save_operation_checkpoint(
                    operation_id, owner_id=kwargs["owner_id"], expected_attempt=replacement.attempt,
                    expected_checkpoint_revision=replacement.checkpoint_revision,
                    expected_lease_token=replacement.lease_token, checkpoint=replacement.checkpoint,
                    stage="recognition_needs_review",
                    terminal_status="needs_review", terminal_summary={"status": "needs_review",
                        "error_code": "provider_submit_uncertain", "final_artifact_id": None},
                )
            return original_heartbeat(operation_id, **kwargs)
        if failure in {"database_error", "release_hung"}:
            raise RuntimeError("simulated database outage")
        assert delayed_release.wait(10)
        try:
            return original_heartbeat(operation_id, **kwargs)
        except LeaseLost as exc:
            late_errors.append(exc)
            raise
        finally:
            delayed_finished.set()

    def failing_release(*args, **kwargs):
        if failure == "release_hung":
            assert delayed_release.wait(10)
            try:
                return original_release(*args, **kwargs)
            finally:
                delayed_finished.set()
        raise RuntimeError("cleanup database outage must not hide lease loss")

    engine.recognize = blocked
    monkeypatch.setattr(workflow_repository, "heartbeat_operation", heartbeat)
    if failure in {"database_error", "release_hung"}:
        monkeypatch.setattr(workflow_repository, "release_operation", failing_release)
    task = asyncio.create_task(run(service, request, data, engine))
    try:
        with pytest.raises(LeaseLost):
            await asyncio.wait_for(task, 10)
        assert cancelled.is_set() and calls == 1
        assert task.cancelling() == 0  # Internal lease loss was not a teacher stop.
        assert heartbeats[0].failure is not None
        persisted = workflow_repository.get_operation(claims[0][0].id, owner_id=request.source.owner_id)
        assert persisted.status == ("needs_review" if failure == "foreign_terminal" else "running")
        assert persisted.checkpoint["calls"][0]["state"] == "pending"
        if failure == "fenced":
            assert persisted.lease_owner == "replacement-worker"
            assert persisted.lease_token != claims[0][0].lease_token
        if failure == "database_hung":
            assert persisted.lease_token is None
            delayed_release.set()
            assert await asyncio.to_thread(delayed_finished.wait, 5)
            assert len(late_errors) == 1
            current = workflow_repository.get_operation(persisted.id, owner_id=request.source.owner_id)
            assert current.lease_token is None  # A late DB thread cannot revive the old lease.
        if failure == "release_hung":
            delayed_release.set()
            assert await asyncio.to_thread(delayed_finished.wait, 5)
            persisted = workflow_repository.get_operation(persisted.id, owner_id=request.source.owner_id)
            assert persisted.lease_token is None
        monkeypatch.setattr(workflow_repository, "heartbeat_operation", original_heartbeat)
        monkeypatch.setattr(workflow_repository, "release_operation", original_release)
        if persisted.lease_token is not None:
            expire(persisted.id)
        resumed = await run(service, request, data, engine)
        assert resumed.status == "needs_review" and resumed.safe_error_code == "provider_submit_uncertain"
        assert calls == 1
    finally:
        delayed_release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_heartbeat_failure_during_error_checkpoint_is_not_user_cancellation(
    tmp_path, monkeypatch, heartbeats, claims,
):
    monkeypatch.setattr(recognition_runs, "_RENEWABLE_HEARTBEAT_SECONDS", 0.2)
    service, request, data, engine = extended(tmp_path)
    saving, release_save, late_finished = threading.Event(), threading.Event(), threading.Event()
    original_save = workflow_repository.save_operation_checkpoint
    original_heartbeat = workflow_repository.heartbeat_operation
    late_errors = []

    async def local_failure(*args, **kwargs):
        raise RecognitionError("recognition_artifact_unavailable")

    def save(operation_id, **kwargs):
        if kwargs.get("terminal_status") == "needs_review":
            saving.set()
            assert release_save.wait(10)
            try:
                return original_save(operation_id, **kwargs)
            except LeaseLost as exc:
                late_errors.append(exc)
                raise
            finally:
                late_finished.set()
        return original_save(operation_id, **kwargs)

    def heartbeat(*args, **kwargs):
        if saving.is_set():
            raise RuntimeError("database unavailable during error checkpoint")
        return original_heartbeat(*args, **kwargs)

    monkeypatch.setattr(recognition_runs.RecognitionAgent, "recognize", local_failure)
    monkeypatch.setattr(workflow_repository, "save_operation_checkpoint", save)
    monkeypatch.setattr(workflow_repository, "heartbeat_operation", heartbeat)
    task = asyncio.create_task(run(service, request, data, engine))
    try:
        with pytest.raises(LeaseLost):
            await asyncio.wait_for(task, 10)
        assert task.cancelling() == 0 and saving.is_set()
        assert heartbeats[0].failure is not None
        row = workflow_repository.get_operation(claims[0][0].id, owner_id=request.source.owner_id)
        assert row.terminal_summary is None and row.lease_token is None
        release_save.set()
        assert await asyncio.to_thread(late_finished.wait, 5)
        assert len(late_errors) == 1
    finally:
        release_save.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["completed", "needs_review", "cancelled"])
async def test_heartbeat_cleans_up_on_every_exit(tmp_path, monkeypatch, heartbeats, claims, outcome):
    monkeypatch.setattr(recognition_runs, "_RENEWABLE_HEARTBEAT_SECONDS", 0.2)
    service, request, data, engine = extended(tmp_path)
    entered = asyncio.Event()
    original_read = engine.recognize

    async def read(unit):
        entered.set()
        if outcome == "cancelled":
            await asyncio.Event().wait()
        if outcome == "needs_review":
            raise RecognitionError("provider_auth_failed")
        return await original_read(unit)

    engine.recognize = read
    task = asyncio.create_task(run(service, request, data, engine))
    await asyncio.wait_for(entered.wait(), 10)
    if outcome == "cancelled":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert (await asyncio.wait_for(task, 10)).status == outcome
    assert heartbeats and all(item.task.done() and item.failure is None for item in heartbeats)
    row = workflow_repository.get_operation(claims[0][0].id, owner_id=request.source.owner_id)
    assert row.lease_token is None


@pytest.mark.asyncio
@pytest.mark.parametrize("retry", [False, True])
async def test_terminal_commit_heartbeat_race_keeps_success_and_explicit_retry(
    tmp_path, monkeypatch, heartbeats, retry,
):
    monkeypatch.setattr(recognition_runs, "_RENEWABLE_HEARTBEAT_SECONDS", 0.3)
    service, request, data, engine = extended(tmp_path)
    if retry:
        entered = asyncio.Event()
        original_read = engine.recognize

        async def blocked(_):
            entered.set()
            await asyncio.Event().wait()

        engine.recognize = blocked
        first = asyncio.create_task(run(service, request, data, engine))
        await asyncio.wait_for(entered.wait(), 10)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        engine.recognize = original_read

    committed, heartbeat_observed = threading.Event(), threading.Event()
    original_save = workflow_repository.save_operation_checkpoint
    original_heartbeat = workflow_repository.heartbeat_operation
    terminal_status = "needs_review" if retry else "completed"
    blocked_operation = []

    def save(operation_id, **kwargs):
        result = original_save(operation_id, **kwargs)
        if kwargs.get("terminal_status") == terminal_status and not blocked_operation:
            blocked_operation.append(operation_id)
            committed.set()
            assert heartbeat_observed.wait(5)
        return result

    def heartbeat(operation_id, **kwargs):
        try:
            return original_heartbeat(operation_id, **kwargs)
        finally:
            if committed.is_set() and operation_id in blocked_operation:
                heartbeat_observed.set()

    monkeypatch.setattr(workflow_repository, "save_operation_checkpoint", save)
    monkeypatch.setattr(workflow_repository, "heartbeat_operation", heartbeat)
    try:
        result = await asyncio.wait_for(service.run(
            request, data, engine=engine, prompt_version="faithful-reader-v1",
            authorized_owner_id=request.source.owner_id,
            retry_key="acknowledged-parent:1" if retry else None,
            acknowledge_pending_retry=retry,
        ), 10)
        assert committed.is_set() and heartbeat_observed.is_set()
        assert result.status == "completed", result.safe_error_code
        assert all(item.failure is None for item in heartbeats)
        assert (await run(service, request, None, engine)).status == ("needs_review" if retry else "already_done")
    finally:
        heartbeat_observed.set()


@pytest.mark.asyncio
async def test_external_cancel_during_heartbeat_shutdown_is_not_swallowed(tmp_path, monkeypatch, heartbeats):
    service, request, data, engine = extended(tmp_path)
    stopping, release = asyncio.Event(), asyncio.Event()
    original = recognition_runs._RecognitionLeaseHeartbeat._run

    async def slow_shutdown(self):
        try:
            await original(self)
        except asyncio.CancelledError:
            stopping.set()
            await release.wait()
            raise

    monkeypatch.setattr(recognition_runs._RecognitionLeaseHeartbeat, "_run", slow_shutdown)
    task = asyncio.create_task(run(service, request, data, engine))
    try:
        await asyncio.wait_for(stopping.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelling() == 1
        # The user's stop is preserved; a committed result is also preserved.
        assert (await run(service, request, None, engine)).status == "already_done"
        assert len(engine.reads) == 1
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("total,engine_kind,purpose,source_scope", [
    (600, "vision", "problems", "assignment_source"),
    (900, "vision", "problems", "assignment_source"),
    (6060, "none", "problems", "assignment_source"),
    (6060, "ocr", "problems", "assignment_source"),
    (6060, "vision", "knowledge", "assignment_source"),
    (6060, "vision", "problems", "knowledge_document"),
])
async def test_legacy_native_ocr_and_knowledge_do_not_use_new_lease_protocol(
    tmp_path, monkeypatch, heartbeats, claims, total, engine_kind, purpose, source_scope,
):
    service, request, data, engine = extended(tmp_path, total=total)
    request = request.model_copy(update={
        "purpose": purpose, "source": request.source.model_copy(update={"scope": source_scope}),
    })
    if total <= 900:
        request = request.model_copy(update={"policy": request.policy.model_copy(update={
            "per_call_seconds": 120, "read_seconds": 600,
        })})
    if engine_kind == "none":
        engine = None
    elif engine_kind == "ocr":
        engine.capabilities = engine.capabilities.model_copy(update={"candidate_kind": "ocr"})

    async def active(**kwargs):
        return True

    async def no_model(*args, **kwargs):
        raise RecognitionError("recognition_budget_exhausted")

    monkeypatch.setattr(service.store, "active_source", active)
    monkeypatch.setattr(recognition_runs.RecognitionAgent, "recognize", no_model)
    result = await run(service, request, data, engine)
    assert result.status == "needs_review"
    assert claims[0][1] == total + 60
    assert heartbeats == []
