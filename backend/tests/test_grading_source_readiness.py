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
            "type": "concept",
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
    from backend.db.provider_repository import upsert_provider_config
    from backend.db.session import session_scope
    from backend.models import ProviderConfig, TaskGradingSetup
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
        order_index=0, number="1",
        type="concept",
        stem="Question", criterion="Award up to 10 points for the method and result.",
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
    provider = upsert_provider_config(
        owner_id,
        ProviderConfig(provider_type="openai", api_key="test-provider-secret", model="gpt-test", base_url="https://api.openai.com/v1"),
        master_key="test-suite-provider-master-key-0123456789abcdef",
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
    }


def _seed_legacy_structured_case(
    *,
    with_answers: bool = True,
    identity_status: str = "matched",
    recognition_needs_review: bool = False,
    question_count: int = 1,
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
    for index in range(question_count):
        assignment_repository.add_question(
            assignment_id=assignment.id, teacher_id=owner_id,
            q_id=f"q{index + 1}", order_index=index, number=str(index + 1),
            type="concept", stem=f"Question {index + 1}", criterion="Award up to 10 points for the method and result.", max_score=10,
        )
    workflow = workflow_repository.ensure_workflow(
        assignment_id=assignment.id,
        owner_id=owner_id,
    )
    student = _student("", "S001", identity_status=identity_status)
    for index in range(1, question_count):
        student["stu_ans"].append({**student["stu_ans"][0], "q_id": f"q{index + 1}"})
    if recognition_needs_review:
        student["stu_ans"][0]["flag"] = ["answer_recognition_confidence:0.4"]
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
        master_key="test-suite-provider-master-key-0123456789abcdef",
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


def test_source_backed_identity_conflict_blocks_until_explicitly_resolved():
    from backend.services import task_facade
    from backend.db import workflow_repository
    from backend.domain.errors import InvalidTransition

    seeded = _seed_source_case("identity_conflict")
    args = {"task_id": seeded["assignment_id"], "owner_id": seeded["owner_id"]}
    readiness = task_facade.grading_readiness(**args)
    before = task_facade.get_task(**args, full=True)
    assert readiness == {"ready": False, "blocking_issues": ["submission_identities_unresolved"], "warnings": []}
    with pytest.raises(InvalidTransition) as exc:
        task_facade.start_task_grading(**args, expected_workflow_revision=seeded["workflow"].workflow_revision)
    assert exc.value.code == "submission_identities_unresolved"
    assert task_facade.get_task(**args, full=True)["student_data"] == before["student_data"]

    resolved = task_facade.update_student_identity(
        **args, current_display_id="S002", new_display_id="S002", new_display_name="Student S002",
        expected_revision=before["workflow_revision"],
    )
    assert task_facade.grading_readiness(**args)["blocking_issues"] == []
    started = task_facade.start_task_grading(**args, expected_workflow_revision=resolved["workflow_revision"])

    assert started["status"] == "started"
    after = task_facade.get_task(**args, full=True)
    assert after["student_data"]["S002"]["identity_status"] == "matched"
    assert after["submission_sources"][1]["status"] == "parsed"
    frozen = workflow_repository.get_run_setup(started["job_id"])
    assert len(frozen.input_manifest["submission_revision_ids"]) == 2
    assert sorted(item["identity_status"] for item in frozen.input_manifest["student_presentations"]) == ["matched", "matched"]


def test_identity_placeholder_cannot_be_bulk_confirmed():
    from backend.domain.errors import ValidationError
    from backend.services import task_facade

    seeded = _seed_source_case("identity_conflict")
    args = {"task_id": seeded["assignment_id"], "owner_id": seeded["owner_id"]}
    before = task_facade.get_task(**args, full=True)
    with pytest.raises(ValidationError) as exc:
        task_facade.update_student_identity(
            **args, current_display_id="S002", new_display_id="S002", new_display_name="[Unknown Student]",
            expected_revision=before["workflow_revision"],
        )
    assert exc.value.code == "student_identity_placeholder"
    assert "[Unknown Student]" in str(exc.value)
    assert task_facade.get_task(**args, full=True) == before


@pytest.mark.parametrize("name", ["Unknown Student", "unknown student", "[unknown student]", "未知学生"])
def test_teacher_edited_identity_is_not_fuzzy_matched_to_placeholder(name):
    from backend.services import task_facade

    seeded = _seed_source_case("identity_conflict")
    args = {"task_id": seeded["assignment_id"], "owner_id": seeded["owner_id"]}
    before = task_facade.get_task(**args, full=True)
    result = task_facade.update_student_identity(
        **args, current_display_id="S002", new_display_id="renamed",
        new_display_name=name, expected_revision=before["workflow_revision"],
    )
    after = task_facade.get_task(**args, full=True)
    assert result["previous_student_id"] == "S002"
    assert result["student"]["stu_name"] == name
    assert "S002" not in after["student_data"]
    assert after["student_data"]["renamed"]["stu_ans"] == before["student_data"]["S002"]["stu_ans"]
    assert after["student_data"]["renamed"]["identity_status"] == "matched"
    assert after["workflow_revision"] == before["workflow_revision"] + 1
    assert task_facade.grading_readiness(**args)["blocking_issues"] == []


@pytest.mark.parametrize("student_id,name", [("   ", "Teacher input"), ("S002", "   "), ("[Unknown Student]", "Teacher input")])
def test_identity_rejects_only_empty_fields_or_the_exact_default(student_id, name):
    from backend.domain.errors import ValidationError
    from backend.services import task_facade

    seeded = _seed_source_case("identity_conflict")
    args = {"task_id": seeded["assignment_id"], "owner_id": seeded["owner_id"]}
    before = task_facade.get_task(**args, full=True)
    with pytest.raises(ValidationError) as exc:
        task_facade.update_student_identity(
            **args, current_display_id="S002", new_display_id=student_id,
            new_display_name=name, expected_revision=before["workflow_revision"],
        )
    assert exc.value.code == ("student_identity_placeholder" if student_id == "[Unknown Student]" else "student_identity_required")
    assert task_facade.get_task(**args, full=True) == before


@pytest.mark.parametrize("failure_flag", ["recognition_failed", "parse_failed", "no_matching_answer"])
def test_answer_recognition_failure_requires_repair_and_confirmation(failure_flag):
    from sqlalchemy import select
    from backend.db.models import SubmissionAnswerRecord
    from backend.db.session import session_scope
    from backend.domain.errors import ValidationError
    from backend.services import task_facade

    seeded = _seed_legacy_structured_case()
    args = {"task_id": seeded["assignment_id"], "owner_id": seeded["owner_id"]}
    with session_scope() as session:
        answer = session.scalar(select(SubmissionAnswerRecord))
        answer.content = ""
        answer.flag = [failure_flag]
    before = task_facade.get_task(**args, full=True)
    assert "submission_recognition_blocked" in task_facade.grading_readiness(**args)["blocking_issues"]
    with pytest.raises(ValidationError) as exc:
        task_facade.update_student_answer(
            **args, display_student_id="S001", q_id="q1",
            patch={"review_status": "confirmed", "flag": []}, expected_revision=before["workflow_revision"],
        )
    assert exc.value.code == "answer_review_blocked"
    assert task_facade.get_task(**args, full=True)["workflow_revision"] == before["workflow_revision"]
    repaired = task_facade.update_student_answer(
        **args, display_student_id="S001", q_id="q1",
        patch={"content": "Verified from the original", "review_status": "edited"},
        expected_revision=before["workflow_revision"],
    )
    assert failure_flag not in repaired["answer"]["flag"]
    assert "recognition_repaired_needs_confirmation" in repaired["answer"]["flag"]
    assert "submission_recognition_blocked" in task_facade.grading_readiness(**args)["blocking_issues"]
    still_pending = task_facade.update_student_answer(
        **args, display_student_id="S001", q_id="q1", patch={"flag": []},
        expected_revision=repaired["workflow_revision"],
    )
    assert "recognition_repaired_needs_confirmation" in still_pending["answer"]["flag"]
    assert "submission_recognition_blocked" in task_facade.grading_readiness(**args)["blocking_issues"]
    task_facade.update_student_answer(
        **args, display_student_id="S001", q_id="q1", patch={"review_status": "confirmed"},
        expected_revision=still_pending["workflow_revision"],
    )
    assert task_facade.grading_readiness(**args)["blocking_issues"] == []


def test_true_blank_answer_without_failure_evidence_is_not_recognition_failure():
    from sqlalchemy import select
    from backend.db.models import SubmissionAnswerRecord
    from backend.db.session import session_scope
    from backend.services import task_facade

    seeded = _seed_legacy_structured_case()
    with session_scope() as session:
        session.scalar(select(SubmissionAnswerRecord)).content = ""
    assert task_facade.grading_readiness(
        task_id=seeded["assignment_id"], owner_id=seeded["owner_id"],
    )["blocking_issues"] == []


@pytest.mark.parametrize("condition", ["empty_stem", "blocking_issue", "rubric_mismatch"])
def test_unusable_question_inputs_cannot_start_grading(condition):
    from sqlalchemy import select
    from backend.db.models import AssignmentQuestionRecord
    from backend.db.session import session_scope
    from backend.domain.errors import InvalidTransition
    from backend.services import task_facade
    from backend.services.question_structure import build_major_question_structure

    seeded = _seed_legacy_structured_case()
    args = {"task_id": seeded["assignment_id"], "owner_id": seeded["owner_id"]}
    with session_scope() as session:
        question = session.scalar(select(AssignmentQuestionRecord))
        if condition == "empty_stem":
            question.stem = " "
        elif condition == "blocking_issue":
            question.source = {"presentation": {"preparation_issues": [{
                "issue_id": "failed", "field": "stem", "severity": "blocking",
                "status": "open", "code": "generation_failed",
            }]}}
        else:
            question.stem = "Question 1. (a) First subpart. (b) Second subpart."
            question.criterion = "(a) 2 分\n(b) 3 分"
            question.source = {"presentation": {"question_structure": build_major_question_structure(
                {"number": "1", "stem": question.stem}, major_order=0,
            ).model_dump()}}
    assert "question_review_blocked" in task_facade.grading_readiness(**args)["blocking_issues"]
    with pytest.raises(InvalidTransition) as exc:
        task_facade.start_task_grading(**args, expected_workflow_revision=seeded["workflow"].workflow_revision)
    assert exc.value.code == "question_review_blocked"
    with pytest.raises(InvalidTransition) as upload_exc:
        task_facade.queue_task_submission_parsing(
            **args, filename="answer.txt", content=b"answer", content_type="text/plain", registry=None,
            expected_workflow_revision=seeded["workflow"].workflow_revision,
        )
    assert upload_exc.value.code == "question_review_blocked"


def test_missing_question_repair_requires_confirmation_after_save():
    from sqlalchemy import select
    from backend.db.models import AssignmentQuestionRecord
    from backend.db.session import session_scope
    from backend.services import task_facade

    seeded = _seed_legacy_structured_case()
    args = {"task_id": seeded["assignment_id"], "owner_id": seeded["owner_id"]}
    with session_scope() as session:
        session.scalar(select(AssignmentQuestionRecord)).stem = ""
    repaired = task_facade.update_problem(
        **args, q_id="q1", patch={"stem": "Verified question text"},
        expected_revision=seeded["workflow"].workflow_revision,
    )
    assert repaired["problem"]["review_status"] == "needs_review"
    assert "question_review_blocked" in task_facade.grading_readiness(**args)["blocking_issues"]
    still_pending = task_facade.update_problem(
        **args, q_id="q1", patch={"preparation_issues": []},
        expected_revision=repaired["workflow_revision"],
    )
    assert "question_review_blocked" in task_facade.grading_readiness(**args)["blocking_issues"]
    task_facade.update_problem(
        **args, q_id="q1", patch={"review_status": "confirmed"},
        expected_revision=still_pending["workflow_revision"],
    )
    assert "question_review_blocked" not in task_facade.grading_readiness(**args)["blocking_issues"]


def test_grade_endpoint_accepts_uncertain_answers_and_preserves_pending_review():
    from fastapi.testclient import TestClient
    from backend.auth import create_token
    from backend.main import app
    from backend.services import task_facade

    seeded = _seed_legacy_structured_case(recognition_needs_review=True)
    args = {"task_id": seeded["assignment_id"], "owner_id": seeded["owner_id"]}
    before = task_facade.get_task(**args, full=True)
    readiness = task_facade.grading_readiness(**args)
    assert readiness == {"ready": True, "blocking_issues": [], "warnings": ["submission_recognition_needs_review"]}

    response = TestClient(app).post(
        f"/tasks/{seeded['assignment_id']}/grade",
        headers={"Authorization": f"Bearer {create_token(seeded['owner_id'], 'teacher')}"},
        json={"expected_workflow_revision": seeded["workflow"].workflow_revision},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "started"
    after = task_facade.get_task(**args, full=True)
    assert after["student_data"] == before["student_data"]
    assert after["problem_data"] == before["problem_data"]
    assert after["problem_data"]["q1"]["review_status"] == "needs_review"
    answer = next(iter(after["student_data"].values()))["stu_ans"][0]
    assert answer["review_status"] == "pending"
    assert answer["flag"] == []
    assert answer["recognition_confidence"] == .4


@pytest.mark.parametrize("flags,expected_warnings", [
    ([], []),
    (["recognition_needs_review", "recognition_low_confidence"], []),
    (["recognition_confidence:0.1"], []),
    (["answer_recognition_confidence:0.6"], []),
    (["answer_recognition_confidence:0.4"], ["submission_recognition_needs_review"]),
])
def test_grading_readiness_confidence_warning_uses_only_individual_answer_score(flags, expected_warnings):
    from sqlalchemy import select
    from backend.db.models import SubmissionAnswerRecord
    from backend.db.session import session_scope
    from backend.services import task_facade

    seeded = _seed_legacy_structured_case()
    args = {"task_id": seeded["assignment_id"], "owner_id": seeded["owner_id"]}
    with session_scope() as session:
        session.scalar(select(SubmissionAnswerRecord)).flag = flags
    assert task_facade.grading_readiness(**args) == {
        "ready": True, "blocking_issues": [], "warnings": expected_warnings,
    }
    before = task_facade.get_task(**args, full=True)
    task_facade.update_student_answer(
        **args, display_student_id="S001", q_id="q1",
        patch={"review_status": "confirmed"}, expected_revision=before["workflow_revision"],
    )
    assert task_facade.grading_readiness(**args)["warnings"] == []


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

    assert readiness["blocking_issues"] == ["submission_recognition_blocked"]
    with pytest.raises(InvalidTransition) as exc:
        task_facade.start_task_grading(
            task_id=seeded["assignment_id"],
            owner_id=seeded["owner_id"],
            expected_workflow_revision=seeded["workflow"].workflow_revision,
        )
    assert exc.value.code == "submission_recognition_blocked"
    assert seeded["grading_repository"].list_runs_for_assignment(
        seeded["assignment_id"],
        actor_id=seeded["owner_id"],
    ) == []


def test_legacy_needs_review_without_source_id_does_not_block_saved_answers():
    from backend.services import task_facade

    seeded = _seed_legacy_structured_case(identity_status="needs_review")
    readiness = task_facade.grading_readiness(
        task_id=seeded["assignment_id"],
        owner_id=seeded["owner_id"],
    )

    assert readiness == {
        "ready": True,
        "blocking_issues": [],
        "warnings": [],
    }
    started = task_facade.start_task_grading(
        task_id=seeded["assignment_id"],
        owner_id=seeded["owner_id"],
        expected_workflow_revision=seeded["workflow"].workflow_revision,
    )
    assert started["status"] == "started"


def test_changed_grading_setup_stays_revision_locked_during_active_grading():
    from fastapi.testclient import TestClient

    from backend.auth import create_token
    from backend.main import app
    from backend.services import task_facade

    seeded = _seed_legacy_structured_case()
    task_facade.start_task_grading(
        task_id=seeded["assignment_id"],
        owner_id=seeded["owner_id"],
        expected_workflow_revision=seeded["workflow"].workflow_revision,
    )
    locked = seeded["workflow_repository"].get_workflow(
        seeded["assignment_id"], owner_id=seeded["owner_id"]
    )
    assert locked.active_operation == "grading"

    client = TestClient(app)
    headers = {
        "Authorization": f"Bearer {create_token(seeded['owner_id'], 'teacher')}"
    }
    payload = client.get(
        f"/tasks/{seeded['assignment_id']}/grading-setup", headers=headers
    )
    assert payload.status_code == 200
    assert "grading_setup_locked" in payload.json()["readiness"]["blocking_issues"]

    changed_setup = {**locked.grading_setup, "teacher_notes": "must not save"}
    response = client.put(
        f"/tasks/{seeded['assignment_id']}/grading-setup",
        headers=headers,
        json={
            "expected_workflow_revision": locked.workflow_revision,
            "grading_setup": changed_setup,
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "grading_setup_locked"
    unchanged = seeded["workflow_repository"].get_workflow(
        seeded["assignment_id"], owner_id=seeded["owner_id"]
    )
    assert unchanged.workflow_revision == locked.workflow_revision
    assert unchanged.grading_setup == locked.grading_setup


def test_changed_grading_setup_stays_revision_locked_while_workflow_is_busy():
    from fastapi.testclient import TestClient

    from backend.auth import create_token
    from backend.main import app

    seeded = _seed_legacy_structured_case()
    operation, _created = seeded["workflow_repository"].create_operation(
        assignment_id=seeded["assignment_id"],
        owner_id=seeded["owner_id"],
        operation_type="submission_recognition",
        input_hash="b" * 64,
    )
    seeded["workflow_repository"].update_operation(
        operation.id,
        owner_id=seeded["owner_id"],
        expected_attempt=operation.attempt,
        status="running",
    )
    busy = seeded["workflow_repository"].update_workflow(
        seeded["assignment_id"],
        owner_id=seeded["owner_id"],
        active_operation="submission_recognition",
        active_job_id=operation.id,
    )

    changed_setup = {**busy.grading_setup, "teacher_notes": "must not save"}
    response = TestClient(app).put(
        f"/tasks/{seeded['assignment_id']}/grading-setup",
        headers={
            "Authorization": f"Bearer {create_token(seeded['owner_id'], 'teacher')}"
        },
        json={
            "expected_workflow_revision": busy.workflow_revision,
            "grading_setup": changed_setup,
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "workflow_busy"
    unchanged = seeded["workflow_repository"].get_workflow(
        seeded["assignment_id"], owner_id=seeded["owner_id"]
    )
    assert unchanged.workflow_revision == busy.workflow_revision
    assert unchanged.grading_setup == busy.grading_setup


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
