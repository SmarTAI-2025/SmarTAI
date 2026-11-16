"""Saved field edits resolve advice; unresolved failures survive confirmation."""
import copy

import pytest
from sqlalchemy import select

from backend.db import assignment_repository
from backend.db.models import AssignmentQuestionRecord
from backend.db.session import session_scope
from backend.services import task_facade
from backend.tests.rubric_fixtures import review_compiler  # noqa: F401
from backend.tests.test_recognition_review_integrity import _case


@pytest.mark.parametrize("failure", [False, True])
def test_answer_save_clears_yellow_without_confirmation_but_keeps_unrepaired_failure(failure):
    flags = ["answer_recognition_confidence:0.2", "authorship_uncertain"]
    if failure:
        flags.append("recognition_failed_pages:2")
    _, args = _case(flags=flags)
    before = task_facade.get_task(**args, full=True)
    changed = task_facade.update_student_answer(**args, display_student_id="S001", q_id="q1",
        patch={"content": "Visible student work\n" if failure else "Teacher transcribed the answer"},
        expected_revision=before["workflow_revision"])
    answer = changed["answer"]
    assert answer["review_status"] == "edited" and answer["recognition_confidence"] == .2
    assert "authorship_uncertain" in answer["flag"]  # Preserve the source observation.
    assert ("recognition_failed_pages:2" in answer["flag"]) == failure
    readiness = task_facade.grading_readiness(**args)
    assert ("submission_recognition_blocked" in readiness["blocking_issues"]) == failure
    assert "submission_recognition_needs_review" not in readiness["warnings"]
    restored = task_facade.get_task(**args, full=True)["student_data"]["S001"]["stu_ans"][0]
    assert restored == answer


@pytest.mark.parametrize("initial_status", ["needs_review", "confirmed"])
def test_question_edits_resolve_only_affected_field_and_confirmation_handles_remaining_advice(review_compiler, initial_status):
    _, args = _case(flags=[])
    with session_scope() as session:
        question = session.scalar(select(AssignmentQuestionRecord))
        source = copy.deepcopy(question.source or {})
        pres = source.setdefault("presentation", {})
        pres["review_status"] = initial_status
        pres["preparation_issues"] = [dict(issue_id=field, q_id="q1", field=field, code="low_confidence",
            severity="warning", status="open", details={"confidence": .2}) for field in ["stem", "answer"]]
        question.source = source
    before = task_facade.get_task(**args, full=True)
    changed = task_facade.update_problem(**args, q_id="q1", patch={"stem": "Teacher corrected question text"},
        expected_revision=before["workflow_revision"])
    statuses = {issue["issue_id"]: issue["status"] for issue in changed["problem"]["preparation_issues"]}
    assert statuses["stem"] == "resolved" and statuses["answer"] == "open"
    assert changed["problem"]["review_status"] == "edited"
    confirmed = task_facade.update_problem(**args, q_id="q1", patch={"review_status": "confirmed"},
        expected_revision=changed["workflow_revision"])
    assert all(issue["status"] != "open" for issue in confirmed["problem"]["preparation_issues"])


@pytest.mark.parametrize("action", ["confirm", "edit"])
def test_current_rubric_advice_does_not_reappear_after_confirmation_or_rubric_edit(action):
    from backend.tests.test_frozen_part_rubric import _persist_method_question
    from backend.services.math_grading_context import problem_from_question
    from backend.services.rubric_drafts import saved_draft
    from backend.services.math_rubric_plan import _content_hash
    args = _persist_method_question()
    with session_scope() as session:
        question = session.scalar(select(AssignmentQuestionRecord))
        source = copy.deepcopy(question.source)
        plan = source["presentation"]["rubric_draft"]["plan"]
        plan["advisories"] = ["Check the stated rubric policy."]
        plan["plan_id"] = _content_hash({key: value for key, value in plan.items() if key != "plan_id"})
        question.source = source
    question, = assignment_repository.list_questions(args["task_id"], teacher_id=args["owner_id"])
    before = task_facade._serialize_problem(question)
    assert any(issue["code"] == "math_rubric_advisory" for issue in before["preparation_issues"])
    patch = {"review_status": "confirmed"} if action == "confirm" else {"criterion": question.criterion + "。"}
    changed = task_facade.update_problem(**args, patch=patch)
    assert all(issue["code"] != "math_rubric_advisory" for issue in changed["problem"]["preparation_issues"])
    question, = assignment_repository.list_questions(args["task_id"], teacher_id=args["owner_id"])
    assert task_facade._serialize_problem(question) == changed["problem"]
    plan = saved_draft(problem_from_question(question)).plan
    assert task_facade._math_rubric_advisory(question, plan.model_copy(update={"input_fingerprint": "new-input",
        "advisories": ("Check the new rubric.",)}), "zh")
