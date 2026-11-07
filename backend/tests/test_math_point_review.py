"""Offline wire-level regressions for sourced, independently validated vote changes."""
import json
from unittest.mock import AsyncMock

import pytest

from backend.services.math_grading_context import math_plans_context
from backend.services.math_rubric_plan import POINT_EVIDENCE_GUIDANCE
from backend.skills import math_grading as grading
from backend.tests.test_math_evidence_reliability import raw_provider
from backend.tests.test_math_grading import answer, assessment, expert_for, plan_for, problem


def point_edit(index=0, score=6, **overrides):
    return {
        "expert_index": index,
        "reason": "The first line supplies a valid method.",
        "comment": "The method earns credit; the numerical result is incorrect.",
        "awards": [{"item_id": "method", "score": score, "reason": "Valid setup.",
                    "citations": [{"source_id": "student:1", "verdict": "supported"}]}],
        **overrides,
    }


async def review(*payloads):
    p, a = problem(), answer("6*7=6*(5+2)\n6*7=41")
    plan = plan_for(p)
    experts = [expert_for(assessment(method=score), plan, a.content) for score in (1, 8)]
    provider, calls = raw_provider(*(json.dumps(payload) for payload in payloads))
    with math_plans_context({p.q_id: plan}), grading.math_review_scope():
        result = await grading.review_math_experts(p, a, experts, provider)
    return experts, result, calls


@pytest.mark.asyncio
async def test_review_changes_one_item_and_preserves_independent_credit_difference():
    original, result, calls = await review({"revisions": [point_edit()]})
    assert [e.score for e in result] == [6, 8] and len(calls) == 1
    assert [e.score for e in original] == [1, 8]
    assert "PointRevision" in calls[0][0].content
    schema = json.loads(calls[0][0].content.split("JSON Schema:\n")[1].split("\n\n")[0])
    assert schema["properties"]["revisions"]["items"]["title"] == "PointRevision" and "MathAssessment" not in schema["$defs"]
    assert "claim_ids" not in schema["$defs"]["PointAward"]["properties"]
    payload = json.loads(calls[0][-1].content)
    assert payload["disputed_item_ids"] == ["method"]
    assert payload["source_catalog"]["student:1"] == "6*7=6*(5+2)"
    assert "initial_assessment" not in payload["experts"][0]
    assert [w["item_id"] for w in payload["experts"][0]["awards"]] == ["method"]
    assert "claim_ids" not in payload["experts"][0]["awards"][0]
    trace = grading.read_math_trace(result[0])
    before = grading.read_math_trace(original[0])["assessment"]
    assert trace["original_assessment"] == before
    assert trace["assessment"]["awards"][1] == before["awards"][1]
    assert trace["assessment"]["claims"][0] == before["claims"][0]
    assert trace["assessment"]["claims"][1]["quote"] == "6*7=6*(5+2)"
    assert trace["targeted_review"]["item_ids"] == ["method"]
    assert sum(s.score for s in result[0].steps) == result[0].score
    assert result[1] == original[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad,code", [
    (point_edit(1, awards=[{"item_id": "method", "score": 6, "reason": "Invalid citation.",
                            "citations": [{"source_id": "student:404", "verdict": "supported"}]}]),
     "invalid_claim_source_id"),
    (point_edit(1, awards=[{"item_id": "result", "score": 2, "citations": [], "reason": "Changed."}]),
     "review_changed_undisputed_award"),
    (point_edit(1, score=12), "invalid_award"),
    ({"expert_index": 1, "awards": "invalid"}, "revision_schema_invalid"),
])
async def test_bad_sibling_preserves_valid_edit_without_reissuing_pair(bad, code):
    original, result, calls = await review({"revisions": [bad, point_edit()]})
    assert len(calls) == 1 and [e.score for e in result] == [6, 8]
    trace = grading.read_math_trace(result[1])
    assert trace["assessment"] == grading.read_math_trace(original[1])["assessment"]
    assert trace["review_diagnostic"]["status"] == "invalid_revision"
    assert trace["review_diagnostic"]["validation_issues"] == [[{"expert_index": 1, "code": code}]]
    assert grading.read_math_trace(result[0])["review_diagnostic"]["status"] == "revised"


@pytest.mark.asyncio
async def test_missing_disposition_gets_repair_while_explicit_abstention_keeps_both_votes():
    original, result, calls = await review({"score": 6}, {"revisions": []})
    assert len(calls) == 2 and [e.score for e in result] == [1, 8]
    for before, after in zip(original, result):
        assert grading.read_math_trace(after)["assessment"] == grading.read_math_trace(before)["assessment"]
        assert grading.read_math_trace(after)["review_diagnostic"]["status"] == "retained"
    assert "PointRevision" in calls[1][-1].content and "MathAssessment" not in calls[1][-1].content
    _, repaired, calls = await review({}, {"revisions": [point_edit()]})
    assert len(calls) == 2 and [e.score for e in repaired] == [6, 8]


@pytest.mark.asyncio
async def test_duplicate_target_never_picks_a_winner_and_repairs_as_one_vote():
    duplicate = {"revisions": [point_edit(), point_edit(score=5)]}
    _, result, calls = await review(duplicate, duplicate)
    assert len(calls) == 2 and [e.score for e in result] == [1, 8]
    trace = grading.read_math_trace(result[0])
    assert trace["review_failure"] == "StructuredOutputInvalidError"
    assert trace["review_diagnostic"]["stage"] == "revision_validation"
    assert all(i["code"] == "duplicate_expert_revision" for attempt in
               trace["review_diagnostic"]["validation_issues"] for i in attempt)


@pytest.mark.asyncio
async def test_unchanged_context_echo_is_discarded_and_catalog_citation_repairs_actual_item():
    edit = point_edit(awards=[
        {"item_id": "method", "score": 6, "reason": "The decomposition is written on the first line.",
         "citations": [{"source_id": "student:1", "verdict": "supported"}]},
        {"item_id": "result", "score": 0, "reason": "Echo with a different explanation.",
         "citations": [{"source_id": "student:999", "verdict": "unknown"}]},
    ])
    original, result, calls = await review({"revisions": [edit]})
    assert len(calls) == 1 and [e.score for e in result] == [6, 8]
    trace = grading.read_math_trace(result[0])
    old = grading.read_math_trace(original[0])["assessment"]
    assert trace["assessment"]["awards"][1] == old["awards"][1]
    assert trace["targeted_review"]["item_ids"] == ["method"]
    claim = trace["assessment"]["claims"][-1]
    assert claim["quote"] == "6*7=6*(5+2)" and claim["item_ids"] == ["method"]
    assert trace["assessment"]["awards"][0]["claim_ids"] == [claim["id"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("citation", [
    {"source_id": "student:999", "verdict": "supported"},
    {"source_id": "rubric:1", "verdict": "refuted"},
])
async def test_source_locators_never_create_unsupported_credit(citation):
    edit = point_edit(awards=[{"item_id": "method", "score": 8, "reason": "Method.",
                                              "citations": [citation]}])
    _, result, calls = await review({"revisions": [edit]}, {"revisions": []})
    assert len(calls) == 2 and [e.score for e in result] == [1, 8]


def test_method_equivalence_guidance_tracks_the_origin_of_the_proof_rubric():
    p = problem(kind="推理题")
    assert grading._generated_proof_guidance(p) == ""
    p._math_rubric_source = {"presentation": {"ai_completion_provenance": {
        "criterion": {"source_kind": "ai_generated"}}}}
    assert "Unless the question or teacher specifies a method" in grading._generated_proof_guidance(p)
    p.type = "计算题"
    assert grading._generated_proof_guidance(p) == ""


@pytest.mark.asyncio
async def test_invalid_response_format_distinct_from_bad_evidence():
    _, result, calls = await review({}, {"oops": True})
    assert len(calls) == 2
    for expert in result:
        diagnostic = grading.read_math_trace(expert)["review_diagnostic"]
        assert diagnostic["stage"] == "response_format_or_attempt_limit"
        assert diagnostic["validation_issues"] == []


@pytest.mark.asyncio
async def test_evidence_conflict_keeps_complete_review_and_valid_sibling():
    p, a, plan = problem(), answer(), plan_for()
    first = expert_for(assessment(method=1, needs_review=True), plan, a.content)
    second = expert_for(assessment(method=8), plan, a.content)
    full = {"expert_index": 0, "source_quote": a.content, "reason": "Credit the method.",
            "assessment": assessment(method=6).model_dump()}
    provider, calls = raw_provider(json.dumps({"revisions": [full, point_edit(1)]}))
    with math_plans_context({p.q_id: plan}), grading.math_review_scope():
        result = await grading.review_math_experts(p, a, [first, second], provider)
    assert len(calls) == 1 and [e.score for e in result] == [6, 8]
    schema = json.loads(calls[0][0].content.split("JSON Schema:\n")[1].split("\n\n")[0])
    assert schema["properties"]["revisions"]["items"]["title"] == "ExpertRevision" and "PointAward" not in schema["$defs"]
    assert grading.read_math_trace(result[1])["review_diagnostic"]["validation_issues"] == [
        [{"expert_index": 1, "code": "review_requires_complete_assessment"}]]


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["alternative_route", "no_partial_credit", "conditional_credit"])
async def test_special_teacher_policies_keep_complete_ledger_review(policy):
    from backend.services.math_rubric_plan import MathRubricPlan, _content_hash

    p, a = problem(), answer()
    data = plan_for(p).model_dump(mode="json", exclude={"plan_id"})
    if policy == "alternative_route":
        data["routes"].append({**data["routes"][0], "id": "alternative"})
    elif policy == "no_partial_credit":
        data["allow_partial_credit"] = False
    else:
        data["routes"][0]["items"][0]["default_adjustments"] = ["follow_through"]
    plan = MathRubricPlan.model_validate({**data, "plan_id": _content_hash(data)})
    original = [expert_for(assessment(method=m), plan, a.content) for m in (0, 8)]
    provider, calls = raw_provider(json.dumps({"revisions": [point_edit()]}), '{"revisions": []}')
    with math_plans_context({p.q_id: plan}), grading.math_review_scope():
        result = await grading.review_math_experts(p, a, original, provider)
    assert len(calls) == 2 and [e.score for e in result] == [0, 8]
    assert "review_requires_complete_assessment" in calls[1][-1].content


@pytest.mark.asyncio
async def test_normal_grading_fanout_shares_standard_and_merges_only_corrected_points(monkeypatch):
    from backend.agents.multi_expert import run_multi_expert
    from backend.tests.test_multi_expert_robustness import _FakeRegistry

    conclusion = "Their product therefore leaves remainder zero."
    p = problem(kind="推理题").model_copy(update={
        "stem": "Prove that the product of two odd integers is odd.",
        "reference_answer": "Both residues are one modulo two; their product also has residue one.",
    })
    a = answer("Each factor leaves remainder one when divided by two.\n" + conclusion)
    plan = plan_for(p)
    edit = point_edit()
    edit["awards"][0]["reason"] = "Correctly identifies the two residues."
    first, first_calls = raw_provider(assessment(method=1, text=conclusion), json.dumps({"revisions": [edit]}))
    second, second_calls = raw_provider(assessment(method=8, text=conclusion))
    first.provider_id, second.provider_id = "offline:first", "offline:second"
    monkeypatch.setattr(grading.kb_tool, "retrieve_for_grading", AsyncMock(return_value=[]))
    with math_plans_context({p.q_id: plan}):
        correction = await run_multi_expert(problem=p, answer=a, student_id="fixture",
            registry=_FakeRegistry([first, second]), aggregation_method="weighted_average")
    assert [len(first_calls), len(second_calls)] == [2, 1]
    assert first_calls[0][1].content == second_calls[0][1].content
    assert "missing premise" in first_calls[0][0].content
    assert [e.score for e in correction.expert_results] == [6, 8]
    assert correction.score == sum(s.score for s in correction.steps) == 7
    assert correction.synthesis_method == "weighted_average"
    assert grading.read_math_trace(correction.expert_results[0])["original_assessment"]["awards"][0]["score"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,expected", [("推理题", True), ("编程题", False)])
async def test_shared_evidence_guidance_scoped_to_requested_written_rubric(kind, expected):
    from backend.agents.ingest_agent import generate_missing_question_materials
    from backend.tests.test_math_rubric_plan import ReplayProvider
    from backend.tests.test_unified_rubric_drafts import choice_definition, choice_question

    question = {**choice_question(), "type": kind}
    provider = ReplayProvider({"candidates": [{"q_id": "q1", "target_id": "q1:criterion",
        "target": "criterion", "rubric_definition": choice_definition()}]})
    await generate_missing_question_materials({"q1": question}, [
        {"q_id": "q1", "target_id": "q1:criterion", "target": "criterion"}
    ], 1, provider)
    assert (POINT_EVIDENCE_GUIDANCE in provider.messages[0][0].content) is expected


@pytest.mark.parametrize("language,phrase", [("zh", "其他完整有效的证明"), ("en", "another valid complete proof")])
def test_new_generated_proof_freezes_method_equivalence_in_the_actual_rubric(language, phrase):
    from backend.services.rubric_drafts import RubricDefinition, generated_rubric, retarget_draft, saved_draft

    problem = {"type": "推理题", "stem": "Prove that an identity is unique.", "max_score": 4}
    definition = RubricDefinition(routes=[{"id": "proof", "description": "Uniqueness",
        "applies_when": "always", "mode": "points", "items": [
            {"id": "reasoning", "description": "Apply the identity property.", "points": 4, "source": "ai"}]}],
        follow_through="Credit demonstrated reasoning.", numerical_policy="Exact proof.")
    text, draft = generated_rubric(problem, definition, language=language)
    assert phrase in text and draft.plan.original_criterion == text
    completed = {**problem, "criterion": text, "rubric_draft": draft.model_dump(mode="json"),
                 "ai_completion_provenance": {"criterion": {"source_kind": "ai_generated"}}}
    assert saved_draft(completed) == draft
    assert generated_rubric(completed, definition, language=language) == (text, draft)
    teacher = {**problem, "criterion": "Use contradiction."}
    assert generated_rubric(teacher, definition, language=language)[0] == teacher["criterion"]
    legacy_text, legacy = generated_rubric({**problem, "type": "其他"}, definition, language=language)
    prior = {**completed, "criterion": legacy_text}
    prior["rubric_draft"] = retarget_draft(legacy, prior).model_dump(mode="json")
    assert generated_rubric(prior, definition, language=language)[0] == legacy_text
    for other in ({**problem, "type": "计算题"}, {**problem, "stem": "Select an option."}):
        assert phrase not in generated_rubric(other, definition, language=language)[0]
