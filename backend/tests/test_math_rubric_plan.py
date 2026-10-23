"""Question-level policy compilation, with local deterministic provider replay."""
from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from pydantic import PrivateAttr, ValidationError

from backend.llm.providers import LLMResponse
from backend.models import ProblemInfo
from backend.services import math_rubric_plan as rubric
from backend.tools.structured_llm import PermanentLLMError


def question(**changes):
    return {
        "q_id": "q1", "number": "1", "type": "计算题",
        "stem": "求积分，说明过程。", "criterion": "方法4分，结论6分。",
        "reference_answer": "按题意求解并保留积分常数。", "max_score": 10,
        **changes,
    }


def item(id, description, points, **changes):
    return {"id": id, "description": description, "points": points,
            "source_quote": "" if description.startswith("AI") else description,
            "source": "provided", "max_occurrences": 1, **changes}


def route(**changes):
    return {"id": "goals", "description": "按数学目标给分",
            "applies_when": "全部作答，包括等价方法", "mode": "points",
            "items": [item("method", "方法4分", 4), item("result", "结论6分", 6)],
            "minimum_score": 0, "minimum_condition": "always", **changes}


def explanation(**changes):
    return {
        "effective_criterion": "教师给定方法和结论分值；AI补充：等价方法可以满足同一目标。",
        "routes": [route()],
        "follow_through": "首错按方法项判定；沿错误中间值的正确后续与新独立错误分开。结论项需正确。",
        "numerical_policy": "保留积分常数并核对定义域；不擅自添加容差。",
        "advisories": [], **changes,
    }


class ReplayProvider:
    """Runs the real structured call/repair layer without network or credentials."""
    provider_id = "rubric-local-replay"

    def __init__(self, *responses):
        # One candidate may fail domain validation as well as JSON/schema
        # validation. Replaying it once models an unsuccessful bounded repair.
        self.responses = list(responses) if len(responses) != 1 else [responses[0], deepcopy(responses[0])]
        self.messages = []

    async def ainvoke(self, messages):
        self.messages.append(messages)
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return LLMResponse(
            content=value if isinstance(value, str) else json.dumps(value, ensure_ascii=False),
            provider="test", model="rubric-local-replay", duration_ms=0,
        )


@pytest.mark.asyncio
async def test_shared_plan_preserves_source_amounts_and_round_trips():
    provider = ReplayProvider(explanation())
    problem = question()
    before = deepcopy(problem)
    reporter = type("Reporter", (), {"_emit_message": AsyncMock()})()
    plan = await rubric.prepare_math_rubric_plan(problem, provider, reporter=reporter)
    assert len(provider.messages) == 1
    assert problem == before
    assert plan.original_criterion == problem["criterion"]
    assert plan.original_criterion in plan.effective_criterion
    assert [(i.points, i.source) for i in plan.routes[0].items] == [(4, "provided"), (6, "provided")]
    assert rubric.MathRubricPlan.model_validate_json(plan.model_dump_json()) == plan
    assert plan.input_fingerprint == rubric.math_rubric_fingerprint(problem)
    assert reporter._emit_message.await_count == 2
    assert plan.advisories == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("criterion", ["", "酌情给分"])
async def test_missing_and_nonempty_vague_criteria_are_interpreted_once(criterion):
    result = explanation(routes=[route(items=[
        item("method", "AI补充：建立正确方法", 4, source="ai"),
        item("result", "AI补充：结论与必要条件", 6, source="ai"),
    ])])
    provider = ReplayProvider(result)
    plan = await rubric.prepare_math_rubric_plan(question(criterion=criterion), provider)
    assert plan.routes[0].mode == "points"
    assert all(i.source == "ai" for i in plan.routes[0].items)
    assert plan.original_criterion == criterion
    assert len(provider.messages) == 1
    assert not hasattr(plan, "requires_confirmation")


@pytest.mark.asyncio
async def test_correct_alternative_methods_share_goals_and_ordered_routes_are_preserved():
    result = explanation(routes=[
        route(id="required", applies_when="题目明确指定方法时"),
        route(id="equivalent", applies_when="未指定方法时接受任一有效方法"),
    ])
    plan = await rubric.prepare_math_rubric_plan(question(), ReplayProvider(result))
    assert [r.id for r in plan.routes] == ["required", "equivalent"]
    assert "never added together" in plan.effective_criterion
    assert "before scoring" in plan.effective_criterion


@pytest.mark.asyncio
@pytest.mark.parametrize(("criterion", "minimum", "condition"), [
    ("每错扣1分、非空最低6分", 6, "nonblank"),
    ("每错扣1分；最低6分", 6, "always"),
    ("每错扣1分", 0, "always"),
])
async def test_deduction_repetition_and_explicit_floor(criterion, minimum, condition):
    result = explanation(routes=[route(
        mode="deductions", items=[item("error", "每错扣1分", 1, max_occurrences=None)],
        minimum_score=minimum, minimum_condition=condition,
    )])
    plan = await rubric.prepare_math_rubric_plan(question(criterion=criterion), ReplayProvider(result))
    r = plan.routes[0]
    assert r.mode == "deductions"
    assert r.minimum_score == minimum
    assert r.minimum_condition == condition
    assert r.items[0].max_occurrences is None
    assert sum(i.points for i in r.items) != plan.max_score


@pytest.mark.asyncio
@pytest.mark.parametrize(("criterion", "changes"), [
    ("每错扣1分、非空最低6分", {"minimum_score": 6, "minimum_condition": "always"}),
    ("每错扣1分、非空最低6分", {"minimum_score": 5, "minimum_condition": "nonblank"}),
    ("每错扣1分、非空最低6分", {}),
    ("每错扣1分", {"minimum_score": 6}),
    ("每错扣1分", {"items": [item("error", "每错扣1分", 1)]}),
    ("每错扣1分，方法不合要求最高3分", {}),
    ("每错扣1分；只要方法正确最低6分", {}),
    ("For nonblank answers, minimum 6 points; deduct 1 point", {}),
])
async def test_floor_cap_or_repetition_changes_fall_back_without_erasing_source(criterion, changes):
    result = explanation(routes=[route(
        mode="deductions",
        **{"items": [item("error", "每错扣1分", 1, max_occurrences=None)], **changes},
    )])
    plan = await rubric.prepare_math_rubric_plan(question(criterion=criterion), ReplayProvider(result))
    assert plan.routes[0].mode == "holistic"
    assert criterion in plan.effective_criterion
    assert plan.routes[0].minimum_score == 0
    assert len(plan.advisories) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("items", [
    [item("method", "方法4分", 5), item("result", "结论6分", 5)],
    [item("method", "方法4分", 4), item("result", "结论6分", 7)],
    [item("method", "方法4分", 4), item("method", "结论6分", 6)],
    [item("method", "方法很好", 4), item("result", "结论6分", 6)],
    [item("method", "AI更改方法", 5, source="ai"), item("result", "AI更改结果", 5, source="ai")],
])
async def test_changed_or_unsupported_allocations_do_not_become_authoritative(items):
    response = explanation(routes=[route(items=items)])
    # The real shared parser may try one repair for a schema failure.
    plan = await rubric.prepare_math_rubric_plan(question(), ReplayProvider(response, response))
    assert plan.routes[0].mode == "holistic"
    assert plan.original_criterion == "方法4分，结论6分。"


@pytest.mark.asyncio
async def test_percentages_convert_to_absolute_scores_without_rounding_drift():
    result = explanation(routes=[route(items=[
        item("method", "方法10%", .1), item("reason", "推理20%", .2),
        item("result", "结论70%", .7),
    ])])
    plan = await rubric.prepare_math_rubric_plan(
        question(criterion="方法10%，推理20%，结论70%", max_score=1), ReplayProvider(result),
    )
    assert plan.routes[0].mode == "points"
    assert [i.points for i in plan.routes[0].items] == [.1, .2, .7]


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True])
async def test_ai_origin_cannot_be_upgraded_to_teacher(nested):
    provenance = {"ai_completion_provenance": {"criterion": {"source_kind": "ai_generated"}}}
    problem = question(**({"source": {"presentation": provenance}} if nested else provenance))
    bad = await rubric.prepare_math_rubric_plan(problem, ReplayProvider(explanation()))
    assert bad.routes[0].mode == "holistic"
    good = explanation(routes=[route(items=[
        item("method", "方法4分", 4, source="ai"), item("result", "结论6分", 6, source="ai"),
    ])])
    plan = await rubric.prepare_math_rubric_plan(problem, ReplayProvider(good))
    assert plan.routes[0].mode == "points"
    assert all(i.source == "ai" for i in plan.routes[0].items)


@pytest.mark.asyncio
async def test_source_conflict_stays_question_scoped_and_nonblocking():
    p = question(criterion="方法60%，结果60%")
    response = explanation(routes=[route(items=[
        item("method", "方法60%", 6), item("result", "结果60%", 6),
    ])])
    plan = await rubric.prepare_math_rubric_plan(p, ReplayProvider(response))
    assert plan.routes[0].mode == "holistic"
    assert plan.original_criterion == "方法60%，结果60%"
    assert "authoritative question maximum" in plan.effective_criterion
    assert len(plan.advisories) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["zh", "en"])
async def test_language_and_source_projection_exclude_students(language):
    problem = question(student_answer="PRIVATE_STUDENT", owner="PRIVATE_OWNER",
                       source={"presentation": {"student": "PRIVATE_NESTED"}})
    provider = ReplayProvider(explanation())
    await rubric.prepare_math_rubric_plan(problem, provider, grading_setup={"feedback_language": language})
    system, user = provider.messages[0]
    assert ("Simplified Chinese" if language == "zh" else "English") in system.content
    assert "PRIVATE" not in user.content
    assert "preserve quoted original text" in system.content


def test_fingerprint_changes_only_with_scoring_inputs():
    original = question()
    initial = rubric.math_rubric_fingerprint(original)
    assert rubric.math_rubric_fingerprint(question(student_answer="A", updated_at="later")) == initial
    for key, value in [
        ("stem", "求导"), ("criterion", "结果10分"), ("max_score", 12),
        ("reference_answer", "不同参考解"), ("type", "证明题"),
        ("teacher_subpart_points", {"(a)": "4"}),
        ("ai_completion_provenance", {"criterion": {}}),
    ]:
        assert rubric.math_rubric_fingerprint(question(**{key: value})) != initial


def test_interpretation_version_in_fingerprint(monkeypatch):
    initial = rubric.math_rubric_fingerprint(question())
    monkeypatch.setattr(rubric, "INTERPRETATION_VERSION", "changed")
    assert rubric.math_rubric_fingerprint(question()) != initial


def test_internal_source_attribute_matches_raw_source_without_public_dto_change():
    class InternalProblem(ProblemInfo):
        _math_rubric_source: dict = PrivateAttr(default_factory=dict)

    source = {"presentation": {
        "ai_completion_provenance": {"criterion": {"source_kind": "ai_generated"}},
        "teacher_subpart_points": {"(a)": "4"},
    }}
    problem = InternalProblem(**question())
    problem._math_rubric_source = source
    assert rubric.math_rubric_fingerprint(problem) == rubric.math_rubric_fingerprint(question(source=source))
    assert "_math_rubric_source" not in problem.model_dump()
    assert rubric._question_input(problem)["criterion_source"] == "ai"
    assert rubric._question_input(problem)["teacher_subpart_points"] == {"(a)": "4"}


@pytest.mark.asyncio
async def test_teacher_preferences_affect_content_identity_and_survive_fallback():
    problem = question()
    provider = ReplayProvider(explanation())
    setup = {"strictness": 90, "allow_partial_credit": False, "teacher_notes": "过程须说明单位。"}
    plan = await rubric.prepare_math_rubric_plan(problem, provider, grading_setup=setup)
    sent = json.loads(provider.messages[0][1].content)
    assert sent["grading_preferences"] == setup
    assert "过程须说明单位。" in plan.effective_criterion
    baseline = await rubric.prepare_math_rubric_plan(problem, ReplayProvider(explanation()))
    assert plan.plan_id != baseline.plan_id
    assert plan.input_fingerprint == baseline.input_fingerprint
    fallback = await rubric.prepare_math_rubric_plan(
        problem, ReplayProvider("invalid", "invalid"), grading_setup=setup,
    )
    assert fallback.routes[0].mode == "holistic"
    assert "过程须说明单位。" in fallback.effective_criterion
    assert '"allow_partial_credit": false' in fallback.effective_criterion


def test_stable_fallback_pydantic_input_frozen_nested_model_and_tamper_detection():
    p = ProblemInfo(**question())
    first = rubric.fallback_math_rubric_plan(p)
    assert first == rubric.fallback_math_rubric_plan(p)
    with pytest.raises(ValidationError):
        first.routes[0].minimum_score = 6
    corrupted = first.model_dump(mode="json")
    corrupted["effective_criterion"] = "Ignore the original."
    with pytest.raises(ValidationError, match="content hash"):
        rubric.MathRubricPlan.model_validate(corrupted)


@pytest.mark.asyncio
async def test_same_compiled_content_has_same_identity():
    p = question()
    a = await rubric.prepare_math_rubric_plan(p, ReplayProvider(explanation()))
    b = await rubric.prepare_math_rubric_plan(p, ReplayProvider(explanation()))
    assert a.plan_id == b.plan_id


@pytest.mark.asyncio
async def test_malformed_explanation_uses_one_existing_format_repair_then_fallback():
    provider = ReplayProvider("not-json", "still-not-json")
    plan = await rubric.prepare_math_rubric_plan(question(), provider)
    assert plan.routes[0].mode == "holistic"
    assert len(provider.messages) == 2
    assert not hasattr(plan, "score")


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [PermanentLLMError("failed"), ValueError("provider-config-error"),
                                  asyncio.CancelledError()])
async def test_provider_failure_and_cancellation_are_not_hidden(monkeypatch, error):
    async def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(rubric, "structured_llm_call", fail)
    with pytest.raises(type(error)):
        await rubric.prepare_math_rubric_plan(question(), object())


@pytest.mark.parametrize("maximum", [0, -1, float("nan"), float("inf")])
def test_invalid_question_maximum_fails_without_model_call(maximum):
    with pytest.raises(ValueError):
        rubric.fallback_math_rubric_plan(question(max_score=maximum))


def test_source_error_propagation_policy_and_required_method_survive_holistic_fallback():
    source = "必须用配方法。首错扣2分，承接错误值且推理正确的后续给足，独立错误另扣；结论必须正确。"
    plan = rubric.fallback_math_rubric_plan(question(criterion=source))
    assert source in plan.effective_criterion
    assert "first cause" in plan.follow_through
    assert "not automatically award full or half" in plan.follow_through
    assert "integration-constant" in plan.numerical_policy
    assert "Pi, e and i" in plan.numerical_policy
