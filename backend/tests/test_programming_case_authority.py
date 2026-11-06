"""Teacher saves establish authority only for changed execution material."""
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from backend.auth import create_token
from backend.db import assignment_repository
from backend.main import create_app
from backend.models import ProblemInfo, StudentAnswerInfo
from backend.services import grading_adapter, task_facade
from backend.services.programming_runs import evidence_for_answer
from backend.tests.test_task_background_workflows import _seed_task
from backend.tools.code_interpreter import run_sandbox


def setup_cases(*, expected="2", extra=()):
    owner, task = _seed_task()
    task_facade._replace_draft_questions(task, owner, {"q1": {
        "q_id": "q1", "number": "1", "type": "programming",
        "stem": "实现 f(xs)。xs 是有序整数列表，返回列表长度。",
        "criterion": "正确得到列表长度给满分。",
        "test_cases": [{"function_name": "f", "function_args": [[1, 2]],
                        "expected_return": expected, "source": "llm_generated"}, *extra],
    }}, "prepared.json")
    client = TestClient(create_app(), headers={"Authorization": "Bearer " + create_token(owner, "teacher")})
    return owner, task, client, f"/tasks/{task}/problems/q1"


def snapshot(client, task):
    return client.get(f"/tasks/{task}").json()


async def execute_saved(owner, task):
    question, = assignment_repository.list_questions(task, teacher_id=owner)
    raw = grading_adapter.build_problem_store([question])["q1"]
    problem = ProblemInfo.model_validate(raw)
    problem._math_rubric_source = raw["_math_rubric_source"]
    return await evidence_for_answer(problem=problem,
        answer=StudentAnswerInfo(q_id="q1", number="1", type="编程题", content="def f(xs): return len(xs)"),
        student_id="synthetic", task_id=task, provider=None, reporter=None,
        generate=None, execute=run_sandbox)


@pytest.mark.asyncio
@pytest.mark.parametrize("edit", ["input", "expected", "both"])
async def test_editing_ai_case_is_executable_teacher_material_with_original_audit(edit):
    owner, task, client, url = setup_cases(expected="1" if edit != "input" else "2")
    before = snapshot(client, task)
    cases = deepcopy(before["problem_data"]["q1"]["test_cases"])
    if edit in {"input", "both"}:
        cases[0]["input"] = "[[2, 3]]"
    if edit in {"expected", "both"}:
        cases[0]["expected_output"] = "2"
    response = client.put(url, json={"expected_workflow_revision": before["workflow_revision"], "test_cases": cases})
    assert response.status_code == 200
    assert client.put(url, json={"review_status": "confirmed"}).status_code == 200
    question, = assignment_repository.list_questions(task, teacher_id=owner)
    assert question.test_cases[0]["source"] == "teacher"
    originals = question.source["programming_original_ai_cases"]
    assert originals[0] == before["problem_data"]["q1"]["test_cases"][0]
    assert originals[0]["source"] == "llm_generated"
    run = await execute_saved(owner, task)
    assert run.status == "executed" and run.report.passed_count == 1
    assert run.report.results[0].test.source == "teacher"


@pytest.mark.asyncio
@pytest.mark.parametrize("edit", [None, {}, {"title": "Reviewed"}, {"description": "Checked"},
    {"visibility": "hidden"}, {"source": "teacher"}, {"input": "[[1,2]]"},
    {"comparison": "numeric", "abs_tol": 0.01}])
async def test_confirmation_and_non_execution_edits_never_authorize_ai_cases(edit):
    owner, task, client, url = setup_cases()
    cases = snapshot(client, task)["problem_data"]["q1"]["test_cases"]
    if edit is not None:
        cases[0].update(edit)
        assert client.put(url, json={"test_cases": cases}).status_code == 200
    assert client.put(url, json={"review_status": "confirmed"}).status_code == 200
    question, = assignment_repository.list_questions(task, teacher_id=owner)
    assert question.test_cases[0]["source"] == "llm_generated"
    run = await execute_saved(owner, task)
    assert run.status == "unavailable" and run.reason == "programming_input_domain_unverified"


def test_reorder_add_remove_restore_and_existing_teacher_cases_preserve_authority():
    owner, task, client, url = setup_cases(extra=[
        {"function_name": "f", "function_args": [[3]], "expected_return": "1", "source": "teacher"},
        {"function_name": "f", "function_args": [[]], "expected_return": "0", "source": "llm_generated"},
    ])
    original = snapshot(client, task)["problem_data"]["q1"]["test_cases"]
    cases = [deepcopy(original[2]), deepcopy(original[0]), deepcopy(original[1])]
    for case in cases:
        case["source"] = "teacher"  # Source is not a client-controlled grant.
    cases[2]["source"] = "llm_generated"
    assert client.put(url, json={"test_cases": cases}).status_code == 200
    saved = snapshot(client, task)["problem_data"]["q1"]["test_cases"]
    assert [c["source"] for c in saved] == ["llm_generated", "llm_generated", "teacher"]
    # Add a teacher-authored example while removing one AI example.
    added = {"input": "[[4, 5, 6]]", "expected_output": "3"}
    assert client.put(url, json={"test_cases": [saved[1], added, saved[2]]}).status_code == 200
    saved = snapshot(client, task)["problem_data"]["q1"]["test_cases"]
    assert [c.get("source", "teacher") for c in saved] == ["llm_generated", "teacher", "teacher"]
    saved[0]["input"] = "[[6, 7]]"
    assert client.put(url, json={"test_cases": saved}).status_code == 200
    saved = snapshot(client, task)["problem_data"]["q1"]["test_cases"]
    assert saved[0]["source"] == "teacher"
    # Restoring the original AI payload is not a new teacher-authored case.
    saved[0].update(input=original[0]["input"], expected_output=original[0]["expected_output"])
    saved.append({**original[2], "source": "teacher"})
    assert client.put(url, json={"test_cases": saved}).status_code == 200
    restored = snapshot(client, task)["problem_data"]["q1"]["test_cases"]
    assert [c.get("source", "teacher") for c in restored] == ["llm_generated", "teacher", "teacher", "llm_generated"]
    question, = assignment_repository.list_questions(task, teacher_id=owner)
    assert len(question.source["programming_original_ai_cases"]) == 2


def test_stale_or_invalid_save_cannot_mutate_authority_or_its_audit():
    owner, task, client, url = setup_cases()
    before = snapshot(client, task)
    cases = before["problem_data"]["q1"]["test_cases"]
    cases[0]["input"] = "[[3, 4]]"
    assert client.put(url, json={"expected_workflow_revision": before["workflow_revision"], "test_cases": cases}).status_code == 200
    question, = assignment_repository.list_questions(task, teacher_id=owner)
    stale = client.put(url, json={"expected_workflow_revision": before["workflow_revision"], "test_cases": []})
    assert stale.status_code == 409
    invalid = client.put(url, json={"test_cases": [{"input": "[[9]]", "expected_output": "1"},
        {"input": "[[9]]", "expected_output": "2"}]})
    assert invalid.status_code == 422
    after, = assignment_repository.list_questions(task, teacher_id=owner)
    assert (after.source, after.test_cases, after.version) == (question.source, question.test_cases, question.version)


@pytest.mark.parametrize("reverse", [False, True])
def test_identical_teacher_material_remains_authoritative_independent_of_row_order(reverse):
    owner, task, client, url = setup_cases(extra=[
        {"function_name": "f", "function_args": [[1, 2]], "expected_return": "2", "source": "teacher"},
    ])
    cases = snapshot(client, task)["problem_data"]["q1"]["test_cases"]
    if reverse:
        cases.reverse()
    for case in cases:
        case["source"] = "llm_generated"
    assert client.put(url, json={"test_cases": cases}).status_code == 200
    question, = assignment_repository.list_questions(task, teacher_id=owner)
    assert all(case["source"] == "teacher" for case in question.test_cases)
    assert question.source["programming_original_ai_cases"][0]["source"] == "llm_generated"


@pytest.mark.parametrize("patch", [
    {"stem": "实现 f(xs)。xs 是有序整数列表。返回列表长度。"},
    {"criterion": "结果正确给 10 分。"},
])
def test_other_question_edits_do_not_authorize_ai_cases(patch):
    owner, task, client, url = setup_cases()
    assert client.put(url, json=patch).status_code == 200
    question, = assignment_repository.list_questions(task, teacher_id=owner)
    assert question.test_cases[0]["source"] == "llm_generated"
    assert "programming_original_ai_cases" not in question.source
