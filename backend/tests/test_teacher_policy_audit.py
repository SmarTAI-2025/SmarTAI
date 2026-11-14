"""Unknown teacher semantics need bounded evidence, never an assumed zero."""
import json
from copy import deepcopy

import pytest

from backend.domain.errors import ValidationError
from backend.llm.providers import LLMResponse
from backend.services.rubric_drafts import compile_parts_draft, saved_draft
from backend.tests.test_teacher_frozen_allocations import make_row, proposed


class PolicyProvider:
    provider_id = "synthetic:policy"
    model = "fixture"
    def __init__(self, candidate, *, verdict="consistent", wrong_quote=False):
        self.candidate, self.verdict, self.wrong_quote = candidate, verdict, wrong_quote
        self.calls = []

    async def ainvoke(self, messages, **kwargs):
        self.calls.append(messages)
        request = json.loads(messages[1].content)
        if "policies_to_verify" in request:
            result = {"checks": [{"policy_id": item["policy_id"],
                "source_quote": "invented quotation" if self.wrong_quote else item["source_quote"],
                "verdict": self.verdict, "reason": "The one fifth rule requires an unconditional two point floor."}
                for item in request["policies_to_verify"]]}
        else:
            result = {"part_rubric": self.candidate}
        return LLMResponse(content=json.dumps(result, ensure_ascii=False), model="fixture", provider="synthetic", duration_ms=1)


def unknown_floor():
    row = make_row("非空作答最低分按满分的五分之一计算")
    raw = proposed()
    raw["nonblank_floor"] = {"pts": 2}
    return row, raw


@pytest.mark.asyncio
async def test_unknown_policy_is_verified_once_and_survives_cache_and_run_snapshot():
    from backend.services.math_grading_context import MATH_GRADING_ENGINE_VERSION, plans_from_snapshots
    row, raw = unknown_floor()
    provider = PolicyProvider(raw)
    draft = await compile_parts_draft(row, provider)
    assert len(provider.calls) == 2
    assert draft.plan.part_rubric.nonblank_floor.resolve(10) == 2
    assert len(draft.semantic_policy_checks) == 1
    assert saved_draft({**row, "rubric_draft": draft.model_dump(mode="json")}) is not None
    manifest = {"math_grading_engine_version": MATH_GRADING_ENGINE_VERSION,
                "math_rubric_plans": {"q1": draft.plan.model_dump(mode="json")}}
    assert "q1" in plans_from_snapshots(manifest, [], for_grading=True)
    changed = deepcopy(manifest)
    changed["math_rubric_plans"]["q1"]["part_rubric"]["nonblank_floor"] = {"pts": 0}
    assert not plans_from_snapshots(changed, [], for_grading=True)
    assert len(provider.calls) == 2  # Loading a frozen version does no semantic work.


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["inconsistent", "uncertain"])
async def test_audit_cannot_approve_unfaithful_or_uncertain_rule(verdict):
    row, raw = unknown_floor()
    raw["nonblank_floor"] = {"pts": 0}
    provider = PolicyProvider(raw, verdict=verdict)
    with pytest.raises(ValidationError, match="could not be verified"):
        await compile_parts_draft(row, provider)
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_invalid_audit_quote_uses_only_remaining_shared_attempt():
    row, raw = unknown_floor()
    provider = PolicyProvider(raw, wrong_quote=True)
    with pytest.raises(ValidationError, match="could not be validated"):
        await compile_parts_draft(row, provider)
    assert len(provider.calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_partial", [False, True])
async def test_initial_generation_uses_same_audit_before_sealing_candidate(allow_partial):
    from backend.agents.ingest_agent import generate_missing_question_materials
    row, raw = unknown_floor()

    class InitialProvider(PolicyProvider):
        async def ainvoke(self, messages, **kwargs):
            if not self.calls:
                self.calls.append(messages)
                value = {"candidates": [{"target_id": "q1:criterion", "q_id": "q1", "target": "criterion",
                    "text_value": row["criterion"], "rubric_definition": {"part_rubric": self.candidate}}]}
                return LLMResponse(content=json.dumps(value), model="fixture", provider="synthetic", duration_ms=1)
            return await super().ainvoke(messages, **kwargs)

    provider = InitialProvider(raw)
    candidates = await generate_missing_question_materials({"q1": row},
        [{"target_id": "q1:criterion", "q_id": "q1", "target": "criterion"}], 3, provider,
        allow_partial=allow_partial)
    assert len(provider.calls) == 2 and len(candidates) == 1
    assert candidates[0].rubric_draft["semantic_policy_checks"]
    assert saved_draft({**row, "rubric_draft": candidates[0].rubric_draft}) is not None


@pytest.mark.asyncio
async def test_captured_gemini_policy_response_replays_through_the_compiler():
    from pathlib import Path
    from backend.tests.test_question_material_format_repair import Provider
    fixture = json.loads((Path(__file__).parent / "fixtures" / "part_rubrics" /
                          "gemini-synthetic-policy-20261112.json").read_text())
    provider = Provider(fixture["responses"])
    draft = await compile_parts_draft(fixture["question"], provider)
    assert len(provider.calls) == 2
    assert draft.plan.part_rubric.nonblank_floor.resolve(10) == 2
    assert [s.total_pts for s in draft.plan.part_rubric.parts[0].steps] == [3, 7]
    assert saved_draft({**fixture["question"], "rubric_draft": draft.model_dump(mode="json")}) is not None
