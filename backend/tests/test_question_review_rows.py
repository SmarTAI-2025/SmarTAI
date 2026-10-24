import pytest
from sqlalchemy import select

from backend.db import assignment_repository, grading_repository, workflow_repository
from backend.db.models import AssignmentQuestionRecord, SubmissionAnswerRecord
from backend.db.session import session_scope
from backend.domain.errors import DomainError
from backend.services import task_facade
from backend.services.question_review_rows import add_question, delete_question
from backend.tests.test_task_background_workflows import _seed_task, _problem
from backend.tests.test_task_grading_retry import case, start, finish  # noqa: F401


def question_task():
    owner, task = _seed_task(with_question=True)
    workflow_repository.update_workflow(task, owner_id=owner, presentation_status="problems_ready")
    return owner, task


def test_add_edit_remove_questions_use_existing_editor_and_keep_empty_matrix():
    owner, task = question_task()
    before = task_facade.get_task(task_id=task, owner_id=owner)
    created = add_question(task_id=task, owner_id=owner, expected_revision=before["workflow_revision"])
    after = task_facade.get_task(task_id=task, owner_id=owner)
    q_id = created["q_id"]
    assert after["problem_data"]["q1"] == before["problem_data"]["q1"]
    assert after["problem_data"][q_id]["stem"] == ""
    assert after["problem_data"][q_id]["type"] == ""
    assert "question_review_blocked" in task_facade.grading_readiness(task_id=task, owner_id=owner)["blocking_issues"]
    with pytest.raises(DomainError):
        task_facade.update_problem(task_id=task, owner_id=owner, q_id=q_id, patch={"review_status": "confirmed"})
    task_facade.update_problem(task_id=task, owner_id=owner, q_id=q_id,
        patch={"number": "2.4", "type": "proof", "stem": "Teacher question", "criterion": "Award 10 points for a correct solution."})
    task_facade.update_problem(task_id=task, owner_id=owner, q_id=q_id, patch={"review_status": "confirmed"})
    after = task_facade.get_task(task_id=task, owner_id=owner)
    assert after["problem_data"][q_id]["number"] == "2.4"
    assert after["problem_data"][q_id]["type"] == "推理题"
    assert after["problem_data"][q_id]["review_status"] == "confirmed"
    for removed in ("q1", q_id):
        snapshot = task_facade.get_task(task_id=task, owner_id=owner)
        delete_question(task_id=task, owner_id=owner, q_id=removed, expected_revision=snapshot["workflow_revision"])
        with pytest.raises(DomainError):
            task_facade.update_problem(task_id=task, owner_id=owner, q_id=removed, patch={"stem": "Old URL"})
    empty = task_facade.get_task(task_id=task, owner_id=owner)
    assert empty["status"] == "problems_ready" and empty["problem_data"] == {}
    assert assignment_repository.get_assignment(task, actor_id=owner).question_count == 0
    assert not assignment_repository.get_questions_by_assignment(task)
    assert "questions_required" in task_facade.grading_readiness(task_id=task, owner_id=owner)["blocking_issues"]
    added = add_question(task_id=task, owner_id=owner, expected_revision=empty["workflow_revision"])
    assert list(task_facade.get_task(task_id=task, owner_id=owner)["problem_data"]) == [added["q_id"]]


def test_question_mutations_reject_stale_foreign_and_later_stage_requests():
    owner, task = question_task()
    stranger, _ = question_task()
    revision = workflow_repository.get_workflow(task, owner_id=owner).workflow_revision
    for mutation in (
        lambda user, rev: add_question(task_id=task, owner_id=user, expected_revision=rev),
        lambda user, rev: delete_question(task_id=task, owner_id=user, q_id="q1", expected_revision=rev),
    ):
        with pytest.raises(DomainError):
            mutation(stranger, revision)
        with pytest.raises(DomainError) as stale:
            mutation(owner, revision - 1)
        assert stale.value.code == "stale_revision"
    for status in ("extracting_problems", "submissions_ready", "grading_setup", "grading", "graded", "finalized", "error"):
        workflow = workflow_repository.update_workflow(task, owner_id=owner, presentation_status=status)
        with pytest.raises(DomainError):
            add_question(task_id=task, owner_id=owner, expected_revision=workflow.workflow_revision)
        with pytest.raises(DomainError):
            delete_question(task_id=task, owner_id=owner, q_id="q1", expected_revision=workflow.workflow_revision)
    assert [q.q_id for q in assignment_repository.list_questions(task, teacher_id=owner)] == ["q1"]


def test_deleted_question_keeps_historical_grades_answers_reports_and_allows_same_logical_id(case, monkeypatch):
    task, owner = case
    run = start(case, "history-before-remove")
    finish(case, run["job_id"], monkeypatch, ())
    old_grades = grading_repository.list_results_for_run(run["job_id"])
    final = task_facade.generate_artifacts(task_id=task, owner_id=owner,
        expected_revision=workflow_repository.get_workflow(task, owner_id=owner).workflow_revision)
    old_bundle = task_facade.artifact_bytes(task_id=task, owner_id=owner,
        version=final["final_result_version"], artifact_id="bundle")[0]
    questions = assignment_repository.list_questions(task, teacher_id=owner)
    with session_scope() as session:
        answer_ids = set(session.scalars(select(SubmissionAnswerRecord.id).where(
            SubmissionAnswerRecord.question_id.in_([q.id for q in questions]))))
    # Simulate returning to the upstream review generation, never deleting in grading review.
    workflow = workflow_repository.update_workflow(task, owner_id=owner,
        presentation_status="problems_ready", grading_job_id=None)
    for question in questions:
        removed = delete_question(task_id=task, owner_id=owner, q_id=question.q_id,
            expected_revision=workflow.workflow_revision)
        workflow = workflow_repository.get_workflow(task, owner_id=owner)
    assert {row.id for row in grading_repository.list_results_for_run(run["job_id"])} == {row.id for row in old_grades}
    with session_scope() as session:
        assert all(session.get(AssignmentQuestionRecord, q.id) for q in questions)
        assert all(session.get(SubmissionAnswerRecord, id_) for id_ in answer_ids)
    assert task_facade.artifact_bytes(task_id=task, owner_id=owner,
        version=final["final_result_version"], artifact_id="bundle")[0] == old_bundle
    task_facade._replace_draft_questions(task, owner, _problem("New q1"), "new.txt",
        expected_workflow_revision=removed["workflow_revision"], replace_confirmed=True)
    current = assignment_repository.list_questions(task, teacher_id=owner)
    assert len(current) == 1 and current[0].q_id == "q1"
    assert current[0].id not in {q.id for q in questions} and current[0].version > 1
    assert {row.id for row in grading_repository.list_results_for_run(run["job_id"])} == {row.id for row in old_grades}
