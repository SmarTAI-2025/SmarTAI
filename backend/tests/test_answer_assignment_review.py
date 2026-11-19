"""Teacher confirmation and temporary duplicate assignments use real persisted revisions."""
import uuid
import pytest
from sqlalchemy import select
from backend.db.models import AssignmentQuestionRecord, SubmissionAnswerRecord, SubmissionRecord
from backend.db.session import session_scope
from backend.domain.errors import DomainError
from backend.services import task_facade
from backend.tests.test_recognition_review_integrity import _case
from backend.tests.test_task_background_workflows import _reviewed_source


def snapshot(args):
    return task_facade.get_task(**args, full=True)


def answers(task):
    return task["student_data"]["S001"]["stu_ans"]


def save(args, task, answer, **patch):
    return task_facade.update_student_answer(**args, display_student_id="S001", q_id=answer["q_id"],
        patch={"answer_id": answer["answer_id"], **patch}, expected_revision=task["workflow_revision"])


def seed(count=2, flags=()):
    _, args = _case(flags=list(flags))
    with session_scope() as session:
        first = session.scalar(select(SubmissionAnswerRecord))
        first.number = "Section 1 / 1"
        session.get(AssignmentQuestionRecord, first.question_id).number = first.number
        for index in range(2, count + 1):
            question = AssignmentQuestionRecord(id=f"question-{index}", assignment_id=args["task_id"],
                q_id=f"q{index}", number=f"Section {index} / 1", type="推理题", stem=f"Question {index}",
                criterion="Award 10 points", max_score=10, order_index=index, version=1)
            session.add(question)
            session.flush()
            session.add(SubmissionAnswerRecord(id=f"ans-{index}-{uuid.uuid4().hex[:8]}", revision_id=first.revision_id,
                question_id=question.id, q_id=question.q_id, number=question.number, type=question.type,
                content=f"Original answer {index}", flag=[]))
        for question in session.scalars(select(AssignmentQuestionRecord)):
            question.source = _reviewed_source(question.q_id, question.number, question.stem,
                                               question.criterion, question.type)
    return args


@pytest.mark.parametrize(("content", "extra", "expected_flags"), [
    ("1.1.20. First step [unclear]", [], ["unclear_text"]),
    ("1.1.20. Work", ["crossed_out_text_unclear"], ["crossed_out_text_unclear"]),
    ("1.1.20. Readable work", [], []),
    ("1.1.31. Readable work", [], ["question_identifier_conflict"]),
    ("1.1.3. Readable work", [], ["question_identifier_conflict"]),
    ("1.1.3)Readable work", [], ["question_identifier_conflict"]),
    ("**1.1.21.** Readable work", [], ["question_identifier_conflict"]),
    ("1.1.20. Work", ["question_identifier_conflict"], ["question_identifier_conflict"]),
    ("1.1.20. Explain why the second statement holds. Student work.",
        [], ["question_identifier_conflict"]),
    ("Student work.\n1.1.20. A reference to this exercise.",
        [], ["recognition_matching_advisory"]),
    ("Question 20: Work", [], ["recognition_matching_advisory"]),
])
def test_answer_matching_advice_uses_saved_heading_evidence(content, extra, expected_flags):
    args = seed(flags=["recognition_manual_pending", *extra])
    with session_scope() as session:
        first = session.scalar(select(SubmissionAnswerRecord).where(SubmissionAnswerRecord.q_id == "q1"))
        first.number = "1.1.20"
        first.content = content
        questions = {q.q_id: q for q in session.scalars(select(AssignmentQuestionRecord))}
        questions["q1"].number = "1.1.20"
        questions["q2"].number = "1.2.20"
        questions["q2"].stem = "Explain why the second statement holds."
        for question in questions.values():
            question.source = _reviewed_source(question.q_id, question.number, question.stem,
                                               question.criterion, question.type)
    task = snapshot(args)
    row = next(a for a in answers(task) if a["q_id"] == "q1")
    assert row["flag"] == expected_flags
    assert row["content"] == content
    assert task_facade.grading_readiness(**args) == {
        "ready": True, "blocking_issues": [],
        "warnings": ["submission_recognition_needs_review"] if expected_flags else [],
    }
    with session_scope() as session:
        saved = session.scalar(select(SubmissionAnswerRecord).where(SubmissionAnswerRecord.q_id == "q1"))
        assert saved.flag == ["recognition_manual_pending", *extra]


@pytest.mark.parametrize("flags", [["recognition_manual_pending"], ["recognition_invalid_fields:number,type"]])
@pytest.mark.parametrize("save_field", ["content", "target_q_id"])
def test_saving_verified_answer_fields_resolves_doubts_without_confirmation(flags, save_field):
    args = seed(flags=flags)
    before = snapshot(args)
    first = next(a for a in answers(before) if a["q_id"] == "q1")
    assert task_facade.grading_readiness(**args)["ready"] == (flags == ["recognition_manual_pending"])
    patch = {save_field: first["content"] if save_field == "content" else first["q_id"]}
    repaired = save(args, before, first, review_status="pending", **patch)
    assert repaired["answer"]["review_status"] == "pending"
    assert repaired["answer"]["flag"] == []
    assert task_facade.grading_readiness(**args)["ready"]
    reloaded = next(a for a in answers(snapshot(args)) if a["answer_id"] == first["answer_id"])
    assert reloaded["review_status"] == "pending"
    assert reloaded["flag"] == []


@pytest.mark.parametrize("unchanged_save", [False, True])
def test_matching_confirmation_without_rewriting_survives_reload_and_sibling_edit(unchanged_save):
    args = seed(flags=["recognition_manual_pending", "authorship_uncertain"])
    before = snapshot(args)
    first = next(a for a in answers(before) if a["q_id"] == "q1")
    with session_scope() as session:
        revision = session.scalar(select(SubmissionRecord.current_revision_id))
    assert task_facade.grading_readiness(**args) == {
        "ready": True, "blocking_issues": [], "warnings": ["submission_recognition_needs_review"],
    }
    confirmed = save(args, before, first, review_status="confirmed", **({"content": first["content"]} if unchanged_save else {}))
    assert confirmed["answer"]["content"] == first["content"]
    assert confirmed["answer"]["answer_id"] == first["answer_id"]
    assert "recognition_manual_pending" not in confirmed["answer"]["flag"]
    with session_scope() as session:
        assert session.scalar(select(SubmissionRecord.current_revision_id)) == revision
    current = snapshot(args)
    second = next(a for a in answers(current) if a["q_id"] == "q2")
    save(args, current, second, content="Teacher-corrected answer 2", review_status="confirmed")
    current = snapshot(args)
    first_again = next(a for a in answers(current) if a["answer_id"] == first["answer_id"])
    assert first_again["review_status"] == "confirmed"
    assert "recognition_manual_pending" not in first_again["flag"]
    assert task_facade.grading_readiness(**args)["ready"]
    with pytest.raises(DomainError):
        save(args, before, first, review_status="pending")
    assert answers(snapshot(args)) == answers(current)


@pytest.mark.parametrize("failure", ["recognition_failed", "parse_failed", "no_matching_answer", "recognition_incomplete",
    "recognition_paused", "recognition_failed_pages:2", "recognition_unprocessed_pages:2", "recognition_invalid_fields:content"])
def test_confirmation_and_reassignment_do_not_clear_missing_or_damaged_content(failure):
    args = seed(flags=["recognition_manual_pending", failure])
    before = snapshot(args)
    first = next(a for a in answers(before) if a["q_id"] == "q1")
    with pytest.raises(DomainError):
        save(args, before, first, review_status="confirmed")
    save(args, before, first, target_q_id="q2")
    current = snapshot(args)
    moved = next(a for a in answers(current) if a["answer_id"] == first["answer_id"])
    assert failure in moved["flag"]
    assert "answer_assignment_conflict" in moved["flag"]
    assert not task_facade.grading_readiness(**args)["ready"]
    other = next(a for a in answers(current) if a["answer_id"] != first["answer_id"])
    save(args, current, other, target_q_id="q1")
    current = snapshot(args)
    moved = next(a for a in answers(current) if a["answer_id"] == first["answer_id"])
    assert "answer_assignment_conflict" not in moved["flag"] and failure in moved["flag"]
    with pytest.raises(DomainError):
        save(args, current, moved, review_status="confirmed")


def test_two_answers_swap_via_saved_duplicate_without_overwrite_and_can_grade():
    args = seed()
    before = snapshot(args)
    first = next(a for a in answers(before) if a["q_id"] == "q1")
    second = next(a for a in answers(before) if a["q_id"] == "q2")
    result = save(args, before, second, target_q_id="q1")
    current = snapshot(args)
    assert sorted(result["answers"], key=lambda a: a["answer_id"]) == sorted(answers(current), key=lambda a: a["answer_id"])
    assert len(answers(current)) == 2
    assert {a["answer_id"] for a in answers(current)} == {first["answer_id"], second["answer_id"]}
    assert all(a["q_id"] == "q1" and "answer_assignment_conflict" in a["flag"] for a in answers(current))
    assert not task_facade.grading_readiness(**args)["ready"]
    with pytest.raises(DomainError):
        task_facade.start_task_grading(**args, expected_workflow_revision=current["workflow_revision"])
    from backend.services import grading_runs
    with pytest.raises(DomainError) as blocked:
        grading_runs.start_run(assignment_id=args["task_id"], teacher_id=args["owner_id"])
    assert blocked.value.code == "answer_assignment_conflict"
    with pytest.raises(DomainError):
        task_facade.update_student_answer(**args, display_student_id="S001", q_id="q1", patch={"content": "wrong target"},
            expected_revision=current["workflow_revision"])
    first_now = next(a for a in answers(current) if a["answer_id"] == first["answer_id"])
    with pytest.raises(DomainError):
        save(args, current, first_now, review_status="confirmed")
    unchanged = save(args, current, first_now, content=first_now["content"], review_status="confirmed")
    assert "answer_assignment_conflict" in unchanged["answer"]["flag"]
    current = snapshot(args)
    edited = save(args, current, first_now, content="Edited first answer", review_status="confirmed")
    assert edited["answer"]["review_status"] == "pending"
    assert "answer_assignment_conflict" in edited["answer"]["flag"]
    current = snapshot(args)
    assert next(a for a in answers(current) if a["answer_id"] == second["answer_id"])["content"] == second["content"]
    first_now = next(a for a in answers(current) if a["answer_id"] == first["answer_id"])
    save(args, current, first_now, target_q_id="q2")
    current = snapshot(args)
    assert all("answer_assignment_conflict" not in a["flag"] for a in answers(current))
    swapped = {a["answer_id"]: a for a in answers(current)}
    assert swapped[first["answer_id"]]["q_id"] == "q2"
    assert swapped[first["answer_id"]]["number"] == "Section 2 / 1"
    assert swapped[first["answer_id"]]["type"] == "推理题"
    assert swapped[first["answer_id"]]["content"] == "Edited first answer"
    assert task_facade.grading_readiness(**args)["ready"]


def test_three_way_conflict_only_clears_resolved_answers():
    args = seed(3)
    original = answers(snapshot(args))
    for answer in original:
        if answer["q_id"] != "q1": save(args, snapshot(args), answer, target_q_id="q1")
    current = snapshot(args)
    assert len(answers(current)) == 3
    first = next(a for a in answers(current) if a["answer_id"] == original[0]["answer_id"])
    save(args, current, first, target_q_id="q3")
    current = snapshot(args)
    assert sum("answer_assignment_conflict" in a["flag"] for a in answers(current)) == 2
    assert not task_facade.grading_readiness(**args)["ready"]


def test_invalid_question_and_stale_reassignment_are_atomic():
    args = seed()
    before = snapshot(args)
    first = answers(before)[0]
    with pytest.raises(DomainError): save(args, before, first, target_q_id="not-a-task-question")
    assert snapshot(args)["workflow_revision"] == before["workflow_revision"]
    save(args, before, first, review_status="confirmed")
    current = snapshot(args)
    with pytest.raises(DomainError): save(args, before, first, target_q_id="q2")
    with pytest.raises(DomainError):
        task_facade.update_student_answer(**args, display_student_id="S001", q_id=first["q_id"],
            patch={"content": "Old browser save", "review_status": "pending"}, expected_revision=before["workflow_revision"])
    assert answers(snapshot(args)) == answers(current)


@pytest.mark.parametrize("action", ["confirm", "reassign"])
def test_late_recognition_publication_cannot_replace_teacher_review(action):
    from backend.db import assignment_repository, workflow_repository
    args = seed(flags=["recognition_manual_pending"])
    before = snapshot(args)
    first = next(a for a in answers(before) if a["q_id"] == "q1")
    save(args, before, first, **({"review_status": "confirmed"} if action == "confirm" else {"target_q_id": "q2"}))
    current = snapshot(args)
    operation, _ = workflow_repository.create_operation(assignment_id=args["task_id"], owner_id=args["owner_id"],
        operation_type="submission_recognition", input_hash=uuid.uuid4().hex)
    workflow_repository.update_operation(operation.id, owner_id=args["owner_id"], expected_attempt=operation.attempt, status="running")
    assignment = assignment_repository.get_assignment(args["task_id"], actor_id=args["owner_id"])
    with pytest.raises(DomainError) as caught:
        task_facade._commit_imported_submissions(**args, course_id=assignment.course_id, replace_existing=True,
            students=[{"stu_id": "S001", "stu_name": "Old OCR", "stu_ans": [{"q_id": "q1", "content": "late OCR", "flag": []}]}],
            expected_workflow_revision=before["workflow_revision"], operation_id=operation.id, expected_operation_attempt=operation.attempt)
    assert caught.value.code == "stale_revision"
    assert answers(snapshot(args)) == answers(current)


@pytest.mark.parametrize("confirm", [False, True])
def test_matching_review_can_start_grading_without_altering_review_status(confirm):
    args = seed(flags=["recognition_manual_pending"])
    before = snapshot(args)
    first = next(a for a in answers(before) if a["q_id"] == "q1")
    if confirm:
        save(args, before, first, content=first["content"], flag=first["flag"], review_status="confirmed")
    current = snapshot(args)
    started = task_facade.start_task_grading(**args, expected_workflow_revision=current["workflow_revision"])
    assert started["job_id"]
    reloaded = next(a for a in answers(snapshot(args)) if a["answer_id"] == first["answer_id"])
    assert reloaded["review_status"] == ("confirmed" if confirm else "pending")
    assert reloaded["content"] == first["content"]
    assert ("recognition_matching_advisory" in reloaded["flag"]) is not confirm


@pytest.mark.parametrize("uncertain", [False, True])
def test_manual_handoff_only_keeps_complete_matching_doubts_confirmable(uncertain):
    from types import SimpleNamespace
    from backend.services.submission_manual_review import fill_manual_answers
    student = {"stu_ans": [{"q_id": "q1", "content": "Original text", "flag": ["recognition_manual_pending"]}],
        "recognition": {"model_review_question_ids": ["q1"]}}
    fill_manual_answers(student, [SimpleNamespace(q_id="q1")], paused=True, uncertain=uncertain)
    answer = student["stu_ans"][0]
    assert task_facade._answer_matching_confirmation_allowed(answer["flag"], answer["content"]) is not uncertain


def test_automatic_import_still_rejects_duplicate_questions():
    from backend.db import assignment_repository
    args = seed()
    before = snapshot(args)
    assignment = assignment_repository.get_assignment(args["task_id"], actor_id=args["owner_id"])
    with pytest.raises(DomainError) as caught:
        task_facade._commit_imported_submissions(**args, course_id=assignment.course_id, replace_existing=True,
            expected_workflow_revision=before["workflow_revision"],
            students=[{"stu_id": "S001", "stu_name": "Synthetic", "stu_ans": [{"q_id": "q1"}, {"q_id": "q1"}]}])
    assert caught.value.code == "answer_assignment_conflict"
    assert answers(snapshot(args)) == answers(before)
