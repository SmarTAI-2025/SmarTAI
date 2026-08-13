"""Workflow worker core (DB-W2-2A).

The ``WorkflowWorker`` polls only registered operation types, claims each row
through the repository's conditional update, runs one async handler per claim
with an immutable leased-operation context, and keeps a bounded set of
in-flight handler tasks. A per-claim heartbeat renews the lease while the
handler runs; lease loss fences the context and cancels the handler so it can
no longer mutate the row. Shutdown cancels and awaits the worker's own tasks
and never bulk-clears database leases.

These tests use synthetic handler names only. No production operation type is
registered until its durable handler exists (W2-3/W2-4).
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
import uuid

import pytest
from sqlalchemy import update

from backend.db import workflow_repository
from backend.db.models import AssignmentRecord, CourseRecord, UserRecord
from backend.db.session import session_scope
from backend.domain.errors import LeaseLost, VersionConflict
from backend.services.workflow_worker import WorkflowWorker


def _seed_operation(operation_type: str = "problem_extraction"):
    suffix = uuid.uuid4().hex[:10]
    owner_id = f"worker_owner_{suffix}"
    course_id = f"worker_course_{suffix}"
    assignment_id = f"worker_assignment_{suffix}"
    with session_scope() as session:
        session.add(UserRecord(
            id=owner_id,
            username=owner_id,
            password_hash="hash",
            role="teacher",
            is_active=True,
        ))
        session.flush()
        session.add(CourseRecord(
            id=course_id,
            name="Worker Course",
            code=f"WC-{suffix}",
            teacher_id=owner_id,
        ))
        session.flush()
        session.add(AssignmentRecord(
            id=assignment_id,
            course_id=course_id,
            teacher_id=owner_id,
            name="Worker Assignment",
            status="draft",
            version=1,
        ))
    workflow_repository.ensure_workflow(
        assignment_id=assignment_id,
        owner_id=owner_id,
    )
    operation, created = workflow_repository.create_operation(
        assignment_id=assignment_id,
        owner_id=owner_id,
        operation_type=operation_type,
        input_hash=uuid.uuid4().hex,
    )
    assert created is True
    return owner_id, assignment_id, operation


def _expire_lease(operation_id: str) -> None:
    with session_scope() as session:
        session.execute(
            update(workflow_repository.WorkflowOperationRecord)
            .where(workflow_repository.WorkflowOperationRecord.id == operation_id)
            .values(lease_expires_at=time.time() - 1)
        )


async def _noop_handler(ctx):
    return None


async def _drain(worker: WorkflowWorker) -> None:
    for _ in range(500):
        if worker.in_flight_count == 0:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("workflow worker did not drain")


def _make_worker(
    handlers,
    *,
    worker_id: str = "worker-a",
    lease_seconds: int = 60,
    heartbeat_seconds: float = 60,
    poll_seconds: float = 60,
    claim_batch_size: int = 10,
    max_in_flight: int = 4,
    shutdown_seconds: float = 1,
) -> WorkflowWorker:
    return WorkflowWorker(
        handlers=handlers,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
        heartbeat_seconds=heartbeat_seconds,
        poll_seconds=poll_seconds,
        claim_batch_size=claim_batch_size,
        max_in_flight=max_in_flight,
        shutdown_seconds=shutdown_seconds,
    )


# ─── Polling scope ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_polls_only_registered_operation_types():
    owner_id, _assignment_id, registered_op = _seed_operation("problem_extraction")
    other_owner, _other_assignment, unregistered_op = _seed_operation("material_import")
    worker = _make_worker({"problem_extraction": _noop_handler})

    assert await worker.poll_once() == 1
    await _drain(worker)

    registered = workflow_repository.get_operation(
        registered_op.id, owner_id=owner_id
    )
    assert registered.status == "running"
    assert registered.lease_owner == "worker-a"
    unregistered = workflow_repository.get_operation(
        unregistered_op.id, owner_id=other_owner
    )
    assert unregistered.status == "pending"
    assert unregistered.lease_owner is None


@pytest.mark.asyncio
async def test_unknown_operation_type_is_not_dispatched():
    _owner_id, _assignment_id, operation = _seed_operation("unregistered_type")
    calls = []

    async def handler(ctx):
        calls.append(ctx.operation_id)

    worker = _make_worker({"problem_extraction": handler})
    assert await worker.poll_once() == 0
    await _drain(worker)

    persisted = workflow_repository.get_operation(operation.id, owner_id=_owner_id)
    assert persisted.status == "pending"
    assert calls == []


@pytest.mark.asyncio
async def test_live_leased_row_is_not_listed_or_dispatched():
    owner_id, _assignment_id, operation = _seed_operation()
    workflow_repository.claim_operation(
        operation.id,
        owner_id=owner_id,
        worker_id="worker-b",
        lease_seconds=60,
    )
    calls = []

    async def handler(ctx):
        calls.append(ctx.operation_id)

    worker = _make_worker({"problem_extraction": handler}, worker_id="worker-a")
    assert await worker.poll_once() == 0
    await _drain(worker)

    persisted = workflow_repository.get_operation(operation.id, owner_id=owner_id)
    assert persisted.lease_owner == "worker-b"
    assert calls == []


@pytest.mark.asyncio
async def test_claim_race_with_another_worker_is_tolerated(monkeypatch):
    owner_id, _assignment_id, operation = _seed_operation()
    # The row is already live-leased by worker-b, but the poller still lists
    # it (e.g. a stale read between listing and claiming). The conditional
    # claim must reject it and the worker must move on without dispatching.
    workflow_repository.claim_operation(
        operation.id,
        owner_id=owner_id,
        worker_id="worker-b",
        lease_seconds=60,
    )
    calls = []

    async def handler(ctx):
        calls.append(ctx.operation_id)

    worker = _make_worker({"problem_extraction": handler}, worker_id="worker-a")
    monkeypatch.setattr(
        workflow_repository,
        "list_claimable_operations",
        lambda *args, **kwargs: [operation],
    )

    assert await worker.poll_once() == 0
    await _drain(worker)

    persisted = workflow_repository.get_operation(operation.id, owner_id=owner_id)
    assert persisted.lease_owner == "worker-b"
    assert calls == []


@pytest.mark.asyncio
async def test_claim_uses_worker_identity():
    owner_id, _assignment_id, operation = _seed_operation()
    worker = _make_worker(
        {"problem_extraction": _noop_handler}, worker_id="worker-z"
    )

    assert await worker.poll_once() == 1
    await _drain(worker)

    persisted = workflow_repository.get_operation(operation.id, owner_id=owner_id)
    assert persisted.lease_owner == "worker-z"


# ─── Handler mapping immutability and construction ───────────────────


async def _async_noop(ctx):
    return None


def test_handler_mapping_is_immutable():
    worker = _make_worker({"problem_extraction": _async_noop})

    with pytest.raises(TypeError):
        worker.handlers["problem_extraction"] = _async_noop  # type: ignore[misc]
    assert set(worker.handlers) == {"problem_extraction"}


def test_worker_rejects_invalid_construction():
    with pytest.raises(ValueError):
        WorkflowWorker(handlers={123: _async_noop}, worker_id="worker-a")
    with pytest.raises(ValueError):
        WorkflowWorker(handlers={"x": "not-callable"}, worker_id="worker-a")
    with pytest.raises(ValueError):
        WorkflowWorker(handlers={}, worker_id="")
    with pytest.raises(ValueError):
        WorkflowWorker(handlers={}, worker_id="worker-a", max_in_flight=0)
    with pytest.raises(ValueError):
        WorkflowWorker(handlers={}, worker_id="worker-a", lease_seconds=0)
    with pytest.raises(ValueError):
        WorkflowWorker(handlers={}, worker_id="worker-a", claim_batch_size=0)


# ─── Handler success / failure ───────────────────────────────────────


@pytest.mark.asyncio
async def test_handler_success_writes_terminal_state():
    owner_id, _assignment_id, operation = _seed_operation()

    async def handler(ctx):
        await ctx.complete(status="done", summary={"ok": True})

    worker = _make_worker({"problem_extraction": handler})
    assert await worker.poll_once() == 1
    await _drain(worker)

    persisted = workflow_repository.get_operation(operation.id, owner_id=owner_id)
    assert persisted.status == "done"
    assert persisted.terminal_summary == {"ok": True}
    assert persisted.error_code is None
    assert persisted.lease_owner is None
    assert persisted.lease_token is None
    assert persisted.lease_expires_at is None
    assert persisted.lease_heartbeat_at is None


@pytest.mark.asyncio
async def test_handler_checkpoint_advances_revision():
    owner_id, _assignment_id, operation = _seed_operation()
    revisions = []

    async def handler(ctx):
        revisions.append(await ctx.checkpoint(stage="parsed", checkpoint={"sources": 1}))
        revisions.append(await ctx.checkpoint(stage="parsed", checkpoint={"sources": 2}))
        await ctx.complete(status="done", summary={"sources": 2})

    worker = _make_worker({"problem_extraction": handler})
    assert await worker.poll_once() == 1
    await _drain(worker)

    assert revisions == [1, 2]
    persisted = workflow_repository.get_operation(operation.id, owner_id=owner_id)
    assert persisted.checkpoint_revision == 3
    assert persisted.checkpoint_stage == "completed"


@pytest.mark.asyncio
async def test_handler_can_heartbeat_explicitly():
    owner_id, _assignment_id, operation = _seed_operation()

    async def handler(ctx):
        before = ctx.lease_expires_at
        await ctx.heartbeat()
        after = workflow_repository.get_operation(
            operation.id, owner_id=owner_id
        ).lease_expires_at
        assert after is not None
        assert before is not None
        assert after > before

    worker = _make_worker({"problem_extraction": handler}, heartbeat_seconds=1000)
    assert await worker.poll_once() == 1
    await _drain(worker)


@pytest.mark.asyncio
async def test_background_heartbeat_renews_lease():
    owner_id, _assignment_id, operation = _seed_operation()
    captured = {}

    async def handler(ctx):
        captured["first_expiry"] = ctx.lease_expires_at
        await asyncio.sleep(0.2)
        captured["during_expiry"] = workflow_repository.get_operation(
            operation.id, owner_id=owner_id
        ).lease_expires_at

    worker = _make_worker(
        {"problem_extraction": handler},
        lease_seconds=10,
        heartbeat_seconds=0.05,
    )
    assert await worker.poll_once() == 1
    await _drain(worker)

    assert captured["during_expiry"] is not None
    assert captured["first_expiry"] is not None
    assert captured["during_expiry"] > captured["first_expiry"]


@pytest.mark.asyncio
async def test_heartbeat_errors_fence_handler_after_local_lease_deadline(monkeypatch):
    owner_id, _assignment_id, operation = _seed_operation()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def handler(ctx):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    def unavailable(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(workflow_repository, "heartbeat_operation", unavailable)
    worker = _make_worker(
        {"problem_extraction": handler},
        lease_seconds=0.1,
        heartbeat_seconds=0.03,
    )
    assert await worker.poll_once() == 1
    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    await _drain(worker)

    persisted = workflow_repository.get_operation(operation.id, owner_id=owner_id)
    assert persisted.status == "running"
    assert persisted.terminal_summary is None


@pytest.mark.asyncio
async def test_domain_failure_records_stable_code():
    owner_id, _assignment_id, operation = _seed_operation()

    async def handler(ctx):
        raise VersionConflict("stale", code="workflow_revision_conflict")

    worker = _make_worker({"problem_extraction": handler})
    assert await worker.poll_once() == 1
    await _drain(worker)

    persisted = workflow_repository.get_operation(operation.id, owner_id=owner_id)
    assert persisted.status == "error"
    assert persisted.error_code == "workflow_revision_conflict"
    assert persisted.completed_at is not None


@pytest.mark.asyncio
async def test_unexpected_failure_records_fallback_code():
    owner_id, _assignment_id, operation = _seed_operation()

    async def handler(ctx):
        raise RuntimeError("boom")

    worker = _make_worker({"problem_extraction": handler})
    assert await worker.poll_once() == 1
    await _drain(worker)

    persisted = workflow_repository.get_operation(operation.id, owner_id=owner_id)
    assert persisted.status == "error"
    assert persisted.error_code == "workflow_failed"


@pytest.mark.asyncio
async def test_failure_logging_does_not_expose_payload(caplog):
    owner_id, _assignment_id, operation = _seed_operation()
    secret = "super-secret-provider-key-12345"

    async def handler(ctx):
        raise RuntimeError(f"handler crashed with {secret}")

    worker = _make_worker({"problem_extraction": handler})
    with caplog.at_level(
        logging.WARNING, logger="backend.services.workflow_worker"
    ):
        assert await worker.poll_once() == 1
        await _drain(worker)

    assert secret not in caplog.text
    assert "workflow_failed" in caplog.text
    persisted = workflow_repository.get_operation(operation.id, owner_id=owner_id)
    assert persisted.error_code == "workflow_failed"


# ─── Lease loss ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lease_loss_cancels_handler():
    owner_id, _assignment_id, operation = _seed_operation()
    saw = {}

    async def handler(ctx):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            saw["cancelled"] = True
            raise

    worker = _make_worker(
        {"problem_extraction": handler},
        lease_seconds=60,
        heartbeat_seconds=0.05,
    )
    assert await worker.poll_once() == 1
    _expire_lease(operation.id)
    await asyncio.sleep(0.15)
    await _drain(worker)

    assert saw.get("cancelled") is True
    persisted = workflow_repository.get_operation(operation.id, owner_id=owner_id)
    assert persisted.status == "running"
    assert persisted.lease_owner == "worker-a"
    assert persisted.terminal_summary is None


@pytest.mark.asyncio
async def test_lease_loss_stops_handler_mutation():
    owner_id, _assignment_id, operation = _seed_operation()
    started = asyncio.Event()
    proceed = asyncio.Event()
    saw = {}

    async def handler(ctx):
        started.set()
        await proceed.wait()
        try:
            await ctx.checkpoint(stage="parsed", checkpoint={"n": 1})
        except LeaseLost:
            saw["lease_lost"] = True

    worker = _make_worker(
        {"problem_extraction": handler},
        lease_seconds=60,
        heartbeat_seconds=1000,
    )
    assert await worker.poll_once() == 1
    await asyncio.wait_for(started.wait(), timeout=1)
    _expire_lease(operation.id)
    proceed.set()
    await _drain(worker)

    assert saw.get("lease_lost") is True
    persisted = workflow_repository.get_operation(operation.id, owner_id=owner_id)
    assert persisted.status == "running"
    assert persisted.checkpoint_revision == 0
    assert persisted.terminal_summary is None


# ─── Duplicate dispatch and bounded in-flight ────────────────────────


@pytest.mark.asyncio
async def test_duplicate_dispatch_is_prevented():
    owner_id, _assignment_id, operation = _seed_operation()
    calls = []

    async def handler(ctx):
        calls.append(ctx.operation_id)
        await asyncio.sleep(0.2)

    worker = _make_worker({"problem_extraction": handler})
    assert await worker.poll_once() == 1
    assert await worker.poll_once() == 0
    await _drain(worker)

    assert calls == [operation.id]


@pytest.mark.asyncio
async def test_in_flight_is_bounded():
    for _ in range(3):
        _seed_operation()
    max_seen = 0
    current = 0

    async def handler(ctx):
        nonlocal max_seen, current
        current += 1
        max_seen = max(max_seen, current)
        await asyncio.sleep(0.1)
        current -= 1

    worker = _make_worker(
        {"problem_extraction": handler},
        max_in_flight=2,
        claim_batch_size=3,
    )
    assert await worker.poll_once() == 2
    assert worker.in_flight_count == 2
    await _drain(worker)
    assert await worker.poll_once() == 1
    await _drain(worker)

    assert max_seen <= 2


# ─── Shutdown / run lifecycle ────────────────────────────────────────


@pytest.mark.asyncio
async def test_shutdown_cancels_and_awaits_in_flight_handlers():
    owner_id, _assignment_id, operation = _seed_operation()
    saw = {}

    async def handler(ctx):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            saw["cancelled"] = True
            raise

    worker = _make_worker({"problem_extraction": handler})
    assert await worker.poll_once() == 1
    assert worker.in_flight_count == 1
    await asyncio.sleep(0)

    await worker.shutdown()

    assert saw.get("cancelled") is True
    assert worker.in_flight_count == 0
    # Shutdown never bulk-clears a live lease; it is left to expire.
    persisted = workflow_repository.get_operation(operation.id, owner_id=owner_id)
    assert persisted.lease_owner == "worker-a"


@pytest.mark.asyncio
async def test_shutdown_is_bounded_when_handler_swallows_cancellation():
    _owner_id, _assignment_id, _operation = _seed_operation()
    started = asyncio.Event()
    release = asyncio.Event()

    async def stubborn_handler(ctx):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()

    worker = _make_worker(
        {"problem_extraction": stubborn_handler}, shutdown_seconds=0.05
    )
    assert await worker.poll_once() == 1
    await asyncio.wait_for(started.wait(), timeout=1)

    await asyncio.wait_for(worker.shutdown(), timeout=0.5)
    assert worker.in_flight_count == 1

    release.set()
    await _drain(worker)


@pytest.mark.asyncio
async def test_shutdown_is_idempotent():
    worker = _make_worker({})
    await worker.shutdown()
    await worker.shutdown()
    assert worker.in_flight_count == 0


@pytest.mark.asyncio
async def test_run_forever_stops_on_stop():
    worker = _make_worker({}, poll_seconds=0.02)
    task = asyncio.create_task(worker.run_forever())
    await asyncio.sleep(0.05)
    worker.stop()
    await asyncio.wait_for(task, timeout=1)
    assert worker.is_stopping


@pytest.mark.asyncio
async def test_poll_returns_zero_after_stop():
    owner_id, _assignment_id, operation = _seed_operation()
    worker = _make_worker({"problem_extraction": _noop_handler})
    worker.stop()

    assert await worker.poll_once() == 0

    persisted = workflow_repository.get_operation(operation.id, owner_id=owner_id)
    assert persisted.status == "pending"


@pytest.mark.asyncio
async def test_reclaim_after_simulated_process_death():
    owner_id, _assignment_id, operation = _seed_operation()
    saw_a = {}

    async def handler_a(ctx):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            saw_a["cancelled"] = True
            raise

    worker_a = _make_worker(
        {"problem_extraction": handler_a}, worker_id="worker-a"
    )
    assert await worker_a.poll_once() == 1
    assert workflow_repository.get_operation(
        operation.id, owner_id=owner_id
    ).lease_owner == "worker-a"

    # Simulate process death: the lease expires while worker-a still holds it
    # and no release is ever written. worker-a's shutdown must not clear it.
    _expire_lease(operation.id)
    await worker_a.shutdown()
    persisted = workflow_repository.get_operation(operation.id, owner_id=owner_id)
    assert persisted.lease_owner == "worker-a"

    saw_b = {}

    async def handler_b(ctx):
        saw_b["ran"] = True
        await ctx.complete(status="done", summary={"ok": True})

    worker_b = _make_worker(
        {"problem_extraction": handler_b}, worker_id="worker-b"
    )
    assert await worker_b.poll_once() == 1
    await _drain(worker_b)

    assert saw_b.get("ran") is True
    persisted = workflow_repository.get_operation(operation.id, owner_id=owner_id)
    assert persisted.status == "done"
    assert persisted.lease_owner is None
    assert persisted.terminal_summary == {"ok": True}


def test_worker_core_does_not_import_task_facade_private_state():
    from backend.services import workflow_worker

    source = inspect.getsource(workflow_worker)
    assert "task_facade" not in source
    assert "_SAFE_ERROR_CODES" not in source
