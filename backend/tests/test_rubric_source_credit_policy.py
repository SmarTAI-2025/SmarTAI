"""Source totals are metadata; explicit no-partial-credit rules constrain new ledgers."""
import json

import pytest

from backend.services import part_rubric
from backend.services.proof_rubric import use_proof_steps
from backend.services.rubric_drafts import RubricAllocationError, generated_rubric, saved_draft
from backend.tests.test_generated_step_granularity import proposal
from backend.tests.test_manual_proof_completion import complete
from backend.tests.test_proof_rubric import STEM, REFERENCE, audit, initial, rubric
from backend.tests.test_question_material_format_repair import Provider


@pytest.mark.parametrize("marker", [
    "本题共4.5分。", "本题共计4.5分。", "共4.5分。",
    "This question is worth 4.5 points.", "This problem carries 4.5 marks.",
    "The question is worth 4.5 points.",
])
def test_total_variants_do_not_bypass_granularity_or_proof_audit(marker):
    row, definition = proposal([[.5] * 9], stem=marker + " " + STEM)
    assert use_proof_steps({**row, "type": "推理题"})
    with pytest.raises(RubricAllocationError, match="at most 8 steps OR"):
        generated_rubric(row, definition)


@pytest.mark.parametrize("text", [
    "本题共10分，答对才给分。", "本题满分10分，必须完整证明，不给过程分。",
    "第二步：证明该元素不是单位元（10分）", "第二步：证明该元素不是单位元共10分。",
    "The witness is worth 10 points.", "This question is worth 10 points for the witness.",
    "This question is worth 10 points. No partial credit.",
    "This question is worth 5 points.",
])
def test_credit_conditions_and_ambiguous_amounts_remain_teacher_policy(text):
    assert part_rubric.has_explicit_scoring_policy(text, 10)
    assert not use_proof_steps(dict(type="推理题", stem=STEM + " " + text, max_score=10))


def test_proof_process_analysis_is_not_a_credit_policy():
    assert use_proof_steps(dict(type="推理题", stem="本题共10分。请证明并给出过程分析。", max_score=10))


@pytest.mark.parametrize("text", [
    "不是不给过程分。", "不要求不给过程分。", "如果答案错误，不给过程分。",
    "第二步不完整，不给过程分。", "No partial credit for step 2.",
    "If the answer is incorrect, no partial credit.", "Do not prohibit partial credit.",
])
def test_conditional_or_negated_rule_is_not_a_global_prohibition(text):
    row, definition = proposal([[5, 5]], stem=STEM + " " + text)
    _, draft = generated_rubric(row, definition)
    assert len(draft.plan.part_rubric.parts[0].steps) == 2


@pytest.mark.parametrize("marker", [
    "本题满分10分，必须完整证明，不给过程分。", "本题共10分，不给部分分。",
    "This question is worth 10 points. No partial credit.",
])
def test_explicit_no_partial_credit_rejects_independent_awards(marker):
    row, definition = proposal([[5, 5]], stem=STEM + " " + marker)
    with pytest.raises(RubricAllocationError, match="source explicitly forbids partial credit"):
        generated_rubric(row, definition)


def binary_definition(points=10):
    row, definition = proposal([[points]], stem=STEM + " No partial credit.")
    rules = definition.part_rubric
    part = rules.parts[0]
    part = part.model_copy(update={"alternative": part.alternative.model_copy(update={"deduct_per_error": points})})
    return row, definition.model_copy(update={"part_rubric": rules.model_copy(update={"parts": (part,)})})


@pytest.mark.parametrize("points", [10, 4.6])
@pytest.mark.parametrize("strictness", [0, 50, 100])
def test_no_partial_policy_is_frozen_without_new_schema_and_overrides_default(points, strictness):
    row, definition = binary_definition(points)
    text, draft = generated_rubric(row, definition, follow_through_strictness=strictness)
    step = draft.plan.part_rubric.parts[0].steps[0]
    assert step.total_pts == points and step.follow_through_pts == 0
    assert "No partial credit" in step.criterion and "No partial credit" in text
    completed = {**row, "criterion": text, "rubric_draft": draft.model_dump(mode="json"),
                 "ai_completion_provenance": {"criterion": {"source_kind": "ai_generated"}}}
    assert saved_draft(completed).plan.part_rubric == draft.plan.part_rubric
    assert generated_rubric(completed, definition)[1].plan.part_rubric == draft.plan.part_rubric


@pytest.mark.parametrize("field,value", [
    ("deduct_per_error", 1), ("maximum", {"points": 8}),
    ("minimum", {"points": 2}), ("max_deduction", 2), ("maximum", {"points": 0}),
])
def test_no_partial_policy_cannot_be_bypassed_by_alternative_awards(field, value):
    row, definition = binary_definition()
    raw = definition.model_dump(mode="json")
    raw["part_rubric"]["parts"][0]["alternative"][field] = value
    with pytest.raises(RubricAllocationError, match="alternative-method policy must not award partial credit"):
        generated_rubric(row, type(definition).model_validate(raw))


@pytest.mark.parametrize("method", ["reference", "alternative"])
@pytest.mark.parametrize("correct", [True, False])
def test_existing_settlement_can_only_award_zero_or_full_credit(method, correct):
    from backend.skills.part_scoring import PartDecision, settle_parts
    row, definition = binary_definition()
    _, draft = generated_rubric(row, definition)
    decision = dict(part_id="p1", target_quote="part:p1", student_quote="student:1",
        method=method, relevance="complete" if correct else "partial", progress="The proof is complete." if correct else
        "The square was computed but nonidentity was not established.",
        reference_method="group relations", student_method="geometric reflections")
    if method == "reference":
        decision["steps"] = [dict(id="s1", outcome="earned" if correct else "missed",
            evidence="student:1", reason=decision["progress"])]
    elif not correct:
        decision["errors"] = [dict(id="e1", rule_id="alternative", evidence="student:1",
            origin_quote="student:1", reason=decision["progress"])]
    score, _ = settle_parts(draft.plan.part_rubric, [PartDecision.model_validate(decision)], REFERENCE, row["stem"])
    assert score == (10 if correct else 0)


def test_nonblank_floor_cannot_bypass_no_partial_rule():
    row, definition = binary_definition()
    definition = definition.model_copy(update={"part_rubric": definition.part_rubric.model_copy(
        update={"nonblank_floor": part_rubric.Floor(pts=2)})})
    # The pre-existing source-bound floor validation rejects this before the
    # new credit-policy guard; preserve that earlier protection and feedback.
    with pytest.raises(RubricAllocationError, match="Preserve the explicit question floor"):
        generated_rubric(row, definition)


def test_existing_snapshot_and_alternative_only_edit_are_not_reinterpreted(monkeypatch):
    row, definition = proposal([[5, 5]], stem=STEM + " No partial credit.")
    with monkeypatch.context() as patch:
        patch.setattr(part_rubric, "bind_generated_credit_policy", lambda rules: rules)
        text, prior = generated_rubric(row, definition)
    completed = {**row, "criterion": text, "rubric_draft": prior.model_dump(mode="json"),
                 "ai_completion_provenance": {"criterion": {"source_kind": "ai_generated"}}}
    assert generated_rubric(completed, definition)[1].plan.part_rubric == prior.plan.part_rubric
    _, edit = generated_rubric(row, definition, check_step_granularity=False)
    assert edit.plan.part_rubric == prior.plan.part_rubric


def test_provided_teacher_rubric_keeps_existing_source_validation_path():
    row, definition = proposal([[5, 5]], stem=STEM + " No partial credit.",
        criterion="按教师指定的两个步骤逐项给分。")
    _, draft = generated_rubric(row, definition)
    assert len(draft.plan.part_rubric.parts[0].steps) == 2


def test_no_partial_policy_does_not_invent_cross_part_settlement():
    row, definition = proposal([[5], [5]], stem="No partial credit.")
    with pytest.raises(RubricAllocationError, match="whole-question no-partial-credit"):
        generated_rubric(row, definition)


def test_part_specific_no_partial_policy_does_not_change_sibling():
    row, definition = proposal([[5], [2, 3]])
    row["question_structure"]["subparts"][0]["stem"] += " No partial credit."
    part = definition.part_rubric.parts[0]
    part = part.model_copy(update={"alternative": part.alternative.model_copy(update={"deduct_per_error": 5})})
    definition = definition.model_copy(update={"part_rubric": definition.part_rubric.model_copy(
        update={"parts": (part, definition.part_rubric.parts[1])})})
    _, draft = generated_rubric(row, definition)
    first, second = draft.plan.part_rubric.parts
    assert first.steps[0].follow_through_pts == 0
    assert [s.total_pts for s in second.steps] == [2, 3]
    assert [s.follow_through_pts for s in second.steps] == [1, 1.5]


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["本题共10分。", "This question is worth 10 points."])
async def test_total_variants_reach_bundle_audit_and_targeted_repair(monkeypatch, marker):
    rules = rubric()
    part = rules.parts[0]
    bundled = part.steps[1].model_copy(update={"total_pts": 5,
        "criterion": "Prove the square equals 1 and prove the element is not the identity."})
    rules = rules.model_copy(update={"parts": (part.model_copy(update={
        "steps": (part.steps[0], bundled, part.steps[3])}),)})
    provider = Provider([initial(rules), audit(bundled_steps=["square"]), initial(reference=False), audit()])
    job, data = await complete(monkeypatch, provider, stem=marker + " " + STEM)
    assert not job.payload["skipped_target_ids"] and len(provider.calls) == 4
    assert data.reference_answer == REFERENCE
    assert [s.total_pts for s in saved_draft(data).plan.part_rubric.parts[0].steps] == [3, 3, 2, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize("repaired", [True, False])
async def test_no_partial_rule_repairs_only_rubric_or_leaves_it_unsaved(monkeypatch, repaired):
    _, definition = binary_definition()
    responses = [initial(), initial(definition.part_rubric, reference=False)] if repaired else [
        initial(), initial(reference=False), initial(reference=False)]
    provider = Provider(responses)
    job, data = await complete(monkeypatch, provider,
        stem=STEM + " 本题满分10分，必须完整证明，不给过程分。")
    assert data.reference_answer == REFERENCE and job.status == "done"
    retry = json.loads(provider.calls[1][-1].content.removeprefix("[Generation request]\n"))
    assert [x["target_id"] for x in retry["requested_targets"]] == ["q1:criterion"]
    assert "forbids partial credit" in retry["validation_feedback"]["q1:criterion"]
    if repaired:
        assert len(provider.calls) == 2 and not job.payload["skipped_target_ids"]
        assert saved_draft(data).plan.part_rubric.parts[0].steps[0].follow_through_pts == 0
    else:
        assert len(provider.calls) == 3 and not data.criterion and saved_draft(data) is None
        assert job.payload["skipped_target_ids"] == ["q1:criterion"]
