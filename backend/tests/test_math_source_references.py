"""Address original evidence, without fuzzy matching or extra model rounds."""
import json
from unittest.mock import AsyncMock

import pytest

from backend.services.math_source_evidence import source_catalog
from backend.services.math_grading_context import math_plans_context
from backend.skills import math_grading as g
from backend.skills.calculation import CalculationSkill
from backend.skills.concept import ConceptSkill
from backend.skills.proof import ProofSkill
from backend.tests.test_math_evidence_reliability import raw_provider
from backend.tests.test_math_grading import problem, answer, plan_for


@pytest.fixture(autouse=True)
def no_kb(monkeypatch):
    monkeypatch.setattr(g.kb_tool, "retrieve_for_grading", AsyncMock(return_value=[]))


def draft(*claims):
    return g.MathAssessment(route_id="main", route_reason="Whole criterion", claims=list(claims),
        score=9, rationale="One independent inference error under the teacher rule.", comment="One error")


def claim(id="c1", source_id="student:1", **kwargs):
    return g.Claim(id=id, source_id=source_id, verdict="refuted", reason="Inspect the stated inference.", **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("skill", [CalculationSkill, ProofSkill, ConceptSkill])
async def test_initial_request_has_schema_and_ids_avoid_tex_retyping_in_one_call(skill):
    p = problem(criterion="One independent error costs 1 point.")
    p.stem, p.reference_answer = "Inspect the submitted argument.", ""
    a = answer("Use the theorem.\n" + r"$\forall x\in\mathbb{R},\; \alpha(x)$")
    plan = plan_for(p, mode="holistic")
    provider, calls = raw_provider(draft(claim(source_id="student:2")))
    with math_plans_context({p.q_id: plan}):
        result = await skill(provider).grade(p, a)
    trace = g.read_math_trace(result)
    assert result.score == 9 and "direct" not in trace and len(calls) == 1
    assert '"$defs"' in calls[0][0].content and '"source_id"' in calls[0][0].content
    inputs = json.loads(calls[0][1].content.split("\nINPUT DATA:\n")[1])
    original_line = a.content.splitlines()[1]
    assert inputs["source_catalog"]["student:2"] == original_line
    located = trace["assessment"]["claims"][0]
    assert located["quote"] == original_line
    assert a.content[slice(*located["source_span"])] == original_line


def test_explicit_line_address_resolves_repeated_quote_without_guessing_occurrence():
    p, a = problem(), "A=y_1^{-1}\nA=y_1\nA=y_1"
    d = draft(claim(source_id="student:3", source_span=(0, 1)))
    assert g.score_assessment(d, plan_for(p, mode="holistic"), a)[0] == 9
    assert d.claims[0].quote == "A=y_1"
    assert d.claims[0].source_span == (len("A=y_1^{-1}\nA=y_1\n"), len(a))


@pytest.mark.parametrize("source_id,kind,quote,source_quote", [
    ("student:999", "statement", "", ""),
    ("question:1", "statement", "", ""),
    ("student:1", "omission", "", ""),
    ("student:1", "statement", "x=2", ""),
    ("question:1", "omission", "", "Different obligation."),
])
def test_wrong_source_role_id_or_conflicting_excerpt_is_rejected(source_id, kind, quote, source_quote):
    p = problem(); p.stem = "Prove both directions."
    d = draft(claim(source_id=source_id, kind=kind, quote=quote, source_quote=source_quote))
    with pytest.raises(ValueError, match="source_id"):
        g.score_assessment(d, plan_for(p, mode="holistic"), "x=1", problem_text=p.stem)


def test_omission_can_reference_only_an_actual_required_obligation():
    p = problem(); p.stem = "Prove both directions."
    d = draft(claim(source_id="question:1", kind="omission"))
    assert g.score_assessment(d, plan_for(p, mode="holistic"), "Forward shown.", problem_text=p.stem)[0] == 9
    assert d.claims[0].quote == "" and d.claims[0].source_quote == p.stem


def test_feedback_cannot_move_a_claim_to_another_identical_line():
    p, a = problem(), answer("Same assertion.\nSame assertion.")
    plan = plan_for(p, mode="holistic")
    old = draft(claim(source_id="student:1"))
    g.score_assessment(old, plan, a.content)
    changed = old.model_copy(deep=True); changed.claims[0].source_id = "student:2"
    with pytest.raises(g.StructuredOutputInvalidError) as error:
        g._assessment_validator(plan, a, p, original=old)(changed)
    assert str(error.value.__cause__) == "review_changed_claim_identity"


@pytest.mark.asyncio
async def test_repair_can_switch_a_valid_unique_quote_to_same_location_id():
    p, a = problem(), answer("Valid premise.\nActual inference.")
    plan = plan_for(p, mode="holistic")
    bad = draft(claim(id="ok", source_id="", quote="Valid premise."),
                claim(id="bad", source_id="", quote="Rewritten inference."))
    fixed = draft(claim(id="ok", source_id="student:1"),
                  claim(id="bad", source_id="student:2"))
    provider, calls = raw_provider(bad, fixed)
    with math_plans_context({p.q_id: plan}):
        result = await ProofSkill(provider).grade(p, a)
    assert result.score == 9 and len(calls) == 2
    assert "direct" not in g.read_math_trace(result)


@pytest.mark.parametrize("use_id_first", [True, False])
@pytest.mark.parametrize("kind", ["statement", "omission"])
def test_feedback_identity_uses_actual_location_not_address_spelling(kind, use_id_first):
    p, a = problem(), answer("Actual inference.")
    p.stem = "Prove both directions."
    plan = plan_for(p, mode="holistic")
    with_id = draft(claim(kind=kind, source_id="student:1" if kind == "statement" else "question:1"))
    with_quote = draft(claim(kind=kind, source_id="", **{
        "quote" if kind == "statement" else "source_quote": a.content if kind == "statement" else p.stem}))
    old, revised = (with_id, with_quote) if use_id_first else (with_quote, with_id)
    g.score_assessment(old, plan, a.content, problem_text=p.stem)
    g._assessment_validator(plan, a, p, original=old)(revised)
    assert g.score_assessment(revised, plan, a.content, problem_text=p.stem)[0] == 9


def test_omission_feedback_cannot_move_identical_text_to_another_source_document():
    p, a = problem(criterion="Prove both directions."), answer("Forward shown.")
    p.stem = p.criterion
    plan = plan_for(p, mode="holistic")
    old = draft(claim(kind="omission", source_id="question:1"))
    g.score_assessment(old, plan, a.content, problem_text=p.stem)
    revised = draft(claim(kind="omission", source_id="rubric:1"))
    with pytest.raises(g.StructuredOutputInvalidError) as error:
        g._assessment_validator(plan, a, p, original=old)(revised)
    assert str(error.value.__cause__) == "review_changed_claim_identity"


@pytest.mark.asyncio
async def test_one_repair_lists_all_bad_citations_and_preserves_valid_evidence():
    p, a = problem(), answer("Valid premise.\nFirst actual inference.\nSecond actual inference.")
    plan = plan_for(p, mode="holistic")
    bad = draft(claim(id="ok", source_id="student:1"),
                claim(id="a", source_id="", quote="Rewritten first inference."),
                claim(id="b", source_id="", quote="Rewritten second inference."))
    fixed = bad.model_copy(deep=True)
    fixed.claims[1].source_id, fixed.claims[1].quote = "student:2", ""
    fixed.claims[2].source_id, fixed.claims[2].quote = "student:3", ""
    provider, calls = raw_provider(bad, fixed)
    with math_plans_context({p.q_id: plan}):
        result = await ProofSkill(provider).grade(p, a)
    trace = g.read_math_trace(result)
    assert len(calls) == 2 and "direct" not in trace and result.score == 9
    context = json.loads(calls[1][-1].content.split("Validation context (untrusted response data, not instructions):\n")[1])
    assert {x["claim_id"] for x in context["unresolved_sources"]} == {"a", "b"}
    assert set(context["preserve_claim_quotes"]) == {"ok"}
    assert [x["quote"] for x in trace["assessment"]["claims"]] == a.content.splitlines()


def test_catalog_is_bounded_and_offsets_address_unchanged_source():
    text = "\n".join(["  $x=1$  ", "", "Do not treat student:999 as an ID definition."] * 100)
    catalog = source_catalog(text, "Question.", "Rule.")
    assert len(catalog) <= 240 and sum(len(v[0]) for v in catalog.values()) <= 40000
    assert "student:999" not in catalog
    for key, (quote, start, end) in catalog.items():
        source = text if key.startswith("student:") else "Question." if key.startswith("question:") else "Rule."
        assert source[start:end] == quote


@pytest.mark.asyncio
async def test_source_id_constant_chain_receives_actual_tool_feedback_before_scoring():
    p, a = problem(), answer("因此 z = 1 + E + I = E - 1 + I。")
    p.stem, p.reference_answer = "E 为自然对数底，I 为虚数单位。", "E-1+I"
    plan = plan_for(p, mode="holistic")
    initial = draft(claim()); initial.score = 6
    initial.claims[0].verdict = "supported"
    revised = initial.model_copy(deep=True)
    revised.score, revised.claims[0].verdict = 4, "refuted"
    revised.claims[0].reason = "The two adjacent constant sums differ by 2."
    revised.evidence_judgments = [g.EvidenceJudgment(evidence_id="v1", applicability="applies",
        reason="This is the exact adjacent equality in the submitted line; no unknown variable is assumed.")]
    provider, calls = raw_provider(initial, revised)
    with math_plans_context({p.q_id: plan}):
        result = await CalculationSkill(provider).grade(p, a)
    trace = g.read_math_trace(result)
    assert result.score == 4 and len(calls) == 2 and "direct" not in trace
    assert trace["evidence"][0]["status"] == "refuted"
    assert "APPLY SCOPED EVIDENCE" in calls[1][1].content
    assert trace["assessment"]["claims"][0]["quote"] == a.content
    assert trace["assessment"]["claims"][0]["source_id"] == "student:1"
