"""Missing funded test evidence must not escape into a scoreless grading run."""
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

from backend.agents import incremental_question_preparation as inc
from backend.agents.ingest_agent import AICompletionCandidateOutput
from backend.api import task_preparation as api
from backend.db import assignment_repository, workflow_repository
from backend.domain.errors import ValidationError
from backend.services import task_facade, rubric_drafts
from backend.services.question_preparation_diagnostics import public_preparation_review, preparation_review, required_programming_tests_issue
from backend.tests.test_incremental_question_preparation import setup, persisted
from backend.tests.test_programming_generated_materials import source, definition
from backend.tests.test_programming_reference_review import frozen_problem
from backend.tests.test_task_background_workflows import _seed_task


@pytest.mark.asyncio
async def test_required_tests_failure_survives_worker_and_manual_handoff(monkeypatch):
    owner, task, operation, _ = await setup(monkeypatch)
    row = {**source(reference_answer="Return the square."), "solution_code": ""}
    monkeypatch.setattr(inc, "_extract_problems_call", AsyncMock(return_value=inc._rows([row])))
    async def only_rubric(data, targets, *_args, **_kwargs):
        if not any(target["target"] == "criterion" for target in targets):
            return []
        text, draft = rubric_drafts.generated_rubric(data["q1"], rubric_drafts.RubricDefinition.model_validate(definition()))
        return [AICompletionCandidateOutput(q_id="q1", target_id="q1:criterion", target="criterion",
            text_value=text, rubric_draft=draft.model_dump(mode="json"))]
    generate = AsyncMock(side_effect=only_rubric)
    monkeypatch.setattr(inc, "generate_missing_question_materials", generate)
    await api.run_durable_question_preparation(operation)
    saved, rows = persisted(owner, operation)
    assert generate.await_count == 3
    assert saved.status == "error" and saved.error_code == "provider_response_invalid"
    review = public_preparation_review(preparation_review(rows))
    issue = next(i for i in review[0]["issues"] if i["field"] == "programming_tests")
    assert issue["severity"] == "blocking"
    assert issue["details"]["reason"] == "required_programming_tests_missing"
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    result = await api.manually_complete_question_preparation(task, operation.operation_id,
        api.ManualQuestionPreparationRequest(expected_workflow_revision=workflow.workflow_revision),
        current=SimpleNamespace(id=owner))
    assert result["status"] == "ready_for_review"
    questions = assignment_repository.list_questions(task, teacher_id=owner)
    assert task_facade._question_review_blockers(questions) == ["question_review_blocked"]
    assert generate.await_count == 3  # Manual handoff neither retries nor supplies evidence.


@pytest.mark.parametrize("confirmed", [False, True])
@pytest.mark.parametrize("saved_marker", [False, True])
def test_legacy_saved_rows_cannot_bypass_required_tests_by_confirmation(confirmed, saved_marker):
    row = inc._rows([source(reference_answer="Return the square.")])["q1"]
    row = frozen_problem(row, [])._math_rubric_source["presentation"]
    row.update(preparation_issues=[required_programming_tests_issue("q1")] if saved_marker else [],
               review_status="confirmed" if confirmed else "needs_review")
    owner, task = _seed_task()
    task_facade._replace_draft_questions(task, owner, {"q1": row}, "prepared.json")
    question, = assignment_repository.list_questions(task, teacher_id=owner)
    version = question.version
    with pytest.raises(ValidationError, match="before confirming"):
        task_facade.update_problem(task_id=task, owner_id=owner, q_id="q1",
            patch={"review_status": "confirmed"})
    view = task_facade._serialize_problem(question)
    issues = [i for i in view["preparation_issues"] if i["field"] == "programming_tests"]
    assert len(issues) == 1 and issues[0]["severity"] == "blocking"
    assert task_facade._question_review_blockers([question]) == ["question_review_blocked"]
    unchanged, = assignment_repository.list_questions(task, teacher_id=owner)
    assert unchanged.version == version and unchanged.source["presentation"]["preparation_issues"] == row["preparation_issues"]

    # Supplying valid cases removes the derived blocker without rewriting the rubric.
    task_facade.update_problem(task_id=task, owner_id=owner, q_id="q1",
        patch={"test_cases": [{"io_mode": "function", "input": str(i), "expected_output": str(i*i)} for i in range(10)]})
    question, = assignment_repository.list_questions(task, teacher_id=owner)
    assert task_facade._question_review_blockers([question]) == []
    assert not [i for i in task_facade._serialize_problem(question)["preparation_issues"]
                if i["field"] == "programming_tests"]
    result = task_facade.update_problem(task_id=task, owner_id=owner, q_id="q1",
        patch={"review_status": "confirmed"})
    assert result["problem"]["review_status"] == "confirmed"


@pytest.mark.parametrize("confirm_on_save", [False, True])
def test_replacing_test_allocation_clears_its_obsolete_blocker(confirm_on_save):
    row = inc._rows([source(reference_answer="Return the square.")])["q1"]
    row = frozen_problem(row, [])._math_rubric_source["presentation"]
    row["preparation_issues"] = [required_programming_tests_issue("q1")]
    owner, task = _seed_task()
    task_facade._replace_draft_questions(task, owner, {"q1": row}, "prepared.json")
    patch = {"criterion": "Award all 10 points for correct code. No points for tests."}
    if confirm_on_save:
        patch["review_status"] = "confirmed"
    task_facade.update_problem(task_id=task, owner_id=owner, q_id="q1", patch=patch)
    question, = assignment_repository.list_questions(task, teacher_id=owner)
    assert task_facade._question_review_blockers([question]) == []
    assert not [i for i in task_facade._serialize_problem(question)["preparation_issues"]
                if i["field"] == "programming_tests"]
    result = task_facade.update_problem(task_id=task, owner_id=owner, q_id="q1",
        patch={"review_status": "confirmed"})
    assert result["problem"]["review_status"] == "confirmed"


def test_replacing_test_allocation_does_not_clear_an_unrelated_blocker():
    row = inc._rows([source(reference_answer="Return the square.")])["q1"]
    row = frozen_problem(row, [])._math_rubric_source["presentation"]
    row["preparation_issues"] = [required_programming_tests_issue("q1"), {
        "issue_id": "test-conflict", "q_id": "q1", "field": "programming_tests",
        "code": "invalid_test_case", "details": {"reason": "test_case_output_conflict"},
        "status": "open", "severity": "blocking",
    }]
    owner, task = _seed_task()
    task_facade._replace_draft_questions(task, owner, {"q1": row}, "prepared.json")
    with pytest.raises(ValidationError, match="before confirming"):
        task_facade.update_problem(task_id=task, owner_id=owner, q_id="q1", patch={
            "criterion": "Award all 10 points for correct code.", "review_status": "confirmed",
        })
