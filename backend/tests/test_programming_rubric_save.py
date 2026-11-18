"""Teacher review must preserve the executable programming score allocation."""
import pytest
from fastapi.testclient import TestClient

from backend.auth import create_token
from backend.agents.incremental_question_preparation import _rows
from backend.db import assignment_repository, workflow_repository
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


@pytest.mark.asyncio
@pytest.mark.parametrize("points, template", [
    (6, "测试占 {tests:g} 分，按通过比例算。算法正确得 {code:g} 分。"),
    (7.5, "测试样例占 {tests:g} 分，按测试通过率计分。代码正确得 {code:g} 分。"),
    (6, "Tests are worth {tests:g} points, proportional to the fraction passed. Correct algorithm: {code:g} points."),
])
async def test_explicit_test_budget_edit_keeps_formula_and_requires_samples(monkeypatch, points, template):
    owner, task, client, url = await setup_question()
    before = saved_problem(owner, task)
    code_points = 10 - points
    criterion = template.format(tests=points, code=code_points)
    saved = client.put(url, json={"criterion": criterion, "max_score": 10, "test_cases": []})
    assert saved.status_code == 200, saved.text
    assert saved.json()["problem"]["effective_criterion"] == criterion
    assert any(issue["details"].get("reason") == "required_programming_tests_missing"
               for issue in saved.json()["problem"]["preparation_issues"])
    after = saved_problem(owner, task)
    assert after.criterion == criterion
    draft = saved_draft(after)
    assert draft is not None and not after.test_cases
    steps = draft.plan.part_rubric.parts[0].steps
    assert [(step.test_suite, step.total_pts) for step in steps] == [(True, points), (False, code_points)]
    blocked = client.put(url, json={"review_status": "confirmed"})
    assert blocked.status_code == 422 and blocked.json()["error"]["code"] == "question_review_blocked"
    questions = assignment_repository.list_questions(task, teacher_id=owner)
    assert task_facade._question_review_blockers(questions) == ["question_review_blocked"]

    repaired = client.put(url, json={"test_cases": [case.model_dump(mode="json") for case in before.test_cases]})
    assert repaired.status_code == 200, repaired.text
    assert client.put(url, json={"review_status": "confirmed"}).status_code == 200
    after = saved_problem(owner, task)
    assert saved_draft(after).plan.part_rubric.parts[0].steps == steps

    from backend.tests.test_question_material_format_repair import Provider
    from backend.models import StudentAnswerInfo
    from backend.skills.programming import ProgrammingSkill
    from unittest.mock import AsyncMock
    import json

    monkeypatch.setattr("backend.skills.programming.kb_tool.retrieve_for_grading", AsyncMock(return_value=[]))
    provider = Provider([json.dumps({"max_score": 10, "confidence": .9, "steps": [
        {"step_no": 1, "max_points": points, "score": points, "desc": "Tests", "is_correct": True},
        {"step_no": 2, "max_points": code_points, "score": code_points, "desc": "Code", "is_correct": True},
    ]})])
    vote = await ProgrammingSkill(provider, language="zh").grade(after,
        StudentAnswerInfo(q_id="q1", number="1", type="编程题",
                          content="def square(x): return x*x if x < 7 else -1"))
    assert vote.error_kind is None, vote.comment
    assert vote.programming_evidence.generated_tests.passed == 7
    assert vote.steps[0].score == pytest.approx(points * .7)
    assert vote.score == pytest.approx(code_points + points * .7)


@pytest.mark.asyncio
@pytest.mark.parametrize("criterion", [
    "测试占 6 分，按通过比例算。代码正确得 3 分。",
    "测试占 6 分，按通过比例算。用例占 2 分。代码正确得 2 分。",
    "测试占 6 分，按通过比例算。代码正确得 4 分。只有全部通过才给测试分。",
    "测试占 6 分，全部通过才给分。代码正确得 4 分。",
    "测试占 6 分，按通过比例算。",
    "The code and tests should both be scored.",
])
async def test_unrepresentable_edit_preserves_previous_rubric_and_revision(criterion):
    owner, task, client, url = await setup_question()
    before = saved_problem(owner, task)
    revision = workflow_repository.get_workflow(task, owner_id=owner).workflow_revision
    response = client.put(url, json={"criterion": criterion, "expected_workflow_revision": revision})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "invalid_structured_rubric"
    after = saved_problem(owner, task)
    assert after.criterion == before.criterion and saved_draft(after) == saved_draft(before)
    assert workflow_repository.get_workflow(task, owner_id=owner).workflow_revision == revision


@pytest.mark.asyncio
async def test_saved_generated_rubric_accepts_direct_budget_edit():
    owner, task, client, url = await setup_question()
    before = saved_problem(owner, task)
    criterion = before.criterion.replace("[6分]", "[7分]").replace("[4分]", "[3分]")
    response = client.put(url, json={"criterion": criterion})
    assert response.status_code == 200, response.text
    reopened = saved_problem(owner, task)
    assert reopened.criterion == criterion and reopened.test_cases == before.test_cases
    steps = saved_draft(reopened).plan.part_rubric.parts[0].steps
    assert [(step.test_suite, step.total_pts) for step in steps] == [(True, 7), (False, 3)]
    from backend.services.programming_input_domain import input_domain

    assert {input_domain(reopened, case) for case in reopened.test_cases} == {"allowed"}


def test_adding_cases_can_recover_a_previously_dropped_explicit_formula():
    criterion = "测试占 6 分，按通过比例算。代码正确得 4 分。"
    row = _rows([{**source(reference_answer="Square the input"), "criterion": criterion}])["q1"]
    owner, task = _seed_task()
    task_facade._replace_draft_questions(task, owner, {"q1": row}, "prepared.json")
    assert saved_draft(saved_problem(owner, task)) is None
    client = TestClient(create_app(), headers={"Authorization": "Bearer " + create_token(owner, "teacher")})
    url = f"/tasks/{task}/problems/q1"
    assert client.put(url, json={"review_status": "confirmed"}).status_code == 422
    response = client.put(url, json={"test_cases": [{"io_mode": "function",
        "input": str(i), "expected_output": str(i*i)} for i in range(10)]})
    assert response.status_code == 200, response.text
    result = saved_draft(saved_problem(owner, task))
    assert result and result.plan.part_rubric.parts[0].steps[0].test_suite
    assert client.put(url, json={"review_status": "confirmed"}).status_code == 200


@pytest.mark.asyncio
async def test_explicitly_removing_test_points_releases_the_confirmation_gate():
    owner, task, client, url = await setup_question()
    response = client.put(url, json={
        "criterion": "代码正确得 10 分，不设测试分。", "test_cases": [],
    })
    assert response.status_code == 200, response.text
    assert saved_draft(saved_problem(owner, task)) is None
    assert client.put(url, json={"review_status": "confirmed"}).status_code == 200
