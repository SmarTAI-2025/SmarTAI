"""Whole-question totals do not exempt new AI proof steps from existing checks."""
import pytest

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


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["本题共10分。", "This question is worth 10 points."])
async def test_total_variants_audit_without_retrying_an_optional_split(monkeypatch, marker):
    rules = rubric()
    part = rules.parts[0]
    bundled = part.steps[1].model_copy(update={"total_pts": 5,
        "criterion": "Prove the square equals 1 and prove the element is not the identity."})
    rules = rules.model_copy(update={"parts": (part.model_copy(update={
        "steps": (part.steps[0], bundled, part.steps[3])}),)})
    provider = Provider([initial(rules), audit(bundled_steps=["square"])])
    job, data = await complete(monkeypatch, provider, stem=marker + " " + STEM)
    assert not job.payload["skipped_target_ids"] and len(provider.calls) == 2
    assert data.reference_answer == REFERENCE
    assert [s.total_pts for s in saved_draft(data).plan.part_rubric.parts[0].steps] == [3, 5, 2]
