"""Scoring feedback must not repeat the shared rubric as model commentary."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.skills import math_grading as grading
from backend.skills.calculation import CalculationSkill
from backend.skills.concept import ConceptSkill
from backend.skills.proof import ProofSkill
from backend.tests.test_math_grading import answer, assessment, plan_for, problem, response
from backend.services.math_grading_context import math_plans_context


def test_point_feedback_displays_reason_once_and_preserves_exact_evidence():
    text = r"$x^2=1$; thus $x\ne 1$ implies order two."
    draft = assessment(text=text)
    draft.awards[0].reason = "Method: the stated calculation is correct."
    before = draft.model_dump()
    score, steps = grading.score_assessment(draft, plan_for(), text)

    assert score == 8
    assert [step.score for step in steps] == [8, 0]
    assert [step.is_correct for step in steps] == [True, False]
    assert steps[0].desc == "Method: the stated calculation is correct."
    assert steps[1].desc == "Result checked."
    assert draft.model_dump(exclude={"claims"}) == {k: v for k, v in before.items() if k != "claims"}
    assert draft.claims[0].quote == text


@pytest.mark.parametrize("source", ["question", "rubric"])
@pytest.mark.parametrize("skill", [CalculationSkill, ConceptSkill, ProofSkill])
@pytest.mark.asyncio
async def test_omission_feedback_keeps_obligation_in_trace_not_as_student_quote(monkeypatch, source, skill):
    p, a = problem(), answer("I used the distributive law.")
    plan = plan_for(p)
    obligation = p.stem if source == "question" else p.criterion
    draft = assessment(text=a.content)
    draft.claims[0].item_ids = ["method"]
    draft.claims.append(grading.Claim(id="missing", kind="omission", source_quote=obligation,
        item_ids=["result"], verdict="refuted", reason="The required final result is absent."))
    draft.awards[1].claim_ids = ["missing"]
    draft.awards[1].reason = "The required final result is absent."
    call = AsyncMock(return_value=(draft, response()))
    monkeypatch.setattr(grading, "structured_llm_call", call)
    monkeypatch.setattr(grading.kb_tool, "retrieve_for_grading", AsyncMock(return_value=[]))
    with math_plans_context({"q1": plan}), grading.math_review_scope():
        grading.defer_local_review(True)
        result = await skill(SimpleNamespace(provider_id="mock:feedback")).grade(p, a)

    assert not result.error_kind and result.score == 8
    assert result.steps[1].desc == "The required final result is absent."
    assert result.steps[0].desc == draft.awards[0].reason
    assert sum(Decimal(str(step.score)) for step in result.steps) == Decimal(8)
    trace = grading.read_math_trace(result)
    assert trace["assessment"]["claims"][0]["quote"] == a.content
    assert trace["assessment"]["claims"][1]["source_quote"] == obligation
    assert trace["assessment"]["claims"][1]["quote"] == ""
    assert trace["plan_id"] == plan.plan_id
    assert call.await_count == 1


def test_multiple_student_claims_keep_their_order_and_exact_quotes():
    text = "First line.\nSecond line."
    draft = assessment(text="First line.")
    draft.claims.append(grading.Claim(id="s2", quote="Second line.", item_ids=["method"],
                                    verdict="supported", reason="Additional supporting work."))
    draft.awards[0].claim_ids = ["s2", "s1"]
    _, steps = grading.score_assessment(draft, plan_for(), text)
    assert steps[0].desc == "Correct setup."
    assert draft.awards[0].claim_ids == ["s2", "s1"]
    assert [claim.quote for claim in draft.claims] == ["First line.", "Second line."]


def test_shared_evidence_does_not_replace_different_expert_reasons_or_scores():
    first, second = assessment(), assessment(method=6)
    first.awards[0].reason = "The submitted method establishes the requested result."
    second.awards[0].reason = "The method omits justification of the required condition."
    plan = plan_for()
    score_a, steps_a = grading.score_assessment(first, plan, "6*7=41")
    score_b, steps_b = grading.score_assessment(second, plan, "6*7=41")
    assert (score_a, score_b) == (8, 6)
    assert steps_a[0].desc == first.awards[0].reason
    assert steps_b[0].desc == second.awards[0].reason
    assert steps_a[0].desc != steps_b[0].desc


def test_deduction_feedback_preserves_losses_and_floor_without_repeating_sources():
    plan = plan_for(mode="deductions", floor=6)
    draft = grading.MathAssessment(route_id="main", route_reason="Teacher rule",
        claims=[grading.Claim(id="s1", quote="work", item_ids=["error"], verdict="refuted", reason="Seven errors")],
        deductions=[grading.Deduction(error_id="e1", rule_id="error", claim_id="s1", occurrences=7,
            reason="Seven independent arithmetic mistakes.")], comment="Check the calculations.")
    score, steps = grading.score_assessment(draft, plan, "work")
    assert score == steps[0].score == 6
    assert steps[0].desc == "−7.0: Seven independent arithmetic mistakes.\nTeacher rule: 6/10"
    assert draft.claims[0].quote == "work"


def browser_fixture():
    """Real formatter output consumed by Playwright; no model/database calls."""
    from backend.services.grading_feedback import _concise_steps
    from backend.services.math_rubric_plan import MathRubricPlan, MathRubricRoute, _content_hash

    labels = ["证明元素平方为单位元。", "说明该元素不是单位元。", "证明所给两个元素生成整个群。"]
    student = r"$x^2=1$. / $x\ne 1$."
    p = problem(kind="reasoning", criterion="\n".join(labels))
    data = plan_for(p).model_dump(mode="json", exclude={"plan_id"})
    data["routes"] = [MathRubricRoute(id="main", description="评分标准", applies_when="always", mode="points", minimum_score=0,
        items=[dict(id=f"i{i}", description=label, points=points, source="provided")
               for i, (label, points) in enumerate(zip(labels, [5, 2, 3]))]).model_dump(mode="json")]
    plan = MathRubricPlan(plan_id=_content_hash(data), **data)
    experts, old_experts, projected = [], [], []
    for index, scores in enumerate(([5, 2, 0], [5, 1, 0])):
        reasons = [r"正确推出 $x^2=1$，此步得满分。",
            r"已说明 $x\ne1$，阶为 2 的论证成立。" if index == 0 else r"只写出 $x\ne1$，未说明原因，得部分分。",
            "未完成生成整个群的证明，本项不得分。" if index == 0 else "缺少生成群的论证，本项不得分。"]
        draft = grading.MathAssessment(route_id="main", route_reason="适用本题", confidence=.9,
            comment="前两步有作答，生成群的证明缺失。", claims=[
                grading.Claim(id="s", quote=student, item_ids=["i0", "i1"], verdict="supported", reason="实际作答。"),
                grading.Claim(id="missing", kind="omission", source_quote=labels[2], item_ids=["i2"], verdict="refuted", reason="缺少证明。")],
            awards=[grading.Award(item_id=f"i{i}", score=score, claim_ids=["s" if i < 2 else "missing"], reason=reasons[i]) for i, score in enumerate(scores)])
        score, steps = grading.score_assessment(draft, plan, student)
        trace = {"version": grading.TRACE_VERSION, "plan_id": plan.plan_id, "assessment": draft.model_dump()}
        expert = dict(provider_id=f"model-{index}", provider=f"Synthetic Model {chr(65 + index)}", score=score,
                      max_score=10, confidence=.9, comment=draft.comment, steps=[s.model_dump() for s in steps])
        old_steps = [{**step, "desc": f"{labels[i]}\n{student if i < 2 else labels[2]}\n{reasons[i]}"} for i, step in enumerate(expert["steps"])]
        experts.append(expert)
        old_experts.append({**expert, "steps": old_steps})
        projected.append({**expert, "steps": _concise_steps(old_steps, trace, plan)})
    return {"new": experts, "legacy": projected, "stored": old_experts, "criterion": p.criterion, "answer": student}
