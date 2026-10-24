"""Accounting and actual skill orchestration; no paid model calls."""
import hashlib
import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.models import ProblemInfo, StudentAnswerInfo
from backend.skills.calculation import CalculationSkill
from backend.skills.proof import ProofSkill
from backend.skills.concept import ConceptSkill
from backend.skills import math_grading as grading
from backend.services.math_rubric_plan import MathRubricPlan, MathRubricRoute, fallback_math_rubric_plan
from backend.services.math_grading_context import math_plans_context


def problem(kind="计算题", criterion="Method 8 points, result 2 points."):
    return ProblemInfo(q_id="q1", number="1", type=kind, stem="Calculate 6*7, showing work.",
                       criterion=criterion, max_score=10, reference_answer="42")


def answer(text="6*7=41"):
    return StudentAnswerInfo(q_id="q1", number="1", type="计算题", content=text)


def plan_for(p=None, *, mode="points", floor=0, nonblank=False):
    p = p or problem()
    data = fallback_math_rubric_plan(p).model_dump(mode="json", exclude={"plan_id"})
    items = ([dict(id="method", description="Method", points=8, source="provided", max_occurrences=1),
              dict(id="result", description="Result", points=2, source="provided", max_occurrences=1)]
             if mode == "points" else
             [dict(id="error", description="Independent arithmetic error", points=1, source="provided",
                   max_occurrences=None)] if mode == "deductions" else [])
    data["routes"] = [MathRubricRoute(**dict(id="main", description="Teacher rule", applies_when="All answers",
                           mode=mode, items=items, minimum_score=floor,
                           minimum_condition="nonblank" if nonblank else "always")).model_dump(mode="json")]
    data["plan_id"] = hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True,
                                               separators=(",", ":")).encode()).hexdigest()
    return MathRubricPlan.model_validate(data)


def assessment(method=8, result=0, *, text="6*7=41", **kwargs):
    return grading.MathAssessment(
        route_id="main", route_reason="Applicable to all answers.",
        claims=[grading.Claim(id="s1", quote=text, item_ids=["method", "result"],
                              verdict="refuted", reason="Final multiplication error.")],
        awards=[grading.Award(item_id="method", score=method, claim_ids=["s1"], reason="Correct setup."),
                grading.Award(item_id="result", score=result, claim_ids=["s1"], reason="Result checked.")],
        confidence=.9, comment="Only the final result loses credit.", **kwargs)


def response():
    return SimpleNamespace(content="{}", duration_ms=1)


def expert_for(draft, plan, text, *, evidence=None):
    score, steps = grading.score_assessment(draft, plan, text)
    return grading.ExpertResult(provider="mock:math", score=score, max_score=plan.max_score,
        confidence=.9, comment=draft.comment, steps=steps, raw_output=json.dumps({
            "version": grading.TRACE_VERSION, "plan_id": plan.plan_id,
            "assessment": draft.model_dump(), "evidence": evidence or []}))


@pytest.fixture
def provider():
    return SimpleNamespace(provider_id="mock:math")


@pytest.fixture(autouse=True)
def no_kb(monkeypatch):
    monkeypatch.setattr(grading.kb_tool, "retrieve_for_grading", AsyncMock(return_value=[]))


@pytest.mark.parametrize("method,result,expected", [(8, 0, 8), (0, 2, 2), (8, 2, 10), (0, 0, 0)])
def test_score_comes_from_rubric_not_final_result(method, result, expected):
    score, steps = grading.score_assessment(assessment(method, result), plan_for(), "6*7=41")
    assert score == expected
    assert sum(Decimal(str(step.score)) for step in steps) == Decimal(str(expected))


@pytest.mark.asyncio
@pytest.mark.parametrize("skill", [CalculationSkill, ProofSkill, ConceptSkill])
async def test_three_types_share_ledger_and_valid_score_with_tool_unknown(monkeypatch, provider, skill):
    p, a, plan = problem(), answer(), plan_for()
    draft = assessment(checks=[grading.Verification(claim_id="s1", context_quote=p.stem,
        request={"student_expression": "41", "reference_expression": "42"})])
    call = AsyncMock(return_value=(draft, response()))
    monkeypatch.setattr(grading, "structured_llm_call", call)
    monkeypatch.setattr(skill, "_verify", AsyncMock(return_value=[
        {"claim_id": "s1", "status": "unknown", "reason": "Unsupported."}]))
    with math_plans_context({"q1": plan}):
        result = await skill(provider).grade(p, a)
    assert result.score == 8 and not result.error_kind
    assert len(result.steps) == 2
    assert call.await_count == 1
    assert grading.read_math_trace(result)["evidence"][0]["status"] == "unknown"


@pytest.mark.asyncio
async def test_correct_result_invalid_process_does_not_force_full(monkeypatch, provider):
    p, a, plan = problem(), answer("6+7=42"), plan_for()
    draft = assessment(0, 2, text=a.content, checks=[grading.Verification(
        claim_id="s1", context_quote=p.stem,
        request={"student_expression": "42", "reference_expression": "42"})])
    # Check evidence is scoped to the final expression; it cannot grant method points.
    draft.claims[0].verdict = "supported"
    monkeypatch.setattr(grading, "structured_llm_call", AsyncMock(return_value=(draft, response())))
    monkeypatch.setattr(CalculationSkill, "_verify", AsyncMock(return_value=[
        {"claim_id": "s1", "status": "supported", "reason": "Final value only."}]))
    with math_plans_context({"q1": plan}):
        result = await CalculationSkill(provider).grade(p, a)
    assert result.score == 2


@pytest.mark.asyncio
async def test_one_targeted_review_preserves_valid_grade_on_review_failure(monkeypatch, provider):
    p, a, plan = problem(), answer(), plan_for()
    call = AsyncMock(side_effect=[(assessment(needs_review=True), response()), ValueError("invalid JSON")])
    monkeypatch.setattr(grading, "structured_llm_call", call)
    with math_plans_context({"q1": plan}), grading.math_review_scope():
        result = await CalculationSkill(provider).grade(p, a)
    assert result.score == 8 and not result.error_kind
    assert call.await_count == 2


@pytest.mark.asyncio
async def test_multi_expert_self_concerns_wait_for_combined_review(monkeypatch, provider):
    p, a, plan = problem(), answer(), plan_for()
    draft = assessment(needs_review=True)
    call = AsyncMock(return_value=(draft, response()))
    monkeypatch.setattr(grading, "structured_llm_call", call)
    with math_plans_context({"q1": plan}), grading.math_review_scope():
        grading.defer_local_review(True)
        first = await CalculationSkill(provider).grade(p, a)
        second = await CalculationSkill(provider).grade(p, a)
        assert call.await_count == 2  # Neither expert consumes the shared review.
        call.return_value = grading.ExpertReview(revisions=[]), response()
        result = await grading.review_math_experts(p, a, [first, second], provider)
        assert result == [first, second] and call.await_count == 3
        await grading.review_math_experts(p, a, [first, second], provider)
        assert call.await_count == 3


@pytest.mark.asyncio
async def test_shared_self_concern_can_correct_equal_item_scores(monkeypatch, provider):
    p, a, plan = problem(), answer(), plan_for()
    experts = [expert_for(assessment(needs_review=True), plan, a.content) for _ in range(2)]
    review = grading.ExpertReview(revisions=[grading.ExpertRevision(
        expert_index=i, source_quote=a.content, reason="Review found missing method justification.",
        assessment=assessment(6, 0)) for i in range(2)])
    monkeypatch.setattr(grading, "structured_llm_call", AsyncMock(return_value=(review, response())))
    with math_plans_context({"q1": plan}), grading.math_review_scope():
        result = await grading.review_math_experts(p, a, experts, provider)
    assert [expert.score for expert in result] == [6, 6]
    assert all("targeted_review" in grading.read_math_trace(expert) for expert in result)


@pytest.mark.asyncio
async def test_surviving_expert_gets_deferred_review(monkeypatch):
    from backend.agents import multi_expert
    from backend.tests.test_multi_expert_robustness import _FakeProvider, _FakeRegistry
    p, a, plan = problem(), answer(), plan_for()
    good, bad = _FakeProvider("good"), _FakeProvider("bad")
    calls = []

    async def model(route, **kwargs):
        calls.append((route.provider_id, kwargs["output_model"]))
        if route is bad:
            raise TimeoutError("Provider failed")
        if kwargs["output_model"] is grading.MathAssessment:
            return assessment(needs_review=True), response()
        assert kwargs["output_model"] is grading.ExpertReview
        revised = assessment(6, 0)
        revised.evidence_judgments = [grading.EvidenceJudgment(evidence_id="v1", applicability="applies",
            reason="The tool confirms the same final arithmetic error.")]
        return grading.ExpertReview(revisions=[grading.ExpertRevision(
            expert_index=0, source_quote=a.content, reason="Missing method justification.",
            assessment=revised)]), response()

    monkeypatch.setattr(grading, "structured_llm_call", model)
    monkeypatch.setattr(multi_expert, "get_skill_for_type", lambda _: CalculationSkill)
    with math_plans_context({"q1": plan}):
        result = await multi_expert.run_multi_expert(
            problem=p, answer=a, student_id="s", registry=_FakeRegistry([good, bad]),
            aggregation_method="weighted_average")
    assert result.score == 6 and result.synthesis_method == "degraded_to_single"
    assert sum(model is grading.ExpertReview for _, model in calls) == 1
    assert result.review_reasons == ["partial_experts"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["evidence_feedback", "expert_review"])
async def test_review_cannot_retarget_existing_tool_claim_id(monkeypatch, provider, stage):
    p, a, plan = problem(), answer("6*7=41; 1+1=2"), plan_for()
    draft = assessment(text="6*7=41", needs_review=True)
    changed = assessment(8, 2, text="1+1=2")  # Reuses s1 for unrelated correct text.
    evidence = [{"claim_id": "s1", "status": "refuted", "reason": "41 != 42"}]
    original = expert_for(draft, plan, a.content, evidence=evidence)
    if stage == "evidence_feedback":
        draft.checks = [grading.Verification(claim_id="s1", context_quote=p.stem,
                    request={"student_expression": "41", "reference_expression": "42"})]
        monkeypatch.setattr(CalculationSkill, "_verify", AsyncMock(return_value=evidence))
        call = AsyncMock(side_effect=[(draft, response()), (changed, response())])
        monkeypatch.setattr(grading, "structured_llm_call", call)
        with math_plans_context({"q1": plan}), grading.math_review_scope():
            result = await CalculationSkill(provider).grade(p, a)
    else:
        review = grading.ExpertReview(revisions=[grading.ExpertRevision(
            expert_index=0, source_quote="1+1=2", reason="Changed claim", assessment=changed)])
        monkeypatch.setattr(grading, "structured_llm_call", AsyncMock(return_value=(review, response())))
        with math_plans_context({"q1": plan}), grading.math_review_scope():
            result = (await grading.review_math_experts(p, a, [original], provider))[0]
    trace = grading.read_math_trace(result)
    assert result.score == 8
    assert trace["assessment"]["claims"][0]["quote"] == "6*7=41"
    assert trace["evidence"] == evidence


@pytest.mark.asyncio
async def test_bad_graph_uses_one_direct_llm_fallback(monkeypatch, provider):
    p, a, plan = problem(), answer(), plan_for()
    bad = assessment(text="Student never wrote this.")
    direct = grading.DirectMathGrade(score=8, confidence=.8, rationale="Method 8/8, result 0/2.",
                                     comment="Final calculation is incorrect.")
    call = AsyncMock(side_effect=[(bad, response()), (direct, response())])
    monkeypatch.setattr(grading, "structured_llm_call", call)
    with math_plans_context({"q1": plan}):
        result = await CalculationSkill(provider).grade(p, a)
    assert result.score == 8 and result.steps[0].score == 8
    assert "direct" in grading.read_math_trace(result)
    diagnostic = grading.read_math_trace(result)["fallback"]
    assert diagnostic["reason"] == "invalid_detailed_assessment"
    assert diagnostic["error_type"] == "ValueError"
    assert diagnostic["draft"] == bad.model_dump(mode="json")
    assert call.await_count == 2
    assert call.await_args.kwargs["output_model"] is grading.DirectMathGrade


@pytest.mark.asyncio
async def test_real_provider_failure_does_not_fabricate_a_zero_grade(monkeypatch, provider):
    call = AsyncMock(side_effect=TimeoutError("provider timed out"))
    monkeypatch.setattr(grading, "structured_llm_call", call)
    with math_plans_context({"q1": plan_for()}):
        result = await CalculationSkill(provider).grade(problem(), answer())
    assert result.error_kind == "transient_llm"
    assert result.steps == [] and call.await_count == 1


@pytest.mark.asyncio
async def test_nonretryable_provider_failure_does_not_launch_direct_fallback(monkeypatch, provider):
    from backend.tools.structured_llm import PermanentLLMError
    call = AsyncMock(side_effect=PermanentLLMError("provider_authentication_failed"))
    monkeypatch.setattr(grading, "structured_llm_call", call)
    with math_plans_context({"q1": plan_for()}):
        result = await CalculationSkill(provider).grade(problem(), answer())
    assert result.error_kind and result.steps == [] and call.await_count == 1


@pytest.mark.asyncio
async def test_typed_transient_failure_never_becomes_structure_fallback(monkeypatch, provider):
    from backend.tools.structured_llm import TransientLLMError
    call = AsyncMock(side_effect=TransientLLMError("transport interrupted"))
    monkeypatch.setattr(grading, "structured_llm_call", call)
    with math_plans_context({"q1": plan_for()}):
        result = await CalculationSkill(provider).grade(problem(), answer())
    assert result.error_kind == "transient_llm" and call.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["initial", "feedback", "expert_review"])
async def test_wrapped_storage_error_is_not_silently_downgraded(monkeypatch, provider, stage):
    from sqlalchemy.exc import OperationalError
    from backend.tools.structured_llm import TransientLLMError
    failure = TransientLLMError("request interrupted")
    failure.__cause__ = OperationalError("SELECT daily_usage", {}, OSError("disk I/O error"))
    p, a, plan = problem(), answer(), plan_for()
    draft = assessment(needs_review=True)
    call = AsyncMock(side_effect=([(draft, response()), failure] if stage == "feedback" else failure))
    monkeypatch.setattr(grading, "structured_llm_call", call)
    with math_plans_context({"q1": plan}), grading.math_review_scope(), pytest.raises(TransientLLMError):
        if stage == "expert_review":
            await grading.review_math_experts(p, a, [expert_for(draft, plan, a.content)], provider)
        else:
            await CalculationSkill(provider).grade(p, a)
    assert call.await_count == (2 if stage == "feedback" else 1)


@pytest.mark.asyncio
async def test_cancel_propagates(monkeypatch, provider):
    import asyncio
    monkeypatch.setattr(grading, "structured_llm_call", AsyncMock(side_effect=asyncio.CancelledError))
    with math_plans_context({"q1": plan_for()}), pytest.raises(asyncio.CancelledError):
        await CalculationSkill(provider).grade(problem(), answer())


@pytest.mark.parametrize("mutate,error", [
    (lambda a: a.awards.append(a.awards[0]), "missing_duplicate"),
    (lambda a: setattr(a.awards[0], "score", 9), "invalid_award"),
    (lambda a: a.claims[0].depends_on.append("s1"), "cyclic"),
    (lambda a: a.claims[0].depends_on.append("missing"), "missing_claim"),
    (lambda a: setattr(a.claims[0], "quote", "invented"), "claim_not"),
])
def test_invalid_evidence_and_ledgers_are_rejected(mutate, error):
    draft = assessment()
    mutate(draft)
    with pytest.raises(ValueError, match=error):
        grading.score_assessment(draft, plan_for(), "6*7=41")


@pytest.mark.parametrize("case", ["unquoted_credit", "blank_quote", "blank_reason", "blank_holistic"])
def test_unsubstantiated_credit_and_empty_evidence_are_rejected(case):
    draft, plan = assessment(), plan_for()
    if case == "unquoted_credit":
        draft.awards[0].claim_ids = []
    elif case == "blank_quote":
        draft.claims[0].quote = " "
    elif case == "blank_reason":
        draft.awards[0].reason = " "
    else:
        plan = plan_for(mode="holistic")
        draft = grading.MathAssessment(route_id="main", route_reason="Whole rubric",
                                       score=8, rationale=" ", comment=" ")
    with pytest.raises(ValueError):
        grading.score_assessment(draft, plan, "6*7=41 ")


def test_missing_item_may_have_zero_credit_without_a_fabricated_quote():
    draft = assessment()
    draft.awards[1].claim_ids = []
    draft.awards[1].reason = "The required final result was not supplied."
    assert grading.score_assessment(draft, plan_for(), "6*7=41")[0] == 8


def test_direct_grade_rejects_whitespace_only_rationale():
    with pytest.raises(ValueError):
        grading.DirectMathGrade(score=8, rationale=" \n ", comment="Partial credit.")


@pytest.mark.asyncio
async def test_original_formula_survives_real_json_parser_and_deduction_scoring():
    """Do not mock the parser whose formatting used to discard this graph."""
    from backend.llm.providers import LLMResponse

    p = problem(kind="推理题")
    a = answer(r"\(2+2=5\)")
    plan = plan_for(p, mode="deductions")
    payload = grading.MathAssessment(
        route_id="main", route_reason="Teacher's deduction rule applies.",
        claims=[grading.Claim(id="s1", quote=a.content, item_ids=["error"],
                              verdict="refuted", reason="One arithmetic error.")],
        deductions=[grading.Deduction(error_id="e1", rule_id="error", claim_id="s1",
                                      reason="One independent error.")],
        confidence=.9, comment="One point deducted.",
    )
    calls = []

    async def invoke(messages):
        calls.append(messages)
        if len(calls) == 2:
            assert "APPLY SCOPED EVIDENCE" in messages[-1].content
            payload.evidence_judgments = [grading.EvidenceJudgment(
                evidence_id="v1", applicability="applies", reason="The explicit arithmetic error is the same one already deducted.")]
        elif len(calls) > 2:
            raise grading.PermanentLLMError("Unexpected extra model call in fixed replay")
        return LLMResponse(content=payload.model_dump_json(), provider="replay", model="raw-json",
                           duration_ms=1)

    with math_plans_context({p.q_id: plan}):
        result = await ProofSkill(SimpleNamespace(provider_id="replay", ainvoke=invoke)).grade(p, a)
    trace = grading.read_math_trace(result)
    assert result.score == 9 and not result.error_kind
    assert len(calls) == 2 and "direct" not in trace
    assert trace["assessment"]["claims"][0]["quote"] == a.content


def test_deductions_apply_only_teacher_floor_and_no_negative_frontend_steps():
    plan = plan_for(mode="deductions", floor=6, nonblank=True)
    draft = grading.MathAssessment(route_id="main", route_reason="Teacher deduction rule",
        claims=[grading.Claim(id="s1", quote="work", item_ids=["error"], verdict="refuted", reason="7 errors")],
        deductions=[grading.Deduction(error_id="e1", rule_id="error", claim_id="s1", occurrences=7,
                                     reason="Seven independent arithmetic mistakes.")],
        comment="Teacher floor applies.", confidence=.9)
    score, steps = grading.score_assessment(draft, plan, "work")
    assert score == 6 and steps[0].score == 6
    draft.deductions.append(draft.deductions[0])
    with pytest.raises(ValueError, match="duplicate_deduction"):
        grading.score_assessment(draft, plan, "work")


def test_nonblank_minimum_does_not_grant_blank_answer_points():
    plan = plan_for(mode="holistic", floor=6, nonblank=True)
    draft = grading.MathAssessment(route_id="main", route_reason="Blank", score=0, confidence=.9,
                                    comment="No submitted content.")
    score, steps = grading.score_assessment(draft, plan, "")
    assert score == 0 and steps[0].score == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("blank_credit", [0, 2])
async def test_blank_answer_uses_actual_rubric_once_without_empty_deduction_full_marks(
    monkeypatch, provider, blank_credit
):
    p = problem(criterion=f"Blank answers earn {blank_credit} points; otherwise deduct errors.")
    plan = plan_for(p, mode="deductions", floor=6, nonblank=True)
    draft = grading.MathAssessment(route_id="main", route_reason="Deduction route",
                                    deductions=[], comment="No written error.")
    with pytest.raises(ValueError, match="blank_deduction"):
        grading.score_assessment(draft, plan, "")
    call = AsyncMock(return_value=(grading.DirectMathGrade(
        score=blank_credit, rationale=f"Explicit blank policy: {blank_credit}/10.",
        comment="Blank answer.", confidence=1), response()))
    monkeypatch.setattr(grading, "structured_llm_call", call)
    with math_plans_context({"q1": plan}):
        result = await CalculationSkill(provider).grade(p, answer(""))
    assert result.score == blank_credit and not result.error_kind
    assert call.await_count == 1
    assert call.await_args.kwargs["output_model"] is grading.DirectMathGrade


def test_holistic_evidence_is_separate_from_brief_comment():
    draft = grading.MathAssessment(route_id="main", route_reason="Whole rubric", score=8,
        claims=[grading.Claim(id="s1", quote="6*7=41", verdict="refuted", reason="Arithmetic error.")],
        rationale="Method 8/8: correct equation. Final arithmetic 0/2: 6*7=41.",
        comment="Final arithmetic error.", confidence=.9)
    score, steps = grading.score_assessment(draft, plan_for(mode="holistic"), "6*7=41")
    assert score == 8 and steps[0].desc == draft.rationale


@pytest.mark.parametrize("duplicate,origin", [(True, None), (False, "s2")])
def test_duplicate_rule_or_unrelated_error_origin_rejected(duplicate, origin):
    draft = grading.MathAssessment(route_id="main", route_reason="Deduction rubric",
        claims=[grading.Claim(id=key, quote=key, item_ids=["error"], verdict="refuted", reason="Arithmetic")
                for key in ("s1", "s2")],
        deductions=[grading.Deduction(error_id="e1", rule_id="error", claim_id="s1",
                                     caused_by=origin, reason="One arithmetic error")],
        comment="Evidence", confidence=.9)
    if duplicate:
        draft.deductions.append(draft.deductions[0].model_copy(update={"error_id": "different-id"}))
    with pytest.raises(ValueError, match="duplicate_rule_application|error_origin_not_a_dependency"):
        grading.score_assessment(draft, plan_for(mode="deductions"), "s1 s2")


@pytest.mark.asyncio
@pytest.mark.parametrize("student,reference,check_options,initial,final,status", [
    ("1.5707963", "pi/2", {"comparison": "numeric", "allow_approximation": True,
                           "absolute_tolerance": 1e-7}, 0, 2, "supported"),
    ("(x**2-1)/(x-1)", "x+1", {"comparison": "equivalence"}, 2, 0, "refuted"),
])
async def test_actual_math_tool_evidence_is_consumed_before_final_ledger(
    monkeypatch, provider, student, reference, check_options, initial, final, status
):
    p = problem()
    p.stem = "Compare the two expressions under the stated precision/domain."
    p.reference_answer = reference
    p.criterion += " Approximation with absolute tolerance 1e-7 is allowed." if status == "supported" else ""
    a, plan = answer(student), plan_for(p)
    draft = assessment(8, initial, text=student, checks=[grading.Verification(
        claim_id="s1", context_quote=p.criterion,
        request={"student_expression": student, "reference_expression": reference, **check_options})])
    seen = []

    async def model(*args, **kwargs):
        seen.append(kwargs)
        if len(seen) == 1:
            return draft, response()
        payload = json.loads(kwargs["user_prompt"].split("APPLY SCOPED EVIDENCE (one pass):\n")[1])
        evidence = payload["evidence"][0]
        assert evidence["status"] == status
        assert evidence["request"]["student_expression"] == student
        if status == "refuted":
            assert "1" in evidence["counterexample"]
        return assessment(8, final, text=student), response()

    monkeypatch.setattr(grading, "structured_llm_call", model)
    with math_plans_context({"q1": plan}), grading.math_review_scope():
        result = await CalculationSkill(provider).grade(p, a)
    assert not result.error_kind and result.score == 8 + final and len(seen) == 2
    assert sum(step.score for step in result.steps) == result.score


@pytest.mark.asyncio
async def test_late_tool_timeout_keeps_earlier_completed_evidence(monkeypatch, provider):
    import asyncio
    from backend.tools import math_verification
    monkeypatch.setattr(grading, "VERIFICATION_BUDGET_SECONDS", .03)
    calls = 0

    async def verify(_):
        nonlocal calls
        calls += 1
        if calls == 1:
            return math_verification.MathVerificationEvidence(
                status="supported", method="exact", reason="First check completed.")
        await asyncio.sleep(60)

    monkeypatch.setattr(math_verification, "verify_math", verify)
    p, a, plan = problem(), answer(), plan_for()
    draft = assessment(checks=[grading.Verification(claim_id="s1", context_quote=p.stem,
        request={"student_expression": "41", "reference_expression": "42"}) for _ in range(3)])
    evidence = await CalculationSkill(provider)._verify(draft, p, a, plan)
    assert [entry["status"] for entry in evidence] == ["supported", "unknown", "unknown"]
    assert calls == 2  # No third launch after the total budget expires.


def test_rounding_preserves_item_total():
    score, steps = grading.score_assessment(assessment(7.46, 1.46), plan_for(), "6*7=41")
    assert score == 8.9
    assert sum(Decimal(str(step.score)) for step in steps) == Decimal("8.9")


@pytest.mark.asyncio
async def test_equal_totals_opposite_items_reviewed_once_preserving_weights(monkeypatch, provider):
    from backend.agents.multi_expert import _weighted_average_fallback
    p, a, plan = problem(), answer(), plan_for()
    drafts = [assessment(8, 0), assessment(6, 2)]
    experts = []
    for index, draft in enumerate(drafts):
        score, steps = grading.score_assessment(draft, plan, a.content)
        experts.append(grading.ExpertResult(provider=f"expert:{index}", score=score, max_score=10,
            confidence=.9, comment=draft.comment, steps=steps, raw_output=json.dumps({
                "version": grading.TRACE_VERSION, "plan_id": plan.plan_id,
                "assessment": draft.model_dump(), "evidence": []})))
    revision = grading.ExpertReview(revisions=[grading.ExpertRevision(
        expert_index=1, source_quote=a.content, reason="Same correct setup; 41 is an arithmetic error.",
        assessment=assessment(8, 0))])
    call = AsyncMock(return_value=(revision, response()))
    monkeypatch.setattr(grading, "structured_llm_call", call)
    with math_plans_context({"q1": plan}), grading.math_review_scope():
        reviewed = await grading.review_math_experts(p, a, experts, provider)
        again = await grading.review_math_experts(p, a, experts, provider)
    assert call.await_count == 1 and again == experts
    assert reviewed[1].score == 8
    assert "original_assessment" in grading.read_math_trace(reviewed[1])
    result = _weighted_average_fallback(p, reviewed)
    assert sum(step.score for step in result.steps) == result.score == 8
    assert a.content in call.await_args.kwargs["user_prompt"]
