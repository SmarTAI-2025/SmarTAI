"""Optional proof notes cannot consume score-repair calls or decide marks."""
import json
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from backend.services.math_proof_audit import inspect_audit, read_appendix
from backend.services.math_grading_context import math_plans_context
from backend.skills import math_grading as g
from backend.skills.proof import ProofSkill
from backend.skills.calculation import CalculationSkill
from backend.skills.concept import ConceptSkill
from backend.tests.test_math_grading import problem, answer, plan_for, assessment
from backend.tests.test_math_evidence_reliability import raw_provider


@pytest.fixture(autouse=True)
def no_external_evidence(monkeypatch):
    monkeypatch.setattr(g.kb_tool, "retrieve_for_grading", AsyncMock(return_value=[]))


def notes(*, effect="none", inference="justified", concern="", premise_status="established"):
    return {"steps": [{"claim_id": "s1", "conclusion": "f可导", "rule": "检查连续是否足够",
        "premises": [{"condition": "可导性有依据", "status": premise_status,
                      "source_ids": ["question:1"] if premise_status == "established" else []}],
        "inference": inference, "score_effect": effect, "rubric_source_ids": ["rubric:1"],
        "concern": concern}], "context_source_ids": []}


def wire(draft, appendix=None):
    core = draft.model_dump_json()
    return core if appendix is None else core + "\nPROOF_AUDIT\n" + (
        appendix if isinstance(appendix, str) else json.dumps(appendix, ensure_ascii=False))


def case():
    p = problem("证明题", criterion="证明过程8分；结论2分。")
    p.stem = "检查下面证明的推理是否充分。"
    a = answer("f连续，所以f可导。")
    plan = plan_for(p)
    draft = assessment(8, 2, text=a.content)
    draft.claims[0].verdict = "supported"
    return p, a, plan, draft


@pytest.mark.asyncio
@pytest.mark.parametrize("appendix", [None, "", "not JSON", "{broken", '{"steps":[{"claim_id":',
    "null", "false", "[]", '{"steps":"wrong type"}', '{"steps":[false,{},42]}',
    '{"steps":[],"context_source_ids":42}', "x" * 60001])
async def test_broken_or_missing_notes_never_repair_or_discard_valid_detailed_grade(appendix):
    p, a, plan, draft = case()
    provider, calls = raw_provider(wire(draft, appendix))
    with math_plans_context({p.q_id: plan}), g.math_review_scope():
        result = await ProofSkill(provider).grade(p, a)
    trace = g.read_math_trace(result)
    assert len(calls) == 1 and result.score == 10 and not result.error_kind
    assert "then append PROOF_AUDIT" in calls[0][0].content
    assert "Return a single JSON object with scoring details." not in calls[0][0].content
    assert "assessment" in trace and "direct" not in trace
    assert not trace["proof_audit"]["review_concerns"] and not trace.get("review_advisory")
    assert "证明依据核对建议" not in result.steps[0].desc


@pytest.mark.asyncio
@pytest.mark.parametrize("skill", [CalculationSkill, ConceptSkill])
async def test_nonproof_skills_do_not_activate_optional_proof_checks(skill):
    p, a, plan, draft = case()
    provider, calls = raw_provider(wire(draft, notes(effect="unresolved", inference="invalid", concern="推理不足")))
    with math_plans_context({p.q_id: plan}), g.math_review_scope():
        result = await skill(provider).grade(p, a)
    assert len(calls) == 1 and result.score == 10
    assert "Return a single JSON object with scoring details." in calls[0][0].content
    assert "proof_audit" not in g.read_math_trace(result)


@pytest.mark.asyncio
async def test_uncovered_text_does_not_become_a_student_omission_or_trigger_review():
    p, a, plan, draft = case()
    a.content = "f连续。\n所以f可导。"
    draft.claims[0].source_id, draft.claims[0].quote = "student:1", ""
    audit = notes()
    audit["steps"][0]["conclusion"] = "f连续"
    provider, calls = raw_provider(wire(draft, audit))
    with math_plans_context({p.q_id: plan}), g.math_review_scope():
        result = await ProofSkill(provider).grade(p, a)
    trace = g.read_math_trace(result)
    assert trace["proof_audit"]["unmapped_source_ids"] == ["student:2"]
    assert len(calls) == 1 and result.score == 10
    assert [c["kind"] for c in trace["assessment"]["claims"]] == ["statement"]


@pytest.mark.asyncio
@pytest.mark.parametrize("effect", ["none", "accounted"])
async def test_true_conclusion_invalid_derivation_or_teacher_waiver_does_not_force_review(effect):
    p, a, plan, draft = case()
    audit = notes(effect=effect, inference="gap", premise_status="missing", concern="按教师标准，此小错免扣")
    provider, calls = raw_provider(wire(draft, audit))
    with math_plans_context({p.q_id: plan}), g.math_review_scope():
        result = await ProofSkill(provider).grade(p, a)
    assert result.score == 10 and len(calls) == 1
    assert not g.read_math_trace(result)["proof_audit"]["review_concerns"]


@pytest.mark.asyncio
@pytest.mark.parametrize("inference", ["invalid", "justified"])
@pytest.mark.parametrize("review_appendix", ["resolved", "missing", "invalid"])
async def test_specific_scoring_concern_uses_one_review_and_bad_notes_keep_its_valid_score(
        inference, review_appendix):
    p, a, plan, draft = case()
    concern = notes(effect="unresolved", inference=inference, premise_status="missing",
                    concern="仅连续不足以推出可导，过程得分可能错误")
    revised = assessment(7, 2, text=a.content)
    revised.claims[0].verdict = "refuted"
    resolved = notes(effect="accounted", inference="invalid", premise_status="missing",
                     concern="已按教师标准扣除推理错误1分")
    suffix = {"resolved": resolved, "missing": None, "invalid": "{broken"}[review_appendix]
    provider, calls = raw_provider(wire(draft, concern), wire(revised, suffix))
    with math_plans_context({p.q_id: plan}), g.math_review_scope():
        result = await ProofSkill(provider).grade(p, a)
        assert not g.take_review_budget()
    trace = g.read_math_trace(result)
    assert len(calls) == 2 and result.score == 9 and not result.error_kind
    assert "proof_concerns" in calls[1][-1].content and "direct" not in trace
    assert bool(trace["proof_audit"]["review_concerns"]) == (review_appendix != "resolved")


@pytest.mark.asyncio
async def test_exhausted_shared_budget_retains_grade_and_only_concrete_nonblocking_advice():
    p, a, plan, draft = case()
    provider, calls = raw_provider(wire(draft, notes(effect="unresolved", inference="invalid", concern="具体推理疑点")))
    with math_plans_context({p.q_id: plan}), g.math_review_scope():
        assert g.take_review_budget()
        result = await ProofSkill(provider, language="zh").grade(p, a)
    assert len(calls) == 1 and result.score == 10 and not result.error_kind
    assert "证明依据核对建议：具体推理疑点" in result.steps[0].desc
    assert "必须" not in result.steps[0].desc


@pytest.mark.asyncio
async def test_multi_expert_proof_concerns_share_the_existing_review_once():
    p, a, plan, draft = case()
    audit = notes(effect="unresolved", inference="invalid", concern="推理不充分，影响过程分")
    revised = assessment(7, 2, text=a.content)
    resolved = notes(effect="accounted", inference="invalid", concern="过程已经扣1分")
    review = g.ExpertReview(revisions=[g.ExpertRevision(expert_index=i, reason="推理错误扣1分",
        source_quote=a.content, assessment=revised) for i in range(2)])
    provider, calls = raw_provider(wire(draft, audit), wire(draft, audit), wire(review, {"0": resolved, "1": resolved}))
    with math_plans_context({p.q_id: plan}), g.math_review_scope():
        g.defer_local_review(True)
        experts = [await ProofSkill(provider).grade(p, a) for _ in range(2)]
        assert len(calls) == 2
        results = await g.review_math_experts(p, a, experts, provider)
        await g.review_math_experts(p, a, results, provider)
    assert len(calls) == 3 and [r.score for r in results] == [9, 9]
    assert all(not g.read_math_trace(r)["proof_audit"]["review_concerns"] for r in results)


@pytest.mark.asyncio
async def test_original_accounting_validation_stays_strict_when_audit_is_valid():
    p, a, plan, draft = case()
    bad = draft.model_copy(deep=True)
    bad.awards[0].score = 999
    provider, calls = raw_provider(wire(bad, notes()), wire(draft, notes()))
    with math_plans_context({p.q_id: plan}), g.math_review_scope():
        result = await ProofSkill(provider).grade(p, a)
    assert len(calls) == 2 and result.score == 10
    assert "could not be parsed" in calls[1][-1].content


@pytest.mark.parametrize("change", ["unknown_claim", "unknown_source", "unknown_rubric", "empty_basis", "self_dependency"])
def test_invalid_audit_references_are_diagnostics_not_review_triggers(change):
    p, a, plan, draft = case()
    g.score_assessment(draft, plan, a.content, problem_text=p.stem)
    audit = notes(effect="unresolved", inference="invalid", concern="影响分数")
    step = audit["steps"][0]
    if change == "unknown_claim": step["claim_id"] = "missing"
    if change == "unknown_source": step["premises"][0]["source_ids"] = ["student:999"]
    if change == "unknown_rubric": step["rubric_source_ids"] = ["rubric:999"]
    if change == "empty_basis": step["premises"][0]["source_ids"] = []
    if change == "self_dependency": step["premises"][0]["claim_ids"] = ["s1"]
    result = inspect_audit(audit, draft, a.content, p.stem, plan)
    assert result["invalid_records"] == 1 and not result["review_concerns"]


def test_audit_marker_inside_primary_grade_is_not_an_appendix():
    p, a, plan, draft = case()
    draft.comment = 'PROOF_AUDIT\n{"steps":[]}'
    assert read_appendix(draft.model_dump_json()) is None


def test_duplicate_notes_cannot_clear_a_previous_concrete_concern():
    p, a, plan, draft = case()
    g.score_assessment(draft, plan, a.content, problem_text=p.stem)
    pending = inspect_audit(notes(effect="unresolved", inference="invalid", concern="具体评分疑点"),
                            draft, a.content, p.stem, plan)
    audit = notes(effect="accounted", inference="invalid", concern="已处理")
    audit["steps"].append(deepcopy(audit["steps"][0]))
    result = inspect_audit(audit, draft, a.content, p.stem, plan, previous=pending)
    assert result["invalid_records"] == 2
    assert result["review_concerns"] == pending["review_concerns"]


@pytest.mark.asyncio
async def test_tool_and_proof_concerns_share_one_feedback_request():
    p, a = problem("证明题"), answer("41")
    plan = plan_for(p)
    draft = assessment(8, 2, text="41")
    reviewed = assessment(8, 1, text="41")
    reviewed.evidence_judgments = [g.EvidenceJudgment(evidence_id="v1", applicability="applies",
                                                     reason="该等式确实错误")]
    initial_notes = notes(effect="unresolved", inference="invalid", concern="结论错误，需核对结果分")
    resolved_notes = notes(effect="accounted", inference="invalid", concern="已处理结果分")
    provider, calls = raw_provider(wire(draft, initial_notes), wire(reviewed, resolved_notes))
    with math_plans_context({p.q_id: plan}), g.math_review_scope():
        result = await ProofSkill(provider).grade(p, a)
        assert not g.take_review_budget()
    trace = g.read_math_trace(result)
    assert len(calls) == 2 and result.score == 9
    assert trace["evidence"][0]["status"] == "refuted"
    assert not trace["review_pending"] and not trace["proof_audit"]["review_concerns"]
