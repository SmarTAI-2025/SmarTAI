"""Teacher review must preserve the executable programming score allocation."""
import pytest
from fastapi.testclient import TestClient

from backend.auth import create_token
from backend.agents.incremental_question_preparation import _rows
from backend.db import assignment_repository
from backend.main import create_app
from backend.models import ProblemInfo
from backend.services import grading_adapter, task_facade
from backend.services.programming_materials import resolve_generated_outputs
from backend.services.rubric_drafts import saved_draft
from backend.tests.test_programming_generated_materials import inputs, source
from backend.tests.test_programming_reference_review import frozen_problem, grade
from backend.tests.test_task_background_workflows import _seed_task


async def setup_question(*, stale=False):
    row = source()
    row["reference_answer"] = "Return the square of the input integer."
    row = _rows([row])["q1"]
    cases = await resolve_generated_outputs(row, inputs())
    problem = frozen_problem(row, [case.model_dump(mode="json") for case in cases])
    if stale:
        problem._math_rubric_source["presentation"]["rubric_draft"]["content_fingerprint"] = "0" * 64
    owner, task = _seed_task()
    task_facade._replace_draft_questions(task, owner,
        {"q1": problem._math_rubric_source["presentation"]}, "prepared.json")
    client = TestClient(create_app(), headers={"Authorization": "Bearer " + create_token(owner, "teacher")})
    return owner, task, client, f"/tasks/{task}/problems/q1"


def saved_problem(owner, task):
    question, = assignment_repository.list_questions(task, teacher_id=owner)
    raw = grading_adapter.build_problem_store([question])["q1"]
    problem = ProblemInfo.model_validate(raw)
    problem._math_rubric_source = raw["_math_rubric_source"]
    return problem


@pytest.mark.asyncio
@pytest.mark.parametrize("edit", ["confirm", "answer", "roundtrip", "number"])
async def test_review_save_preserves_seven_of_ten_score(monkeypatch, edit):
    owner, task, client, url = await setup_question()
    before = saved_problem(owner, task)
    old_draft = saved_draft(before)
    assert old_draft is not None
    code = "def square(x): return x*x if x < 7 else -1"
    initial = await grade(monkeypatch, before, code)
    assert initial.score == 8.2 and initial.steps[0].score == 4.2
    patch = {"review_status": "confirmed"}
    if edit == "answer":
        patch = {"reference_answer": "Compute x multiplied by itself and return that integer."}
    elif edit == "roundtrip":
        patch.update(before.model_dump(mode="json", include={
            "number", "type", "stem", "criterion", "max_score", "reference_answer", "test_cases"}))
    elif edit == "number":
        patch = {"number": "2"}
    response = client.put(url, json=patch)
    assert response.status_code == 200, response.text
    after = saved_problem(owner, task)
    new_draft = saved_draft(after)
    assert new_draft is not None
    assert new_draft.plan.part_rubric.parts == old_draft.plan.part_rubric.parts
    assert after.test_cases == before.test_cases
    result = await grade(monkeypatch, after, code)
    assert result.error_kind is None, result.comment
    assert result.programming_evidence.generated_tests.passed == 7
    assert result.programming_evidence.generated_tests.total == 10
    assert result.score == 8.2 and result.steps[0].score == 4.2


@pytest.mark.asyncio
@pytest.mark.parametrize("patch", [{"criterion": "Award all 10 points for correct code."},
    {"max_score": 20}, {"stem": "Implement square(x): x is an integer. Return x*x*x."}])
async def test_scoring_changes_do_not_reseal_old_programming_rules(patch):
    owner, task, client, url = await setup_question()
    response = client.put(url, json=patch)
    assert response.status_code == 200, response.text
    assert saved_draft(saved_problem(owner, task)) is None


@pytest.mark.asyncio
async def test_type_change_still_requires_its_own_rubric_compilation():
    owner, task, client, url = await setup_question()
    before = saved_problem(owner, task)
    response = client.put(url, json={"type": "其他"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "no_provider_configured"
    assert saved_draft(saved_problem(owner, task)) == saved_draft(before)


@pytest.mark.asyncio
async def test_reference_edit_does_not_revalidate_an_already_stale_draft():
    owner, task, client, url = await setup_question(stale=True)
    assert saved_draft(saved_problem(owner, task)) is None
    response = client.put(url, json={"reference_answer": "Return x*x."})
    assert response.status_code == 200, response.text
    assert saved_draft(saved_problem(owner, task)) is None
