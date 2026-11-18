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


async def setup_question(*, stale=False, funded=True):
    row = source()
    row["reference_answer"] = "Return the square of the input integer."
    row = _rows([row])["q1"]
    if funded:
        cases = await resolve_generated_outputs(row, inputs())
        problem = frozen_problem(row, [case.model_dump(mode="json") for case in cases])
        row = problem._math_rubric_source["presentation"]
        if stale:
            row["rubric_draft"]["content_fingerprint"] = "0" * 64
    else:
        row["criterion"] = "代码正确得 10 分。"
    owner, task = _seed_task()
    task_facade._replace_draft_questions(task, owner, {"q1": row}, "prepared.json")
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
async def test_stem_change_requires_reaffirming_the_saved_test_allocation():
    owner, task, client, url = await setup_question()
    response = client.put(url, json={"stem": "Implement square(x): x is an integer. Return x*x*x."})
    assert response.status_code == 200, response.text
    assert saved_draft(saved_problem(owner, task)) is None
    assert client.put(url, json={"review_status": "confirmed"}).status_code == 422


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
@pytest.mark.parametrize("points, criterion, funded", [
    (6, "代码正确得 4 分。", True),
    (7.5, "Correct algorithm: 2.5 points.", True),
    (6, "代码注释得 4 分。", False),
    (6, "Use clear invariant comments and concise names.", False),
])
async def test_structured_test_budget_requires_samples_and_keeps_quality_out_of_input_checks(
    monkeypatch, points, criterion, funded,
):
    import json
    from unittest.mock import AsyncMock
    from backend.models import StudentAnswerInfo
    from backend.services.programming_rubric import domain_criterion
    from backend.skills.programming import ProgrammingSkill
    from backend.tests.test_question_material_format_repair import Provider

    owner, task, client, url = await setup_question(funded=funded)
    before = saved_problem(owner, task)
    code_points = 10 - points
    policy = {"test_points": points, "input_rules": ""}
    saved = client.put(url, json={"criterion": criterion, "max_score": 10,
                                  "programming_scoring": policy, "test_cases": []})
    assert saved.status_code == 200, saved.text
    problem_view = saved.json()["problem"]
    assert problem_view["programming_scoring"] == {**policy, "code_criterion": criterion}
    assert any(issue["details"].get("reason") == "required_programming_tests_missing"
               for issue in problem_view["preparation_issues"])
    after = saved_problem(owner, task)
    assert after.criterion == criterion and domain_criterion(after) == ""
    draft = saved_draft(after)
    assert draft is not None and not after.test_cases
    steps = draft.plan.part_rubric.parts[0].steps
    assert [(step.test_suite, step.total_pts) for step in steps] == [(True, points), (False, code_points)]
    blocked = client.put(url, json={"review_status": "confirmed"})
    assert blocked.status_code == 422 and blocked.json()["error"]["code"] == "question_review_blocked"
    questions = assignment_repository.list_questions(task, teacher_id=owner)
    assert task_facade._question_review_blockers(questions) == ["question_review_blocked"]

    cases = before.test_cases or await resolve_generated_outputs(source(), inputs())
    repaired = client.put(url, json={"test_cases": [case.model_dump(mode="json") for case in cases]})
    assert repaired.status_code == 200, repaired.text
    assert client.put(url, json={"review_status": "confirmed"}).status_code == 200
    after = saved_problem(owner, task)
    assert saved_draft(after).plan.part_rubric.parts[0].steps == steps

    monkeypatch.setattr("backend.skills.programming.kb_tool.retrieve_for_grading", AsyncMock(return_value=[]))
    provider = Provider([json.dumps({"max_score": 10, "score": 10, "confidence": .9, "steps": [
        {"step_no": 1, "max_points": points, "score": points, "desc": "Tests", "is_correct": True},
        {"step_no": 2, "max_points": code_points, "score": code_points, "desc": "Code", "is_correct": True},
    ]})])
    vote = await ProgrammingSkill(provider, language="zh").grade(after,
        StudentAnswerInfo(q_id="q1", number="1", type="编程题",
                          content="def square(x): return x*x if x < 7 else -1"))
    assert vote.error_kind is None, vote.comment
    assert len(provider.calls) == 1
    assert vote.programming_evidence.generated_tests.passed == 7
    assert vote.programming_evidence.generated_tests.total == 10
    assert vote.steps[0].score == pytest.approx(points * .7)
    assert vote.score == pytest.approx(code_points + points * .7)


@pytest.mark.asyncio
@pytest.mark.parametrize("funded", [False, True])
@pytest.mark.parametrize("criterion", [
    "通过测试的比例乘以 6 分，代码正确得 4 分。",
    "按测试通过比例给 6 分，代码正确得 4 分。",
    "代码正确得 10 分。",
])
async def test_prose_alone_cannot_set_or_cancel_test_points(funded, criterion):
    owner, task, client, url = await setup_question(funded=funded)
    before = saved_problem(owner, task)
    revision = workflow_repository.get_workflow(task, owner_id=owner).workflow_revision
    response = client.put(url, json={"criterion": criterion + " ", "expected_workflow_revision": revision})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "programming_test_award_required"
    after = saved_problem(owner, task)
    assert after.criterion == before.criterion and saved_draft(after) == saved_draft(before)
    assert workflow_repository.get_workflow(task, owner_id=owner).workflow_revision == revision


@pytest.mark.asyncio
async def test_unverified_saved_contract_stays_blocked_after_adding_tests():
    owner, task = _seed_task()
    row = _rows([{**source(reference_answer="Square the input"),
                  "criterion": "通过测试的比例乘以 6 分，代码正确得 4 分。"}])["q1"]
    mismatched = frozen_problem(_rows([source(reference_answer="Square the input")])["q1"], [])
    row["rubric_draft"] = mismatched._math_rubric_source["presentation"]["rubric_draft"]
    row["review_status"] = "confirmed"  # A historical confirmation did not verify the changed text.
    task_facade._replace_draft_questions(task, owner, {"q1": row}, "prepared.json")
    client = TestClient(create_app(), headers={"Authorization": "Bearer " + create_token(owner, "teacher")})
    url = f"/tasks/{task}/problems/q1"
    assert client.put(url, json={"review_status": "confirmed"}).status_code == 422
    response = client.put(url, json={"test_cases": [{"io_mode": "function",
        "input": str(i), "expected_output": str(i*i)} for i in range(10)]})
    assert response.status_code == 200, response.text
    assert response.json()["problem"]["programming_scoring"]["test_points"] is None
    assert client.put(url, json={"review_status": "confirmed"}).status_code == 422
    questions = assignment_repository.list_questions(task, teacher_id=owner)
    assert task_facade._question_review_blockers(questions) == ["question_review_blocked"]
    fixed = client.put(url, json={"criterion": "代码正确得 4 分。",
                                  "programming_scoring": {"test_points": 6, "input_rules": ""}})
    assert fixed.status_code == 200, fixed.text
    assert client.put(url, json={"review_status": "confirmed"}).status_code == 200


@pytest.mark.asyncio
async def test_explicit_zero_is_distinct_from_unset_test_points():
    owner, task, client, url = await setup_question()
    response = client.put(url, json={"criterion": "代码注释清晰。", "test_cases": [],
        "programming_scoring": {"test_points": 0, "input_rules": ""}})
    assert response.status_code == 200, response.text
    draft = saved_draft(saved_problem(owner, task))
    assert draft.programming_scoring.test_points == 0
    assert all(not step.test_suite for part in draft.plan.part_rubric.parts for step in part.steps)
    assert client.put(url, json={"review_status": "confirmed"}).status_code == 200


@pytest.mark.asyncio
async def test_changing_test_points_replaces_the_formula_with_the_same_code_text():
    owner, task, client, url = await setup_question()
    criterion = "Write clear comments explaining the algorithm."
    for points in (6, 7):
        response = client.put(url, json={"criterion": criterion,
            "programming_scoring": {"test_points": points, "input_rules": ""}})
        assert response.status_code == 200, response.text
        draft = saved_draft(saved_problem(owner, task))
        assert draft.programming_scoring.test_points == points
        steps = draft.plan.part_rubric.parts[0].steps
        assert [(step.test_suite, step.total_pts) for step in steps] == [(True, points), (False, 10 - points)]


@pytest.mark.asyncio
async def test_code_edit_keeps_the_same_tests_and_separately_saved_input_constraints(monkeypatch):
    from backend.models import TestCase
    from backend.services.programming_input_domain import input_domain

    owner, task, client, url = await setup_question()
    before = saved_problem(owner, task)
    policy = {"test_points": 6, "input_rules": "x >= 0."}
    for criterion in ("代码正确得 4 分。", "代码注释得 4 分。"):
        response = client.put(url, json={"criterion": criterion, "programming_scoring": policy})
        assert response.status_code == 200, response.text
    assert client.put(url, json={"review_status": "confirmed"}).status_code == 200
    problem = saved_problem(owner, task)
    assert problem.stem == before.stem and problem.test_cases == before.test_cases
    assert input_domain(problem, TestCase(io_mode="function", function_args=[-1])) == "outside"
    assert input_domain(problem, TestCase(io_mode="function", function_args=[0])) == "allowed"
    vote = await grade(monkeypatch, problem, "def square(x): return x*x")
    assert vote.error_kind is None and vote.score == 10
    assert vote.programming_evidence.generated_tests.passed == vote.programming_evidence.generated_tests.total == 10


@pytest.mark.asyncio
async def test_conflicting_explicit_code_points_roll_back_the_policy_and_text():
    owner, task, client, url = await setup_question()
    before = saved_problem(owner, task)
    response = client.put(url, json={"criterion": "通过测试的比例乘以 6 分，代码正确得 4 分。",
        "programming_scoring": {"test_points": 6, "input_rules": ""}})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "invalid_programming_test_award"
    after = saved_problem(owner, task)
    assert after.criterion == before.criterion and saved_draft(after) == saved_draft(before)


@pytest.mark.asyncio
async def test_declared_test_points_without_matching_frozen_formula_cannot_confirm():
    from copy import deepcopy
    from backend.db.session import session_scope
    from backend.db.assignment_repository import AssignmentQuestionRecord

    owner, task, client, url = await setup_question()
    response = client.put(url, json={"criterion": "代码注释得 4 分。",
        "programming_scoring": {"test_points": 6, "input_rules": ""}})
    assert response.status_code == 200, response.text
    row, = assignment_repository.list_questions(task, teacher_id=owner)
    damaged = deepcopy(row.source)
    damaged["presentation"]["rubric_draft"]["programming_scoring"]["test_points"] = 7
    # Simulate a previously persisted invalid snapshot, bypassing the normal
    # write validation that would reject it today.
    with session_scope() as session:
        session.get(AssignmentQuestionRecord, row.id).source = damaged
    assert saved_draft(saved_problem(owner, task)) is None
    assert client.put(url, json={"review_status": "confirmed"}).status_code == 422
    assert task_facade._question_review_blockers(assignment_repository.list_questions(task, teacher_id=owner))
