"""Natural-language domains must not make valid generated suites impossible."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.models import ProblemInfo, StudentAnswerInfo, TestCase
from backend.services.programming_input_domain import input_domain
from backend.services.programming_input_review import review_input_domains
from backend.services.programming_materials import resolve_generated_outputs
from backend.services import programming_runs as runs
from backend.tools.code_interpreter import run_sandbox
from backend.tools.structured_llm import StructuredOutputInvalidError
from backend.tests.test_question_material_format_repair import Provider
from backend.tests.test_programming_generated_materials import source, inputs


# Public synthetic Demo wording, deliberately without the artificial type
# declaration used by older sandbox fixtures.
STEM = "Implement stable_softmax(xs). Return [] for empty input and avoid overflow for values near 1000. Do not use NumPy.\nRequired signature\ndef stable_softmax(xs):\n    ..."
CODE = "import math\ndef stable_softmax(xs):\n    if not xs: return []\n    peak = max(xs)\n    exps = [math.exp(x-peak) for x in xs]\n    return [x/sum(exps) for x in exps]\n"
VALUES = [[], [0], [0, 0], [1, 2, 3], [1000, 1000], [-1000, -1000],
          [1000, 999, 998], [-1, 0, 1], [.1, .2, .3], [10, -10]]


@pytest.fixture(autouse=True)
def isolated_runner_loop(monkeypatch):
    # Each pytest case owns a different event loop; the production lifespan
    # initializes this semaphore once for its single service loop.
    monkeypatch.setattr("backend.tools.sandbox_runtime._SEMAPHORE", None)


def demo():
    return {**source(), "stem": STEM, "solution_code": CODE}


def cases():
    return [TestCase(io_mode="function", function_name="stable_softmax", function_args=[xs],
                     source="llm_generated") for xs in VALUES]


def reviewed(verdict="allowed", positions=range(1, 11), quote="Implement stable_softmax(xs)."):
    return json.dumps({"checks": [dict(position=i, verdict=verdict, source_quote=quote,
        reason="Finite numeric vectors, including the required empty and large-value cases.") for i in positions]})


def candidates(*, count=10, include_saved=False):
    rows = [dict(target_id="q1:solution_code", text_value=CODE),
            dict(target_id="q1:test_cases", test_cases=[c.model_dump() for c in cases()[:count]])]
    if include_saved:
        rows += [dict(target_id="q1:reference_answer", text_value="Subtract the maximum before exponentiating."),
                 dict(target_id="q1:criterion", rubric_definition={"part_rubric": {"total_pts": 10, "parts": [
                     {"id": "p1", "total_pts": 10, "alternative": {"deduct_per_error": 1}, "steps": [
                         {"id": "empty", "criterion": "Return [] for empty input", "total_pts": 2},
                         {"id": "shift", "criterion": "Subtract the maximum", "total_pts": 3},
                         {"id": "normalize", "criterion": "Normalize the exponentials", "total_pts": 3},
                         {"id": "tests", "criterion": "Test suite", "total_pts": 2, "test_suite": True}]}]}})]
    return json.dumps({"candidates": rows})


@pytest.mark.asyncio
async def test_real_demo_wording_executes_ten_inputs_after_semantic_review():
    row, probes = demo(), cases()
    before = deepcopy(row)
    assert {input_domain(ProblemInfo.model_validate(row), c) for c in probes} == {"unknown"}
    provider = Provider([reviewed()])
    execute = AsyncMock(wraps=run_sandbox)
    result = await resolve_generated_outputs(row, probes, provider=provider, execute=execute)
    assert len(result) == 10 and result[0].expected_return == "[]"
    assert result[2].expected_return == result[4].expected_return == "[0.5, 0.5]"
    assert row == before and all(c.expected_return is None for c in probes)
    assert len(provider.calls) == execute.await_count == 1
    context = json.loads(provider.calls[0][1].content)
    assert context["stem"] == STEM and len(context["inputs"]) == 10
    assert "solution_code" not in context and "reference_answer" not in context
    assert all("expected_return" not in c and "expected_output" not in c for c in context["inputs"])


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["outside", "unknown"])
async def test_semantically_invalid_or_ambiguous_inputs_never_execute(verdict):
    provider, execute = Provider([reviewed(verdict)]), AsyncMock()
    with pytest.raises(ValueError, match="1: " + verdict):
        await resolve_generated_outputs(demo(), cases(), provider=provider, execute=execute)
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_literal_constraints_remain_authoritative_and_free_of_model_calls():
    row, provider = source(), Provider([])
    await resolve_generated_outputs(row, inputs(), provider=provider)
    row["stem"] += " x >= 0."
    invalid = [TestCase(io_mode="function", function_args=[-1]), *inputs()[1:]]
    execute = AsyncMock()
    with pytest.raises(ValueError, match="1: outside"):
        await resolve_generated_outputs(row, invalid, provider=provider, execute=execute)
    execute.assert_not_awaited()
    assert provider.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    reviewed(positions=range(1, 10)), reviewed(positions=[1] * 10),
    reviewed(positions=range(2, 12)), reviewed(quote="Invented source statement"),
    reviewed(positions=[True, *range(2, 11)]), "not JSON",
])
async def test_review_must_cover_exact_inputs_with_real_source_quotes(bad):
    provider, execute = Provider([bad, bad]), AsyncMock()
    with pytest.raises(StructuredOutputInvalidError):
        await resolve_generated_outputs(demo(), cases(), provider=provider, execute=execute)
    assert len(provider.calls) == 2  # Existing single format repair, never an open loop.
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_cannot_override_teacher_or_deterministic_verdicts():
    provider = Provider([reviewed(positions=[3])])
    verdicts = ["teacher", "outside", "unknown", "allowed"]
    result = await review_input_domains(ProblemInfo.model_validate(demo()), cases()[:4], verdicts, provider)
    assert result == ["teacher", "outside", "allowed", "allowed"]
    assert verdicts == ["teacher", "outside", "unknown", "allowed"]
    assert [row["position"] for row in json.loads(provider.calls[0][1].content)["inputs"]] == [3]


@pytest.mark.asyncio
async def test_unknown_teacher_constraint_is_sent_in_full_and_cannot_be_inferred_from_code():
    row = demo()
    row["criterion"] = "Only vectors from the instructor's separately defined subset are permitted."
    provider, execute = Provider([reviewed("unknown")]), AsyncMock()
    with pytest.raises(ValueError, match="unknown"):
        await resolve_generated_outputs(row, cases(), provider=provider, execute=execute)
    assert json.loads(provider.calls[0][1].content)["teacher_criterion"] == row["criterion"]
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_stdin_domain_uses_the_same_review_and_preserves_bytes():
    row = source()
    row.update(stem="Read a line of text from stdin and print its characters in reverse order.",
               solution_code="import sys\ns=sys.stdin.readline().rstrip('\\n')\nprint(s[::-1])")
    probes = [TestCase(input=f" {i} text \n") for i in range(10)]
    provider = Provider([reviewed(quote=row["stem"])])
    result = await resolve_generated_outputs(row, probes, provider=provider)
    assert [c.input for c in result] == [c.input for c in probes]
    assert result[0].expected_output == " txet 0 \n"
    assert json.loads(provider.calls[0][1].content)["inputs"][0]["stdin"] == " 0 text \n"


@pytest.mark.asyncio
async def test_semantic_approval_never_overwrites_teacher_expected_output():
    row = demo()
    row["test_cases"] = [TestCase(io_mode="function", function_name="stable_softmax",
                                 function_args=[[0, 0]], expected_return="[1.0, 1.0]")]
    provider = Provider([])
    with pytest.raises(ValueError, match="contradicts supplied teacher"):
        await resolve_generated_outputs(row, cases(), provider=provider)
    assert row["test_cases"][0].expected_return == "[1.0, 1.0]" and provider.calls == []


@pytest.mark.asyncio
async def test_manual_retry_really_requests_only_failed_material_and_keeps_saved_success(monkeypatch):
    from backend.agents import incremental_question_preparation as inc
    from backend.api import task_preparation as api
    from backend.db import workflow_repository
    from backend.domain.errors import ValidationError
    from backend.progress.tracker import ProgressReporter
    from backend.services import task_facade
    from backend.tests.test_incremental_question_preparation import setup, persisted
    from backend.tests.test_question_preparation_recovery import _RecoveryRegistry, _claim
    owner, task, operation, pipeline = await setup(monkeypatch)
    row = {**demo(), "solution_code": ""}
    extract = AsyncMock(return_value=inc._rows([row]))
    monkeypatch.setattr(inc, "_extract_problems_call", extract)
    provider = Provider([candidates(count=9, include_saved=True), candidates(count=9), candidates(count=9)])
    pipeline.provider = provider
    with pytest.raises(ValidationError):
        await pipeline.run()
    _, saved = persisted(owner, operation)
    original = deepcopy(saved["q1"])
    assert original["reference_answer"] and original["criterion"] and not original.get("test_cases")
    task_facade._fail_operation(task, owner, operation.operation_id, operation.attempt,
        "provider_response_invalid", expected_lease_token=operation.lease_token)
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    registry = _RecoveryRegistry()
    retried = await api.retry_question_preparation(task_id=task, job_id=operation.operation_id,
        request=api.RetryQuestionPreparationRequest(expected_workflow_revision=workflow.workflow_revision),
        background_tasks=None, current=SimpleNamespace(id=owner), registry=registry)
    next_operation = _claim(owner, retried["job_id"], "retry-programming-domain")
    restored = api._rehydrate_question_preparation_inputs(next_operation)
    retry_provider = Provider([candidates(), reviewed()])
    retry = inc.IncrementalQuestionPreparation(operation=next_operation, sources=restored["sources"],
        provider=retry_provider, provider_id=restored["recognition_provider_id"],
        reporter=ProgressReporter(next_operation.operation_id), score_policy=restored["score_policy"],
        read_source=pipeline.read_source)
    await retry.run()
    final, rows = persisted(owner, next_operation)
    assert next_operation.attempt == 2 and len(provider.calls) == 3 and len(retry_provider.calls) == 2
    request = json.loads(retry_provider.calls[0][1].content.split("\n", 1)[1])
    assert {t["target"] for t in request["requested_targets"]} == {"solution_code", "test_cases"}
    assert len(rows["q1"]["test_cases"]) == 10 and not final.checkpoint["incremental_errors"]
    assert rows["q1"]["test_cases"][4]["expected_return"] == "[0.5, 0.5]"
    assert rows["q1"]["reference_answer"] == original["reference_answer"]
    assert rows["q1"]["criterion"] == original["criterion"]
    extract.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("frozen", [False, True])
async def test_prepared_suite_is_reviewed_once_for_all_students_and_preserves_real_failure(frozen):
    from backend.tests.test_programming_reference_review import frozen_problem
    prepared = await resolve_generated_outputs(demo(), cases(), provider=Provider([reviewed()]))
    if frozen:
        problem = frozen_problem(demo(), prepared)
    else:
        row = {**demo(), "criterion": "Correct softmax including the required boundary cases.",
               "test_cases": prepared, "ai_completion_provenance": {"test_cases": {"source_kind": "ai_generated"}}}
        problem = ProblemInfo.model_validate(row)
        problem._math_rubric_source = {"presentation": row}
    provider = Provider([reviewed()])
    generate = AsyncMock(side_effect=AssertionError("Saved inputs must not be generated again"))
    broken = "import math\ndef stable_softmax(xs):\n    if not xs: return []\n    exps=[math.exp(x) for x in xs]\n    return [x/sum(exps) for x in exps]"
    async def run(student, code):
        return await runs.evidence_for_answer(problem=problem,
            answer=StudentAnswerInfo(q_id="q1", number="1", type="编程题", content=code),
            student_id=student, task_id=None, provider=provider, reporter=None, generate=generate, execute=run_sandbox)
    with runs.programming_run(SimpleNamespace(pick_default=lambda: provider), None):
        good, bad = await asyncio.gather(run("good", CODE), run("bad", broken))
    assert good.status == bad.status == "executed"
    assert good.report.passed_count == 10 and bad.report.passed_count < 10
    assert any("OverflowError" in r.error for r in bad.report.results)
    assert len(provider.calls) == 1
    generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_interrupted_domain_review_is_not_reissued_for_another_student():
    from backend.tests.test_programming_reference_review import frozen_problem
    prepared = await resolve_generated_outputs(demo(), cases(), provider=Provider([reviewed()]))
    problem = frozen_problem(demo(), prepared)
    entered = asyncio.Event()
    async def blocked(*_):
        entered.set()
        await asyncio.Event().wait()
    provider = SimpleNamespace(provider_id="blocked", ainvoke=AsyncMock(side_effect=blocked))
    execute = AsyncMock()
    async def run(student):
        return await runs.evidence_for_answer(problem=problem,
            answer=StudentAnswerInfo(q_id="q1", number="1", type="编程题", content=CODE),
            student_id=student, task_id=None, provider=provider, reporter=None,
            generate=AsyncMock(), execute=execute)
    with runs.programming_run(SimpleNamespace(pick_default=lambda: provider), None):
        first = asyncio.create_task(run("one"))
        await asyncio.wait_for(entered.wait(), 2)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = await run("two")
    assert second.reason == "programming_preparation_interrupted"
    provider.ainvoke.assert_awaited_once()
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_oversized_review_is_rejected_without_truncating_source_constraints():
    row = demo()
    row["stem"] += "x" * (256 * 1024)
    provider = Provider([])
    with pytest.raises(ValueError, match="programming_cases_unavailable"):
        await review_input_domains(ProblemInfo.model_validate(row), cases(), ["unknown"] * 10, provider)
    assert provider.calls == []


@pytest.mark.asyncio
async def test_unresolved_semantic_review_withholds_the_suite_for_every_student():
    from backend.tests.test_programming_reference_review import frozen_problem
    prepared = await resolve_generated_outputs(demo(), cases(), provider=Provider([reviewed()]))
    problem = frozen_problem(demo(), prepared)
    provider, execute = Provider([reviewed("unknown")]), AsyncMock()
    with runs.programming_run(SimpleNamespace(pick_default=lambda: provider), None):
        for student in ("one", "two"):
            result = await runs.evidence_for_answer(problem=problem,
                answer=StudentAnswerInfo(q_id="q1", number="1", type="编程题", content=CODE),
                student_id=student, task_id=None, provider=provider, reporter=None,
                generate=AsyncMock(), execute=execute)
            assert result.reason == "programming_input_domain_unverified" and result.report is None
    assert len(provider.calls) == 1
    execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("review_result", ["refused", "invalid", "checkpoint_failed"])
async def test_failed_input_review_is_local_to_the_programming_question(monkeypatch, review_result):
    from backend.agents import grading_agent, multi_expert
    from backend.llm.providers import LLMResponse
    from backend.models import ExpertResult
    from backend.services.grading_adapter import correction_to_result
    from backend.services.grading_checkpoints import CheckpointWriteError
    from backend.tests.test_programming_reference_review import frozen_problem

    prepared = await resolve_generated_outputs(demo(), cases(), provider=Provider([reviewed()]))
    problem = frozen_problem(demo(), prepared)
    raw = {**problem.model_dump(), "_math_rubric_source": problem._math_rubric_source}
    refusal = LLMResponse(content="", model="synthetic", provider="synthetic", duration_ms=1,
                          finish_reason="refused", refusal_code="provider_content_blocked")
    invoke = {
        "refused": {"return_value": refusal},
        "invalid": {"return_value": LLMResponse(content="not JSON", model="synthetic",
                                                provider="synthetic", duration_ms=1)},
        "checkpoint_failed": {},
    }
    provider = SimpleNamespace(provider_id="synthetic:batch", provider_type="synthetic",
                               ainvoke=AsyncMock(**invoke[review_result]))
    registry = SimpleNamespace(pick_default=lambda: provider, list_available=lambda: [provider])
    runner = AsyncMock()
    monkeypatch.setattr("backend.skills.programming.run_sandbox", runner)
    if review_result == "checkpoint_failed":
        def fail_write(*_args):
            raise CheckpointWriteError("checkpoint write failed")
        monkeypatch.setattr(runs.ProgrammingRun, "store", fail_write)
    # Keep the real programming path, but isolate the unrelated ordinary
    # question from model-based rubric preparation and grading.
    monkeypatch.setattr("backend.services.math_grading_context.prepare_math_plans", AsyncMock(return_value={}))
    original_skill = multi_expert.get_skill_for_type
    ordinary_grading = AsyncMock(side_effect=lambda *_args, **_kw: ExpertResult(
        provider=provider.provider_id, score=3, max_score=4, confidence=.95, comment="Ordinary answer graded."))

    class OrdinarySkill:
        def __init__(self, *_args, **_kwargs):
            self.grade = ordinary_grading

    monkeypatch.setattr(multi_expert, "get_skill_for_type",
                        lambda question_type: OrdinarySkill if question_type == "概念题" else original_skill(question_type))
    batch = grading_agent.grade_batch(
        student_store={sid: {"stu_id": sid, "stu_ans": [
            {"q_id": "q1", "content": CODE}, {"q_id": "q2", "content": "4"},
        ]} for sid in ("one", "two")},
        problem_store={"q1": raw, "q2": {"q_id": "q2", "number": "2", "type": "概念题",
                                       "stem": "How many sides does a square have?", "criterion": "4", "max_score": 4}},
        registry=registry, multi_sample_n=1,
    )
    if review_result == "checkpoint_failed":
        with pytest.raises(CheckpointWriteError, match="checkpoint write failed"):
            await batch
        ordinary_grading.assert_not_awaited()
        provider.ainvoke.assert_not_awaited()
    else:
        result = await batch
        assert len(result) == ordinary_grading.await_count == 2
        for student in result:
            by_question = {row.q_id: row for row in student["corrections"]}
            assert by_question["q2"].score == 3
            persisted = correction_to_result(run_id="run", revision_id="revision",
                question=SimpleNamespace(q_id="q1", id="question", max_score=10),
                student_id=student["student_id"], correction=by_question["q1"])
            assert persisted.ai_score is None and persisted.requires_review
            assert by_question["q1"].expert_results[0].programming_evidence.score_status == "unavailable"
            assert by_question["q1"].expert_results[0].error_kind == (
                "provider_content_blocked" if review_result == "refused" else "parse_failed")
        assert provider.ainvoke.await_count == (2 if review_result == "invalid" else 1)
    runner.assert_not_awaited()
