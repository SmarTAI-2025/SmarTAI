"""Operation lease schema and repository contract (DB-W2-1).

The lease is the worker-fencing generation for a workflow operation attempt:
``lease_owner`` identifies the worker, ``lease_token`` is a random token that
rotates on every claim/reclaim, and ``lease_expires_at`` bounds how long that
token is authoritative. A previous worker cannot heartbeat, checkpoint, commit
a terminal state, or release after the token rotated or the lease expired.

These tests pin the repository contract that Task 2 (the worker) and Tasks
3-5 (the durable handlers) build on. The lease primitives are deliberately
conditional single-row UPDATEs, not process locks or long transactions.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import time
import uuid

import pytest
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from backend.db import workflow_repository
from backend.db.models import AssignmentRecord, CourseRecord, UserRecord
from backend.db.session import session_scope
from backend.domain.errors import InvalidTransition, LeaseLost, NotFound, ValidationError


def _seed_operation(operation_type: str = "submission_recognition"):
    suffix = uuid.uuid4().hex[:10]
    owner_id = f"lease_owner_{suffix}"
    course_id = f"lease_course_{suffix}"
    assignment_id = f"lease_assignment_{suffix}"
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
            name="Lease Course",
            code=f"LC-{suffix}",
            teacher_id=owner_id,
        ))
        session.flush()
        session.add(AssignmentRecord(
            id=assignment_id,
            course_id=course_id,
            teacher_id=owner_id,
            name="Lease Assignment",
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


def test_pending_operation_is_claimable():
    owner_id, _assignment_id, operation = _seed_operation()

    claimed = workflow_repository.claim_operation(
        operation.id,
        owner_id=owner_id,
        worker_id="worker-a",
        lease_seconds=60,
    )

    assert claimed.status == "running"
    assert claimed.lease_owner == "worker-a"
    assert claimed.lease_token is not None
    assert claimed.lease_expires_at is not None
    assert claimed.lease_expires_at > time.time()
    assert claimed.lease_heartbeat_at is not None


def test_unleased_running_operation_is_claimable():
    owner_id, _assignment_id, operation = _seed_operation()
    workflow_repository.update_operation(
        operation.id,
        owner_id=owner_id,
        expected_attempt=operation.attempt,
        status="running",
    )

    claimed = workflow_repository.claim_operation(
        operation.id,
        owner_id=owner_id,
        worker_id="worker-a",
        lease_seconds=60,
    )

    assert claimed.status == "running"
    assert claimed.lease_owner == "worker-a"


def test_concurrent_claim_selects_one_winner():
    owner_id, _assignment_id, operation = _seed_operation()

    def claim(worker: str):
        try:
            claimed = workflow_repository.claim_operation(
                operation.id,
                owner_id=owner_id,
                worker_id=worker,
                lease_seconds=60,
            )
            return "claimed", claimed.lease_owner
        except LeaseLost as exc:
            return "lost", exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, ("worker-a", "worker-b")))

    assert [kind for kind, _value in results].count("claimed") == 1
    assert results.count(("lost", "operation_not_claimable")) == 1
    persisted = workflow_repository.get_operation(operation.id, owner_id=owner_id)
    assert persisted.status == "running"
    assert persisted.lease_owner in {"worker-a", "worker-b"}
    assert persisted.lease_token is not None
    assert persisted.lease_expires_at is not None
    assert persisted.lease_heartbeat_at is not None


def test_expired_reclaim_rotates_token_and_fences_previous_worker():
    owner_id, _assignment_id, operation = _seed_operation()
    first = workflow_repository.claim_operation(
        operation.id,
        owner_id=owner_id,
        worker_id="worker-a",
        lease_seconds=60,
    )
    _expire_lease(operation.id)

    second = workflow_repository.claim_operation(
        operation.id,
        owner_id=owner_id,
        worker_id="worker-b",
        lease_seconds=60,
    )

    assert second.lease_owner == "worker-b"
    assert second.lease_token is not None
    assert second.lease_token != first.lease_token
    with pytest.raises(LeaseLost):
        workflow_repository.heartbeat_operation(
            operation.id,
            owner_id=owner_id,
            worker_id="worker-a",
            lease_token=first.lease_token,
            lease_seconds=60,
        )


def test_live_lease_rejects_same_worker_reclaim():
    """A live lease fences a second claim even from the same worker_id.

    Two concurrent coroutines in one process share one worker_id, so a claim
    predicate that treats ``lease_owner == worker_id`` as claimable lets both
    receive a successful claim. Only an unleased or expired row may be
    claimed/reclaimed.
    """
    owner_id, _assignment_id, operation = _seed_operation()
    first = workflow_repository.claim_operation(
        operation.id,
        owner_id=owner_id,
        worker_id="worker-a",
        lease_seconds=60,
    )

    with pytest.raises(LeaseLost) as second_claim:
        workflow_repository.claim_operation(
            operation.id,
            owner_id=owner_id,
            worker_id="worker-a",
            lease_seconds=60,
        )
    assert second_claim.value.code == "operation_not_claimable"

    persisted = workflow_repository.get_operation(operation.id, owner_id=owner_id)
    assert persisted.lease_owner == "worker-a"
    assert persisted.lease_token == first.lease_token
    assert persisted.lease_expires_at == first.lease_expires_at


def test_concurrent_same_worker_claim_selects_one_winner():
    """Two concurrent coroutines sharing a worker_id cannot both win a claim."""
    owner_id, _assignment_id, operation = _seed_operation()

    def claim(worker: str):
        try:
            claimed = workflow_repository.claim_operation(
                operation.id,
                owner_id=owner_id,
                worker_id=worker,
                lease_seconds=60,
            )
            return "claimed", claimed.lease_owner, claimed.lease_token
        except LeaseLost as exc:
            return "lost", exc.code, None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, ("worker-a", "worker-a")))

    assert [kind for kind, _owner, _token in results].count("claimed") == 1
    assert results.count(("lost", "operation_not_claimable", None)) == 1
    persisted = workflow_repository.get_operation(operation.id, owner_id=owner_id)
    assert persisted.lease_owner == "worker-a"
    assert persisted.lease_token is not None
    assert persisted.lease_expires_at is not None
    assert persisted.lease_heartbeat_at is not None


def test_heartbeat_extends_live_lease_and_fences_stale_workers():
    owner_id, _assignment_id, operation = _seed_operation()
    claimed = workflow_repository.claim_operation(
        operation.id,
        owner_id=owner_id,
        worker_id="worker-a",
        lease_seconds=60,
    )
    original_expiry = claimed.lease_expires_at

    assert workflow_repository.heartbeat_operation(
        operation.id,
        owner_id=owner_id,
        worker_id="worker-a",
        lease_token=claimed.lease_token,
        lease_seconds=60,
    ) is True
    persisted = workflow_repository.get_operation(operation.id, owner_id=owner_id)
    assert persisted.lease_expires_at > original_expiry

    with pytest.raises(LeaseLost):
        workflow_repository.heartbeat_operation(
            operation.id,
            owner_id=owner_id,
            worker_id="worker-a",
            lease_token="wrong-token",
            lease_seconds=60,
        )
    with pytest.raises(LeaseLost):
        workflow_repository.heartbeat_operation(
            operation.id,
            owner_id=owner_id,
            worker_id="worker-b",
            lease_token=claimed.lease_token,
            lease_seconds=60,
        )
    _expire_lease(operation.id)
    with pytest.raises(LeaseLost):
        workflow_repository.heartbeat_operation(
            operation.id,
            owner_id=owner_id,
            worker_id="worker-a",
            lease_token=claimed.lease_token,
            lease_seconds=60,
        )


def test_release_clears_lease_and_allows_reclaim():
    owner_id, _assignment_id, operation = _seed_operation()
    claimed = workflow_repository.claim_operation(
        operation.id,
        owner_id=owner_id,
        worker_id="worker-a",
        lease_seconds=60,
    )

    released = workflow_repository.release_operation(
        operation.id,
        owner_id=owner_id,
        worker_id="worker-a",
        lease_token=claimed.lease_token,
    )

    assert released.lease_owner is None
    assert released.lease_token is None
    assert released.lease_expires_at is None
    assert released.lease_heartbeat_at is None
    reclaimed = workflow_repository.claim_operation(
        operation.id,
        owner_id=owner_id,
        worker_id="worker-b",
        lease_seconds=60,
    )
    assert reclaimed.lease_owner == "worker-b"


def test_release_with_rotated_token_is_fenced():
    owner_id, _assignment_id, operation = _seed_operation()
    first = workflow_repository.claim_operation(
        operation.id,
        owner_id=owner_id,
        worker_id="worker-a",
        lease_seconds=60,
    )
    _expire_lease(operation.id)
    workflow_repository.claim_operation(
        operation.id,
        owner_id=owner_id,
        worker_id="worker-b",
        lease_seconds=60,
    )

    with pytest.raises(LeaseLost):
        workflow_repository.release_operation(
            operation.id,
            owner_id=owner_id,
            worker_id="worker-a",
            lease_token=first.lease_token,
        )
    persisted = workflow_repository.get_operation(operation.id, owner_id=owner_id)
    assert persisted.lease_owner == "worker-b"


def test_lease_operations_are_owner_scoped():
    owner_id, _assignment_id, operation = _seed_operation()
    other_owner, _other_assignment, _other_operation = _seed_operation()
    claimed = workflow_repository.claim_operation(
        operation.id,
        owner_id=owner_id,
        worker_id="worker-a",
        lease_seconds=60,
    )

    with pytest.raises(NotFound) as wrong_owner_claim:
        workflow_repository.claim_operation(
            operation.id,
            owner_id=other_owner,
            worker_id="worker-b",
            lease_seconds=60,
        )
    with pytest.raises(NotFound) as missing_claim:
        workflow_repository.claim_operation(
            "missing-operation",
            owner_id=other_owner,
            worker_id="worker-b",
            lease_seconds=60,
        )
    assert wrong_owner_claim.value.code == missing_claim.value.code == "not_found"

    with pytest.raises(NotFound):
        workflow_repository.heartbeat_operation(
            operation.id,
            owner_id=other_owner,
            worker_id="worker-a",
            lease_token=claimed.lease_token,
            lease_seconds=60,
        )
    with pytest.raises(NotFound):
        workflow_repository.release_operation(
            operation.id,
            owner_id=other_owner,
            worker_id="worker-a",
            lease_token=claimed.lease_token,
        )


def test_worker_checkpoint_requires_live_matching_lease_token():
    owner_id, _assignment_id, operation = _seed_operation()
    claimed = workflow_repository.claim_operation(
        operation.id,
        owner_id=owner_id,
        worker_id="worker-a",
        lease_seconds=60,
    )

    saved = workflow_repository.save_operation_checkpoint(
        operation.id,
        owner_id=owner_id,
        expected_attempt=operation.attempt,
        expected_checkpoint_revision=0,
        stage="parsed",
        checkpoint={"completed_sources": 1},
        expected_lease_token=claimed.lease_token,
    )
    assert saved.checkpoint_revision == 1

    with pytest.raises(LeaseLost) as wrong_token:
        workflow_repository.save_operation_checkpoint(
            operation.id,
            owner_id=owner_id,
            expected_attempt=operation.attempt,
            expected_checkpoint_revision=saved.checkpoint_revision,
            stage="parsed",
            checkpoint={"completed_sources": 2},
            expected_lease_token="not-the-token",
        )
    assert wrong_token.value.code == "lease_lost"


def test_terminal_checkpoint_clears_only_matching_lease():
    owner_id, _assignment_id, operation = _seed_operation()
    claimed = workflow_repository.claim_operation(
        operation.id,
        owner_id=owner_id,
        worker_id="worker-a",
        lease_seconds=60,
    )

    saved = workflow_repository.save_operation_checkpoint(
        operation.id,
        owner_id=owner_id,
        expected_attempt=operation.attempt,
        expected_checkpoint_revision=0,
        stage="completed",
        checkpoint={"processed_sources": 1},
        terminal_summary={"outcome": "done"},
        terminal_status="done",
        expected_lease_token=claimed.lease_token,
    )

    assert saved.status == "done"
    assert saved.lease_owner is None
    assert saved.lease_token is None
    assert saved.lease_expires_at is None
    assert saved.lease_heartbeat_at is None


def test_stale_lease_token_cannot_write_terminal_state():
    owner_id, _assignment_id, operation = _seed_operation()
    first = workflow_repository.claim_operation(
        operation.id,
        owner_id=owner_id,
        worker_id="worker-a",
        lease_seconds=60,
    )
    _expire_lease(operation.id)
    workflow_repository.claim_operation(
        operation.id,
        owner_id=owner_id,
        worker_id="worker-b",
        lease_seconds=60,
    )

    with pytest.raises(LeaseLost):
        workflow_repository.save_operation_checkpoint(
            operation.id,
            owner_id=owner_id,
            expected_attempt=operation.attempt,
            expected_checkpoint_revision=0,
            stage="completed",
            checkpoint={},
            terminal_summary={"outcome": "done"},
            terminal_status="done",
            expected_lease_token=first.lease_token,
        )
    persisted = workflow_repository.get_operation(operation.id, owner_id=owner_id)
    assert persisted.lease_owner == "worker-b"
    assert persisted.status == "running"
    assert persisted.terminal_summary is None


def test_legacy_checkpoint_write_without_token_works_when_unleased():
    owner_id, _assignment_id, operation = _seed_operation()

    saved = workflow_repository.save_operation_checkpoint(
        operation.id,
        owner_id=owner_id,
        expected_attempt=operation.attempt,
        expected_checkpoint_revision=0,
        stage="parsed",
        checkpoint={"completed_sources": 1},
    )

    assert saved.checkpoint_revision == 1


def test_unleased_writer_is_fenced_when_an_active_lease_exists():
    owner_id, _assignment_id, operation = _seed_operation()
    workflow_repository.claim_operation(
        operation.id,
        owner_id=owner_id,
        worker_id="worker-a",
        lease_seconds=60,
    )

    with pytest.raises(LeaseLost) as checkpoint_error:
        workflow_repository.save_operation_checkpoint(
            operation.id,
            owner_id=owner_id,
            expected_attempt=operation.attempt,
            expected_checkpoint_revision=0,
            stage="parsed",
            checkpoint={},
        )
    assert checkpoint_error.value.code == "lease_lost"

    with pytest.raises(LeaseLost) as update_error:
        workflow_repository.update_operation(
            operation.id,
            owner_id=owner_id,
            expected_attempt=operation.attempt,
            progress={"done": 1},
        )
    assert update_error.value.code == "lease_lost"
    assert workflow_repository.get_operation(
        operation.id, owner_id=owner_id
    ).lease_owner == "worker-a"


def test_new_attempt_resets_lease_fields():
    owner_id, assignment_id, operation = _seed_operation()
    claimed = workflow_repository.claim_operation(
        operation.id,
        owner_id=owner_id,
        worker_id="worker-a",
        lease_seconds=60,
    )
    workflow_repository.update_operation(
        operation.id,
        owner_id=owner_id,
        expected_attempt=operation.attempt,
        status="error",
        error_code="parse_failed",
        expected_lease_token=claimed.lease_token,
    )

    retried, created = workflow_repository.create_operation(
        assignment_id=assignment_id,
        owner_id=owner_id,
        operation_type=operation.operation_type,
        input_hash=operation.input_hash,
    )

    assert created is True
    assert retried.attempt == operation.attempt + 1
    assert retried.lease_owner is None
    assert retried.lease_token is None
    assert retried.lease_expires_at is None
    assert retried.lease_heartbeat_at is None


def test_list_claimable_operations_filters_status_lease_and_type():
    owner_id, _assignment_id, pending_op = _seed_operation("problem_extraction")
    running_owner, _running_assignment, running_op = _seed_operation("submission_recognition")
    workflow_repository.update_operation(
        running_op.id,
        owner_id=running_owner,
        expected_attempt=running_op.attempt,
        status="running",
    )
    leased_owner, _leased_assignment, leased_op = _seed_operation("submission_recognition")
    workflow_repository.claim_operation(
        leased_op.id,
        owner_id=leased_owner,
        worker_id="worker-c",
        lease_seconds=60,
    )
    expired_owner, _expired_assignment, expired_op = _seed_operation("ai_completion")
    workflow_repository.claim_operation(
        expired_op.id,
        owner_id=expired_owner,
        worker_id="worker-d",
        lease_seconds=60,
    )
    _expire_lease(expired_op.id)
    _unknown_owner, _unknown_assignment, unknown_op = _seed_operation("material_import")

    claimable = workflow_repository.list_claimable_operations(
        ["problem_extraction", "submission_recognition", "ai_completion"]
    )
    ids = {row.id for row in claimable}

    assert pending_op.id in ids
    assert running_op.id in ids
    assert expired_op.id in ids
    assert leased_op.id not in ids
    assert unknown_op.id not in ids


def test_list_claimable_operations_is_bounded():
    owner_id, _assignment_id, _operation = _seed_operation()

    assert workflow_repository.list_claimable_operations(
        ["submission_recognition"], limit=0
    ) == []
    with pytest.raises(ValidationError):
        workflow_repository.list_claimable_operations(
            ["submission_recognition"], limit=101
        )


def test_claim_rejects_nonpositive_lease_seconds():
    owner_id, _assignment_id, operation = _seed_operation()

    with pytest.raises(ValidationError):
        workflow_repository.claim_operation(
            operation.id,
            owner_id=owner_id,
            worker_id="worker-a",
            lease_seconds=0,
        )
    with pytest.raises(ValidationError):
        workflow_repository.heartbeat_operation(
            operation.id,
            owner_id=owner_id,
            worker_id="worker-a",
            lease_token="token",
            lease_seconds=-1,
        )


def test_claim_rejects_invalid_worker_id():
    """Non-string, empty, or over-length worker IDs fail with a stable code."""
    owner_id, _assignment_id, operation = _seed_operation()

    for worker_id in ("", "w" * 129, None, 123):
        with pytest.raises(ValidationError) as invalid:
            workflow_repository.claim_operation(
                operation.id,
                owner_id=owner_id,
                worker_id=worker_id,
                lease_seconds=60,
            )
        assert invalid.value.code == "invalid_worker_id"


def test_heartbeat_and_release_reject_invalid_worker_id_and_token():
    owner_id, _assignment_id, operation = _seed_operation()
    claimed = workflow_repository.claim_operation(
        operation.id,
        owner_id=owner_id,
        worker_id="worker-a",
        lease_seconds=60,
    )

    with pytest.raises(ValidationError) as empty_worker:
        workflow_repository.heartbeat_operation(
            operation.id,
            owner_id=owner_id,
            worker_id="",
            lease_token=claimed.lease_token,
            lease_seconds=60,
        )
    assert empty_worker.value.code == "invalid_worker_id"

    with pytest.raises(ValidationError) as empty_token:
        workflow_repository.release_operation(
            operation.id,
            owner_id=owner_id,
            worker_id="worker-a",
            lease_token="",
        )
    assert empty_token.value.code == "invalid_lease_token"

    with pytest.raises(ValidationError) as overlong_token:
        workflow_repository.heartbeat_operation(
            operation.id,
            owner_id=owner_id,
            worker_id="worker-a",
            lease_token="t" * 65,
            lease_seconds=60,
        )
    assert overlong_token.value.code == "invalid_lease_token"


def test_expected_lease_token_inputs_are_bounded():
    owner_id, _assignment_id, operation = _seed_operation()
    workflow_repository.claim_operation(
        operation.id,
        owner_id=owner_id,
        worker_id="worker-a",
        lease_seconds=60,
    )

    with pytest.raises(ValidationError) as checkpoint_error:
        workflow_repository.save_operation_checkpoint(
            operation.id,
            owner_id=owner_id,
            expected_attempt=operation.attempt,
            expected_checkpoint_revision=0,
            stage="parsed",
            checkpoint={},
            expected_lease_token="",
        )
    assert checkpoint_error.value.code == "invalid_lease_token"

    with pytest.raises(ValidationError) as update_error:
        workflow_repository.update_operation(
            operation.id,
            owner_id=owner_id,
            expected_attempt=operation.attempt,
            expected_lease_token="t" * 65,
            progress={"done": 1},
        )
    assert update_error.value.code == "invalid_lease_token"


def test_worker_and_token_boundary_lengths_are_accepted():
    """A 128-char worker ID and 64-char token pass validation and match."""
    owner_id, _assignment_id, operation = _seed_operation()
    workflow_repository.claim_operation(
        operation.id,
        owner_id=owner_id,
        worker_id="w" * 128,
        lease_seconds=60,
    )
    with session_scope() as session:
        session.execute(
            update(workflow_repository.WorkflowOperationRecord)
            .where(workflow_repository.WorkflowOperationRecord.id == operation.id)
            .values(lease_token="t" * 64)
        )

    assert workflow_repository.heartbeat_operation(
        operation.id,
        owner_id=owner_id,
        worker_id="w" * 128,
        lease_token="t" * 64,
        lease_seconds=60,
    ) is True


def test_lease_consistency_constraint_rejects_partial_lease_state():
    """The database rejects a row that is neither fully inactive nor fully active.

    A lease row with an expiry or heartbeat but no owner/token is a partial
    state and must fail the consistency check at write time.
    """
    owner_id, _assignment_id, operation = _seed_operation()

    with pytest.raises(IntegrityError):
        with session_scope() as session:
            session.execute(
                update(workflow_repository.WorkflowOperationRecord)
                .where(workflow_repository.WorkflowOperationRecord.id == operation.id)
                .values(lease_expires_at=time.time() + 60)
            )
    with pytest.raises(IntegrityError):
        with session_scope() as session:
            session.execute(
                update(workflow_repository.WorkflowOperationRecord)
                .where(workflow_repository.WorkflowOperationRecord.id == operation.id)
                .values(lease_heartbeat_at=time.time())
            )


def test_orm_metadata_includes_lease_columns_and_consistency_constraint():
    table = workflow_repository.WorkflowOperationRecord.__table__
    columns = {column.name for column in table.columns}
    assert {
        "lease_owner",
        "lease_token",
        "lease_expires_at",
        "lease_heartbeat_at",
    } <= columns

    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in table.constraints
        if hasattr(constraint, "sqltext")
    }
    assert "ck_workflow_operations_lease_consistency" in checks
    lease_check = checks["ck_workflow_operations_lease_consistency"]
    # An inactive lease has all four lease fields null; an active lease has
    # owner, token, expiry, and heartbeat all non-null. No partial lease state
    # (e.g. an expiry without an owner) is representable.
    for column in ("lease_owner", "lease_token", "lease_expires_at", "lease_heartbeat_at"):
        assert f"{column} IS NULL" in lease_check
        assert f"{column} IS NOT NULL" in lease_check
    index_names = {index.name for index in table.indexes}
    assert "ix_workflow_operations_claimable" in index_names
