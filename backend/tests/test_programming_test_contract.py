"""Saved text, source attribution, execution arguments and the edit transaction."""
import json
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from backend.agents.ingest_agent import _saved_test_case_supported, parse_material_import_to_candidates
from backend.auth import create_token
from backend.db import assignment_repository, workflow_repository
from backend.main import create_app
from backend.models import ProblemInfo, TestCase
from backend.services import grading_adapter, task_facade
from backend.services.programming_test_contract import (
    ProgrammingCaseError, adapt_programming_cases, bind_programming_cases, literal_programming_case_groups, required_function_name,
)
from backend.tests.test_question_material_format_repair import Provider
from backend.tests.test_task_background_workflows import _seed_task
from backend.tools.code_interpreter import run_sandbox


@pytest.mark.parametrize("signature,raw,args,kwargs", [
    ("constant()", "", [], {}),
    ("constant()", "[]", [], {}),
    ("add(a,b)", "2、3", [2, 3], {}),
    ("add(a,b)", "2 3", [2, 3], {}),
    ("sort_items(items)", "[3,1,2]", [[3, 1, 2]], {}),
    ("sort_items(items)", "[]", [[]], {}),
    ("scale(x, /, *, factor)", "3, factor=4", [3], {"factor": 4}),
    ("match(*, value)", 'value={"a": [true, null, 0]}', [], {"value": {"a": [True, None, 0]}}),
    ("kw(**kwargs)", "left=1, right=2", [], {"left": 1, "right": 2}),
    ("echo(value)", '"a、b"', ["a、b"], {}),
])
def test_plain_call_freezes_original_and_is_idempotent(signature, raw, args, kwargs):
    stem = f"实现 {signature}。"
    case = TestCase(input=raw, expected_output="0", visibility="hidden", purpose="boundary")
    prepared, = adapt_programming_cases(stem, [case])
    assert (prepared.input, prepared.expected_output, prepared.source) == (raw, "0", "teacher")
    assert (prepared.function_args, prepared.function_kwargs or {}) == (args, kwargs)
    assert (prepared.visibility, prepared.purpose) == ("hidden", "boundary")
    # Includes function-only rows reloaded from actual serialized data.
    assert bind_programming_cases(stem, [prepared.model_dump()], "def unrelated(): pass") == [prepared]
    assert case.function_args is None


@pytest.mark.parametrize("expected,result", [
    ("", "''"), ("''", "''"), ("None", "None"), ("False", "False"), ('"5"', '"5"'),
    ("5", "5"), ("hello", "'hello'"), ("9007199254740993", "9007199254740993"),
    ('{"ok":false}', "{'ok': False}"), ("[[], [0, 2]]", "[[], [0, 2]]"),
])
def test_expected_value_and_type_survive_recovery(expected, result):
    stem = "Implement f(x)."
    case, = adapt_programming_cases(stem, [{"input": "1", "expected_output": expected}])
    assert case.expected_output == expected and case.expected_return == result
    assert adapt_programming_cases(stem, [case.model_dump()]) == [case]


def test_same_plain_material_function_or_stdin_including_an_explicit_false_expectation():
    raw = [dict(input="输入2、3，预期9", purpose="boundary", source="teacher")]
    fn, = bind_programming_cases("编写函数 add(a,b)", raw, "def add(a,b): return 5")
    assert (fn.function_name, fn.function_args, fn.expected_return, fn.input) == ("add", [2, 3], "9", raw[0]["input"])
    stem = "Read two integers from stdin, each on a separate line; print the sum."
    saved = adapt_programming_cases(stem, raw)
    std, = bind_programming_cases(stem, saved, "def helper(): pass")
    assert std.input == "2\n3" and std.expected_output == "9" and std.io_mode == "stdin"
    assert saved[0].input == raw[0]["input"]
    literal = dict(input="2,3\n", expected_output="5")
    assert bind_programming_cases("Read CSV integers from stdin.", [literal], "")[0].input == "2,3\n"


def test_unspecified_entry_uses_all_cases_without_first_def_or_mutating_tests():
    stem = "Write a function that adds two numbers."
    raw = [dict(input="2,3", expected_output="5"), dict(input="0,0", expected_output="42", source="llm_generated")]
    saved = adapt_programming_cases(stem, raw)
    correct_shape = "def helper(): pass\ndef another_name(a,b): return -1"
    bound = bind_programming_cases(stem, saved, correct_shape)
    assert [case.function_name for case in bound] == ["another_name"] * 2
    assert [case.expected_return for case in bound] == ["5", "42"]
    assert [case.source for case in bound] == ["teacher", "llm_generated"]
    assert all(case.function_name is None for case in saved)
    two = correct_shape + "\ndef second(a,b): pass"
    with pytest.raises(ProgrammingCaseError):
        bind_programming_cases(stem, saved, two)
    assert bind_programming_cases(stem, saved, two, entry_hint="second")[0].function_name == "second"
    for hint in ("helper", "typo"):
        with pytest.raises(ProgrammingCaseError):
            bind_programming_cases(stem, saved, two, entry_hint=hint)
    with pytest.raises(ProgrammingCaseError):
        bind_programming_cases(stem, saved, two + "\ndef second(): pass", entry_hint="second")


def test_explicit_entry_survives_wrong_student_name_or_signature_and_conflicting_hint():
    stem = "Implement the following:\n\ndef add(a, b):\n    ..."
    assert required_function_name(stem) == "add"
    for code in ("def wrong(a,b): pass", "def add(): pass", "def add(:"):
        case, = bind_programming_cases(stem, [dict(input="2, 3", expected_output="5")], code, entry_hint="wrong")
        assert (case.function_name, case.function_args, case.expected_return) == ("add", [2, 3], "5")
    with pytest.raises(ProgrammingCaseError):
        bind_programming_cases("Solve the problem.", [dict(input="1", expected_output="2")],
                               "def f(x): return x\nprint(input())", entry_hint="f")


@pytest.mark.parametrize("row", [
    {"function_name": "f", "function_args": "not-a-list", "expected_return": 1},
    {"function_name": "f", "expected_return": "5"},
    {"function_name": "f", "function_args": []},
    {"input": "0"},
    {"input": "0", "expected_output": "1", "comparison": "other"},
    {"input": "0", "expected_output": "1", "abs_tol": -1},
    {"input": "0", "expected_output": "1", "rel_tol": float("nan")},
    {"input": "0", "expected_output": "Inf", "comparison": "numeric"},
    {"input": "0", "expected_output": "not json", "comparison": "json"},
    {"function_name": "f", "function_args": [float("inf")], "expected_return": "0"},
    {"io_mode": "stdin", "input": "0", "expected_output": "2", "function_name": "f", "function_args": [0]},
    {"function_name": "f.bad", "function_args": [], "expected_return": "1"},
    {"function_name": "f", "function_args": [{"x": (1, 2)}], "expected_return": "1"},
    {"function_name": "f", "function_kwargs": {"bad name": 1}, "expected_return": "1"},
    {"io_mode": "function", "input": "__import__('os')", "expected_output": "1"},
    {"io_mode": "function", "input": "a=1, a=2", "expected_output": "1"},
])
def test_reject_incomplete_conflicting_or_nonliteral_cases(row):
    with pytest.raises(ProgrammingCaseError):
        adapt_programming_cases("", [row])


def test_malformed_or_conflicting_question_contracts_fail_before_binding():
    raw = [dict(input="2, 3", expected_output="5")]
    for stem in ("Implement f(x).", "def f(a,b):\ndef g(a,b):"):
        with pytest.raises(ProgrammingCaseError):
            adapt_programming_cases(stem, raw)
    with pytest.raises(ProgrammingCaseError):
        adapt_programming_cases("Implement add(a,b).", [*raw, {**raw[0], "expected_output": "6"}])


@pytest.mark.asyncio
async def test_imported_teacher_pair_uses_same_adapter_and_never_substitutes_an_answer():
    stem = "实现 add(a,b)。"
    row = {"q_id": "q1", "target": "test_cases", "match_status": "exact", "confidence": 1,
           "source_excerpt": "输入2、3，预期9", "test_cases": [
               {"input": "2、3", "expected_output": "9", "source": "llm_generated"}]}
    async def parse(candidate):
        return await parse_material_import_to_candidates(text="题目 1\n输入2、3，预期9",
            problems_data={"q1": dict(number="1", stem=stem, type="编程题", solution_code="return 5")},
            targets=["test_cases"], structure_mode="organized", extraction_hint="",
            provider=Provider([json.dumps({"candidates": [candidate]})]))
    imported, = await parse(row)
    case, = imported.test_cases
    assert (case.source, case.input, case.function_args, case.expected_return) == ("teacher", "2、3", [2, 3], "9")
    row["test_cases"][0]["expected_output"] = "5"
    with pytest.raises(ProgrammingCaseError):
        await parse(row)


@pytest.mark.asyncio
async def test_mounted_editor_persists_derived_fields_and_rolls_back_invalid_batch():
    owner, task = _seed_task(with_question=True)
    client = TestClient(create_app(), headers={"Authorization": "Bearer " + create_token(owner, "teacher")})
    url = f"/tasks/{task}/problems/q1"
    stem = "实现 fib(n)。"
    saved = client.put(url, json={"type":"programming", "stem": stem, "test_cases":[
        {"function_name":"fib", "function_args":[0], "expected_return":"0", "source":"teacher"},
        {"function_name":"fib", "function_args":[31], "expected_return":"999", "source":"llm_generated", "visibility":"hidden"}
    ]})
    assert saved.status_code == 200
    get = lambda: client.get(f"/tasks/{task}").json()
    before = get()
    cases = before["problem_data"]["q1"]["test_cases"]
    assert cases[0]["input"] == "[0]" and cases[0]["expected_output"] == "0"
    submitted = deepcopy(cases)
    submitted[0].update(input="[5]", expected_output="5", comparison="numeric", abs_tol=1e-6, rel_tol=0.01)
    saved = client.put(url, json={"expected_workflow_revision":before["workflow_revision"], "test_cases":submitted})
    assert saved.status_code == 200
    persisted = get()
    saved_cases = persisted["problem_data"]["q1"]["test_cases"]
    assert saved_cases[0] == {**submitted[0], "function_args":[5], "expected_return":"5"}
    assert saved_cases[1] == cases[1]
    question, = assignment_repository.list_questions(task, teacher_id=owner)
    assert question.test_cases == saved_cases
    grade_input = ProblemInfo.model_validate(grading_adapter.build_problem_store([question])["q1"])
    code = "def fib(n):\n    assert n == 5\n    return 5"
    effective = bind_programming_cases(grade_input.stem, grade_input.test_cases, code)
    assert [(c.function_args, c.expected_return, c.source) for c in effective] == [
        ([5], "5", "teacher"), ([31], "999", "llm_generated")]
    execution = await run_sandbox(code, effective)
    assert [row.passed for row in execution.results] == [True, False]
    # Original browser D02, including a simultaneous otherwise-valid change.
    invalid = client.put(url, json={"criterion":"Must roll back",
        "expected_workflow_revision":persisted["workflow_revision"],
        "test_cases":[submitted[0], {"function_name":"fib","function_args":"not-a-list","expected_return":1}]})
    assert invalid.status_code == 422 and invalid.json()["error"]["code"] == "invalid_programming_test_cases"
    assert invalid.json()["error"]["details"] == {"case_number": 2, "issue": "configuration"}
    duplicate = client.put(url, json={"test_cases": [
        {"input":"1", "expected_output":"1"}, {"input":"1", "expected_output":"2"}]})
    assert duplicate.status_code == 422
    assert duplicate.json()["error"]["details"] == {"case_number": 2, "issue": "conflicting_expectations"}
    assert get()["problem_data"] == persisted["problem_data"]
    assert get()["workflow_revision"] == persisted["workflow_revision"]
    assert assignment_repository.list_questions(task, teacher_id=owner)[0].version == question.version


def test_recovered_package_and_editor_agree_on_keyword_only_contract():
    owner, task = _seed_task()
    stem = "Implement scale(x, *, factor)."
    task_facade._replace_draft_questions(task, owner, {"q1": dict(q_id="q1", number="1",
        type="programming", stem=stem, criterion="Correct results.", test_cases=[
            {"input":"2, factor=3", "expected_output":"6", "source":"teacher", "comparison":"numeric",
             "abs_tol":0, "rel_tol":0}])}, "restored.json")
    question, = assignment_repository.list_questions(task, teacher_id=owner)
    case, = question.test_cases
    assert (case["function_name"], case["function_args"], case["function_kwargs"]) == ("scale", [2], {"factor":3})
    edit = {**case, "input":"4, factor=2", "expected_output":"8"}
    task_facade.update_problem(task_id=task, owner_id=owner, q_id="q1", patch={"test_cases":[edit]})
    restored = task_facade.get_task(task_id=task, owner_id=owner)["problem_data"]["q1"]["test_cases"]
    effective, = bind_programming_cases(stem, restored, "")
    assert (effective.function_args, effective.function_kwargs, effective.expected_return) == ([4], {"factor":2}, "8")


def test_plain_input_list_remains_unbound_until_an_unspecified_signature_is_known():
    stem = "Write a function to process the input."
    saved = adapt_programming_cases(stem, [dict(input="[2,3]", expected_output="5")])
    assert bind_programming_cases(stem, saved, "def foo(a,b):pass")[0].function_args == [2, 3]
    assert bind_programming_cases(stem, saved, "def bar(xs):pass")[0].function_args == [[2, 3]]
    with pytest.raises(ProgrammingCaseError):
        bind_programming_cases(stem, saved, "def foo(a,b):pass\ndef bar(xs):pass")


def test_explicit_declaration_selects_required_entry_even_with_a_provided_helper():
    stem = "Implement add(a,b).\ndef helper(x): return x"
    assert required_function_name(stem) == "add"
    case, = bind_programming_cases(stem, [dict(input="2,3", expected_output="5")], "def helper(x):pass")
    assert (case.function_name, case.function_args) == ("add", [2, 3])


@pytest.mark.asyncio
async def test_numbered_teacher_sheet_retains_all_cases_with_zero_provider_calls():
    text = "Question 1\n输入2、3，预期5\n输入100、-3，预期99\n输入2、3，预期5"
    provider = Provider([])
    plan = await parse_material_import_to_candidates(text=text, problems_data={"q1": dict(
        number="1", type="编程题", stem="实现 add(a,b)。")}, targets=["test_cases"],
        structure_mode="organized", extraction_hint="", provider=provider)
    assert not provider.calls and len(plan) == 1 and plan[0].match_status == "exact"
    assert [(c.function_args, c.expected_return, c.source) for c in plan[0].test_cases] == [
        ([2, 3], "5", "teacher"), ([100, -3], "99", "teacher"), ([2, 3], "5", "teacher")]

def test_explicitly_free_name_overrides_an_example_name_and_uses_mapped_entry():
    stem = "Write a function; any function name is allowed. Example: def demo(x): ..."
    cases = [dict(function_name="demo", function_args=[1], expected_return="2")]
    assert required_function_name(stem) is None
    case, = bind_programming_cases(stem, cases, "def helper(x):pass\ndef mine(x):pass", entry_hint="mine")
    assert case.function_name == "mine" and case.function_args == [1]


def test_multiple_accepted_import_groups_survive_apply_and_replay():
    owner, task = _seed_task(with_question=True)
    client = TestClient(create_app(), headers={"Authorization": "Bearer " + create_token(owner, "teacher")})
    first = client.put(f"/tasks/{task}/problems/q1", json={"type":"programming", "stem":"Implement add(a,b)."})
    revision = first.json()["workflow_revision"]
    cases = [{"input":"2、3", "expected_output":"5"}, {"input":"2、-3", "expected_output":"-2"}]
    candidates = [dict(candidate_id=f"c{i}", q_id="q1", target="test_cases", confidence=1,
        match_status="exact", test_cases=[case]) for i, case in enumerate(cases)]
    job, _ = workflow_repository.create_operation(assignment_id=task, owner_id=owner,
        operation_type="material_import", input_hash="c" * 64, payload={"candidates":candidates})
    workflow_repository.update_operation(job.id, owner_id=owner, expected_attempt=job.attempt, status="ready")
    url = f"/tasks/{task}/material-imports/{job.id}/apply"
    request = {"accepted_candidate_ids":["c0", "c1"], "expected_workflow_revision":revision}
    for _ in range(2):
        response = client.post(url, json=request)
        assert response.status_code == 200, response.text
        question, = assignment_repository.list_questions(task, teacher_id=owner)
        assert [case["function_args"] for case in question.test_cases] == [[2, 3], [2, -3]]
        assert [case["expected_return"] for case in question.test_cases] == ["5", "-2"]

def test_teacher_evidence_preserves_keyword_only_arguments_and_their_types():
    case = dict(function_name="run", function_kwargs={"active": True}, expected_return="1")
    assert _saved_test_case_supported(case, "", json.dumps(case))
    assert _saved_test_case_supported(case, "", "run(active=True) returns 1")
    assert not _saved_test_case_supported(case, "", "run(active=1) returns 1")
    assert not _saved_test_case_supported(case, "", json.dumps({**case, "function_kwargs": {"active": 1}}))

@pytest.mark.parametrize("stem", ["请实现函数 add，返回两个数的和。", "实现一个名为 add 的函数。",
    "Implement a function named add.", "Implement `add` to return the sum."])
def test_named_requirement_without_signature_is_not_renamed(stem):
    assert required_function_name(stem) == "add"
    case, = bind_programming_cases(stem, [dict(input="2、3", expected_output="5")], "def sum2(a,b):pass")
    assert case.function_name == "add" and case.function_args == [2, 3]


@pytest.mark.parametrize("line,original,expected", [
    ("2、3→5", "2、3", "5"), ("3、4 -> 7", "3、4", "7"),
    ('"a->b" → " c→d "', '"a->b"', '" c→d "'), ('1 -> ""', "1", '""'),
    ("input: left -> right, expected output: one → two", "left -> right", "one → two"),
    ("输入 2、3，预期", "2、3", ""),
])
def test_literal_sheet_separators_preserve_the_source(line, original, expected):
    groups = literal_programming_case_groups(line)
    assert [(n, [(c.input, c.expected_output, c.source) for c in cases]) for n, cases in groups] == [
        ("", [(original, expected, "teacher")])]
    assert _saved_test_case_supported(dict(input=original, expected_output=expected), "", line)


@pytest.mark.parametrize("text", [
    "2 ->", "-> 5", "1 -> 2 -> 3", "source -> destination", "def add(a, b) -> int:",
    '"a -> b"', '"unclosed -> 7', "sum(2, 3) → 5", "1 -> 2 # explanation",
    "Section notes\n2、3→5", "Question 1\n2、3→5\nQuestion 2\nsee diagram → appendix",
])
def test_ambiguous_or_partial_sheet_is_not_guessed(text):
    assert literal_programming_case_groups(text) is None


def test_quoted_or_chained_arrows_are_not_alternative_teacher_expectations():
    assert not _saved_test_case_supported(dict(input='"a', expected_output='b"'), "", '"a->b"')
    assert not _saved_test_case_supported(dict(input='"a', expected_output='b" -> "x"'), "", '"a->b" -> "x"')
    assert not _saved_test_case_supported(dict(input="1 -> 2", expected_output="3"), "", "1 -> 2 -> 3")


@pytest.mark.asyncio
async def test_numbered_arrow_sheet_routes_in_source_order_and_unnumbered_sheet_never_guesses():
    problems = {"q1": dict(number="1", type="编程题", stem="Implement add(a,b)."),
                "q2": dict(number="2", type="编程题", stem="Read two integers from stdin and print their sum.")}
    provider = Provider([])
    plan = await parse_material_import_to_candidates("Question 2\n3、4 -> 7\nQuestion 1\n2、3→5", problems,
        ["test_cases"], "organized", "", provider)
    assert not provider.calls
    assert [(p.q_id, p.source_location, p.match_status) for p in plan] == [("q2", "2", "exact"), ("q1", "1", "exact")]
    assert plan[0].test_cases[0].source == plan[1].test_cases[0].source == "teacher"
    stdin, = bind_programming_cases(problems["q2"]["stem"], plan[0].test_cases, "")
    assert (stdin.input, stdin.expected_output) == ("3 4", "7")
    assert plan[1].test_cases[0].function_args == [2, 3]
    for text, targets in [("2、3→5", ["test_cases"]), ("Question 3\n2、3→5", ["test_cases"]),
                          ("Question 1\n2、3→5", ["criterion"]), ("Question 1\nroad → destination", ["test_cases"])]:
        fallback = Provider([json.dumps({"candidates":[]})])
        assert await parse_material_import_to_candidates(text, problems, targets, "organized", "", fallback) == []
        assert len(fallback.calls) == 1


@pytest.mark.asyncio
async def test_unnumbered_arrow_txt_upload_apply_readback_and_execute(monkeypatch):
    from unittest.mock import AsyncMock
    from backend.api import task_preparation
    from backend.llm.registry import get_scoped_expert_registry
    from backend.tests.test_auxiliary_operation_recovery import _claim_auxiliary_operation
    from backend.tests.test_question_preparation_recovery import _RecoveryRegistry

    app = create_app()
    registry = _RecoveryRegistry()
    app.dependency_overrides[get_scoped_expert_registry] = lambda: registry
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: registry)
    forbidden = AsyncMock(side_effect=AssertionError("A literal test sheet made a model request"))
    monkeypatch.setattr("backend.agents.ingest_agent.ainvoke_with_retry", forbidden)
    owner, task = _seed_task(with_question=True)
    client = TestClient(app, headers={"Authorization": "Bearer " + create_token(owner, "teacher")})
    root = f"/tasks/{task}"
    stem = "实现 add(a,b)。"
    client.put(root + "/problems/q1", json={"type":"programming", "stem":stem}).raise_for_status()
    body = "2、3→5\n3、4 -> 7\n输入 4、4，预期 99"
    preflight = client.post(root + "/material-imports/preflight",
        files={"file":("teacher.txt", body.encode(), "text/plain")}, data={"targets":'["test_cases"]'})
    preflight.raise_for_status()
    started = client.post(root + "/material-imports", json={"source_token":preflight.json()["source_token"]})
    started.raise_for_status()
    job = started.json()["job_id"]
    await task_preparation.run_durable_material_import(_claim_auxiliary_operation(owner, job))
    plan_url = root + "/material-imports/" + job
    plan = client.get(plan_url).json()
    assert plan["status"] == "ready", plan
    candidate, = plan["candidates"]
    assert candidate["match_status"] == "exact" and candidate["source_location"] == ""
    applied = client.post(plan_url + "/apply", json={"accepted_candidate_ids":[candidate["candidate_id"]],
        "expected_workflow_revision":plan["workflow_revision"]})
    applied.raise_for_status()
    saved = client.get(root).json()["problem_data"]["q1"]["test_cases"]
    assert [(c["input"], c["expected_output"], c["source"]) for c in saved] == [
        ("2、3", "5", "teacher"), ("3、4", "7", "teacher"), ("4、4", "99", "teacher")]
    code = "def add(a,b): return a+b"
    bound = bind_programming_cases(stem, saved, code)
    execution = await run_sandbox(code, bound)
    assert [row.passed for row in execution.results] == [True, True, False]
    forbidden.assert_not_awaited()
