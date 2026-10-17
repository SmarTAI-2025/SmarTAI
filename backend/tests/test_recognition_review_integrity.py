"""Recognition failures must survive projection, confirmation and grading gates."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from backend.db.models import AssignmentQuestionRecord, SubmissionAnswerRecord
from backend.db.session import session_scope
from backend.domain.errors import InvalidTransition, ValidationError
from backend.services import task_facade
from backend.tests.test_grading_source_readiness import (
    _seed_legacy_structured_case,
    _seed_source_case,
)


def _case(*, flags, content="Visible student work", source_backed=False):
    seeded = _seed_source_case(None) if source_backed else _seed_legacy_structured_case()
    with session_scope() as session:
        answer = session.scalar(select(SubmissionAnswerRecord))
        answer.content = content
        answer.flag = list(flags)
    return seeded, {"task_id": seeded["assignment_id"], "owner_id": seeded["owner_id"]}


@pytest.mark.parametrize("source_backed", [False, True], ids=["legacy", "source-backed"])
@pytest.mark.parametrize("content", ["", "First page contains a real answer"], ids=["empty", "nonempty"])
@pytest.mark.parametrize("page_flag", ["recognition_failed_pages:2", "recognition_unprocessed_pages:2"])
def test_historical_partial_recognition_is_projected_and_cannot_start_grading(source_backed, content, page_flag):
    seeded, args = _case(
        flags=["recognition_needs_review", page_flag],
        content=content, source_backed=source_backed,
    )
    task = task_facade.get_task(**args, full=True)
    flags = task["student_data"]["S001"]["stu_ans"][0]["flag"]
    assert page_flag in flags
    assert "recognition_failed" in flags  # Existing clients understand this failure flag.
    readiness = task_facade.grading_readiness(**args)
    assert readiness["ready"] is False
    assert "submission_recognition_blocked" in readiness["blocking_issues"]
    with pytest.raises(InvalidTransition):
        task_facade.start_task_grading(**args, expected_workflow_revision=task["workflow_revision"])
    assert seeded["grading_repository"].list_runs_for_assignment(
        seeded["assignment_id"], actor_id=seeded["owner_id"],
    ) == []


@pytest.mark.parametrize("page_flag", ["recognition_failed_pages:2", "recognition_unprocessed_pages:2"])
def test_confirmation_or_flag_edits_cannot_erase_unread_page_evidence(page_flag):
    _seeded, args = _case(flags=["recognition_needs_review", page_flag], content="")
    before = task_facade.get_task(**args, full=True)
    with pytest.raises(ValidationError) as exc:
        task_facade.update_student_answer(
            **args, display_student_id="S001", q_id="q1",
            patch={"review_status": "confirmed", "flag": []},
            expected_revision=before["workflow_revision"],
        )
    assert exc.value.code == "answer_review_blocked"
    assert task_facade.get_task(**args, full=True)["workflow_revision"] == before["workflow_revision"]
    changed = task_facade.update_student_answer(
        **args, display_student_id="S001", q_id="q1", patch={"flag": []},
        expected_revision=before["workflow_revision"],
    )
    assert page_flag in changed["answer"]["flag"]
    assert "submission_recognition_blocked" in task_facade.grading_readiness(**args)["blocking_issues"]


def test_formatting_only_edit_does_not_repair_failed_recognition():
    _seeded, args = _case(flags=["recognition_failed_pages:2"])
    before = task_facade.get_task(**args, full=True)
    with pytest.raises(ValidationError):
        task_facade.update_student_answer(**args, display_student_id="S001", q_id="q1",
            patch={"content": "Visible student work\n", "review_status": "confirmed"},
            expected_revision=before["workflow_revision"])


@pytest.mark.parametrize("content", ["", "Visible student work"], ids=["genuine-blank", "nonempty"])
@pytest.mark.parametrize("flags", [[], ["recognition_needs_review"]], ids=["no-flags", "generic-review"])
def test_blank_or_uncalibrated_review_is_not_a_recognition_failure(content, flags):
    _seeded, args = _case(flags=flags, content=content)
    before = task_facade.get_task(**args, full=True)
    assert before["student_data"]["S001"]["stu_ans"][0]["flag"] == flags
    assert task_facade.grading_readiness(**args)["blocking_issues"] == []
    task_facade.update_student_answer(
        **args, display_student_id="S001", q_id="q1", patch={"review_status": "confirmed"},
        expected_revision=before["workflow_revision"],
    )
    assert task_facade.grading_readiness(**args)["ready"] is True


@pytest.mark.parametrize("low_flag", ["very_low_confidence", "recognition_low_confidence", "low_confidence"])
def test_specific_low_confidence_requires_confirmation_but_not_rewriting(low_flag):
    _seeded, args = _case(flags=[low_flag])
    before = task_facade.get_task(**args, full=True)
    assert "submission_recognition_blocked" in task_facade.grading_readiness(**args)["blocking_issues"]
    confirmed = task_facade.update_student_answer(
        **args, display_student_id="S001", q_id="q1", patch={"review_status": "confirmed"},
        expected_revision=before["workflow_revision"],
    )
    assert confirmed["answer"]["content"] == "Visible student work"
    assert low_flag in confirmed["answer"]["flag"]
    assert task_facade.grading_readiness(**args)["blocking_issues"] == []


@pytest.mark.parametrize("coverage", [
    {"failed_pages": [2]},
    {"unprocessed_pages": [2]},
    {"missing_targets": ["1.2.3"]},
])
def test_question_partial_evidence_is_blocking_even_when_stored_as_warning(coverage):
    _seeded, args = _case(flags=[])
    with session_scope() as session:
        question = session.scalar(select(AssignmentQuestionRecord))
        question.source = {"presentation": {"preparation_issues": [{
            "issue_id": "partial-source", "q_id": "q1", "field": "stem",
            "code": "recognition_partial", "status": "open", "severity": "warning",
            "details": {"coverage": coverage},
        }]}}
    before = task_facade.get_task(**args, full=True)
    issues = before["problem_data"]["q1"]["preparation_issues"]
    assert any(item["severity"] == "blocking" for item in issues)
    assert "question_review_blocked" in task_facade.grading_readiness(**args)["blocking_issues"]
    with pytest.raises(ValidationError):
        task_facade.update_problem(
            **args, q_id="q1", patch={"review_status": "confirmed", "preparation_issues": []},
            expected_revision=before["workflow_revision"],
        )
    with pytest.raises(ValidationError):
        task_facade.update_problem(
            **args, q_id="q1", patch={"review_status": "confirmed", "stem": before["problem_data"]["q1"]["stem"] + "\n"},
            expected_revision=before["workflow_revision"],
        )
    assert "question_review_blocked" in task_facade.grading_readiness(**args)["blocking_issues"]


def test_question_unverified_target_is_not_proof_of_missing_content():
    _seeded, args = _case(flags=[])
    with session_scope() as session:
        question = session.scalar(select(AssignmentQuestionRecord))
        question.source = {"presentation": {"preparation_issues": [{
            "issue_id": "unverified-source", "q_id": "q1", "field": "stem",
            "code": "recognition_partial", "status": "open", "severity": "warning",
            "details": {"coverage": {"unverified_targets": ["1.2.3"]}},
        }]}}
    task = task_facade.get_task(**args, full=True)
    assert all(item["severity"] != "blocking" for item in task["problem_data"]["q1"]["preparation_issues"])
    assert task_facade.grading_readiness(**args)["blocking_issues"] == []


def test_question_low_confidence_edit_still_requires_confirmation():
    _seeded, args = _case(flags=[])
    with session_scope() as session:
        question = session.scalar(select(AssignmentQuestionRecord))
        question.source = {"presentation": {"preparation_issues": [{
            "issue_id": "low-source", "q_id": "q1", "field": "stem", "code": "recognition_needs_review",
            "status": "open", "severity": "warning", "details": {"confidence_reasons": ["low_confidence_content"]},
        }]}}
    before = task_facade.get_task(**args, full=True)
    task_facade.update_problem(**args, q_id="q1", patch={"stem": "Corrected question text", "review_status": "edited"},
                               expected_revision=before["workflow_revision"])
    assert "question_review_blocked" in task_facade.grading_readiness(**args)["blocking_issues"]
    current = task_facade.get_task(**args, full=True)
    task_facade.update_problem(**args, q_id="q1", patch={"preparation_issues": []},
                               expected_revision=current["workflow_revision"])
    assert "question_review_blocked" in task_facade.grading_readiness(**args)["blocking_issues"]
    current = task_facade.get_task(**args, full=True)
    task_facade.update_problem(**args, q_id="q1", patch={"review_status": "confirmed"},
                               expected_revision=current["workflow_revision"])
    assert "question_review_blocked" not in task_facade.grading_readiness(**args)["blocking_issues"]
