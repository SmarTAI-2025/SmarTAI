"""Contract tests for durable workflow sources and per-file outcomes."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from backend.db import source_outcome_repository, workflow_repository
from backend.db.file_repository import save_file
from backend.db.models import AssignmentRecord, CourseRecord, UserRecord
from backend.db.session import session_scope
from backend.domain.errors import NotFound, ValidationError, VersionConflict
from backend.storage.local import LocalStorage


def _seed_owner_assignment(*, owner_id: str, assignment_id: str) -> None:
    course_id = f"course-{assignment_id}"
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
            name="Course",
            teacher_id=owner_id,
        ))
        session.flush()
        session.add(AssignmentRecord(
            id=assignment_id,
            course_id=course_id,
            teacher_id=owner_id,
            name="Assignment",
            status="draft",
            version=1,
        ))
    workflow_repository.ensure_workflow(
        assignment_id=assignment_id,
        owner_id=owner_id,
    )


def _save_assignment_file(
    tmp_path,
    *,
    owner_id: str,
    assignment_id: str,
    index: int,
    name_prefix: str = "answers",
):
    return save_file(
        storage=LocalStorage(tmp_path / f"uploads-{owner_id}"),
        owner_id=owner_id,
        kind="submission_source",
        original_name=f"{name_prefix}-{index}.pdf",
        content=f"answer-{index}".encode(),
        content_type="application/pdf",
        assignment_id=assignment_id,
    )


def _seed_source_context(tmp_path):
    suffix = uuid.uuid4().hex[:8]
    owner_id = f"teacher-{suffix}"
    assignment_id = f"assignment-{suffix}"
    _seed_owner_assignment(owner_id=owner_id, assignment_id=assignment_id)
    operation, created = workflow_repository.create_operation(
        assignment_id=assignment_id,
        owner_id=owner_id,
        operation_type="submission_recognition",
        input_hash=uuid.uuid4().hex,
    )
    assert created is True
    stored = _save_assignment_file(
        tmp_path,
        owner_id=owner_id,
        assignment_id=assignment_id,
        index=0,
    )
    return owner_id, assignment_id, operation, stored


def test_register_source_requires_owned_matching_rows(tmp_path):
    owner_id, assignment_id, operation, stored = _seed_source_context(tmp_path)

    source, created = source_outcome_repository.register_source(
        owner_id=owner_id,
        assignment_id=assignment_id,
        operation_id=operation.id,
        expected_attempt=operation.attempt,
        order_index=0,
        stored_file_id=stored.id,
    )

    assert created is True
    assert source.owner_id == owner_id
    assert source.assignment_id == assignment_id
    assert source.operation_id == operation.id
    assert source.attempt == operation.attempt
    assert source.order_index == 0
    assert source.original_name == "answers-0.pdf"
    assert source.sha256 == stored.sha256
    assert source.content_type == "application/pdf"
    assert source.size_bytes == len(b"answer-0")
    assert source.storage_key == stored.storage_key

    with pytest.raises(NotFound) as absent:
        source_outcome_repository.register_source(
            owner_id=owner_id,
            assignment_id=assignment_id,
            operation_id=operation.id,
            expected_attempt=operation.attempt,
            order_index=1,
            stored_file_id="missing-file",
        )
    with pytest.raises(NotFound) as wrong_owner:
        source_outcome_repository.register_source(
            owner_id="another-owner",
            assignment_id=assignment_id,
            operation_id=operation.id,
            expected_attempt=operation.attempt,
            order_index=1,
            stored_file_id=stored.id,
        )
    assert absent.value.code == wrong_owner.value.code == "not_found"

    other_owner = f"other-{uuid.uuid4().hex[:8]}"
    other_assignment = f"assignment-{uuid.uuid4().hex[:8]}"
    _seed_owner_assignment(owner_id=other_owner, assignment_id=other_assignment)
    mismatched_file = _save_assignment_file(
        tmp_path,
        owner_id=other_owner,
        assignment_id=other_assignment,
        index=1,
    )
    with pytest.raises(NotFound):
        source_outcome_repository.register_source(
            owner_id=owner_id,
            assignment_id=assignment_id,
            operation_id=operation.id,
            expected_attempt=operation.attempt,
            order_index=1,
            stored_file_id=mismatched_file.id,
        )


@pytest.mark.parametrize("content_type", [None, ""])
def test_register_source_requires_persisted_mime_type(tmp_path, content_type):
    suffix = uuid.uuid4().hex[:8]
    owner_id = f"teacher-{suffix}"
    assignment_id = f"assignment-{suffix}"
    _seed_owner_assignment(owner_id=owner_id, assignment_id=assignment_id)
    operation, _ = workflow_repository.create_operation(
        assignment_id=assignment_id,
        owner_id=owner_id,
        operation_type="submission_recognition",
        input_hash=uuid.uuid4().hex,
    )
    stored = save_file(
        storage=LocalStorage(tmp_path / "missing-mime"),
        owner_id=owner_id,
        kind="submission_source",
        original_name="answers.pdf",
        content=b"answer",
        content_type=content_type,
        assignment_id=assignment_id,
    )

    with pytest.raises(ValidationError):
        source_outcome_repository.register_source(
            owner_id=owner_id,
            assignment_id=assignment_id,
            operation_id=operation.id,
            expected_attempt=operation.attempt,
            order_index=0,
            stored_file_id=stored.id,
        )


def test_source_registration_is_idempotent_and_rejects_position_conflicts(tmp_path):
    owner_id, assignment_id, operation, stored = _seed_source_context(tmp_path)
    source, created = source_outcome_repository.register_source(
        owner_id=owner_id,
        assignment_id=assignment_id,
        operation_id=operation.id,
        expected_attempt=operation.attempt,
        order_index=0,
        stored_file_id=stored.id,
    )

    replay, replay_created = source_outcome_repository.register_source(
        owner_id=owner_id,
        assignment_id=assignment_id,
        operation_id=operation.id,
        expected_attempt=operation.attempt,
        order_index=0,
        stored_file_id=stored.id,
    )
    assert replay.id == source.id
    assert replay_created is False

    other_file = _save_assignment_file(
        tmp_path,
        owner_id=owner_id,
        assignment_id=assignment_id,
        index=1,
    )
    with pytest.raises(VersionConflict):
        source_outcome_repository.register_source(
            owner_id=owner_id,
            assignment_id=assignment_id,
            operation_id=operation.id,
            expected_attempt=operation.attempt,
            order_index=0,
            stored_file_id=other_file.id,
        )


def test_source_registration_rejects_invalid_and_stale_attempts(tmp_path):
    owner_id, assignment_id, operation, stored = _seed_source_context(tmp_path)

    for attempt, order_index in ((0, 0), (operation.attempt, -1)):
        with pytest.raises(ValidationError):
            source_outcome_repository.register_source(
                owner_id=owner_id,
                assignment_id=assignment_id,
                operation_id=operation.id,
                expected_attempt=attempt,
                order_index=order_index,
                stored_file_id=stored.id,
            )

    workflow_repository.update_operation(
        operation.id,
        owner_id=owner_id,
        expected_attempt=operation.attempt,
        status="error",
        error_code="submission_parse_failed",
    )
    retried, created = workflow_repository.create_operation(
        assignment_id=assignment_id,
        owner_id=owner_id,
        operation_type=operation.operation_type,
        input_hash=operation.input_hash,
    )
    assert created is True
    assert retried.attempt == operation.attempt + 1

    with pytest.raises(VersionConflict) as stale:
        source_outcome_repository.register_source(
            owner_id=owner_id,
            assignment_id=assignment_id,
            operation_id=operation.id,
            expected_attempt=operation.attempt,
            order_index=0,
            stored_file_id=stored.id,
        )
    assert stale.value.code == "stale_operation_attempt"


def test_outcome_is_immutable_idempotent_and_owner_scoped(tmp_path):
    owner_id, assignment_id, operation, stored = _seed_source_context(tmp_path)
    source, _ = source_outcome_repository.register_source(
        owner_id=owner_id,
        assignment_id=assignment_id,
        operation_id=operation.id,
        expected_attempt=operation.attempt,
        order_index=0,
        stored_file_id=stored.id,
    )
    artifact = _save_assignment_file(
        tmp_path,
        owner_id=owner_id,
        assignment_id=assignment_id,
        index=0,
        name_prefix="artifact",
    )

    outcome, created = source_outcome_repository.record_outcome(
        source_id=source.id,
        owner_id=owner_id,
        status="no_matching_answer",
        student_candidate=None,
        matched_answer_count=0,
        unknown_question_ids=["q404"],
        stable_error_code="no_matching_answer",
        retryable=True,
        artifact_file_id=artifact.id,
    )
    restored = source_outcome_repository.get_outcome(
        source.id,
        owner_id=owner_id,
    )

    assert created is True
    assert restored == outcome
    assert restored is not None
    assert restored.status == "no_matching_answer"
    assert restored.student_candidate is None
    assert restored.matched_answer_count == 0
    assert restored.unknown_question_ids == ("q404",)
    assert restored.stable_error_code == "no_matching_answer"
    assert restored.retryable is True
    assert restored.artifact_file_id == artifact.id

    replay, replay_created = source_outcome_repository.record_outcome(
        source_id=source.id,
        owner_id=owner_id,
        status="no_matching_answer",
        student_candidate=None,
        matched_answer_count=0,
        unknown_question_ids=["q404"],
        stable_error_code="no_matching_answer",
        retryable=True,
        artifact_file_id=artifact.id,
    )
    assert replay == restored
    assert replay_created is False

    with pytest.raises(VersionConflict):
        source_outcome_repository.record_outcome(
            source_id=source.id,
            owner_id=owner_id,
            status="parsed",
            student_candidate="student-1",
            matched_answer_count=1,
            unknown_question_ids=[],
            stable_error_code=None,
            retryable=False,
            artifact_file_id=artifact.id,
        )

    with pytest.raises(NotFound) as absent:
        source_outcome_repository.record_outcome(
            source_id="missing-source",
            owner_id=owner_id,
            status="parse_failed",
            student_candidate=None,
            matched_answer_count=0,
            unknown_question_ids=[],
            stable_error_code="submission_parse_failed",
            retryable=True,
        )
    with pytest.raises(NotFound) as wrong_owner:
        source_outcome_repository.record_outcome(
            source_id=source.id,
            owner_id="another-owner",
            status="parse_failed",
            student_candidate=None,
            matched_answer_count=0,
            unknown_question_ids=[],
            stable_error_code="submission_parse_failed",
            retryable=True,
        )
    assert absent.value.code == wrong_owner.value.code == "not_found"


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "pending"},
        {"matched_answer_count": -1},
        {"unknown_question_ids": ("q1",)},
        {"unknown_question_ids": [1]},
        {"unknown_question_ids": ["q" * 65]},
        {"unknown_question_ids": [f"q{i}" for i in range(101)]},
        {"unknown_question_ids": ["题" * 64 for _ in range(100)]},
    ],
)
def test_outcome_rejects_unbounded_or_invalid_evidence(tmp_path, changes):
    owner_id, assignment_id, operation, stored = _seed_source_context(tmp_path)
    source, _ = source_outcome_repository.register_source(
        owner_id=owner_id,
        assignment_id=assignment_id,
        operation_id=operation.id,
        expected_attempt=operation.attempt,
        order_index=0,
        stored_file_id=stored.id,
    )
    values = {
        "source_id": source.id,
        "owner_id": owner_id,
        "status": "parsed",
        "student_candidate": "student-1",
        "matched_answer_count": 1,
        "unknown_question_ids": [],
        "stable_error_code": None,
        "retryable": False,
    }
    values.update(changes)

    with pytest.raises(ValidationError) as invalid:
        source_outcome_repository.record_outcome(**values)
    assert invalid.value.code == "validation_error"


@pytest.mark.parametrize(
    "status, matched_answer_count",
    [("pending", 0), ("parsed", -1)],
)
def test_database_constraints_reject_invalid_outcomes(
    tmp_path,
    status,
    matched_answer_count,
):
    owner_id, assignment_id, operation, stored = _seed_source_context(tmp_path)
    source, _ = source_outcome_repository.register_source(
        owner_id=owner_id,
        assignment_id=assignment_id,
        operation_id=operation.id,
        expected_attempt=operation.attempt,
        order_index=0,
        stored_file_id=stored.id,
    )

    with pytest.raises(IntegrityError):
        with session_scope() as session:
            session.add(source_outcome_repository.WorkflowSourceOutcomeRecord(
                source_id=source.id,
                status=status,
                student_candidate=None,
                matched_answer_count=matched_answer_count,
                unknown_question_ids=[],
                stable_error_code=None,
                retryable=False,
                created_at=1,
            ))
            session.flush()


def test_twenty_sources_preserve_failed_and_conflicting_results(tmp_path):
    owner_id, assignment_id, operation, first_file = _seed_source_context(tmp_path)
    stored_files = [first_file]
    stored_files.extend(
        _save_assignment_file(
            tmp_path,
            owner_id=owner_id,
            assignment_id=assignment_id,
            index=index,
        )
        for index in range(1, 20)
    )
    sources = []
    for index, stored in enumerate(stored_files):
        source, _ = source_outcome_repository.register_source(
            owner_id=owner_id,
            assignment_id=assignment_id,
            operation_id=operation.id,
            expected_attempt=operation.attempt,
            order_index=index,
            stored_file_id=stored.id,
        )
        sources.append(source)

    for index, source in enumerate(sources[:19]):
        status = "parsed" if index < 18 else "parse_failed"
        source_outcome_repository.record_outcome(
            source_id=source.id,
            owner_id=owner_id,
            status=status,
            student_candidate=f"student-{index}" if status == "parsed" else None,
            matched_answer_count=1 if status == "parsed" else 0,
            unknown_question_ids=[],
            stable_error_code=None if status == "parsed" else "submission_parse_failed",
            retryable=status != "parsed",
        )

    incomplete = source_outcome_repository.summarize_sources(
        operation_id=operation.id,
        owner_id=owner_id,
        attempt=operation.attempt,
    )
    assert incomplete.uploaded_count == 20
    assert incomplete.success_count == 18
    assert incomplete.failed_count == 1
    assert incomplete.conflict_count == 0
    assert incomplete.pending_count == 1
    assert incomplete.is_complete is False

    source_outcome_repository.record_outcome(
        source_id=sources[19].id,
        owner_id=owner_id,
        status="identity_conflict",
        student_candidate="student-0",
        matched_answer_count=1,
        unknown_question_ids=[],
        stable_error_code="student_identity_conflict",
        retryable=False,
    )
    listed = source_outcome_repository.list_sources(
        operation_id=operation.id,
        owner_id=owner_id,
        attempt=operation.attempt,
    )
    summary = source_outcome_repository.summarize_sources(
        operation_id=operation.id,
        owner_id=owner_id,
        attempt=operation.attempt,
    )

    assert [source.order_index for source in listed] == list(range(20))
    assert [source.original_name for source in listed] == [
        f"answers-{index}.pdf" for index in range(20)
    ]
    assert summary.uploaded_count == 20
    assert summary.success_count == 18
    assert summary.failed_count == 1
    assert summary.conflict_count == 1
    assert summary.pending_count == 0
    assert summary.is_complete is True
    assert summary.uploaded_count == (
        summary.success_count + summary.failed_count + summary.conflict_count
    )


def test_no_matching_counts_as_failed_and_owner_scoped_reads_hide_batch(tmp_path):
    owner_id, assignment_id, operation, stored = _seed_source_context(tmp_path)
    source, _ = source_outcome_repository.register_source(
        owner_id=owner_id,
        assignment_id=assignment_id,
        operation_id=operation.id,
        expected_attempt=operation.attempt,
        order_index=0,
        stored_file_id=stored.id,
    )
    source_outcome_repository.record_outcome(
        source_id=source.id,
        owner_id=owner_id,
        status="no_matching_answer",
        student_candidate="student-1",
        matched_answer_count=0,
        unknown_question_ids=["q404"],
        stable_error_code="no_matching_answer",
        retryable=True,
    )

    summary = source_outcome_repository.summarize_sources(
        operation_id=operation.id,
        owner_id=owner_id,
        attempt=operation.attempt,
    )
    assert summary.uploaded_count == 1
    assert summary.failed_count == 1
    assert source_outcome_repository.get_outcome(
        source.id,
        owner_id=owner_id,
    ).status == "no_matching_answer"
    assert source_outcome_repository.get_outcome(
        source.id,
        owner_id="another-owner",
    ) is None

    with pytest.raises(NotFound):
        source_outcome_repository.get_source(
            source.id,
            owner_id="another-owner",
        )
    with pytest.raises(NotFound):
        source_outcome_repository.list_sources(
            operation_id=operation.id,
            owner_id="another-owner",
            attempt=operation.attempt,
        )
    with pytest.raises(NotFound):
        source_outcome_repository.summarize_sources(
            operation_id=operation.id,
            owner_id="another-owner",
            attempt=operation.attempt,
        )


def test_retry_source_preserves_lineage_and_artifact_reference(tmp_path):
    owner_id, assignment_id, operation, stored = _seed_source_context(tmp_path)
    original, _ = source_outcome_repository.register_source(
        owner_id=owner_id,
        assignment_id=assignment_id,
        operation_id=operation.id,
        expected_attempt=operation.attempt,
        order_index=0,
        stored_file_id=stored.id,
    )
    artifact = _save_assignment_file(
        tmp_path,
        owner_id=owner_id,
        assignment_id=assignment_id,
        index=0,
        name_prefix="artifact",
    )
    source_outcome_repository.record_outcome(
        source_id=original.id,
        owner_id=owner_id,
        status="parse_failed",
        student_candidate=None,
        matched_answer_count=0,
        unknown_question_ids=[],
        stable_error_code="submission_parse_failed",
        retryable=True,
        artifact_file_id=artifact.id,
    )
    workflow_repository.update_operation(
        operation.id,
        owner_id=owner_id,
        expected_attempt=operation.attempt,
        status="error",
        error_code="submission_parse_failed",
    )
    retried_operation, _ = workflow_repository.create_operation(
        assignment_id=assignment_id,
        owner_id=owner_id,
        operation_type=operation.operation_type,
        input_hash=operation.input_hash,
    )
    replacement = _save_assignment_file(
        tmp_path,
        owner_id=owner_id,
        assignment_id=assignment_id,
        index=1,
    )
    retried_source, _ = source_outcome_repository.register_source(
        owner_id=owner_id,
        assignment_id=assignment_id,
        operation_id=retried_operation.id,
        expected_attempt=retried_operation.attempt,
        order_index=0,
        stored_file_id=replacement.id,
        retry_of_source_id=original.id,
    )

    assert retried_source.retry_of_source_id == original.id
    assert source_outcome_repository.get_source(
        retried_source.id,
        owner_id=owner_id,
    ).retry_of_source_id == original.id
    assert source_outcome_repository.get_outcome(
        original.id,
        owner_id=owner_id,
    ).artifact_file_id == artifact.id

    second_replacement = _save_assignment_file(
        tmp_path,
        owner_id=owner_id,
        assignment_id=assignment_id,
        index=2,
    )
    with pytest.raises(NotFound):
        source_outcome_repository.register_source(
            owner_id=owner_id,
            assignment_id=assignment_id,
            operation_id=retried_operation.id,
            expected_attempt=retried_operation.attempt,
            order_index=1,
            stored_file_id=second_replacement.id,
            retry_of_source_id=retried_source.id,
        )
