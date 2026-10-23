"""Real parser/orchestration regressions; fixed raw JSON is not an accuracy test."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.llm.providers import LLMResponse
from backend.services.math_grading_context import math_plans_context
from backend.services.math_rubric_plan import MathRubricRoute, _content_hash
from backend.services.math_tool_planning import automatic_checks
from backend.skills import math_grading as g
from backend.skills.proof import ProofSkill
from backend.skills.calculation import CalculationSkill
from backend.tests.test_math_grading import problem, answer, plan_for, assessment, expert_for


@pytest.fixture(autouse=True)
def no_kb(monkeypatch):
    monkeypatch.setattr(g.kb_tool, "retrieve_for_grading", AsyncMock(return_value=[]))


def raw_provider(*outputs):
    calls = []
    async def invoke(messages):
        calls.append(messages)
        if len(calls) > len(outputs):
            raise g.PermanentLLMError("Unexpected extra request in offline test")
        payload = outputs[len(calls) - 1]
        if isinstance(payload, Exception):
            raise payload
        content = payload if isinstance(payload, str) else payload.model_dump_json()
        return LLMResponse(content=content, provider="offline", model="fixed-json", duration_ms=1)
    return SimpleNamespace(provider_id="offline", ainvoke=invoke), calls


def change_route(plan, **changes):
    data = plan.model_dump(mode="json", exclude={"plan_id"})
    data["routes"][0].update(changes)
    data["routes"][0] = MathRubricRoute.model_validate(data["routes"][0]).model_dump(mode="json")
    return type(plan).model_validate({**data, "plan_id": _content_hash(data)})


@pytest.mark.asyncio
async def test_bad_quote_is_repaired_locally_without_losing_valid_claims():
    p, a = problem("证明题"), answer("Let n be odd. Thus n squared is odd.")
    plan = plan_for(p)
    bad = assessment(text="Let n be odd.")
    bad.claims.append(g.Claim(id="s2", quote="invented result", verdict="supported", reason="Conclusion."))
    fixed = bad.model_copy(deep=True)
    fixed.claims[1].quote = "Thus n squared is odd."
    provider, calls = raw_provider(bad, fixed)
    with math_plans_context({p.q_id: plan}):
        result = await ProofSkill(provider).grade(p, a)
    trace = g.read_math_trace(result)
    assert not result.error_kind and "direct" not in trace
    assert len(calls) == 2 and "preserve_claim_quotes" in calls[1][-1].content
    assert [c["quote"] for c in trace["assessment"]["claims"]] == ["Let n be odd.", "Thus n squared is odd."]


@pytest.mark.asyncio
async def test_invalid_quote_is_not_silently_dropped_to_create_full_marks():
    p, a = problem("证明题"), answer("An unsupported claim.")
    plan = plan_for(p, mode="deductions")
    bad = g.MathAssessment(route_id="main", route_reason="Deduct errors", claims=[
        g.Claim(id="c", quote="invented error", item_ids=["error"], verdict="refuted", reason="Error")],
        deductions=[g.Deduction(error_id="e", rule_id="error", claim_id="c", reason="Error")], comment="One error")
    direct = g.DirectMathGrade(score=9, rationale="One unsupported claim under the frozen one-point deduction rule.", comment="One error")
    provider, calls = raw_provider(bad, bad, direct)
    with math_plans_context({p.q_id: plan}):
        result = await ProofSkill(provider).grade(p, a)
    trace = g.read_math_trace(result)
    assert len(calls) == 3 and result.score == 9
    assert "direct" in trace and trace["fallback"]["draft"]["claims"][0]["quote"] == "invented error"


def test_omission_is_grounded_in_requirement_not_fabricated_student_quote():
    p = problem("证明题", criterion="Forward direction 8 points, reverse direction 2 points.")
    p.stem = "Prove both directions."
    draft = assessment(text="Forward established.")
    draft.claims.append(g.Claim(id="missing", kind="omission", source_quote=p.stem,
        item_ids=["result"], verdict="refuted", reason="Reverse direction missing."))
    draft.awards[1].claim_ids = ["missing"]
    assert g.score_assessment(draft, plan_for(p), "Forward established.", problem_text=p.stem)[0] == 8
    draft.awards[1].score = 2
    with pytest.raises(ValueError, match="omission_is_not"):
        g.score_assessment(draft, plan_for(p), "Forward established.", problem_text=p.stem)


def test_consequences_exemptions_and_rule_limit_are_executed_by_ledger():
    plan = change_route(plan_for(mode="deductions"), error_policy="no_repeat_deduction",
        error_policy_source_quote="Do not deduct the same error twice.", exemptions=[
        dict(id="minor", description="Minor notation costs no points", source_quote="Minor notation costs no points")])
    draft = g.MathAssessment(route_id="main", route_reason="Teacher rule", comment="Specific evidence",
        claims=[g.Claim(id=f"c{i}", quote=f"line{i}", item_ids=["error", "minor"],
            depends_on=["c1"] if i == 2 else [], verdict="refuted", reason="Located error") for i in range(1, 5)],
        deductions=[g.Deduction(error_id=f"e{i}", rule_id="minor" if i == 3 else "error", claim_id=f"c{i}",
            caused_by="c1" if i == 2 else None, reason="Independent or declared consequential error") for i in range(1, 5)])
    assert g.score_assessment(draft, plan, "line1 line2 line3 line4")[0] == 8
    data = plan.routes[0].items[0].model_dump(mode="json")
    data["max_occurrences"] = 1
    capped = change_route(plan, items=[data])
    assert g.score_assessment(draft, capped, "line1 line2 line3 line4")[0] == 9


@pytest.mark.parametrize("student,reference", [("4", "4"), ("x^2+2*x+1", "(x+1)^2"),
    ("3 m/s^2", "3 m/s^2"), ("pi/2", "pi/2"), ("E", "E"), ("I", "I"), ("x=3", "x=3")])
@pytest.mark.asyncio
async def test_explicit_math_opportunity_runs_real_tool_when_model_checks_empty(student, reference):
    p, a = problem(), answer(student)
    p.reference_answer = reference
    plan = plan_for(p)
    initial = assessment(8, 2, text=student)
    reviewed = initial.model_copy(deep=True)
    reviewed.claims[0].verdict = "supported"
    reviewed.evidence_judgments = [g.EvidenceJudgment(evidence_id="v1", applicability="applies", reason="Scoped relation is the submitted result.")]
    provider, calls = raw_provider(initial, reviewed)
    with math_plans_context({p.q_id: plan}), g.math_review_scope():
        result = await CalculationSkill(provider).grade(p, a)
    trace = g.read_math_trace(result)
    assert result.score == 10 and not result.error_kind and len(calls) == 2
    assert trace["evidence"][0]["status"] == "supported"
    assert trace["evidence"][0]["source_binding"] == "explicit_syntax"
    assert not trace["review_pending"]


def test_equation_constraint_is_not_an_unconditional_identity():
    p = problem()
    p.reference_answer = ""
    claim = g.Claim(id="c", quote="x=3", verdict="supported", reason="Solution")
    assert automatic_checks([claim], p) == []


@pytest.mark.asyncio
async def test_refuted_tool_and_failed_feedback_keeps_specific_nonblocking_advice():
    p, a = problem(), answer("41")
    initial = assessment(8, 2, text="41")
    provider, calls = raw_provider(initial, "{bad JSON", "{bad JSON")
    with math_plans_context({p.q_id: plan_for(p)}), g.math_review_scope():
        result = await CalculationSkill(provider).grade(p, a)
    trace = g.read_math_trace(result)
    assert len(calls) == 3 and result.score == 10 and not result.error_kind
    assert trace["review_failure"] and trace["review_advisory"]
    assert trace["evidence"][0]["status"] == "refuted"
    assert "Tool verification" in result.steps[0].desc


@pytest.mark.parametrize("tool_request", [
    {"student_expression": "1", "reference_expression": "1"},
    {"student_expression": "41", "reference_expression": "42", "comparison": "numeric",
     "allow_approximation": True, "absolute_tolerance": 2},
    {"student_expression": "41", "reference_expression": "42", "assumptions": {"x": {"positive": True}}},
])
@pytest.mark.asyncio
async def test_ungrounded_requests_never_reach_cas(monkeypatch, tool_request):
    from backend.tools import math_verification
    verify = AsyncMock()
    monkeypatch.setattr(math_verification, "verify_math", verify)
    p, a = problem(), answer("41")
    draft = assessment(text="41", checks=[g.Verification(claim_id="s1", context_quote=p.stem, request=tool_request)])
    evidence = await CalculationSkill(SimpleNamespace(provider_id="offline"))._verify(draft, p, a, plan_for(p))
    assert evidence[0]["status"] == "unknown" and evidence[0]["binding_issue"]
    assert verify.await_count == 0


@pytest.mark.asyncio
async def test_invalid_and_duplicate_proposals_do_not_consume_tool_slots(monkeypatch):
    from backend.tools import math_verification
    verify = AsyncMock(wraps=math_verification.verify_math)
    monkeypatch.setattr(math_verification, "verify_math", verify)
    p, a = problem(), answer("41")
    invalid = g.Verification(claim_id="s1", context_quote=p.stem,
        request={"student_expression": "1", "reference_expression": "1"})
    initial = assessment(8, 0, text="41", checks=[invalid] * 8)
    reviewed = initial.model_copy(deep=True)
    reviewed.checks = []
    reviewed.evidence_judgments = [g.EvidenceJudgment(evidence_id="v1", applicability="applies", reason="The result differs by one.")]
    provider, calls = raw_provider(initial, reviewed)
    plan = plan_for(p)
    with math_plans_context({p.q_id: plan}), g.math_review_scope():
        result = await CalculationSkill(provider).grade(p, a)
    trace = g.read_math_trace(result)
    assert result.score == 8 and len(calls) == 2 and verify.await_count == 1
    assert len(trace["rejected_checks"]) == 8 and trace["evidence"][0]["status"] == "refuted"
    valid = g.Verification(claim_id="s1", context_quote=p.reference_answer,
        request={"student_expression": "41", "reference_expression": "42"})
    initial.checks = [valid] * 8
    scheduled, rejected = g._schedule_checks(initial, p, a, plan)
    assert len(scheduled) == 1 and not rejected


@pytest.mark.asyncio
async def test_mixed_direct_and_detailed_experts_receive_real_parser_review():
    p, a, plan = problem(), answer(), plan_for()
    detailed = expert_for(assessment(), plan, a.content)
    direct = detailed.model_copy(update={"score": 2, "raw_output": json.dumps({
        "version": g.TRACE_VERSION, "plan_id": plan.plan_id, "evidence": [],
        "direct": {"score": 2, "rationale": "Only the result considered", "comment": "Incomplete grading"}})})
    revision = g.ExpertReview(revisions=[g.ExpertRevision(expert_index=1, reason="The teacher awards eight method points.",
        source_quote=a.content, assessment=assessment())])
    provider, calls = raw_provider(revision)
    with math_plans_context({p.q_id: plan}), g.math_review_scope():
        results = await g.review_math_experts(p, a, [detailed, direct], provider)
        results = await g.review_math_experts(p, a, results, provider)
    assert len(calls) == 1 and [r.score for r in results] == [8, 8]
    trace = g.read_math_trace(results[1])
    assert "original_direct" in trace and "assessment" in trace and "direct" not in trace


@pytest.mark.asyncio
async def test_review_source_error_gets_one_local_repair_not_silent_discard():
    p, a, plan = problem(), answer(), plan_for()
    experts = [expert_for(assessment(method=score), plan, a.content) for score in (8, 5)]
    fixed = g.ExpertReview(revisions=[g.ExpertRevision(expert_index=1, reason="Eight method points follow the rule.",
        source_quote=a.content, assessment=assessment())])
    invalid = fixed.model_copy(deep=True)
    invalid.revisions[0].source_quote = "invented student text"
    provider, calls = raw_provider(invalid, fixed)
    with math_plans_context({p.q_id: plan}), g.math_review_scope():
        results = await g.review_math_experts(p, a, experts, provider)
    assert len(calls) == 2 and [r.score for r in results] == [8, 8]
    assert "review_source_or_expert_not_found" in calls[1][-1].content


@pytest.mark.parametrize("student,verdict,applicability,pending", [
    ("41", "supported", "applies", True),
    ("42", "refuted", "uncertain", True),
    ("42", "refuted", "partial", False),
    ("41", "refuted", "applies", False),
])
@pytest.mark.asyncio
async def test_feedback_does_not_confuse_scoped_evidence_with_resolved_scoring(student, verdict, applicability, pending):
    from backend.agents.multi_expert import _expert_to_correction
    p, a, plan = problem(), answer(student), plan_for()
    initial = assessment(8, 2, text=student)
    initial.claims[0].verdict = verdict
    reviewed = initial.model_copy(deep=True)
    reviewed.evidence_judgments = [g.EvidenceJudgment(evidence_id="v1", applicability=applicability,
        reason="The relation is only local when the preceding premise is wrong.")]
    provider, calls = raw_provider(initial, reviewed)
    with math_plans_context({p.q_id: plan}), g.math_review_scope():
        result = await CalculationSkill(provider).grade(p, a)
    trace = g.read_math_trace(result)
    assert result.score == 10 and len(calls) == 2  # tools do not dictate points
    assert trace["review_pending"] is pending
    correction = _expert_to_correction(p.q_id, p.type, result, "single")
    assert ("math_scoring_uncertainty" in correction.review_reasons) is pending
    assert not result.error_kind


def test_supported_predecessor_is_not_an_error_origin():
    plan = change_route(plan_for(mode="deductions"), error_policy="no_repeat_deduction",
        error_policy_source_quote="Do not deduct the same error twice.")
    draft = g.MathAssessment(route_id="main", route_reason="Teacher rule", comment="One error",
        claims=[g.Claim(id="ok", quote="2+2=4", verdict="supported", reason="Correct"),
                g.Claim(id="bad", quote="4+1=6", depends_on=["ok"], verdict="refuted", reason="New arithmetic error")],
        deductions=[g.Deduction(error_id="e", rule_id="error", claim_id="bad", caused_by="ok", reason="Uses prior value")])
    with pytest.raises(ValueError, match="error_origin_must_identify_an_error"):
        g.score_assessment(draft, plan, "2+2=4;4+1=6")
    draft.deductions[0].caused_by = None
    assert g.score_assessment(draft, plan, "2+2=4;4+1=6")[0] == 9


@pytest.mark.asyncio
async def test_same_total_but_different_exemptions_are_reviewed():
    p, a = problem(), answer("line1;line2")
    plan = change_route(plan_for(p, mode="deductions"), exemptions=[dict(id="minor",
        description="Minor costs zero", source_quote="Minor costs zero")])
    first = g.MathAssessment(route_id="main", route_reason="Teacher rule", comment="One error",
        claims=[g.Claim(id=f"c{i}", quote=f"line{i}", item_ids=["error", "minor"],
                       verdict="refuted", reason="Located issue") for i in (1, 2)],
        deductions=[g.Deduction(error_id=f"e{i}", rule_id="error", claim_id=f"c{i}",
                    exemption_id="minor" if i == 2 else None, reason="Apply rule") for i in (1, 2)])
    second = first.model_copy(deep=True)
    second.deductions[0].exemption_id = "minor"
    second.deductions[1].exemption_id = None
    experts = [expert_for(draft, plan, a.content) for draft in (first, second)]
    provider, calls = raw_provider(g.ExpertReview(revisions=[]))
    with math_plans_context({p.q_id: plan}), g.math_review_scope():
        results = await g.review_math_experts(p, a, experts, provider)
    assert len(calls) == 1 and [r.score for r in results] == [9, 9]
    assert g.math_scoring_disagreement(results)


def test_local_quote_repair_must_retain_located_missing_obligation():
    p, a = problem("证明题"), answer("Forward direction shown.")
    p.stem = "Prove both directions."
    plan = plan_for(p, mode="deductions")
    draft = g.MathAssessment(route_id="main", route_reason="Teacher rule", comment="Missing reverse",
        claims=[g.Claim(id="good", quote=a.content, verdict="supported", reason="Forward proof"),
                g.Claim(id="missing", kind="omission", source_quote=p.stem, verdict="refuted", reason="Reverse omitted"),
                g.Claim(id="invalid", quote="never written", verdict="refuted", reason="Bad quote")],
        deductions=[g.Deduction(error_id="e", rule_id="error", claim_id="missing", reason="Reverse omitted")])
    validator = g._assessment_validator(plan, a, p)
    with pytest.raises(g.StructuredOutputInvalidError):
        validator(draft)
    repair = draft.model_copy(deep=True)
    repair.claims = repair.claims[:1]
    repair.deductions = []
    with pytest.raises(g.StructuredOutputInvalidError) as exc:
        validator(repair)
    assert "repair_changed_valid_source_claim" in exc.value.repair_context
