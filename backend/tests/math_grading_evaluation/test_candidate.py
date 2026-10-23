"""Independent task4 acceptance against the integrated production interfaces."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.models import ProblemInfo, StudentAnswerInfo, TaskGradingSetup
from backend.services.math_grading_context import math_plans_context
from backend.services.math_rubric_plan import prepare_math_rubric_plan
from backend.skills import math_grading as engine
from tools.evaluation.math_grading.candidate import evaluate_group, registry_for
from tools.evaluation.math_grading.candidate_fixtures import assessment_reply, plan_reply
from tools.evaluation.math_grading.corpus import CASES, corpus_hash
from tools.evaluation.math_grading.metrics import summarize

ROOT = Path(__file__).resolve().parents[3]


async def prepared(case):
    async def reply(_provider, *, output_model, **kwargs):
        return output_model.model_validate(plan_reply(case)), SimpleNamespace(duration_ms=0)
    problem = ProblemInfo(**case["problem"])
    with patch("backend.services.math_rubric_plan.structured_llm_call", reply):
        plan = await prepare_math_rubric_plan(problem, SimpleNamespace(provider_id="fixture"))
    return problem, plan


def test_report_retains_all_frozen_oracle_cases_and_no_false_zero():
    report = json.loads((ROOT / "tools/evaluation/math_grading/reports/candidate-72377b72.json").read_text())
    assert report["corpus_sha256"] == corpus_hash()
    checked = summarize(CASES, report["results"])
    all_checks = checked["aggregates"]["all"]
    assert all_checks["score_available_as_expected"]["value"] == 1
    assert all_checks["synthetic_interval_hit"] == {"value": 1, "observed": 38, "total": 40}
    assert all_checks["step_sum_consistent"]["value"] == 1
    assert all_checks["no_unwanted_forced_handling"]["value"] == 1
    assert checked["provider_entry_calls"] == 0
    for row in report["results"]:
        if row["id"] in {"C26", "C27"}:
            assert row["score"] == 10 and row["required_handling"] is False
            assert len([c for c in row["call_stages"] if c["stage"] == "MathAssessment"]) == 1
            assert all(e["status"] == "unknown" for e in row["trace"]["evidence"])
        if row["id"] == "C07":
            assert row["score"] == 0 and row["required_handling"] is False
            assert "direct" in row["trace"]
        assert "math_rubric" not in row["correction"]


def test_real_batch_freezes_before_three_students_and_ignores_name_order():
    cases = CASES[:3]
    first = asyncio.run(evaluate_group(cases))
    second = asyncio.run(evaluate_group(list(reversed(cases)),
                                        student_names={"C01": "Changed A", "C02": "Changed B"}))
    assert len({r["plan_id"] for r in first + second}) == 1
    assert {r["id"]: r["score"] for r in first} == {"C01": 2, "C02": 2, "C03": 8}
    for rows in (first, second):
        events = rows[0]["execution_events"]
        assert events[0]["stage"] == "plan_ready"
        assert sum(e["stage"] == "plan_ready" for e in events) == 1
        assert sum(c["stage"] == "plan" for c in rows[0]["call_stages"]) == 1
        assert all(e["plan_id"] == rows[0]["plan_id"] for e in events)


@pytest.mark.parametrize("language,label", [("zh", "Chinese"), ("en", "English")])
def test_subparts_keep_one_question_result_and_progress_unit(language, label):
    from backend.agents import grading_agent
    from backend.progress.tracker import ProgressReporter
    case = deepcopy(next(c for c in CASES if c["id"] == "P05"))
    case["problem"]["stem"] = (
        "无空气阻力竖直上抛。(a)写出重力做功与势能关系；"
        "(b)结合动能定理证明机械能守恒。"
    )
    reporter = ProgressReporter(f"evaluation-subparts-{language}", 1, 1)
    actual_batch = grading_agent.grade_batch

    async def with_reporter(**kwargs):
        provider_id = kwargs["registry"].pick_default().provider_id
        kwargs.update(reporter=reporter, language=language,
                      grading_setup=TaskGradingSetup(
                          selected_provider_ids=[provider_id], primary_provider_id=provider_id,
                          feedback_language=language))
        return await actual_batch(**kwargs)

    async def run():
        with patch.object(grading_agent, "grade_batch", with_reporter):
            rows = await evaluate_group([case])
        return rows, await reporter.snapshot()

    rows, progress = asyncio.run(run())
    assert len(rows) == 1
    result = rows[0]["correction"]
    assert result["q_id"] == "P05" and result["max_score"] == 10
    assert result["score"] == 10 and len(result["steps"]) == 2
    assert progress.total_questions == 1 and progress.total_students == 1
    assert progress.completed_units == 1 and progress.phase == "done"
    assert all(label in prompt["system"] for prompt in rows[0]["captured_prompts"])


@pytest.mark.parametrize("alter", ["quote", "cycle", "duplicate_award"])
def test_forged_or_malformed_evidence_cannot_silently_score(alter):
    case = CASES[2]
    _, plan = asyncio.run(prepared(case))
    draft = assessment_reply(case)
    if alter == "quote":
        draft["claims"][0]["quote"] = "a step the student never wrote"
    elif alter == "cycle":
        draft["claims"][0]["depends_on"] = ["s1"]
    else:
        draft["awards"].append(deepcopy(draft["awards"][0]))
    with pytest.raises(ValueError):
        engine.score_assessment(engine.MathAssessment(**draft), plan, case["answer"])


@pytest.mark.parametrize("alter", ["same_error", "same_application", "false_dependency"])
def test_duplicate_or_ungrounded_deductions_are_rejected(alter):
    case = next(c for c in CASES if c["id"] == "C09")
    _, plan = asyncio.run(prepared(case))
    draft = assessment_reply(case)
    if alter == "same_error":
        draft["deductions"][1]["error_id"] = "e1"
    elif alter == "same_application":
        draft["deductions"][1]["claim_id"] = "s1"
    else:
        draft["deductions"][0]["caused_by"] = "s2"
    with pytest.raises(ValueError):
        engine.score_assessment(engine.MathAssessment(**draft), plan, case["answer"])


@pytest.mark.parametrize("votes,expected", [([[5, 0], [0, 5]], 5), ([[5, 5], [0, 5]], 8.8)])
def test_actual_multi_expert_reviews_items_once_and_keeps_custom_weights(votes, expected):
    from backend.agents.multi_expert import run_multi_expert
    case = next(c for c in CASES if c["id"] == "P06")
    problem, plan = asyncio.run(prepared(case))
    providers = [SimpleNamespace(provider_id=f"fixed:{i}") for i in range(2)]
    setup = TaskGradingSetup(selected_provider_ids=[p.provider_id for p in providers],
                             primary_provider_id=providers[0].provider_id,
                             aggregation_method="weighted_average",
                             expert_weights={"fixed:0": 3, "fixed:1": 1})
    calls = []

    async def reply(provider, *, output_model, user_prompt, **kwargs):
        calls.append(output_model.__name__)
        if output_model is engine.ExpertReview:
            payload = json.loads(user_prompt)
            assert payload["answer"] == case["answer"]
            assert payload["disputed_item_ids"]
            return output_model(revisions=[]), SimpleNamespace(duration_ms=0)
        draft = assessment_reply(case)
        for award, points in zip(draft["awards"], votes[providers.index(provider)]):
            award.update(score=points, claim_ids=["s1"] if points else [])
        return output_model(**draft), SimpleNamespace(duration_ms=0)

    async def run():
        with math_plans_context({problem.q_id: plan}):
            return await run_multi_expert(
                problem=problem, answer=StudentAnswerInfo(
                    q_id=problem.q_id, number=problem.number, type=problem.type, content=case["answer"]),
                student_id="synthetic", registry=registry_for(providers), multi_sample_n=1,
                aggregation_method="weighted_average", grading_setup=setup)
    with patch.object(engine, "structured_llm_call", reply), \
            patch.object(engine.kb_tool, "retrieve_for_grading", AsyncMock(return_value=[])):
        result = asyncio.run(run())
    assert calls.count("MathAssessment") == 2 and calls.count("ExpertReview") == 1
    assert result.score == expected
    assert sum(step.score for step in result.steps) == pytest.approx(expected)
    assert len({engine.read_math_trace(e)["plan_id"] for e in result.expert_results}) == 1


def test_direct_fallback_uses_original_answer_and_same_plan_once():
    case = CASES[2]
    problem, plan = asyncio.run(prepared(case))
    calls = []

    async def reply(provider, *, output_model, user_prompt, **kwargs):
        calls.append(output_model.__name__)
        data = json.loads(user_prompt.split("\nINPUT DATA:\n")[1])
        assert data["student_answer"] == case["answer"]
        assert data["rubric_plan"]["plan_id"] == plan.plan_id
        if output_model is engine.MathAssessment:
            malformed = assessment_reply(case)
            malformed["claims"][0]["quote"] = "fabricated"
            return output_model(**malformed), SimpleNamespace(duration_ms=0)
        return output_model(score=8, confidence=.95, rationale="移项4分、除法方法4分；末行算错0分。",
                            comment="方法正确，末行算错。"), SimpleNamespace(duration_ms=0)

    async def run():
        from backend.skills.calculation import CalculationSkill
        with math_plans_context({problem.q_id: plan}):
            return await CalculationSkill(SimpleNamespace(provider_id="fixed")).grade(
                problem, StudentAnswerInfo(q_id=problem.q_id, number=problem.number,
                                           type=problem.type, content=case["answer"]))
    with patch.object(engine, "structured_llm_call", reply), \
            patch.object(engine.kb_tool, "retrieve_for_grading", AsyncMock(return_value=[])):
        result = asyncio.run(run())
    assert result.score == 8 and not result.error_kind
    assert calls == ["MathAssessment", "DirectMathGrade"]
    assert engine.read_math_trace(result)["evidence"] == []


def test_old_adapter_is_explicitly_retired_on_candidate():
    from tools.evaluation.math_grading.replay import legacy_case
    with pytest.raises(RuntimeError, match="old production base"):
        asyncio.run(legacy_case(CASES[0]))


@pytest.mark.parametrize("degraded", [False, True])
def test_requested_review_can_correct_shared_omission_or_single_survivor(degraded):
    """Task3's final review fixes must work through the real dispatcher."""
    from backend.agents.multi_expert import run_multi_expert
    from backend.tools.structured_llm import PermanentLLMError
    case = next(c for c in CASES if c["id"] == "P05")
    problem, plan = asyncio.run(prepared(case))
    providers = [SimpleNamespace(provider_id=f"fixed:{i}") for i in range(2)]
    setup = TaskGradingSetup(selected_provider_ids=[p.provider_id for p in providers],
                             primary_provider_id=providers[0].provider_id,
                             aggregation_method="weighted_average",
                             expert_weights={"fixed:0": 3, "fixed:1": 1})
    calls = []

    async def reply(provider, *, output_model, user_prompt, **kwargs):
        calls.append(output_model.__name__)
        if output_model is engine.ExpertReview:
            fixed = assessment_reply(case)
            return output_model(revisions=[dict(
                expert_index=0, reason="原文W=-ΔU已覆盖第一项，恢复该项5分。",
                source_quote="重力做功W=-ΔU", assessment=fixed)]), SimpleNamespace(duration_ms=0)
        if degraded and provider is providers[1]:
            raise PermanentLLMError("synthetic failed sibling")
        draft = assessment_reply(case)
        draft["awards"][0].update(score=0, claim_ids=[])
        draft["needs_review"] = provider is providers[0]
        return output_model(**draft), SimpleNamespace(duration_ms=0)

    async def run():
        with math_plans_context({problem.q_id: plan}):
            return await run_multi_expert(
                problem=problem, answer=StudentAnswerInfo(
                    q_id=problem.q_id, number=problem.number, type=problem.type, content=case["answer"]),
                student_id="synthetic", registry=registry_for(providers), multi_sample_n=1,
                aggregation_method="weighted_average", grading_setup=setup)
    with patch.object(engine, "structured_llm_call", reply), \
            patch.object(engine.kb_tool, "retrieve_for_grading", AsyncMock(return_value=[])):
        result = asyncio.run(run())
    assert calls.count("ExpertReview") == 1
    assert result.score == (10 if degraded else 8.8)
    assert sum(s.score for s in result.steps) == pytest.approx(result.score)
    if degraded:
        assert result.synthesis_method == "degraded_to_single"
        assert len(result.expert_results) == 2


def test_feedback_cannot_rebind_existing_claim_id_to_a_different_quote():
    from backend.skills.calculation import CalculationSkill
    case = CASES[2]
    problem, plan = asyncio.run(prepared(case))
    calls = []

    async def reply(provider, *, output_model, **kwargs):
        calls.append(output_model.__name__)
        draft = assessment_reply(case)
        if len(calls) == 2:
            draft["claims"][0]["quote"] = "x = 3"  # Present, but changes the existing identity.
            draft["awards"][2].update(score=2, claim_ids=["s1"])
        return output_model(**draft), SimpleNamespace(duration_ms=0)

    async def run():
        with math_plans_context({problem.q_id: plan}):
            return await CalculationSkill(SimpleNamespace(provider_id="fixed")).grade(
                problem, StudentAnswerInfo(q_id=problem.q_id, number=problem.number,
                                           type=problem.type, content=case["answer"]))
    with patch.object(engine, "structured_llm_call", reply), \
            patch.object(engine.kb_tool, "retrieve_for_grading", AsyncMock(return_value=[])):
        result = asyncio.run(run())
    assert calls == ["MathAssessment", "MathAssessment"]
    assert result.score == 8
    trace = engine.read_math_trace(result)
    assert trace["assessment"]["claims"][0]["quote"] == case["answer"]
    assert trace["evidence"][0]["status"] == "refuted"
