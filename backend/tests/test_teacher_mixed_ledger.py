"""AI additions cannot replace a teacher's per-error scoring basis."""
import json
from copy import deepcopy

import pytest

from backend.services.math_rubric_plan import _content_hash
from backend.services.math_grading_context import MATH_GRADING_ENGINE_VERSION, plans_from_snapshots
from backend.services.part_rubric import PartRubric
from backend.domain.errors import ValidationError
from backend.services.rubric_drafts import (
    RubricAllocationError, RubricDefinition, compile_parts_draft, generated_rubric, saved_draft,
)
from backend.skills.part_scoring import PartDecision, settle_parts
from backend.tests.test_question_material_format_repair import Provider
from backend.tests.test_teacher_frozen_allocations import make_row, proposed
from backend.tests.test_teacher_policy_audit import PolicyProvider
from backend.services.teacher_point_allocations import TeacherPolicyUnresolved


def mixed_candidate():
    row = make_row("每个独立计算错误扣1分。")
    row.update(stem="笔和本共17元，本10元，求笔价。", reference_answer="10+x=17，解得x=7。")
    raw = proposed(("列式", "计算结果"), (5, 5))
    raw["parts"][0]["deductions"] = [dict(
        id="calc", criterion="每个独立计算错误", source_quote=row["criterion"], deduct_pts=1)]
    return row, raw


def arithmetic_decision(*, steps=False):
    return PartDecision(
        part_id="p", target_quote="part:p", student_quote="student:1\nstudent:2",
        method="reference", relevance="partial", progress="方程正确但求解算错",
        errors=[dict(id="e", rule_id="calc", evidence="student:2", origin_quote="x=6", reason="计算错误")],
        steps=[dict(id="eq", outcome="earned", evidence="student:1", reason="方程正确"),
               dict(id="val", outcome="missed", evidence="student:2", reason="一次计算错误")] if steps else [])


def test_teacher_error_rate_does_not_authorize_additional_ai_step_losses():
    row, raw = mixed_candidate()
    with pytest.raises(RubricAllocationError, match="teacher.*funded"):
        generated_rubric(row, RubricDefinition(part_rubric=raw))


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_reference_quotes", [False, True])
async def test_raw_mixed_json_repairs_to_teacher_deductions_and_settles_once(invalid_reference_quotes):
    row, raw = mixed_candidate()
    repaired = deepcopy(raw)
    repaired["parts"][0].pop("steps")
    if invalid_reference_quotes:
        # Also exercise the incidental invalid fields seen in the live synthetic
        # reply. Fixing these fields alone must not authorize a mixed ledger.
        raw["parts"][0]["steps"][0]["source_quote"] = "10+x=17"
        raw["parts"][0]["steps"][1]["source_quote"] = "解得x=7。"
        raw["parts"][0]["deductions"][0]["max_deduction"] = 10
    provider = Provider([json.dumps({"part_rubric": raw}), json.dumps({"part_rubric": repaired})])
    draft = await compile_parts_draft(row, provider)
    assert len(provider.calls) == 2
    assert all('"policies_to_verify"' not in messages[1].content for messages in provider.calls)
    assert not draft.plan.part_rubric.parts[0].steps
    assert settle_parts(draft.plan.part_rubric, [arithmetic_decision()], "10+x=17\nx=6", row["stem"])[0] == 9


@pytest.mark.parametrize("criterion", [
    "列式5分、计算结果5分，每个独立计算错误扣1分。",
    "列式5分，其余按解答质量给分，每个独立计算错误扣1分。",
])
def test_teacher_funded_mixed_policy_allows_missing_step_supplements(criterion):
    row, raw = mixed_candidate()
    row["criterion"] = criterion
    _, draft = generated_rubric(row, RubricDefinition(part_rubric=raw))
    rules = draft.plan.part_rubric
    assert rules.parts[0].steps and rules.parts[0].deductions
    assert settle_parts(rules, [arithmetic_decision(steps=True)], "10+x=17\nx=6", row["stem"])[0] == 4


def test_unfunded_mixed_historical_draft_cannot_supply_new_grading_rules():
    row, raw = mixed_candidate()
    raw["parts"][0].pop("steps")
    _, draft = generated_rubric(row, RubricDefinition(part_rubric=raw))
    stored = draft.model_dump(mode="json")
    stored["plan"]["part_rubric"]["parts"][0]["steps"] = mixed_candidate()[1]["parts"][0]["steps"]
    stored["plan"]["part_rubric"] = PartRubric.model_validate(stored["plan"]["part_rubric"]).model_dump(mode="json")
    stored["plan"]["plan_id"] = _content_hash({key: value for key, value in stored["plan"].items() if key != "plan_id"})
    assert saved_draft({**row, "rubric_draft": stored}) is None
    manifest = {"math_grading_engine_version": MATH_GRADING_ENGINE_VERSION,
                "math_rubric_plans": {"q1": stored["plan"]}}
    assert plans_from_snapshots(manifest, [], for_grading=True) == {}
    assert "q1" in plans_from_snapshots(manifest, [])  # Historical display remains unchanged.


@pytest.mark.parametrize("suffix", ["", "最多计2次。", "非空作答最低2分。", "最多计2次。非空作答最低2分。"])
@pytest.mark.asyncio
async def test_fully_covered_pure_deduction_policies_repair_without_audit(suffix):
    row, raw = mixed_candidate()
    row["criterion"] += suffix
    if "2次" in suffix:
        raw["parts"][0]["deductions"][0]["limit"] = 2
    repaired = deepcopy(raw)
    repaired["parts"][0].pop("steps")
    provider = Provider([json.dumps({"part_rubric": raw}), json.dumps({"part_rubric": repaired})])
    draft = await compile_parts_draft(row, provider)
    assert len(provider.calls) == 2 and not draft.semantic_policy_checks
    assert all('"policies_to_verify"' not in messages[1].content for messages in provider.calls)


@pytest.mark.parametrize("grant", [
    "按步骤评分，再额外扣除下列错误。",
    "过程和结果都应给分，具体配分可合理补充。",
    "Award steps as well as deducting the stated errors; choose appropriate step budgets.",
])
@pytest.mark.asyncio
async def test_unquantified_mixed_authorization_is_audited_once_then_reused(grant):
    row, raw = mixed_candidate()
    row["criterion"] = grant + row["criterion"]
    provider = PolicyProvider(raw)
    draft = await compile_parts_draft(row, provider)
    assert len(provider.calls) == 2 and len(draft.semantic_policy_checks) == 1
    audit = json.loads(provider.calls[1][1].content)
    assert audit["policies_to_verify"][0]["type"] == "mixed_scoring"
    assert audit["policies_to_verify"][0]["source_quote"] == row["criterion"]
    frozen = saved_draft({**row, "rubric_draft": draft.model_dump(mode="json")})
    assert frozen is not None
    assert settle_parts(frozen.plan.part_rubric, [arithmetic_decision(steps=True)], "10+x=17\nx=6", row["stem"])[0] == 4
    assert len(provider.calls) == 2


@pytest.mark.parametrize("verdict", ["inconsistent", "uncertain"])
@pytest.mark.asyncio
async def test_extra_prose_does_not_itself_authorize_mixed_scoring(verdict):
    row, raw = mixed_candidate()
    row["criterion"] = "请严格按以下规则执行。" + row["criterion"]
    before = deepcopy(row)
    provider = PolicyProvider(raw, verdict=verdict)
    with pytest.raises(ValidationError, match="could not be verified"):
        await compile_parts_draft(row, provider)
    assert len(provider.calls) == 2 and row == before and saved_draft(row) is None


@pytest.mark.asyncio
async def test_mixed_authorization_does_not_reset_three_request_budget():
    row, raw = mixed_candidate()
    row["criterion"] = "按步骤评分，再额外扣除下列错误。" + row["criterion"]
    provider = PolicyProvider(raw, wrong_quote=True)
    with pytest.raises(ValidationError, match="could not be validated"):
        await compile_parts_draft(row, provider)
    assert len(provider.calls) == 3


@pytest.mark.parametrize("change", ["step_budget", "step_target", "deduction_group"])
@pytest.mark.asyncio
async def test_mixed_approval_binds_all_proposed_targets_and_budgets(change):
    row, raw = mixed_candidate()
    row["criterion"] = "按步骤评分，再额外扣除下列错误。" + row["criterion"]
    draft = await compile_parts_draft(row, PolicyProvider(raw))
    revised = draft.plan.part_rubric.model_dump(mode="json")
    if change == "step_budget":
        revised["parts"][0]["steps"][0]["total_pts"] = 4
        revised["parts"][0]["steps"][1]["total_pts"] = 6
    elif change == "step_target":
        revised["parts"][0]["steps"][0]["criterion"] = "不同的数学目标"
    else:
        revised["parts"][0]["deductions"][0]["exclusive_group"] = "new_group"
    with pytest.raises(TeacherPolicyUnresolved):
        generated_rubric(row, RubricDefinition(part_rubric=revised),
            verified_policy_ids=[check["policy_id"] for check in draft.semantic_policy_checks])
