"""Programming votes, source provenance and the released grade use the same execution facts."""
import asyncio
import csv
from io import StringIO
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from backend.agents.multi_expert import run_multi_expert
from backend.db import assignment_repository, course_repository, grading_repository, submission_repository
from backend.db.models import UserRecord
from backend.db.session import session_scope
from backend.domain.errors import LeaseLost, ResultNotReleasable
from backend.llm.providers import LLMResponse
from backend.models import TaskGradingSetup, TestCase
from backend.services import grading_runs, task_facade
from backend.services.grading_checkpoints import CheckpointWriteError
from backend.skills import programming
from backend.tests.test_programming_skill import _make_answer, _make_problem
from backend.tools.structured_llm import (
    DailyQuotaError, PermanentLLMError, StructuredOutputInvalidError,
)


def response(value):
    return LLMResponse(content=json.dumps(value), provider="fixed", model="fixed", duration_ms=1)


def award(score, *, maximum=10, subtotal=None):
    return {"score": score, "max_score": maximum, "confidence": .95, "comment": "rubric reasoning",
            "steps": [{"step_no": 1, "desc": "Apply the stated criterion to this implementation.",
                       "is_correct": score == maximum, "score": score if subtotal is None else subtotal}]}


def fixed_provider(*outputs, name="fixed"):
    return SimpleNamespace(provider_id=name, ainvoke=AsyncMock(side_effect=outputs))


@pytest.mark.asyncio
async def test_generation_uses_frozen_sources_and_only_repairs_model_output():
    problem = _make_problem(stem="Required:\ndef compute(x):")
    problem.reference_answer = "independent teacher reference"
    problem.solution_code = "def compute(x): return 0"
    valid = {"cases": [{"function_name": "compute", "function_args": [1], "expected_return": "0",
                         "title": "boundary", "purpose": "boundary", "source": "teacher"}]}
    wrong = {"cases": [{"function_name": "helper", "function_args": [1], "expected_return": "0"}]}
    route = fixed_provider(response(wrong), response(valid))
    cases, error = await programming._generate_test_cases(route, problem)
    assert error is None and len(cases) == 1
    assert cases[0].function_name == "compute" and cases[0].source == "llm_generated"
    assert cases[0].purpose == cases[0].title == "boundary"
    first = route.ainvoke.await_args_list[0].args[0]
    material = json.loads(first[1].content)
    assert material == {"problem": problem.stem, "rubric": problem.criterion, "max_score": 10,
                        "reference_answer": problem.reference_answer, "solution_code": problem.solution_code,
                        "required_function_name": "compute"}
    assert route.ainvoke.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("exc, expected", [
    (DailyQuotaError(), "daily_quota_exhausted"), (PermanentLLMError("provider_zero_quota"), "zero_quota"),
    (PermanentLLMError("401"), "provider_auth_failed"), (TimeoutError(), "transient_llm"),
    (PermanentLLMError("provider_content_blocked"), "provider_content_blocked"),
    (PermanentLLMError("provider_submit_uncertain"), "provider_submit_uncertain"),
    (StructuredOutputInvalidError("malformed"), "parse_failed"), (ValueError("invalid operation"), "general"),
])
async def test_generate_failure_kinds(monkeypatch, exc, expected):
    mock = AsyncMock(side_effect=exc)
    monkeypatch.setattr(programming, "structured_llm_call", mock)
    assert await programming._generate_test_cases(fixed_provider(), _make_problem()) == ([], expected)
    mock.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [CheckpointWriteError("lease"), LeaseLost("lease"), asyncio.CancelledError()])
async def test_critical_run_failures_never_become_votes(monkeypatch, failure):
    from backend.services import programming_runs
    monkeypatch.setattr(programming_runs, "evidence_for_answer", AsyncMock(side_effect=failure))
    route = fixed_provider()
    with pytest.raises(type(failure)):
        await programming.ProgrammingSkill(route).grade(_make_problem(), _make_answer("print(0)"))
    route.ainvoke.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("score, subtotal, max_points, wanted", [
    (20, 20, 100, 2), (1, 1.01, 10, 1), (1, 1.011, 10, None),
    (0, 0, 10, 0), (100.5, 100.5, 100, 10), (-1, -1, 10, None),
    (float("nan"), 1, 10, None), (float("inf"), 1, 10, None),
])
async def test_rubric_awards_are_validated_within_existing_format_repair(score, subtotal, max_points, wanted):
    data = response(award(score, maximum=max_points, subtotal=subtotal))
    route = fixed_provider(data, data)
    problem = _make_problem(test_cases=[TestCase(input="", expected_output="1")])
    result = await programming.ProgrammingSkill(route).grade(problem, _make_answer("print(1)"))
    if wanted is None:
        assert result.error_kind == "parse_failed" and result.programming_evidence.score_status == "unavailable"
        assert route.ainvoke.await_count == 2
    else:
        assert result.error_kind is None and result.score == wanted
        route.ainvoke.assert_awaited_once()


@pytest.mark.asyncio
async def test_mixed_sources_late_failures_and_bounded_distinct_reasons():
    cases = [TestCase(input=str(i), expected_output=str(i)) for i in range(5)]
    cases += [TestCase(input=str(i), expected_output="wanted " + "x" * 1000) for i in (5, 6, 7)]
    cases += [TestCase(input="8", expected_output="8", source="llm_generated")]
    route = fixed_provider(response(award(7)))
    problem = _make_problem(test_cases=cases)
    problem.criterion = "Award 7 points for reading input; 3 for all correct outputs."
    code = "n=int(input())\nif n==8: raise ValueError('PRIVATE_ERROR')\nprint(n)"
    result = await programming.ProgrammingSkill(route).grade(problem, _make_answer(code))
    evidence = result.programming_evidence
    assert result.score == 7 and evidence.teacher_tests.model_dump() == {"passed": 5, "total": 8}
    assert evidence.generated_tests.model_dump() == {"passed": 0, "total": 1}
    assert evidence.failure_counts == {"wrong_answer": 3, "runtime_error": 1}
    assert "teacher-provided tests: passed 5/8" in result.comment and "AI-generated tests: passed 0/1" in result.comment
    material = json.loads(route.ainvoke.await_args.args[0][1].content)
    examples = material["case_examples"]
    assert [(r["case_label"], r["outcome"]) for r in examples[:2]] == [
        ("case-6", "wrong_answer"), ("case-9", "runtime_error")]
    assert examples[0]["input"] == "5"
    assert examples[0]["actual"].strip() == "5" and examples[0]["expected"].startswith("wanted")
    assert len(examples) <= 6 and len(examples[0]["expected"]) <= 140
    assert examples[1]["error_type"] == "ValueError" and "PRIVATE_ERROR" not in json.dumps(examples)
    assert all(r["case_id"] for r in examples)
    assert material["submitted_source"] == code and material["rubric"] == problem.criterion


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["weighted_average", "judge_agent"])
async def test_selected_experts_share_one_execution_and_weighted_real_zero(monkeypatch, method):
    from backend.tools.code_interpreter import run_sandbox
    execute = AsyncMock(wraps=run_sandbox)
    monkeypatch.setattr(programming, "run_sandbox", execute)
    a = fixed_provider(response(award(0)), response({**award(1), "comment": "Combined feedback."}), name="a")
    b = fixed_provider(response(award(8)), name="b")
    registry = SimpleNamespace(list_available=lambda: [a,b], get={"a":a, "b":b}.get, pick_default=lambda:a)
    setup = TaskGradingSetup(selected_provider_ids=["a","b"], primary_provider_id="a", aggregation_method=method,
                             expert_weights={"a":1,"b":3})
    cases = [TestCase(input="", expected_output="1", source="llm_generated")]
    corr = await run_multi_expert(problem=_make_problem(test_cases=cases), answer=_make_answer("print(0)"),
                                  registry=registry, student_id="s1", grading_setup=setup, aggregation_method=method)
    assert corr.score == 6 and corr.synthesis_method == method
    assert sum(s.score for s in corr.steps) == corr.score
    assert sorted(e.score for e in corr.expert_results) == [0, 8]
    assert len({e.programming_evidence.evidence_id for e in corr.expert_results}) == 1
    assert corr.requires_human_review and "programming_ai_only" in corr.review_reasons
    assert "AI-generated tests: passed 0/1" in corr.comment
    execute.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["ai_only_confirm", "ai_only_edit", "code_only", "entry_only", "teacher_zero", "platform", "configuration", "no_submission"])
async def test_run_review_finalization_and_release(monkeypatch, scenario):
    await exercise_review_and_release(monkeypatch, scenario)


async def exercise_review_and_release(monkeypatch, scenario, *, native=False):
    from backend.config import settings
    from backend.tools.oci_grading_runner import IsolatedRunnerUnavailable
    monkeypatch.setattr(settings, "multi_sample_n", 1)
    suffix = uuid4().hex[:10]
    teacher, student = f"program-teacher-{suffix}", f"program-student-{suffix}"
    with session_scope() as session:
        for uid, role in [(teacher, "teacher"), (student, "student")]:
            session.add(UserRecord(id=uid, username=uid, password_hash="h", role=role, is_active=True))
    task = task_facade.create_task(owner_id=teacher, name="Programming", semester_id=None,
                                   course_id=None, idempotency_key=suffix)
    assignment = assignment_repository.get_assignment(task["task_id"], actor_id=teacher)
    args = {"task_id": assignment.id, "owner_id": teacher}
    course_repository.enroll(course_id=assignment.course_id, student_id=student)
    source = "llm_generated" if scenario.startswith("ai_only") else "teacher"
    cases = [{"input":"4", "expected_output":"4", "source":source}]
    if scenario == "configuration":
        cases += [{"input":"4", "expected_output":"5", "source":"teacher"}]
    stem = "Read stdin and print the input."
    if scenario == "entry_only":
        stem = "Implement def add(a,b): return the sum."
        cases = [{"function_name": "add", "function_args": [2, 3], "expected_return": "5", "source": "teacher"}]
    q = assignment_repository.add_question(assignment.id, teacher_id=teacher, q_id="q1", order_index=0,
        type="编程题", stem=stem, criterion="Exact output.", test_cases=cases)
    assignment_repository.publish(assignment_id=assignment.id, teacher_id=teacher, expected_version=1)
    sub = submission_repository.create_submission(assignment_id=assignment.id, student_id=student)
    code = "" if scenario == "no_submission" else "print(0)"
    if scenario == "entry_only":
        code = "def add(pair): return pair[0] + pair[1]"
    if scenario == "code_only":
        code = "#include <iostream>\nint main(){return 0;}"
    flags = []
    if native:
        from backend.services.programming_submission_mapping import source_answers
        code = '#include <stdio.h>\nint main(void){puts("0");}'
        mapped, = source_answers("answer.c", code, {"q1":{"type": q.type, "stem": q.stem}}, {"q1"})
        flags = mapped["flag"]
    submission_repository.add_revision(submission_id=sub.id, student_id=student, source="online",
        answers=[{"question_id":q.id, "q_id":q.q_id, "type":q.type, "content":code, "flag":flags}])
    if scenario == "platform":
        if native:
            monkeypatch.setattr(settings, "program_runner_image", "smartai-intentionally-absent:missing")
        else:
            monkeypatch.setattr(programming, "run_sandbox", AsyncMock(side_effect=IsolatedRunnerUnavailable()))
    ai_score = 7 if scenario in {"ai_only_edit", "code_only", "entry_only"} else 0
    route = fixed_provider(response(award(ai_score)))
    registry = SimpleNamespace(count=lambda: 1, list_available=lambda: [route], pick_default=lambda: route)
    run = grading_runs.create_run(teacher_id=teacher, assignment_id=assignment.id)
    await grading_runs.process_run(run_id=run.id, worker_id="worker", registry=registry)
    row, = grading_repository.list_results_for_run(run.id)
    public = task_facade._serialize_correction(row)
    evidence = row.ai_expert_results[0]["programming_evidence"]
    assert row.ai_expert_results[0]["raw_output"] is None
    if scenario in {"platform", "configuration"}:
        route.ainvoke.assert_not_awaited()
        assert row.result_status == "failed" and row.ai_score is None and public["score"] is None
        assert evidence["score_status"] == "unavailable" and task_facade._correction_review_blocks_finalization(row)
        reason, zh, en = (
            ("programming_execution_unavailable", "测试服务未能完成运行", "test service could not finish")
            if scenario == "platform" else
            ("programming_conflicting_expectations", "同一输入有不同的预期结果", "same input has different expected results")
        )
        assert evidence["primary_reason"] == reason and reason not in public["comment"]
        assert zh in public["comment"] or en in public["comment"]
        assert public["expert_results"][0]["comment"] == public["comment"]
        failure = task_facade._grading_failure_code(grading_repository.get_run(run.id))
        assert failure == ("programming_execution_unavailable" if scenario == "platform"
                           else "programming_test_material_needs_review")
        with pytest.raises(ResultNotReleasable):
            grading_runs.release(teacher_id=teacher, run_id=run.id)
        assert not grading_runs.student_results(student_id=student, assignment_id=assignment.id)
    elif scenario in {"ai_only_confirm", "ai_only_edit", "code_only", "entry_only"}:
        reason = {"code_only": "programming_code_review_only", "entry_only": "programming_interface_only"}.get(scenario, "programming_ai_only")
        if scenario == "entry_only":
            assert evidence["primary_reason"] == "signature_mismatch" and evidence["teacher_tests"]["total"] == 1
        assert public["provisional_score"] == public["score"] == ai_score
        assert evidence["score_status"] == "provisional" and reason in row.initial_review_reasons

        def assert_blocked():
            state = task_facade.finalization(**args)
            assert not state["ready_for_confirmation"] and state["remaining_review_count"] == 1
            assert reason in state["remaining_reviews"][0]["reasons"]
            assert state["required_review_count"] == 1 and state["confirmed_required_count"] == 0
            with pytest.raises(ResultNotReleasable, match="programming_review_required"):
                grading_runs.release(teacher_id=teacher, run_id=run.id)
            with pytest.raises(ResultNotReleasable, match="programming_review_required"):
                task_facade.generate_artifacts(**args, expected_revision=state["workflow_revision"])
            assert not task_facade.artifact_index(**args)["versions"]
            assert not grading_runs.student_results(student_id=student, assignment_id=assignment.id)

        assert_blocked()
        grading_repository.add_teacher_review(row.id, teacher_id=teacher, new_score=ai_score,
                                             new_comment="Comment-only edit", confirm=False)
        assert_blocked()
        queue = grading_runs.list_review_queue(assignment_id=assignment.id, teacher_id=teacher)
        assert [r.id for r in queue] == [row.id]
        if scenario == "ai_only_edit":
            grading_repository.add_teacher_review(row.id, teacher_id=teacher, new_score=8, confirm=False)
            assert task_facade.finalization(**args)["ready_for_confirmation"]
            grading_repository.add_teacher_review(row.id, teacher_id=teacher, new_score=ai_score, confirm=False)
            assert_blocked()  # Reverting to the provisional vote restores its original requirement.
        confirmed = scenario == "ai_only_confirm"
        resolved = ai_score if confirmed else 8.5
        grading_repository.add_teacher_review(row.id, teacher_id=teacher, new_score=resolved,
                                             new_comment="Teacher decision", confirm=confirmed)
        state = task_facade.finalization(**args)
        assert state["ready_for_confirmation"] and not state["remaining_reviews"]
        assert state["required_review_count"] == 1 and state["confirmed_required_count"] == int(confirmed)
        if confirmed:
            grading_runs.release(teacher_id=teacher, run_id=run.id)
        task_facade.generate_artifacts(**args, expected_revision=state["workflow_revision"])
        data, _, _ = task_facade.artifact_bytes(**args, version=1, artifact_id="grades_csv")
        csv_row, = csv.DictReader(StringIO(data.decode("utf-8-sig")))
        assert float(csv_row["q1_score"]) == resolved
        seen, = grading_runs.student_results(student_id=student, assignment_id=assignment.id)
        assert seen.effective_score == resolved and seen.ai_score == ai_score
        assert seen.ai_expert_results[0]["programming_evidence"] == evidence
    else:
        assert public["score"] == 0 and not task_facade._correction_review_blocks_finalization(row)
        assert evidence["primary_reason"] == ("no_submission" if scenario == "no_submission" else "teacher_case_failed")
        assert row.result_status == "graded"
        grading_runs.release(teacher_id=teacher, run_id=run.id)
        seen, = grading_runs.student_results(student_id=student, assignment_id=assignment.id)
        assert seen.effective_score == 0


@pytest.mark.asyncio
async def test_unsupported_language_with_frozen_function_protocol_is_unscored(monkeypatch):
    route = fixed_provider()
    execute = AsyncMock(side_effect=AssertionError("unsupported function must never run"))
    monkeypatch.setattr(programming, "run_sandbox", execute)
    problem = _make_problem(test_cases=[TestCase(function_name="add", function_args=[1, 2], expected_return="3")])
    result = await programming.ProgrammingSkill(route).grade(problem, _make_answer("#include <iostream>\nint add(int a,int b){return a+b;}"))
    assert result.error_kind == "mode_unsupported"
    assert result.programming_evidence.execution_status == "unsupported"
    assert result.programming_evidence.score_status == "unavailable"
    route.ainvoke.assert_not_awaited()
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_transport_retry_and_one_generation_repair_share_three_requests(monkeypatch):
    from tenacity import wait_none
    from backend.tools.structured_llm import _ainvoke_with_retry
    monkeypatch.setattr(_ainvoke_with_retry.retry, "wait", wait_none())
    route = fixed_provider(TimeoutError("timed out"), response({"cases": [{"input": "1"}]}),
                           response({"cases": [{"input": "1", "expected_output": "2"}]}))
    cases, reason = await programming._generate_test_cases(route, _make_problem())
    assert reason is None and cases[0].expected_output == "2"
    assert route.ainvoke.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["compile_error", "runtime_error", "syntax_error", "timeout", "output_limit",
                                   "memory_limit", "pid_limit", "target_function_missing", "signature_mismatch", "internal_error"])
async def test_execution_categories_keep_platform_failures_out_of_rubric(monkeypatch, reason):
    from backend.services import programming_runs
    from backend.tools.code_interpreter import ExecutionReport, TestResult
    case = TestCase(input="1", expected_output="2")
    row = TestResult(case, False, "", "/private/host/path\nprivate raw stderr", 2, exit_reason=reason)
    evidence = SimpleNamespace(status="executed", code="print(1)", cases=[case], reason="", evidence_id="run-a",
        case_ids=("frozen-case-id",), report=ExecutionReport(0, 1, 0, [row]))
    monkeypatch.setattr(programming_runs, "evidence_for_answer", AsyncMock(return_value=evidence))
    route = fixed_provider(response(award(4)))
    result = await programming.ProgrammingSkill(route).grade(_make_problem(), _make_answer(evidence.code))
    if reason == "internal_error":
        route.ainvoke.assert_not_awaited()
        assert result.programming_evidence.score_status == "unavailable"
        return
    assert result.score == 4 and result.programming_evidence.failure_counts == {reason: 1}
    assert result.programming_evidence.primary_reason == reason
    case_prompt = json.loads(route.ainvoke.await_args.args[0][1].content)["case_examples"][0]
    assert case_prompt["case_id"] == "frozen-case-id" and case_prompt["outcome"] == reason
    assert "host/path" not in json.dumps(case_prompt) and "private raw stderr" not in result.comment
    assert "Failed cases: case 1 (" in result.comment and reason not in result.comment


@pytest.mark.asyncio
@pytest.mark.parametrize("score", [0, 0.01])
async def test_code_only_fallback_cannot_publish_an_unexecuted_zero(score):
    problem = _make_problem(stem="Build a tkinter GUI.")
    route = fixed_provider(response(award(score)))
    vote = await programming.ProgrammingSkill(route).grade(problem, _make_answer("import tkinter"))
    assert vote.error_kind == "programming_unverified_zero"
    assert vote.programming_evidence.execution_status == "unsupported"
    assert vote.programming_evidence.score_status == "unavailable"


@pytest.mark.asyncio
async def test_generated_solution_is_never_an_independent_oracle():
    problem = _make_problem()
    problem.solution_code = "print('AI-only answer')"
    problem.reference_answer = "Independent teacher reference."
    problem._math_rubric_source = {"presentation": {"ai_completion_provenance": {"solution_code": {}}}}
    route = fixed_provider(response({"cases": [{"input":"1", "expected_output":"1"}]}))
    cases, err = await programming._generate_test_cases(route, problem)
    prompt = route.ainvoke.await_args.args[0][1].content
    assert not err and cases and problem.reference_answer in prompt and problem.solution_code not in prompt
    assert programming._teacher_material(problem)["ai_generated_fields"] == ["solution_code"]


@pytest.mark.asyncio
@pytest.mark.parametrize("language,detail,summary", [
    ("zh", "模型返回的内容不完整", "样例 1（结果与预期不符）"),
    ("en", "model returned an incomplete", "case 1 (result differs from the expected value)"),
])
async def test_grading_model_failure_after_tests_keeps_localized_execution_evidence(monkeypatch, language, detail, summary):
    from backend.services import programming_runs
    from backend.tools.code_interpreter import ExecutionReport, TestResult
    case = TestCase(input="1", expected_output="2")
    run = SimpleNamespace(status="executed", code="print(1)", cases=[case], reason="", evidence_id="private-run-id",
                          report=ExecutionReport(0, 1, 0, [TestResult(case, False, "1", "", 1, exit_reason="wrong_answer")]))
    monkeypatch.setattr(programming_runs, "evidence_for_answer", AsyncMock(return_value=run))
    monkeypatch.setattr(programming, "structured_llm_call", AsyncMock(side_effect=StructuredOutputInvalidError("PRIVATE")))
    result = await programming.ProgrammingSkill(fixed_provider(), language=language).grade(_make_problem(), _make_answer(run.code))
    assert result.error_kind == "parse_failed" and result.programming_evidence.score_status == "unavailable"
    assert detail in result.comment and summary in result.comment
    assert all(token not in result.comment for token in ("parse_failed", "wrong_answer", "private-run-id", "PRIVATE"))
