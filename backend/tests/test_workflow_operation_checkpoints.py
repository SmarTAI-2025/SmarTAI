from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import math
import uuid

import pytest

from backend.db import workflow_repository
from backend.db.file_repository import delete_unlinked_file, get_file, save_file
from backend.db.models import AssignmentRecord, CourseRecord, UserRecord
from backend.db.session import session_scope
from backend.domain.errors import (
    InvalidTransition,
    NotFound,
    ValidationError,
    VersionConflict,
)
from backend.storage.local import LocalStorage


def _seed_operation():
    suffix = uuid.uuid4().hex[:10]
    owner_id = f"checkpoint_owner_{suffix}"
    course_id = f"checkpoint_course_{suffix}"
    assignment_id = f"checkpoint_assignment_{suffix}"
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
            name="Checkpoint Course",
            code=f"CP-{suffix}",
            teacher_id=owner_id,
        ))
        session.flush()
        session.add(AssignmentRecord(
            id=assignment_id,
            course_id=course_id,
            teacher_id=owner_id,
            name="Checkpoint Assignment",
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
        operation_type="submission_recognition",
        input_hash=uuid.uuid4().hex,
    )
    assert created is True
    return owner_id, assignment_id, operation


def test_operation_starts_with_empty_checkpoint_state():
    owner_id, _assignment_id, operation = _seed_operation()

    assert getattr(operation, "checkpoint_revision", None) == 0
    assert getattr(operation, "checkpoint_stage", "missing") is None
    assert getattr(operation, "checkpoint", None) == {}
    assert getattr(operation, "artifact_refs", None) == []
    assert getattr(operation, "terminal_summary", "missing") is None

    persisted = workflow_repository.get_operation(operation.id, owner_id=owner_id)
    assert persisted.checkpoint_revision == 0
    assert persisted.checkpoint_stage is None
    assert persisted.checkpoint == {}
    assert persisted.artifact_refs == []
    assert persisted.terminal_summary is None


def test_legacy_operation_json_payloads_are_bounded_before_write():
    owner_id, assignment_id, operation = _seed_operation()
    oversized_payload = {"text": "x" * (4 * 1024 * 1024 + 1)}
    oversized_progress = {"detail": "x" * (64 * 1024 + 1)}

    with pytest.raises(ValidationError) as payload_error:
        workflow_repository.create_operation(
            assignment_id=assignment_id,
            owner_id=owner_id,
            operation_type="problem_extraction",
            input_hash=uuid.uuid4().hex,
            payload=oversized_payload,
        )
    assert payload_error.value.code == "operation_payload_too_large"

    with pytest.raises(ValidationError) as progress_error:
        workflow_repository.update_operation(
            operation.id,
            owner_id=owner_id,
            expected_attempt=operation.attempt,
            progress=oversized_progress,
        )
    assert progress_error.value.code == "operation_progress_too_large"

    with pytest.raises(ValidationError) as create_progress_error:
        workflow_repository.create_operation(
            assignment_id=assignment_id,
            owner_id=owner_id,
            operation_type="material_import",
            input_hash=uuid.uuid4().hex,
            progress=oversized_progress,
        )
    assert create_progress_error.value.code == "operation_progress_too_large"

    with pytest.raises(ValidationError) as update_payload_error:
        workflow_repository.update_operation(
            operation.id,
            owner_id=owner_id,
            expected_attempt=operation.attempt,
            payload=oversized_payload,
        )
    assert update_payload_error.value.code == "operation_payload_too_large"

    persisted = workflow_repository.get_operation(operation.id, owner_id=owner_id)
    assert persisted.progress == {}


def test_checkpoint_write_increments_revision_and_persists_artifact_refs(tmp_path):
    owner_id, assignment_id, operation = _seed_operation()
    artifact = save_file(
        storage=LocalStorage(tmp_path / "checkpoint-artifacts"),
        owner_id=owner_id,
        kind="ocr_artifact",
        original_name="page-1.json",
        content=b"{}",
        content_type="application/json",
        assignment_id=assignment_id,
    )

    saved = workflow_repository.save_operation_checkpoint(
        operation.id,
        owner_id=owner_id,
        expected_attempt=operation.attempt,
        expected_checkpoint_revision=0,
        stage="ocr_complete",
        checkpoint={"completed_sources": 1},
        artifact_refs=[artifact.id, artifact.id],
    )

    assert saved.checkpoint_revision == 1
    assert saved.checkpoint_stage == "ocr_complete"
    assert saved.checkpoint == {"completed_sources": 1}
    assert saved.artifact_refs == [artifact.id]
    assert saved.terminal_summary is None

    persisted = workflow_repository.get_operation(operation.id, owner_id=owner_id)
    assert persisted.checkpoint_revision == 1
    assert persisted.checkpoint_stage == "ocr_complete"
    assert persisted.checkpoint == {"completed_sources": 1}
    assert persisted.artifact_refs == [artifact.id]


def test_checkpoint_reference_prevents_artifact_cleanup(tmp_path):
    owner_id, assignment_id, operation = _seed_operation()
    storage = LocalStorage(tmp_path / "checkpoint-protected")
    artifact = save_file(
        storage=storage,
        owner_id=owner_id,
        kind="submission_container",
        original_name="submissions.zip",
        content=b"archive",
        content_type="application/zip",
        assignment_id=assignment_id,
    )
    workflow_repository.save_operation_checkpoint(
        operation.id,
        owner_id=owner_id,
        expected_attempt=operation.attempt,
        expected_checkpoint_revision=0,
        stage="submission_container_saved",
        checkpoint={"container_file_id": artifact.id},
        artifact_refs=[artifact.id],
    )

    deleted = delete_unlinked_file(
        storage=storage,
        file_id=artifact.id,
        owner_id=owner_id,
        assignment_id=assignment_id,
    )

    assert deleted is False
    assert get_file(file_id=artifact.id, owner_id=owner_id) is not None
    assert storage.exists(artifact.storage_key)


def test_checkpoint_revision_cas_allows_exactly_one_writer():
    owner_id, _assignment_id, operation = _seed_operation()

    def write_checkpoint(worker: str):
        try:
            saved = workflow_repository.save_operation_checkpoint(
                operation.id,
                owner_id=owner_id,
                expected_attempt=operation.attempt,
                expected_checkpoint_revision=0,
                stage="parsing",
                checkpoint={"worker": worker},
            )
            return "saved", saved.checkpoint["worker"]
        except VersionConflict as exc:
            return "conflict", exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(write_checkpoint, ("first", "second")))

    assert [kind for kind, _value in results].count("saved") == 1
    assert results.count(("conflict", "stale_checkpoint_revision")) == 1
    persisted = workflow_repository.get_operation(operation.id, owner_id=owner_id)
    assert persisted.checkpoint_revision == 1
    assert persisted.checkpoint["worker"] in {"first", "second"}


def test_nonterminal_checkpoint_can_advance_through_multiple_revisions():
    owner_id, _assignment_id, operation = _seed_operation()

    first = workflow_repository.save_operation_checkpoint(
        operation.id,
        owner_id=owner_id,
        expected_attempt=operation.attempt,
        expected_checkpoint_revision=0,
        stage="ocr_complete",
        checkpoint={"completed_sources": 1},
    )
    second = workflow_repository.save_operation_checkpoint(
        operation.id,
        owner_id=owner_id,
        expected_attempt=operation.attempt,
        expected_checkpoint_revision=first.checkpoint_revision,
        stage="parsing",
        checkpoint={"completed_sources": 2},
    )

    assert second.checkpoint_revision == 2
    assert second.checkpoint_stage == "parsing"
    assert second.checkpoint == {"completed_sources": 2}


def test_terminal_summary_is_repeatable_and_cannot_be_overwritten():
    owner_id, _assignment_id, operation = _seed_operation()
    terminal = {"outcome": "done", "processed_sources": 20}

    saved = workflow_repository.save_operation_checkpoint(
        operation.id,
        owner_id=owner_id,
        expected_attempt=operation.attempt,
        expected_checkpoint_revision=0,
        stage="completed",
        checkpoint={"processed_sources": 20},
        terminal_summary=terminal,
        terminal_status="done",
    )
    assert saved.terminal_summary == terminal
    assert saved.status == "done"
    assert saved.completed_at is not None
    assert workflow_repository.get_operation(
        operation.id, owner_id=owner_id
    ).terminal_summary == terminal

    with pytest.raises(VersionConflict) as replay_error:
        workflow_repository.save_operation_checkpoint(
            operation.id,
            owner_id=owner_id,
            expected_attempt=operation.attempt,
            expected_checkpoint_revision=0,
            stage="completed",
            checkpoint={"processed_sources": 20},
            terminal_summary=terminal,
            terminal_status="done",
        )
    assert replay_error.value.code == "stale_checkpoint_revision"
    assert workflow_repository.get_operation(
        operation.id, owner_id=owner_id
    ).terminal_summary == terminal

    with pytest.raises(InvalidTransition) as legacy_update_error:
        workflow_repository.update_operation(
            operation.id,
            owner_id=owner_id,
            expected_attempt=operation.attempt,
            status="running",
            progress={"processed_sources": 21},
        )
    assert legacy_update_error.value.code == "operation_already_terminal"

    with pytest.raises(InvalidTransition) as terminal_error:
        workflow_repository.save_operation_checkpoint(
            operation.id,
            owner_id=owner_id,
            expected_attempt=operation.attempt,
            expected_checkpoint_revision=saved.checkpoint_revision,
            stage="changed",
            checkpoint={"processed_sources": 21},
        )
    assert terminal_error.value.code == "operation_already_terminal"
    assert workflow_repository.get_operation(
        operation.id, owner_id=owner_id
    ).terminal_summary == terminal


@pytest.mark.parametrize(
    ("terminal_summary", "terminal_status", "expected_code"),
    [
        ({"outcome": "done"}, None, "invalid_operation_terminal_state"),
        (None, "done", "invalid_operation_terminal_state"),
        ({"outcome": "done"}, "", "invalid_operation_terminal_status"),
        ({"outcome": "done"}, "pending", "invalid_operation_terminal_status"),
        ({"outcome": "done"}, "running", "invalid_operation_terminal_status"),
        ({"outcome": "done"}, "x" * 33, "invalid_operation_terminal_status"),
    ],
)
def test_terminal_checkpoint_requires_a_valid_paired_status(
    terminal_summary,
    terminal_status,
    expected_code,
):
    owner_id, _assignment_id, operation = _seed_operation()

    with pytest.raises(ValidationError) as invalid:
        workflow_repository.save_operation_checkpoint(
            operation.id,
            owner_id=owner_id,
            expected_attempt=operation.attempt,
            expected_checkpoint_revision=0,
            stage="completed",
            checkpoint={"processed_sources": 20},
            terminal_summary=terminal_summary,
            terminal_status=terminal_status,
        )

    assert invalid.value.code == expected_code
    persisted = workflow_repository.get_operation(operation.id, owner_id=owner_id)
    assert persisted.status == "pending"
    assert persisted.checkpoint_revision == 0
    assert persisted.terminal_summary is None


def test_retry_resets_checkpoint_and_fences_the_previous_attempt():
    owner_id, assignment_id, operation = _seed_operation()
    checkpointed = workflow_repository.save_operation_checkpoint(
        operation.id,
        owner_id=owner_id,
        expected_attempt=operation.attempt,
        expected_checkpoint_revision=0,
        stage="parsed",
        checkpoint={"completed_sources": 1},
    )
    workflow_repository.update_operation(
        operation.id,
        owner_id=owner_id,
        expected_attempt=operation.attempt,
        status="error",
        error_code="parse_failed",
    )

    retried, created = workflow_repository.create_operation(
        assignment_id=assignment_id,
        owner_id=owner_id,
        operation_type=operation.operation_type,
        input_hash=operation.input_hash,
    )
    assert created is True
    assert retried.attempt == operation.attempt + 1
    assert retried.checkpoint_revision == 0
    assert retried.checkpoint_stage is None
    assert retried.checkpoint == {}
    assert retried.artifact_refs == []
    assert retried.terminal_summary is None

    with pytest.raises(VersionConflict) as stale_attempt:
        workflow_repository.save_operation_checkpoint(
            operation.id,
            owner_id=owner_id,
            expected_attempt=operation.attempt,
            expected_checkpoint_revision=checkpointed.checkpoint_revision,
            stage="late_worker",
            checkpoint={"worker": "old"},
        )
    assert stale_attempt.value.code == "stale_operation_attempt"

    resumed = workflow_repository.save_operation_checkpoint(
        retried.id,
        owner_id=owner_id,
        expected_attempt=retried.attempt,
        expected_checkpoint_revision=0,
        stage="restarted",
        checkpoint={"completed_sources": 0},
    )
    assert resumed.checkpoint_revision == 1
    assert resumed.checkpoint_stage == "restarted"


def test_orm_metadata_includes_checkpoint_revision_constraint():
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in workflow_repository.WorkflowOperationRecord.__table__.constraints
        if hasattr(constraint, "sqltext")
    }

    assert "ck_workflow_operations_checkpoint_revision_nonnegative" in checks
    assert "checkpoint_revision >= 0" in checks[
        "ck_workflow_operations_checkpoint_revision_nonnegative"
    ]


def test_checkpoint_owner_and_artifact_predicates_hide_other_owners(tmp_path):
    owner_id, _assignment_id, operation = _seed_operation()
    other_owner, other_assignment, other_operation = _seed_operation()
    suffix = uuid.uuid4().hex[:10]
    same_owner_course = f"checkpoint_course_same_owner_{suffix}"
    same_owner_assignment = f"checkpoint_assignment_same_owner_{suffix}"
    with session_scope() as session:
        session.add(CourseRecord(
            id=same_owner_course,
            name="Other Assignment Course",
            code=f"CP-O-{suffix}",
            teacher_id=owner_id,
        ))
        session.flush()
        session.add(AssignmentRecord(
            id=same_owner_assignment,
            course_id=same_owner_course,
            teacher_id=owner_id,
            name="Other Assignment",
            status="draft",
            version=1,
        ))
    other_artifact = save_file(
        storage=LocalStorage(tmp_path / "other-owner-artifacts"),
        owner_id=other_owner,
        kind="ocr_artifact",
        original_name="secret.json",
        content=b"{}",
        content_type="application/json",
        assignment_id=other_assignment,
    )
    wrong_assignment_artifact = save_file(
        storage=LocalStorage(tmp_path / "wrong-assignment-artifacts"),
        owner_id=owner_id,
        kind="ocr_artifact",
        original_name="other-assignment.json",
        content=b"{}",
        content_type="application/json",
        assignment_id=same_owner_assignment,
    )

    with pytest.raises(NotFound) as wrong_owner:
        workflow_repository.save_operation_checkpoint(
            operation.id,
            owner_id=other_owner,
            expected_attempt=operation.attempt,
            expected_checkpoint_revision=0,
            stage="parsing",
            checkpoint={},
        )
    with pytest.raises(NotFound) as missing_operation:
        workflow_repository.save_operation_checkpoint(
            "missing-operation",
            owner_id=other_owner,
            expected_attempt=operation.attempt,
            expected_checkpoint_revision=0,
            stage="parsing",
            checkpoint={},
        )
    assert wrong_owner.value.code == missing_operation.value.code == "not_found"

    with pytest.raises(NotFound) as wrong_artifact:
        workflow_repository.save_operation_checkpoint(
            operation.id,
            owner_id=owner_id,
            expected_attempt=operation.attempt,
            expected_checkpoint_revision=0,
            stage="parsing",
            checkpoint={},
            artifact_refs=[other_artifact.id],
        )
    with pytest.raises(NotFound) as missing_artifact:
        workflow_repository.save_operation_checkpoint(
            operation.id,
            owner_id=owner_id,
            expected_attempt=operation.attempt,
            expected_checkpoint_revision=0,
            stage="parsing",
            checkpoint={},
            artifact_refs=["missing-artifact"],
        )
    assert wrong_artifact.value.code == missing_artifact.value.code == "not_found"
    with pytest.raises(NotFound) as wrong_assignment:
        workflow_repository.save_operation_checkpoint(
            operation.id,
            owner_id=owner_id,
            expected_attempt=operation.attempt,
            expected_checkpoint_revision=0,
            stage="parsing",
            checkpoint={},
            artifact_refs=[wrong_assignment_artifact.id],
        )
    assert wrong_assignment.value.code == "not_found"
    assert other_operation.owner_id == other_owner


@pytest.mark.parametrize(
    ("changes", "expected_code"),
    [
        ({"stage": "x" * 65}, "invalid_checkpoint_stage"),
        ({"checkpoint": []}, "invalid_operation_checkpoint"),
        ({"checkpoint": {"value": math.nan}}, "invalid_operation_checkpoint"),
        (
            {"checkpoint": {"value": "x" * (64 * 1024 + 1)}},
            "operation_checkpoint_too_large",
        ),
        ({"artifact_refs": "not-a-list"}, "invalid_operation_artifact_refs"),
        ({"artifact_refs": [""]}, "invalid_operation_artifact_refs"),
        ({"artifact_refs": ["x" * 65]}, "invalid_operation_artifact_refs"),
        (
            {"artifact_refs": [f"artifact-{index}" for index in range(101)]},
            "operation_artifact_refs_too_large",
        ),
        ({"terminal_summary": []}, "invalid_operation_terminal_summary"),
        (
            {"terminal_summary": {"value": "x" * (16 * 1024 + 1)}},
            "operation_terminal_summary_too_large",
        ),
    ],
)
def test_checkpoint_values_are_bounded_before_write(changes, expected_code):
    owner_id, _assignment_id, operation = _seed_operation()
    request = {
        "stage": "parsing",
        "checkpoint": {},
        "artifact_refs": [],
        "terminal_summary": None,
        **changes,
    }

    with pytest.raises(ValidationError) as invalid:
        workflow_repository.save_operation_checkpoint(
            operation.id,
            owner_id=owner_id,
            expected_attempt=operation.attempt,
            expected_checkpoint_revision=0,
            **request,
        )
    assert invalid.value.code == expected_code
    persisted = workflow_repository.get_operation(operation.id, owner_id=owner_id)
    assert persisted.checkpoint_revision == 0
