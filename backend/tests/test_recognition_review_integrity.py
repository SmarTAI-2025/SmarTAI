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


@pytest.mark.parametrize("content", ["", " \n\t "])
@pytest.mark.parametrize("flags", [["recognition_manual_pending"], ["recognition_failed"],
                                  ["recognition_failed_pages:2", "recognition_incomplete"]])
def test_individual_blank_confirmation_clears_required_failure_but_preserves_evidence(content, flags):
    _seeded, args = _case(flags=flags, content="")
    before = task_facade.get_task(**args, full=True)
    assert "submission_recognition_blocked" in task_facade.grading_readiness(**args)["blocking_issues"]
    result = task_facade.update_student_answer(**args, display_student_id="S001", q_id="q1",
        patch={"content": content, "flag": [*flags, "manual_blank_confirmed"], "review_status": "confirmed"},
        expected_revision=before["workflow_revision"])
    answer = result["answer"]
    assert answer["content"] == "" and answer["review_status"] == "confirmed"
    assert answer["flag"] == ["manual_blank_confirmed"]
    assert task_facade.grading_readiness(**args)["ready"]
    after = task_facade.get_task(**args, full=True)
    assert after["student_data"]["S001"]["stu_ans"][0]["review_status"] == "confirmed"


@pytest.mark.parametrize("content", ["", "Visible student work"], ids=["genuine-blank", "nonempty"])
@pytest.mark.parametrize("flags", [[], ["recognition_needs_review"]], ids=["no-flags", "generic-review"])
def test_blank_or_uncalibrated_review_is_not_a_recognition_failure(content, flags):
    _seeded, args = _case(flags=flags, content=content)
    before = task_facade.get_task(**args, full=True)
    assert before["student_data"]["S001"]["stu_ans"][0]["flag"] == []
    assert task_facade.grading_readiness(**args)["blocking_issues"] == []
    task_facade.update_student_answer(
        **args, display_student_id="S001", q_id="q1", patch={"review_status": "confirmed"},
        expected_revision=before["workflow_revision"],
    )
    assert task_facade.grading_readiness(**args)["ready"] is True


@pytest.mark.parametrize("low_flag", ["very_low_confidence", "recognition_low_confidence", "low_confidence"])
def test_uncalibrated_low_labels_do_not_require_confirmation(low_flag):
    _seeded, args = _case(flags=[low_flag])
    before = task_facade.get_task(**args, full=True)
    assert task_facade.grading_readiness(**args)["ready"] is True
    confirmed = task_facade.update_student_answer(
        **args, display_student_id="S001", q_id="q1", patch={"review_status": "confirmed"},
        expected_revision=before["workflow_revision"],
    )
    assert confirmed["answer"]["content"] == "Visible student work"
    assert low_flag not in confirmed["answer"]["flag"]
    assert task_facade.grading_readiness(**args)["blocking_issues"] == []


def test_numeric_very_low_answer_is_advisory_and_can_be_confirmed_without_rewriting():
    _seeded, args = _case(flags=["recognition_very_low_confidence"])
    before = task_facade.get_task(**args, full=True)
    assert "submission_recognition_blocked" not in task_facade.grading_readiness(**args)["blocking_issues"]
    task_facade.update_student_answer(**args, display_student_id="S001", q_id="q1",
        patch={"review_status": "confirmed"}, expected_revision=before["workflow_revision"])
    assert task_facade.grading_readiness(**args)["ready"]


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


def test_question_low_confidence_edit_remains_advisory():
    _seeded, args = _case(flags=[])
    with session_scope() as session:
        question = session.scalar(select(AssignmentQuestionRecord))
        question.source = {"presentation": {"preparation_issues": [{
            "issue_id": "low-source", "q_id": "q1", "field": "stem", "code": "recognition_needs_review",
            "status": "open", "severity": "warning", "details": {"confidence": 0.2},
        }]}}
    before = task_facade.get_task(**args, full=True)
    task_facade.update_problem(**args, q_id="q1", patch={"stem": "Corrected question text", "review_status": "edited"},
                               expected_revision=before["workflow_revision"])
    assert "question_review_blocked" not in task_facade.grading_readiness(**args)["blocking_issues"]
    current = task_facade.get_task(**args, full=True)
    task_facade.update_problem(**args, q_id="q1", patch={"preparation_issues": []},
                               expected_revision=current["workflow_revision"])
    assert "question_review_blocked" not in task_facade.grading_readiness(**args)["blocking_issues"]
    current = task_facade.get_task(**args, full=True)
    task_facade.update_problem(**args, q_id="q1", patch={"review_status": "confirmed"},
                               expected_revision=current["workflow_revision"])
    assert "question_review_blocked" not in task_facade.grading_readiness(**args)["blocking_issues"]


@pytest.mark.parametrize("confidence,required", [(None, False), ("low", False), (.2499, False), (.25, False), (.5999, False), (.6, False)])
def test_source_confidence_reaches_persisted_question_gate(confidence, required):
    from backend.services.question_sources import attach_recognition_review
    _seeded, args = _case(flags=[])
    packages = {"q1": {}}
    attach_recognition_review(packages, [{"recognition": {"operation_id": "source", "confidence": confidence, "coverage": {}}}])
    with session_scope() as session:
        question = session.scalar(select(AssignmentQuestionRecord))
        question.source = {"presentation": packages["q1"]}
    assert ("question_review_blocked" in task_facade.grading_readiness(**args)["blocking_issues"]) is required


def test_source_failure_is_required_even_with_high_confidence():
    from backend.services.question_sources import attach_recognition_review
    _seeded, args = _case(flags=[])
    packages = {"q1": {}}
    attach_recognition_review(packages, [{"recognition": {"operation_id": "source", "confidence": .99, "error_code": "provider_unreachable", "coverage": {}}}])
    with session_scope() as session:
        question = session.scalar(select(AssignmentQuestionRecord))
        question.source = {"presentation": packages["q1"]}
    assert "question_review_blocked" in task_facade.grading_readiness(**args)["blocking_issues"]


def test_recognition_confidence_is_numeric_metadata_and_survives_teacher_edit():
    _seeded, args = _case(flags=["answer_recognition_confidence:0.72"])
    before = task_facade.get_task(**args, full=True)
    answer = before["student_data"]["S001"]["stu_ans"][0]
    assert answer["recognition_confidence"] == .72
    assert not any(flag.startswith("answer_recognition_confidence:") for flag in answer["flag"])
    changed = task_facade.update_student_answer(**args, display_student_id="S001", q_id="q1",
        patch={"content": "Teacher transcribed content", "flag": [], "review_status": "confirmed"},
        expected_revision=before["workflow_revision"])
    assert changed["answer"]["recognition_confidence"] == .72 and changed["answer"]["flag"] == []
    assert task_facade.get_task(**args, full=True)["student_data"]["S001"]["stu_ans"][0]["recognition_confidence"] == .72


def test_old_blocking_low_confidence_and_missing_answer_are_advisory_but_empty_rubric_blocks():
    _seeded, args = _case(flags=[])
    with session_scope() as session:
        question = session.scalar(select(AssignmentQuestionRecord))
        question.reference_answer = ""
        question.source = {"presentation": {"preparation_issues": [
            {"issue_id": "low", "field": "stem", "code": "low_confidence", "status": "open", "severity": "blocking", "details": {"confidence": .1}},
            {"issue_id": "answer", "field": "answer", "code": "generation_failed", "status": "open", "severity": "blocking"},
            {"issue_id": "answer-partial", "field": "answer", "code": "recognition_partial", "status": "open", "severity": "blocking", "details": {"coverage": {"failed_pages": [2]}}},
            {"issue_id": "manual", "field": "source", "code": "manual_completion_required", "status": "open", "severity": "warning", "details": {"required_fields": ["reference_answer"]}},
        ]}}
    before = task_facade.get_task(**args, full=True)
    assert task_facade.grading_readiness(**args)["ready"]
    confirmed = task_facade.update_problem(**args, q_id="q1", patch={"review_status": "confirmed", "review_fields": ["stem"]}, expected_revision=before["workflow_revision"])
    assert confirmed["problem"]["review_status"] == "confirmed"
    assert all(issue["status"] == "acknowledged" for issue in confirmed["problem"]["preparation_issues"])
    with session_scope() as session:
        question = session.scalar(select(AssignmentQuestionRecord))
        question.criterion = ""
    current = task_facade.get_task(**args, full=True)
    assert not task_facade.grading_readiness(**args)["ready"]
    with pytest.raises(ValidationError):
        task_facade.update_problem(**args, q_id="q1", patch={"review_status": "confirmed"}, expected_revision=current["workflow_revision"])


def test_import_fills_omitted_question_but_preserves_explicit_blank_answer():
    from backend.db import assignment_repository
    seeded = _seed_legacy_structured_case(question_count=2)
    args = {"task_id": seeded["assignment_id"], "owner_id": seeded["owner_id"]}
    assignment = assignment_repository.get_assignment(args["task_id"], actor_id=args["owner_id"])
    before = task_facade.get_task(**args)
    task_facade._commit_imported_submissions(**args, course_id=assignment.course_id,
        students=[{"stu_id": "S001", "stu_name": "Known Student", "identity_status": "matched",
                   "stu_ans": [{"q_id": "q1", "content": "", "flag": []}]}],
        replace_existing=True, expected_workflow_revision=before["workflow_revision"])
    current = task_facade.get_task(**args, full=True)
    answers = {answer["q_id"]: answer for answer in current["student_data"]["S001"]["stu_ans"]}
    assert answers["q1"]["content"] == "" and answers["q1"]["flag"] == []
    assert answers["q2"]["content"] == "" and "recognition_manual_pending" in answers["q2"]["flag"]
    assert "submission_recognition_blocked" in task_facade.grading_readiness(**args)["blocking_issues"]
    with pytest.raises(ValidationError):
        task_facade.update_student_answer(**args, display_student_id="S001", q_id="q2",
            patch={"review_status": "confirmed"}, expected_revision=current["workflow_revision"])
    task_facade.update_student_answer(**args, display_student_id="S001", q_id="q2",
        patch={"content": "", "flag": ["manual_blank_confirmed"], "review_status": "confirmed"},
        expected_revision=current["workflow_revision"])
    assert task_facade.grading_readiness(**args)["ready"]


def test_historical_missing_answer_row_cannot_bypass_grading_gate():
    seeded = _seed_legacy_structured_case(question_count=2)
    args = {"task_id": seeded["assignment_id"], "owner_id": seeded["owner_id"]}
    with session_scope() as session:
        answer = session.scalar(select(SubmissionAnswerRecord).where(SubmissionAnswerRecord.q_id == "q2"))
        session.delete(answer)
    assert "submission_recognition_blocked" in task_facade.grading_readiness(**args)["blocking_issues"]
    current = task_facade.get_task(**args, full=True)
    missing = next(answer for answer in current["student_data"]["S001"]["stu_ans"] if answer["q_id"] == "q2")
    assert missing["content"] == "" and "recognition_manual_pending" in missing["flag"]
    with pytest.raises(ValidationError):
        task_facade.update_student_answer(**args, display_student_id="S001", q_id="q2",
            patch={"review_status": "confirmed"}, expected_revision=current["workflow_revision"])
    task_facade.update_student_answer(**args, display_student_id="S001", q_id="q2",
        patch={"content": "Teacher transcribed q2", "review_status": "confirmed"},
        expected_revision=current["workflow_revision"])
    assert task_facade.grading_readiness(**args)["ready"]


def test_explicit_partial_evidence_survives_reference_answer_projection():
    from backend.agents.question_preparation_agent import _issue
    from backend.services.task_facade import _effective_question_issues
    for field in ("stem", "rubric", "answer"):
        issue = _issue("q1", field, "recognition_partial", "blocking", ["source-1"])
        projected = _effective_question_issues({"preparation_issues": [issue]})[0]
        assert projected["details"]["incomplete"] is True
        assert projected["severity"] == ("warning" if field == "answer" else "blocking")
        resolved = _effective_question_issues({"preparation_issues": [{**projected, "status": "resolved"}]})[0]
        assert resolved["status"] == "resolved"


@pytest.mark.parametrize("missing", [[], ["criterion"], ["type", "criterion"]])
def test_required_material_repairs_allow_confirmation_without_reference_answer(missing):
    _seeded, args = _case(flags=[])
    with session_scope() as session:
        question = session.scalar(select(AssignmentQuestionRecord))
        question.reference_answer = ""
        for field in missing:
            setattr(question, field, "")
        issues = [{"issue_id": "answer", "field": "answer", "code": "generation_failed", "status": "open", "severity": "warning"}]
        if "criterion" in missing:
            issues.append({"issue_id": "rubric", "field": "rubric", "code": "generation_failed", "status": "open", "severity": "blocking"})
        question.source = {**(question.source or {}), "presentation": {
            **((question.source or {}).get("presentation") or {}),
            "review_status": "needs_review", "preparation_issues": issues}}
    current = task_facade.get_task(**args, full=True)
    if missing:
        with pytest.raises(ValidationError, match="required material"):
            task_facade.update_problem(**args, q_id="q1", patch={"review_status": "confirmed"},
                expected_revision=current["workflow_revision"])
        repaired = task_facade.update_problem(**args, q_id="q1",
            patch={field: "概念题" if field == "type" else "Full credit for a correct explanation." for field in missing},
            expected_revision=current["workflow_revision"])
        current = task_facade.get_task(**args, full=True)
        assert repaired["problem"]["reference_answer"] == ""
        assert not any(i["field"] == "rubric" and i["status"] == "open" for i in repaired["problem"]["preparation_issues"])
    confirmed = task_facade.update_problem(**args, q_id="q1", patch={"review_status": "confirmed"},
        expected_revision=current["workflow_revision"])
    assert confirmed["problem"]["review_status"] == "confirmed"
    assert confirmed["problem"]["reference_answer"] == ""
    readiness = task_facade.grading_readiness(**args)
    # Editing question content invalidates the fixture's prior submissions.
    # No question/answer-material gate may remain after confirmation.
    assert readiness["blocking_issues"] == (["submissions_required"] if missing else [])
