"""Policy verification stays inside the real preparation and cache boundaries."""
import json

import pytest

from backend.agents.ingest_agent import generate_missing_question_materials
from backend.agents.question_preparation_agent import _validate_major_question_candidates
from backend.llm.providers import LLMResponse
from backend.services.math_grading_context import MATH_GRADING_ENGINE_VERSION, plans_from_snapshots
from backend.services.question_structure import build_major_question_structure
from backend.services.math_rubric_plan import _content_hash
from backend.services.rubric_drafts import compile_parts_draft, rebind_review_edit, saved_draft
from backend.tests.test_teacher_policy_audit import PolicyProvider, unknown_floor
from backend.tools.structured_llm import StructuredOutputInvalidError


class InitialPolicyProvider(PolicyProvider):
    def __init__(self, candidate, source, **kwargs):
        super().__init__(candidate, **kwargs)
        self.source = source

    async def ainvoke(self, messages, **kwargs):
        if '"policies_to_verify"' in messages[1].content:
            return await super().ainvoke(messages, **kwargs)
        self.calls.append(messages)
        value = {"candidates": [{"target_id": "q1:criterion", "q_id": "q1", "target": "criterion",
            "text_value": self.source["criterion"], "rubric_definition": {"part_rubric": self.candidate}}]}
        return LLMResponse(content=json.dumps(value), model="fixture", provider="synthetic", duration_ms=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_partial", [False, True])
async def test_initial_subparts_are_audited_before_real_question_validation(allow_partial):
    row, raw = unknown_floor()
    row["stem"] = "(1)求甲。\n(2)求乙。"
    row["question_structure"] = build_major_question_structure(row, major_order=0).model_dump()
    raw["parts"] = [{"id": f"p{i}", "label": f"({i})", "total_pts": 5,
        "steps": [{"id": f"s{i}", "criterion": goal, "total_pts": 5}],
        "alternative": {"deduct_per_error": 1}} for i, goal in enumerate(("求甲", "求乙"), 1)]
    targets = [{"target_id": "q1:criterion", "q_id": "q1", "target": "criterion"}]
    observed = []

    def real_validation(candidates):
        assert all(c.rubric_draft and c.rubric_draft["semantic_policy_checks"] for c in candidates)
        observed.extend(candidates)
        _validate_major_question_candidates("q1", targets, candidates, row)

    provider = InitialPolicyProvider(raw, row)
    candidates = await generate_missing_question_materials({"q1": row}, targets, 3, provider,
        allow_partial=allow_partial, validate_candidates=real_validation)
    assert len(provider.calls) == 2 and len(candidates) == 1 and observed
    assert saved_draft({**row, "rubric_draft": candidates[0].rubric_draft}) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_partial", [False, True])
async def test_failed_initial_audit_cannot_publish_candidate_or_reset_call_budget(allow_partial):
    row, raw = unknown_floor()
    provider = InitialPolicyProvider(raw, row, wrong_quote=True)
    published = []
    kwargs = dict(allow_partial=allow_partial, validate_candidates=published.extend)
    targets = [{"target_id": "q1:criterion", "q_id": "q1", "target": "criterion"}]
    if allow_partial:
        assert await generate_missing_question_materials({"q1": row}, targets, 3, provider, **kwargs) == []
    else:
        with pytest.raises(StructuredOutputInvalidError, match="complete original teacher rule"):
            await generate_missing_question_materials({"q1": row}, targets, 3, provider, **kwargs)
    assert len(provider.calls) == 3
    assert published == []


@pytest.mark.asyncio
async def test_cosmetic_policy_edit_rebinds_exact_evidence_without_another_call():
    row, raw = unknown_floor()
    provider = PolicyProvider(raw)
    prior = await compile_parts_draft(row, provider)
    changed = {**row, "criterion": row["criterion"] + "！"}
    revised = rebind_review_edit(prior, row, changed)
    assert revised is not None
    assert saved_draft({**changed, "rubric_draft": revised.model_dump(mode="json")}) is not None
    assert revised.semantic_policy_checks[0]["source_quote"] == changed["criterion"]
    assert revised.semantic_policy_checks[0]["policy_id"] != prior.semantic_policy_checks[0]["policy_id"]
    assert len(provider.calls) == 2
    assert rebind_review_edit(prior, row, {**row, "criterion": row["criterion"].replace("五分之一", "三分之一")}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["policy_id", "source_quote", "verdict", "reason"])
async def test_invalid_stored_policy_evidence_is_rejected_without_crashing(missing):
    row, raw = unknown_floor()
    draft = await compile_parts_draft(row, PolicyProvider(raw))
    stored = draft.model_dump(mode="json")
    stored["semantic_policy_checks"][0].pop(missing)
    assert saved_draft({**row, "rubric_draft": stored}) is None
    plan = draft.plan.model_dump(mode="json")
    plan["semantic_policy_checks"][0].pop(missing)
    plan["plan_id"] = _content_hash({key: value for key, value in plan.items() if key != "plan_id"})
    manifest = {"math_grading_engine_version": MATH_GRADING_ENGINE_VERSION,
                "math_rubric_plans": {"q1": plan}}
    assert plans_from_snapshots(manifest, [], for_grading=True) == {}
