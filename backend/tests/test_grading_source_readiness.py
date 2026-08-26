from __future__ import annotations

import pytest


def _student(source_id: str, student_id: str, *, identity_status: str = "matched") -> dict:
    return {
        "stu_id": student_id,
        "stu_name": f"Student {student_id}",
        "source_id": source_id,
        "source_filename": f"{student_id}.txt",
        "identity_match_method": "filename",
        "identity_status": identity_status,
        "stu_ans": [{
            "q_id": "q1",
            "number": "1",
            "type": "short",
            "content": "answer",
            "flag": [],
        }],
    }


def _seed_source_case(second_status: str | None):
    from backend.db import (
        assignment_repository,
        course_repository,
        grading_repository,
        source_outcome_repository,
        workflow_repository,
    )
    from backend.db.file_repository import save_file
    from backend.db.models import UserRecord
    from backend.db.session import session_scope
    from backend.models import TaskGradingSetup
    from backend.services import task_facade
    from backend.storage import get_storage

    owner_id = "readiness-owner"
    with session_scope() as session:
        session.add(UserRecord(
            id=owner_id,
            username=owner_id,
            password_hash="hash",
            role="teacher",
            is_active=True,
        ))
    course = course_repository.create_course(
        teacher_id=owner_id,
        name="Readiness",
    )
    assignment = assignment_repository.create_assignment(
        teacher_id=owner_id,
        course_id=course.id,
        name="Readiness",
    )
    assignment_repository.add_question(
        assignment_id=assignment.id,
        teacher_id=owner_id,
        q_id="q1",
        order_index=0,
        type="short",
        stem="Question",
        max_score=10,
    )
    workflow = workflow_repository.ensure_workflow(
        assignment_id=assignment.id,
        owner_id=owner_id,
    )
    operation, _created = workflow_repository.create_operation(
        assignment_id=assignment.id,
        owner_id=owner_id,
        operation_type="submission_recognition",
        input_hash="a" * 64,
    )
    workflow_repository.update_operation(
        operation.id,
        owner_id=owner_id,
        expected_attempt=operation.attempt,
        status="running",
    )
    workflow = workflow_repository.update_workflow(
        assignment.id,
        owner_id=owner_id,
        parse_job_id=operation.id,
        active_operation="submission_recognition",
        active_job_id=operation.id,
    )

    def create_source(order_index: int, student_id: str):
        stored = save_file(
            storage=get_storage(),
            owner_id=owner_id,
            kind="submission_source",
            original_name=f"{student_id}.txt",
            content=b"answer",
            content_type="text/plain",
            assignment_id=assignment.id,
        )
        source, _ = source_outcome_repository.register_source(
            owner_id=owner_id,
            assignment_id=assignment.id,
            operation_id=operation.id,
            expected_attempt=operation.attempt,
            order_index=order_index,
            stored_file_id=stored.id,
        )
        return source

    first = create_source(0, "S001")
    source_outcome_repository.record_outcome(
        source_id=first.id,
        owner_id=owner_id,
        status="parsed",
        student_candidate="S001",
        matched_answer_count=1,
        unknown_question_ids=[],
        stable_error_code=None,
        failure_phase=None,
        retryable=False,
    )
    students = [_student(first.id, "S001")]

    if second_status is not None:
        second = create_source(1, "S002")
        if second_status == "parse_failed":
            source_outcome_repository.record_outcome(
                source_id=second.id,
                owner_id=owner_id,
                status="parse_failed",
                student_candidate=None,
                matched_answer_count=0,
                unknown_question_ids=[],
                stable_error_code="submission_parse_failed",
                failure_phase="recognition",
                retryable=True,
            )
        elif second_status == "identity_conflict":
            source_outcome_repository.record_outcome(
                source_id=second.id,
                owner_id=owner_id,
                status="identity_conflict",
                student_candidate="S002",
                matched_answer_count=1,
                unknown_question_ids=[],
                stable_error_code="identity_needs_review",
                failure_phase="identity",
                retryable=False,
            )
            students.append(_student(
                second.id,
                "S002",
                identity_status="needs_review",
            ))
        elif second_status != "pending":
            raise AssertionError(f"unsupported fixture status: {second_status}")

    task_facade._commit_imported_submissions(
        task_id=assignment.id,
        owner_id=owner_id,
        course_id=course.id,
        students=students,
        expected_workflow_revision=workflow.workflow_revision,
        operation_id=operation.id,
        expected_operation_attempt=operation.attempt,
        submission_file_name="submissions.zip",
    )
    if second_status == "pending":
        # Model an interrupted worker whose operation is still non-terminal
        # even though an earlier successful source is already visible.
        workflow_repository.update_operation(
            operation.id,
            owner_id=owner_id,
            expected_attempt=operation.attempt,
            status="running",
        )
    setup = TaskGradingSetup(
        selected_provider_ids=["provider"],
        primary_provider_id="provider",
        knowledge_scope="none",
    )
    workflow = workflow_repository.update_workflow(
        assignment.id,
        owner_id=owner_id,
        grading_setup=setup.model_dump(mode="json"),
        grading_setup_fingerprint="approved",
    )
    return {
        "owner_id": owner_id,
        "assignment_id": assignment.id,
        "workflow": workflow,
        "grading_repository": grading_repository,
    }


def _seed_legacy_structured_case(
    *,
    with_answers: bool = True,
    identity_status: str = "matched",
):
    from backend.db import (
        assignment_repository,
        course_repository,
        grading_repository,
        submission_repository,
        workflow_repository,
    )
    from backend.db.models import UserRecord
    from backend.db.provider_repository import upsert_provider_config
    from backend.db.session import session_scope
    from backend.models import ProviderConfig, TaskGradingSetup
    from backend.services import task_facade

    owner_id = "legacy-readiness-owner"
    with session_scope() as session:
        session.add(UserRecord(
            id=owner_id,
            username=owner_id,
            password_hash="hash",
            role="teacher",
            is_active=True,
        ))
    course = course_repository.create_course(
        teacher_id=owner_id,
        name="Legacy readiness",
    )
    assignment = assignment_repository.create_assignment(
        teacher_id=owner_id,
        course_id=course.id,
        name="Legacy readiness",
    )
    assignment_repository.add_question(
        assignment_id=assignment.id,
        teacher_id=owner_id,
        q_id="q1",
        order_index=0,
        type="short",
        stem="Question",
        max_score=10,
    )
    workflow = workflow_repository.ensure_workflow(
        assignment_id=assignment.id,
        owner_id=owner_id,
    )
    student = _student("", "S001", identity_status=identity_status)
    if not with_answers:
        student["stu_ans"] = []
    task_facade._commit_imported_submissions(
        task_id=assignment.id,
        owner_id=owner_id,
        course_id=course.id,
        students=[student],
        expected_workflow_revision=workflow.workflow_revision,
        submission_file_name=None,
    )
    provider = upsert_provider_config(
        owner_id,
        ProviderConfig(
            provider_type="openai",
            api_key="legacy-provider-secret",
            model="gpt-test",
            base_url="https://api.openai.com/v1",
        ),
        master_key="test-suite-provider-master-key",
    )
    setup = TaskGradingSetup(
        selected_provider_ids=[provider.id],
        primary_provider_id=provider.id,
        knowledge_scope="none",
    )
    workflow = workflow_repository.update_workflow(
        assignment.id,
        owner_id=owner_id,
        grading_setup=setup.model_dump(mode="json"),
        grading_setup_fingerprint="approved",
    )
    return {
        "owner_id": owner_id,
        "assignment_id": assignment.id,
        "workflow": workflow,
        "grading_repository": grading_repository,
        "submission_repository": submission_repository,
        "workflow_repository": workflow_repository,
    }


@pytest.mark.parametrize(
    ("second_status", "blocker"),
    [
        ("parse_failed", "submission_sources_failed"),
        ("pending", "submission_sources_pending"),
        ("identity_conflict", "submission_identities_unresolved"),
    ],
)
def test_grading_source_blockers_fail_closed_without_creating_run(
    second_status,
    blocker,
):
    from backend.domain.errors import InvalidTransition
    from backend.services import task_facade

    seeded = _seed_source_case(second_status)
    readiness = task_facade.grading_readiness(
        task_id=seeded["assignment_id"],
        owner_id=seeded["owner_id"],
    )

    assert blocker in readiness["blocking_issues"]
    if second_status == "identity_conflict":
        assert "submission_source_evidence_missing" not in readiness["blocking_issues"]
    with pytest.raises(InvalidTransition) as exc:
        task_facade.start_task_grading(
            task_id=seeded["assignment_id"],
            owner_id=seeded["owner_id"],
            expected_workflow_revision=seeded["workflow"].workflow_revision,
        )
    assert exc.value.code == blocker
    assert seeded["grading_repository"].list_runs_for_assignment(
        seeded["assignment_id"],
        actor_id=seeded["owner_id"],
    ) == []


def test_stale_preflight_revision_cannot_create_grading_run():
    from backend.domain.errors import VersionConflict
    from backend.services import task_facade

    seeded = _seed_source_case(None)
    with pytest.raises(VersionConflict) as exc:
        task_facade.start_task_grading(
            task_id=seeded["assignment_id"],
            owner_id=seeded["owner_id"],
            expected_workflow_revision=seeded["workflow"].workflow_revision - 1,
        )

    assert exc.value.code == "version_conflict"
    assert seeded["grading_repository"].list_runs_for_assignment(
        seeded["assignment_id"],
        actor_id=seeded["owner_id"],
    ) == []


def test_legacy_structured_inputs_without_source_evidence_can_start_through_grade_endpoint():
    from fastapi.testclient import TestClient

    from backend.auth import create_token
    from backend.main import app
    from backend.services import task_facade

    seeded = _seed_legacy_structured_case()
    readiness = task_facade.grading_readiness(
        task_id=seeded["assignment_id"],
        owner_id=seeded["owner_id"],
    )

    assert readiness == {
        "ready": True,
        "blocking_issues": [],
        "warnings": [],
    }
    response = TestClient(app).post(
        f"/tasks/{seeded['assignment_id']}/grade",
        headers={
            "Authorization": (
                f"Bearer {create_token(seeded['owner_id'], 'teacher')}"
            )
        },
        json={
            "expected_workflow_revision": seeded["workflow"].workflow_revision,
        },
    )
    assert response.status_code == 200
    started = response.json()
    assert started["status"] == "started"

    frozen = seeded["workflow_repository"].get_run_setup(started["job_id"])
    submissions = seeded["submission_repository"].list_submissions(
        seeded["assignment_id"],
        actor_id=seeded["owner_id"],
    )
    assert frozen is not None
    assert frozen.input_manifest["submission_revision_ids"] == [
        submissions[0].current_revision_id
    ]


def test_structured_submission_without_answers_remains_blocked():
    from backend.domain.errors import InvalidTransition
    from backend.services import task_facade

    seeded = _seed_legacy_structured_case(with_answers=False)
    readiness = task_facade.grading_readiness(
        task_id=seeded["assignment_id"],
        owner_id=seeded["owner_id"],
    )

    assert readiness["blocking_issues"] == ["answers_required"]
    with pytest.raises(InvalidTransition) as exc:
        task_facade.start_task_grading(
            task_id=seeded["assignment_id"],
            owner_id=seeded["owner_id"],
            expected_workflow_revision=seeded["workflow"].workflow_revision,
        )
    assert exc.value.code == "answers_required"
    assert seeded["grading_repository"].list_runs_for_assignment(
        seeded["assignment_id"],
        actor_id=seeded["owner_id"],
    ) == []


def test_legacy_unresolved_identity_remains_blocked_without_source_id():
    from backend.services import task_facade

    seeded = _seed_legacy_structured_case(identity_status="needs_review")
    readiness = task_facade.grading_readiness(
        task_id=seeded["assignment_id"],
        owner_id=seeded["owner_id"],
    )

    assert readiness["blocking_issues"] == ["submission_identities_unresolved"]


def test_projection_sanitizes_legacy_unsafe_diagnostic():
    from backend.db import source_outcome_repository
    from backend.db.session import session_scope
    from backend.services import task_facade

    seeded = _seed_source_case(None)
    task = task_facade.get_task(
        task_id=seeded["assignment_id"],
        owner_id=seeded["owner_id"],
        full=True,
    )
    source_id = task["submission_sources"][0]["source_id"]
    with session_scope() as session:
        row = session.get(
            source_outcome_repository.WorkflowSourceOutcomeRecord,
            source_id,
        )
        assert row is not None
        row.status = "parse_failed"
        row.stable_error_code = "raw-provider-secret-message"
        row.failure_phase = "raw-stack-trace"

    projected = task_facade.get_task(
        task_id=seeded["assignment_id"],
        owner_id=seeded["owner_id"],
        full=True,
    )["submission_sources"][0]
    assert projected["status"] == "failed"
    assert projected["reason_code"] == "submission_parse_failed"
    assert projected["failure_phase"] == "recognition"
    assert "raw" not in str(projected)
