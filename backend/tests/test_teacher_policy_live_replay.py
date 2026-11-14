"""Captured synthetic model replies must obey the same source/score contracts."""
import json
from pathlib import Path

import pytest

from backend.domain.errors import ValidationError
from backend.services.rubric_drafts import compile_parts_draft, saved_draft
from backend.tests.test_question_material_format_repair import Provider

FIXTURE = json.loads((Path(__file__).parent / "fixtures/part_rubrics/"
                     "gemini-35-policy-diagnostic-20261112.json").read_text())
VERIFIED = json.loads((Path(__file__).parent / "fixtures/part_rubrics/"
                      "gemini-35-policy-verified-20261112.json").read_text())


@pytest.mark.asyncio
@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda c: c["case"])
async def test_captured_generation_does_not_relax_teacher_source_checks(case):
    provider = Provider([case["response"], case["response"]])
    if case["status"] == "passed":
        draft = await compile_parts_draft(case["question"], provider)
        assert [s.total_pts for s in draft.plan.part_rubric.parts[0].steps] == [3, 7]
        assert draft.plan.part_rubric.nonblank_floor.resolve(10) == 2
        assert saved_draft({**case["question"], "rubric_draft": draft.model_dump(mode="json")}) is not None
        assert len(provider.calls) == 1
    else:
        # Changed generation guidance must not make an old ambiguous source,
        # invented condition or unauthorized second ledger executable.
        with pytest.raises(ValidationError):
            await compile_parts_draft(case["question"], provider)
        assert len(provider.calls) == 2
        assert saved_draft(case["question"]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("case", VERIFIED["cases"], ids=lambda c: c["case"])
async def test_captured_verified_generation_preserves_teacher_policy_and_reloads(case):
    provider = Provider(case["responses"])
    draft = await compile_parts_draft(case["question"], provider)
    reloaded = saved_draft({**case["question"], "rubric_draft": draft.model_dump(mode="json")})
    assert reloaded is not None and reloaded.plan.plan_id == draft.plan.plan_id
    rules = reloaded.plan.part_rubric
    part = rules.parts[0]
    assert rules.total_pts == part.total_pts == 10
    if case["case"] == "error_count":
        assert not part.steps and len(part.deductions) == 1
        penalty = part.deductions[0]
        assert penalty.deduct_pts == 1 and penalty.limit is None and penalty.max_deduction is None
    else:
        expected = {"nested_subtotal": [1, 2, 7], "follow_through_target": [2, 8]}.get(case["case"], [3, 7])
        assert [s.total_pts for s in part.steps] == expected and not part.deductions
        assert rules.nonblank_floor.resolve(10) == (2 if "floor" in case["case"] else 0)
        if case["case"] == "follow_through_target":
            assert [s.follow_through_award() for s in part.steps] == [0, 4]
    assert len(provider.calls) == (2 if case["case"] == "semantic_floor" else 1)


@pytest.mark.asyncio
async def test_captured_reference_can_be_adopted_before_rule_compilation():
    from backend.agents.ingest_agent import generate_missing_question_materials
    case = VERIFIED["initial_case"]
    row = case["question"]
    provider = Provider(case["responses"])
    candidates = await generate_missing_question_materials({row["q_id"]: row},
        [{"target_id": row["q_id"] + ":reference_answer", "q_id": row["q_id"], "target": "reference_answer"}],
        1, provider)
    assert len(candidates) == 1 and candidates[0].rubric_draft is None
    adopted = {**row, "reference_answer": candidates[0].text_value,
               "ai_completion_provenance": {"reference_answer": {"source_kind": "ai_generated"}}}
    draft = await compile_parts_draft(adopted, provider)
    assert len(provider.calls) == 2
    assert [s.total_pts for s in draft.plan.part_rubric.parts[0].steps] == [1, 2, 7]
    assert saved_draft({**adopted, "rubric_draft": draft.model_dump(mode="json")}) is not None
