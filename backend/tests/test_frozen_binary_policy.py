"""Presentation cannot change a verified scoring policy. No network/model calls."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.models import ProblemInfo, StudentAnswerInfo
from backend.services.math_grading_context import math_plans_context, prepare_math_plans
from backend.services.question_preparation_artifacts import ArtifactProblemV1
from backend.skills.calculation import CalculationSkill
from backend.skills import math_grading as grading
from backend.tests.test_math_rubric_plan import ReplayProvider
from backend.tests.test_unified_rubric_drafts import choice_definition, generate_choice


@pytest.fixture(autouse=True)
def isolated_checks(monkeypatch):
    monkeypatch.setattr(grading.kb_tool, "retrieve_for_grading", AsyncMock(return_value=[]))
    monkeypatch.setattr(CalculationSkill, "_verify", AsyncMock(return_value=[]))


async def saved_choice(title, language, *, definition=None):
    definition = definition or choice_definition()
    definition["routes"][0]["description"] = title
    row = await generate_choice(definition=definition, language=language)
    row = ArtifactProblemV1.model_validate_json(ArtifactProblemV1(**row).model_dump_json()).model_dump(mode="json")
    registry = SimpleNamespace(pick_default=AsyncMock(side_effect=AssertionError("No second compiler")))
    plans = await prepare_math_plans({"q1": row}, registry)
    registry.pick_default.assert_not_called()
    problem = ProblemInfo.model_validate(row)
    problem._math_rubric_source = {"presentation": {
        "ai_completion_provenance": row["ai_completion_provenance"], "rubric_draft": row["rubric_draft"],
    }}
    return row, problem, plans


def proposed_grade(correct):
    return dict(route_id="answer", route_reason="判断最终选项", comment="核对选项", confidence=.9,
        claims=[dict(id="s1", quote="B" if correct else "A", verdict="supported" if correct else "refuted",
                     reason="最终答案判断", item_ids=["option"])],
        awards=[dict(item_id="option", score=2, claim_ids=["s1"], reason="模型返回不一致的中间分")])


@pytest.mark.asyncio
@pytest.mark.parametrize("title", ["选择题评分", "Answer assessment", "评分方案 — 任意展示标题"])
@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("correct", [False, True])
@pytest.mark.parametrize("path", ["ledger", "direct", "review"])
async def test_rendering_independent_zero_full_through_persisted_plan(title, language, correct, path):
    _, problem, plans = await saved_choice(title, language)
    answer = StudentAnswerInfo(q_id="q1", number="1", type="计算题", content="B" if correct else "A")
    if path == "review":
        from backend.tests.test_math_grading import expert_for
        import json
        old = grading.MathAssessment(**proposed_grade(correct))
        old.awards[0].score = 4 if correct else 0
        expert = expert_for(old, plans["q1"], answer.content)
        trace = grading.read_math_trace(expert)
        trace["assessment"]["needs_review"] = True
        expert.raw_output = json.dumps(trace)
        provider = ReplayProvider(dict(revisions=[dict(expert_index=0, source_quote=answer.content,
            reason="核对最终选项", assessment=proposed_grade(correct))]))
        with math_plans_context(plans), grading.math_review_scope():
            result = (await grading.review_math_experts(problem, answer, [expert], provider))[0]
        assert len(provider.messages) == 1
    else:
        response = proposed_grade(correct)
        provider = (ReplayProvider(response) if path == "ledger" else ReplayProvider({}, {}, dict(
            score=2, binary_correct=correct, rationale="判断最终选项", comment="核对选项")))
        with math_plans_context(plans):
            result = await CalculationSkill(provider).grade(problem, answer)
        assert len(provider.messages) == (1 if path == "ledger" else 3)
    assert not result.error_kind
    assert result.score == (4 if correct else 0)
    assert sum(step.score for step in result.steps) == result.score


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["zh", "en"])
async def test_english_item_policy_and_arbitrary_non_scoring_descriptions_are_frozen(language):
    definition = choice_definition()
    definition["routes"][0]["applies_when"] = "Every submitted response"
    definition["routes"][0]["items"][0]["description"] = "Choose B receives 4 points; otherwise 0 points"
    definition["follow_through"] = "Only the final selection is required."
    definition["numerical_policy"] = "Use the exact value of the sum."
    _, _, plans = await saved_choice("Marking scheme", language, definition=definition)
    assert plans["q1"].binary_policy.source == "generated_item"
    for correct in (True, False):
        grade = grading.MathAssessment(**proposed_grade(correct))
        score, _ = grading.score_assessment(grade, plans["q1"], "B" if correct else "A")
        assert score == (4 if correct else 0)


@pytest.mark.asyncio
async def test_frozen_constraint_survives_preference_binding_and_snapshot_restore():
    from backend.services.math_grading_context import plans_from_snapshots, MATH_GRADING_ENGINE_VERSION
    row, _, plans = await saved_choice("评分计划", "en")
    policy = plans["q1"].binary_policy
    registry = SimpleNamespace(pick_default=AsyncMock(side_effect=AssertionError("No recompile")))
    for strictness in (0, 50, 100):
        for partial in (False, True):
            bound = await prepare_math_plans({"q1": row}, registry,
                grading_setup=dict(strictness=strictness, allow_partial_credit=partial))
            restored = plans_from_snapshots(dict(math_grading_engine_version=MATH_GRADING_ENGINE_VERSION,
                math_rubric_plans={"q1": bound["q1"].model_dump(mode="json")}), [])
            assert restored["q1"].binary_policy == policy
            grade = grading.MathAssessment(**proposed_grade(False))
            assert grading.score_assessment(grade, restored["q1"], "A")[0] == 0
    registry.pick_default.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["source_quote", "route_id", "item_id"])
async def test_policy_evidence_cannot_be_rebound_even_with_a_recomputed_hash(field):
    from backend.services.math_rubric_plan import MathRubricPlan, _content_hash
    _, _, plans = await saved_choice("评分计划", "zh")
    payload = plans["q1"].model_dump(mode="json", exclude={"plan_id"})
    payload["binary_policy"][field] = "changed"
    with pytest.raises(ValueError, match="binary_policy_source_or_scope_changed"):
        MathRubricPlan(plan_id=_content_hash(payload), **payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["multi_choice", "blanks", "choice_explanation"])
@pytest.mark.parametrize("language", ["zh", "en"])
async def test_generated_partial_policies_remain_partial_across_renderings(kind, language):
    definition = choice_definition()
    items = definition["routes"][0]["items"]
    if kind == "multi_choice":
        items[0]["description"] = "全部选对得四分，否则零分。例外：少选且无错选得两分。"
    else:
        items[0].update(points=2, description=("第一空答对得两分，否则零分。" if kind == "blanks"
                                             else "选项正确得两分，否则零分。"))
        items.append({**items[0], "id": "second", "description": "第二空答案" if kind == "blanks" else "解释正确"})
    _, _, plans = await saved_choice("自定义评分标题", language, definition=definition)
    plan = plans["q1"]
    assert plan.binary_policy is None
    grade = proposed_grade(False)
    if kind != "multi_choice":
        grade["awards"].append(dict(item_id="second", score=0, claim_ids=["s1"], reason="此部分未完成"))
        grade["claims"][0]["item_ids"].append("second")
    score, steps = grading.score_assessment(grading.MathAssessment(**grade), plan, "A")
    assert score == 2 == sum(step.score for step in steps)


@pytest.mark.asyncio
async def test_new_teacher_exception_cannot_be_overridden_by_frozen_constraint():
    from backend.services.math_rubric_plan import _with_preferences
    _, _, plans = await saved_choice("选择题评分", "en")
    bound = _with_preferences(plans["q1"], {"teacher_notes": "例外：少选且无错选得两分。"})
    assert bound.binary_policy is None
    score, _ = grading.score_assessment(grading.MathAssessment(**proposed_grade(False)), bound, "A")
    assert score == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["zh", "en"])
async def test_completing_reference_keeps_verified_policy_when_new_metadata_changes(language):
    from backend.agents.ingest_agent import generate_missing_question_materials
    from backend.services.rubric_drafts import saved_draft
    from backend.tests.test_unified_rubric_drafts import choice_question
    raw = choice_question()
    definition = choice_definition()
    definition["routes"][0]["description"] = "原评分标题"
    first = ReplayProvider({"candidates": [dict(target_id="q1:criterion", q_id="q1", target="criterion",
        rubric_definition=definition)]})
    candidates = await generate_missing_question_materials({"q1": raw}, [dict(target_id="q1:criterion",
        q_id="q1", target="criterion")], 6, first, generation_language="zh", allow_partial=True)
    raw.update(criterion=candidates[0].text_value, rubric_draft=candidates[0].rubric_draft,
               ai_completion_provenance={"criterion": {"source_kind": "ai_generated"}})
    prior = saved_draft(raw)
    assert prior and prior.plan.binary_policy
    definition["routes"][0]["description"] = "A different display heading"
    definition["follow_through"] = "Only the final selection is needed."
    second = ReplayProvider({"candidates": [dict(target_id="q1:reference_answer", q_id="q1",
        target="reference_answer", text_value="B，结果为4。", rubric_definition=definition)]})
    candidates = await generate_missing_question_materials({"q1": raw}, [dict(target_id="q1:reference_answer",
        q_id="q1", target="reference_answer")], 6, second, generation_language=language, allow_partial=True)
    raw.update(reference_answer=candidates[0].text_value, rubric_draft=candidates[0].rubric_draft)
    raw["ai_completion_provenance"]["reference_answer"] = {"source_kind": "ai_generated"}
    current = saved_draft(raw)
    assert current and current.plan.binary_policy == prior.plan.binary_policy
    assert current.plan.routes == prior.plan.routes
    assert len(first.messages) == len(second.messages) == 1
    registry = SimpleNamespace(pick_default=AsyncMock(side_effect=AssertionError("No recompile")))
    plans = await prepare_math_plans({"q1": raw}, registry)
    assert grading.score_assessment(grading.MathAssessment(**proposed_grade(False)), plans["q1"], "A")[0] == 0
    registry.pick_default.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["ledger", "direct"])
async def test_skill_without_run_context_still_consumes_verified_persisted_policy(path):
    _, problem, _ = await saved_choice("选择题评分", "en")
    provider = (ReplayProvider(proposed_grade(False)) if path == "ledger" else
        ReplayProvider({}, {}, dict(score=2, binary_correct=False, rationale="选项错误", comment="核对选项")))
    result = await CalculationSkill(provider).grade(problem,
        StudentAnswerInfo(q_id="q1", number="1", type="计算题", content="A"))
    assert not result.error_kind and result.score == 0
    assert len(provider.messages) == (1 if path == "ledger" else 3)


@pytest.mark.asyncio
async def test_old_plan_without_frozen_policy_cannot_supply_reused_votes():
    from backend.services.math_grading_context import (
        plans_from_snapshots, expert_matches_plan, MATH_GRADING_ENGINE_VERSION,
    )
    from backend.services.math_rubric_plan import _content_hash
    _, _, plans = await saved_choice("选择题评分", "en")
    old = plans["q1"].model_dump(mode="json", exclude={"plan_id", "binary_policy"})
    old["schema_version"] = "math_rubric_v2"
    old["plan_id"] = _content_hash(old)
    restored = plans_from_snapshots(dict(math_grading_engine_version=MATH_GRADING_ENGINE_VERSION,
        math_rubric_plans={"q1": old}), [])
    assert restored == {}
    assert not expert_matches_plan(dict(q_id="q1", math_grading_engine_version=MATH_GRADING_ENGINE_VERSION,
        math_plan_id=old["plan_id"]), {"q1"}, restored)


@pytest.mark.asyncio
@pytest.mark.parametrize("strictness", [0, 100])
async def test_explicit_binary_item_is_not_weakened_by_inferred_default_adjustments(strictness):
    from backend.services.math_rubric_plan import _with_preferences
    definition = choice_definition()
    definition["routes"][0]["items"][0]["default_adjustments"] = ["minor_omission"]
    _, _, plans = await saved_choice("选择题评分", "en", definition=definition)
    plan = _with_preferences(plans["q1"], dict(strictness=strictness, allow_partial_credit=True))
    assert plan.binary_policy is not None
    assert grading.score_assessment(grading.MathAssessment(**proposed_grade(False)), plan, "A")[0] == 0
