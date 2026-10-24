"""Default calibration: fixed source decisions plus real ledger/API arithmetic.

These are code-contract tests, not claims of LLM mathematical accuracy.
"""
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
from itertools import permutations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.services import math_rubric_plan as rubric
from backend.skills.math_grading import MathAssessment, score_assessment, math_scoring_disagreement
from backend.tests.test_math_grading import expert_for
from backend.tests.test_math_rubric_plan import ReplayProvider, explanation, question, route, item


def plan(strictness, *, mode="points", eligible=True, partial=True, cap=None):
    data = rubric.fallback_math_rubric_plan(question(), grading_setup={
        "strictness": strictness, "allow_partial_credit": partial,
    }).model_dump(mode="json", exclude={"plan_id"})
    defaults = ["follow_through"] if eligible else []
    items = ([dict(id="first", description="First computation", points=4, source="ai"),
              dict(id="next", description="Correct dependent computation", points=3, source="ai", default_adjustments=defaults),
              dict(id="independent", description="Independent computation", points=2, source="ai"),
              dict(id="condition", description="Non-core condition", points=1, source="ai",
                   default_adjustments=["minor_omission"] if eligible else [])]
             if mode == "points" else
             [dict(id="error", description="Deduction for an error", points=1, source="ai", max_occurrences=cap,
                   default_adjustments=["minor_omission", "follow_through"] if eligible else [])])
    data["routes"] = [rubric.MathRubricRoute(id="main", description="Shared goals", applies_when="All",
        mode=mode, items=items).model_dump(mode="json")]
    return rubric.MathRubricPlan(plan_id=rubric._content_hash(data), **data)


ANSWER = "a=2+3=6\nb=2*a=12\nc=7+1=8"
STEM = "Compute a=2+3, b=2a and c=7+1. State the nonzero denominator condition."


def assessment(*, mode="points"):
    ids = ["first", "next", "independent", "condition"] if mode == "points" else ["error"] * 4
    claims = [
        dict(id="a", quote="a=2+3=6", verdict="refuted", item_ids=[ids[0]], reason="First arithmetic error."),
        dict(id="b", quote="b=2*a=12", verdict="refuted", depends_on=["a"], item_ids=[ids[1]],
             reason="Correct multiplication on the erroneous intermediate value, not a new error."),
        dict(id="c", quote="c=7+1=8", verdict="supported", item_ids=[ids[2]], reason="Independent correct work."),
        dict(id="d", kind="omission", source_quote="State the nonzero denominator condition.",
             verdict="refuted", item_ids=[ids[3]], reason="Minor condition absent."),
    ]
    awards = [dict(item_id=key, score=score, claim_ids=[claim], reason="Apply shared rule.", **extra)
              for key, score, claim, extra in [
                  ("first", 0, "a", {}),
                  ("next", 0, "b", dict(adjustment="follow_through", conditional_credit=3, caused_by="a")),
                  ("independent", 2, "c", {}),
                  ("condition", 0, "d", dict(adjustment="minor_omission", conditional_credit=1)),
              ]] if mode == "points" else []
    deductions = [dict(error_id=claim, rule_id="error", claim_id=claim, reason="Apply shared rule.", **extra)
                  for claim, extra in [("a", {}), ("b", dict(adjustment="follow_through", caused_by="a")),
                                       ("d", dict(adjustment="minor_omission"))]] if mode == "deductions" else []
    return MathAssessment(route_id="main", route_reason="All answers", claims=claims, awards=awards,
                          deductions=deductions, comment="Evidence in ledger.")


@pytest.mark.parametrize("strictness", range(0, 101, 5))
@pytest.mark.parametrize("mode", ["points", "deductions"])
def test_every_slider_tick_has_linear_arithmetic_and_additive_display(strictness, mode):
    result, steps = score_assessment(assessment(mode=mode), plan(strictness, mode=mode), ANSWER, problem_text=STEM)
    expected = (2 + 4 * (1 - strictness / 100)) if mode == "points" else (9 - 2 * strictness / 100)
    assert result == pytest.approx(expected)
    assert sum(Decimal(str(step.score)) for step in steps) == Decimal(str(result))
    if mode == "points":
        assert steps[0].score == 0  # actual first error never restored
        assert steps[2].score == 2  # independent correct work is not collateral damage


@pytest.mark.parametrize("strictness", [0, 50, 100])
def test_teacher_fixed_policy_is_unchanged_at_all_strictness_levels(strictness):
    draft = assessment()
    draft.awards[1].score = 1.5  # explicit teacher: half credit downstream
    draft.awards[3].score = 0  # explicit teacher: omitted condition loses its point
    for award in draft.awards:
        award.adjustment = award.caused_by = None
        award.conditional_credit = 0
    result, _ = score_assessment(draft, plan(strictness, eligible=False), ANSWER, problem_text=STEM)
    assert result == 3.5


@pytest.mark.parametrize("mode", ["points", "deductions"])
def test_disabling_partial_credit_retains_existing_no_partial_semantics(mode):
    result, _ = score_assessment(assessment(mode=mode), plan(0, mode=mode, partial=False), ANSWER, problem_text=STEM)
    assert result == (2 if mode == "points" else 7)


@pytest.mark.parametrize("failure", ["core", "not_omitted", "not_ancestor", "not_error", "over_budget", "unselected"])
def test_default_cannot_invent_credit_or_override_core_teacher_rule(failure):
    draft, p = assessment(), plan(0, eligible=failure != "core")
    if failure == "not_omitted":
        draft.awards[3].claim_ids = ["c"]
        draft.claims[2].item_ids.append("condition")
    if failure == "not_ancestor":
        draft.claims[1].depends_on = []
    if failure == "not_error":
        draft.claims[0].verdict = "supported"
    if failure == "over_budget":
        draft.awards[1].conditional_credit = 4
    if failure == "unselected":
        draft.awards[1].adjustment = None
    with pytest.raises(ValueError):
        score_assessment(draft, p, ANSWER, problem_text=STEM)


def test_equal_base_awards_but_different_conditional_credit_are_expert_disagreement():
    p = plan(100)
    first, second = assessment(), assessment()
    second.awards[1].conditional_credit = 2
    # No omission is needed for this expert comparison helper's problem input.
    for draft in [first, second]:
        draft.claims[3].source_quote = p.original_criterion
    experts = [expert_for(draft, p, ANSWER) for draft in [first, second]]
    assert experts[0].score == experts[1].score
    assert math_scoring_disagreement(experts)


def test_zero_deduction_does_not_consume_cap_before_independent_error():
    p = plan(0, mode="deductions")
    data = p.model_dump(mode="json", exclude={"plan_id"})
    data["routes"][0]["items"][0]["max_occurrences"] = 1
    p = rubric.MathRubricPlan(plan_id=rubric._content_hash(data), **data)
    draft = assessment(mode="deductions")
    draft.deductions = [draft.deductions[2], draft.deductions[0]]
    result, _ = score_assessment(draft, p, ANSWER, problem_text=STEM)
    assert result == 9


@pytest.mark.parametrize("cap", [1, 2, 3])
@pytest.mark.parametrize("strictness", range(0, 101, 5))
def test_capped_deductions_are_order_independent_and_monotone(strictness, cap):
    p = plan(strictness, mode="deductions", cap=cap)
    expected = float((Decimal(9) - Decimal(cap - 1) * strictness / 100).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP))
    for order in permutations(range(3)):
        draft = assessment(mode="deductions")
        draft.deductions = [draft.deductions[index] for index in order]
        score, steps = score_assessment(draft, p, ANSWER, problem_text=STEM)
        assert score == pytest.approx(expected)
        assert sum(step.score for step in steps) == score


def test_count_cap_is_not_reinterpreted_as_a_total_money_cap():
    p = plan(50, mode="deductions", cap=1)
    draft = assessment(mode="deductions")
    draft.deductions = draft.deductions[1:]  # two half-loss occurrences
    assert score_assessment(draft, p, ANSWER, problem_text=STEM)[0] == 9.5


def test_multiple_occurrences_are_ranked_by_unit_loss_not_combined_loss():
    p = plan(50, mode="deductions", cap=2)
    draft = assessment(mode="deductions")
    draft.deductions[2].occurrences = 3
    draft.deductions = [draft.deductions[2], draft.deductions[0]]
    assert score_assessment(draft, p, ANSWER, problem_text=STEM)[0] == 8.5


def test_teacher_fixed_capped_deductions_stay_unchanged():
    for strictness in [0, 50, 100]:
        p = plan(strictness, mode="deductions", eligible=False, cap=1)
        for order in permutations(range(3)):
            draft = assessment(mode="deductions")
            for deduction in draft.deductions:
                deduction.adjustment = None
            draft.deductions = [draft.deductions[index] for index in order]
            assert score_assessment(draft, p, ANSWER, problem_text=STEM)[0] == 9


def test_cap_does_not_hide_invalid_adjustment_evidence():
    p = plan(50, mode="deductions", cap=1)
    draft = assessment(mode="deductions")
    draft.deductions[2].adjustment = "follow_through"  # omission is not a dependent computation
    with pytest.raises(ValueError, match="follow_through_requires_error_ancestor"):
        score_assessment(draft, p, ANSWER, problem_text=STEM)


@pytest.mark.asyncio
async def test_normal_plan_compiler_can_produce_mixed_capped_deductions():
    provider = ReplayProvider(explanation(routes=[route(id="main", mode="deductions", items=[
        item("error", "AI补充：计算或必要条件错误", 1, source="ai",
             default_adjustments=["minor_omission", "follow_through"]),
    ])]))
    p = await rubric.prepare_math_rubric_plan(
        question(stem=STEM, criterion="按解答正确程度酌情给分。"), provider,
        grading_setup={"strictness": 50},
    )
    assert p.compilation_status == "compiled" and len(provider.messages) == 1
    assert p.routes[0].items[0].max_occurrences == 1
    for order in [(2, 0), (0, 2)]:
        draft = assessment(mode="deductions")
        draft.deductions = [draft.deductions[index] for index in order]
        assert score_assessment(draft, p, ANSWER, problem_text=STEM)[0] == 9


def test_holistic_prose_item_labels_do_not_discard_source_evidence():
    p = rubric.fallback_math_rubric_plan(question())
    draft = MathAssessment(route_id="holistic", route_reason="Original rules as a whole",
        claims=[dict(id="a", quote="a=2+3=6", verdict="refuted", item_ids=["prose-only-a"], reason="Arithmetic error")],
        score=6, rationale="Teacher rule preserves subsequent correct computation", comment="Arithmetic error")
    result, _ = score_assessment(draft, p, ANSWER)
    assert result == 6
    assert draft.claims[0].quote == "a=2+3=6"
    assert draft.claims[0].item_ids == []
    draft.claims[0].quote = "a=2+3=5"
    with pytest.raises(ValueError, match="claim_not_in_student_answer"):
        score_assessment(draft, p, ANSWER)


@pytest.mark.parametrize("mode", ["points", "deductions"])
def test_unique_validated_dependency_completes_redundant_error_origin(mode):
    draft = assessment(mode=mode)
    entry = draft.awards[1] if mode == "points" else draft.deductions[1]
    entry.caused_by = None
    result, _ = score_assessment(draft, plan(50, mode=mode), ANSWER, problem_text=STEM)
    assert result == (4 if mode == "points" else 8)
    assert entry.caused_by == "a"


def test_ambiguous_error_origin_cannot_be_inferred():
    draft = assessment()
    draft.awards[1].caused_by = None
    draft.claims[1].depends_on.append("c")
    draft.claims[2].verdict = "refuted"
    with pytest.raises(ValueError, match="follow_through_requires_error_ancestor"):
        score_assessment(draft, plan(50), ANSWER, problem_text=STEM)


@pytest.mark.asyncio
async def test_compile_classification_once_and_keep_exact_authenticated_value():
    response = explanation(routes=[route(items=[
        item("method", "方法4分", 4, default_adjustments=["follow_through"]),
        item("result", "结论6分", 6),
    ])])
    plans = []
    for value in [0, 25, 50, 75, 100]:
        provider = ReplayProvider(deepcopy(response))
        p = await rubric.prepare_math_rubric_plan(question(), provider, grading_setup={"strictness": value})
        plans.append(p)
        assert len(provider.messages) == 1  # no new compiler/review loop
        assert p.strictness == value
        assert p.routes[0].items[0].default_adjustments == ("follow_through",)
        assert p.routes[0].items[1].default_adjustments == ()
        assert f"strictness {value}/100" in p.effective_criterion
    assert len({p.input_fingerprint for p in plans}) == 1
    assert len({p.plan_id for p in plans}) == 5


@pytest.mark.asyncio
async def test_holistic_fallback_keeps_numeric_defaults_and_teacher_notes():
    setup = {"strictness": 25, "teacher_notes": "本题后续计算正常给满分"}
    p = await rubric.prepare_math_rubric_plan(question(), ReplayProvider("{}"), grading_setup=setup)
    assert p.compilation_status == "holistic"
    assert p.strictness == 25
    assert "× 25/100" in p.effective_criterion
    assert setup["teacher_notes"] in p.effective_criterion
    assert p.original_criterion == question()["criterion"]


@pytest.mark.asyncio
async def test_explicit_deduction_amount_cannot_be_scaled_by_an_ai_default():
    response = explanation(routes=[route(mode="deductions", items=[
        item("error", "每错扣1分", 1, max_occurrences=None,
             default_adjustments=["minor_omission", "follow_through"]),
    ])])
    provider = ReplayProvider(response)
    p = await rubric.prepare_math_rubric_plan(question(criterion="每错扣1分"), provider,
                                             grading_setup={"strictness": 0})
    assert len(provider.messages) == 1
    assert p.routes[0].items[0].default_adjustments == ()
    assert p.routes[0].items[0].points == 1


def test_real_api_save_reload_and_run_freeze_preserve_slider_values():
    from backend.api import tasks
    from backend.auth import create_token
    from backend.db import workflow_repository
    from backend.services import task_facade
    from backend.tests.test_grading_source_readiness import _seed_legacy_structured_case

    seeded = _seed_legacy_structured_case()
    owner, task_id = seeded["owner_id"], seeded["assignment_id"]
    app = FastAPI()
    app.include_router(tasks.router)
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {create_token(owner, 'teacher')}"}
    url = f"/tasks/{task_id}/grading-setup"
    fingerprints = set()
    for strictness in [0, 25, 50, 75, 100]:
        workflow = workflow_repository.get_workflow(task_id, owner_id=owner)
        setup = {**workflow.grading_setup, "strictness": strictness}
        saved = client.put(url, headers=headers, json={"grading_setup": setup,
            "expected_workflow_revision": workflow.workflow_revision})
        assert saved.status_code == 200, saved.text
        fingerprints.add(saved.json()["grading_setup_fingerprint"])
        assert client.get(url, headers=headers).json()["grading_setup"]["strictness"] == strictness
    assert len(fingerprints) == 5
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner)
    started = task_facade.start_task_grading(task_id=task_id, owner_id=owner,
        expected_workflow_revision=workflow.workflow_revision)
    frozen = workflow_repository.get_run_setup(started["job_id"])
    assert frozen.setup["strictness"] == 100
