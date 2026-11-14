"""The upload slider freezes only unspecified follow-through awards before grading."""
import json
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
import pytest
from pydantic import ValidationError
from backend.models import QuestionScorePolicy
from backend.services.rubric_drafts import RubricDefinition, generated_rubric, saved_draft, bind_draft, compile_parts_draft
from backend.tests.test_teacher_frozen_allocations import make_row, proposed
from backend.tests.test_question_material_format_repair import Provider
from backend.tests.test_follow_through_policy_coverage import example


def row_with_policy(criterion, strictness):
    row = make_row(criterion)
    row["rubric_generation_preferences"] = {"follow_through_strictness": strictness}
    return row


@pytest.mark.parametrize("strictness", range(0, 101, 5))
def test_every_tick_changes_only_unspecified_follow_through_fields(strictness):
    row = row_with_policy("列式3分，计算结果7分。", strictness)
    text, draft = generated_rubric(row, RubricDefinition(part_rubric=proposed()))
    rules = draft.plan.part_rubric
    assert rules.follow_through_strictness == strictness
    assert [s.total_pts for s in rules.parts[0].steps] == [3, 7]
    expected = [float((Decimal(n) * (100-strictness) / 100).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)) for n in (3, 7)]
    assert [s.follow_through_pts for s in rules.parts[0].steps] == expected
    other = generated_rubric(row_with_policy(row["criterion"], 50), RubricDefinition(part_rubric=proposed()))[1].plan.part_rubric
    assert rules.parts[0].alternative == other.parts[0].alternative
    assert rules.parts[0].deductions == other.parts[0].deductions
    restored = saved_draft({**row, "criterion": text, "rubric_draft": json.loads(draft.model_dump_json())})
    assert restored is not None
    assert bind_draft(restored, row, grading_setup={"strictness": 0, "allow_partial_credit": False}) == restored.plan


@pytest.mark.parametrize("strictness", [0, 50, 100])
@pytest.mark.parametrize("award", [0, 4, 8])
def test_explicit_teacher_award_including_zero_and_full_overrides_slider(strictness, award):
    row = row_with_policy(f"列式2分，计算结果8分。计算结果沿用错值但本步正确保留{award}分。", strictness)
    raw = proposed(points=(2, 8)); raw["parts"][0]["steps"][1]["follow_through_pts"] = award
    _, draft = generated_rubric(row, RubricDefinition(part_rubric=raw))
    steps = draft.plan.part_rubric.parts[0].steps
    assert steps[1].follow_through_pts == award
    assert steps[0].follow_through_pts == 2*(100-strictness)/100


class ScopedProvider(Provider):
    def __init__(self, raw, *, wrong_target=False):
        super().__init__([]); self.raw=raw; self.wrong_target=wrong_target
    async def ainvoke(self, messages, **kwargs):
        request=json.loads(messages[1].content)
        if "policies_to_verify" in request:
            reply={"checks":[{"policy_id":p["policy_id"], "source_quote":p["source_quote"],
                "verdict":"consistent", "reason":"The teacher specifies only the result step.",
                "follow_through_target_ids":[{"part_id":"p", "step_id":"missing" if self.wrong_target else "val"}]}
                for p in request["policies_to_verify"]]}
        else: reply={"part_rubric":self.raw}
        self.responses=iter([json.dumps(reply,ensure_ascii=False)])
        return await super().ainvoke(messages, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("award", [0, 4])
async def test_semantic_scope_preserves_teacher_field_and_computes_other_defaults(award):
    row, raw=example(f"计算结果沿用错值但本步正确得{award}分", award=award)
    row["rubric_generation_preferences"]={"follow_through_strictness":25}
    provider=ScopedProvider(raw)
    draft=await compile_parts_draft(row,provider)
    assert len(provider.calls)==2
    assert [s.follow_through_pts for s in draft.plan.part_rubric.parts[0].steps]==[1.5,award]
    assert [t["step_id"] for t in draft.semantic_policy_checks[0]["follow_through_targets"]]==["val"]
    loaded=saved_draft({**row,"rubric_draft":json.loads(draft.model_dump_json())})
    assert loaded is not None and loaded.plan.part_rubric==draft.plan.part_rubric
    bad=deepcopy(loaded.model_dump(mode="json"))
    # A persisted approval cannot be reused after the teacher-governed field changes.
    bad["plan"]["part_rubric"]["parts"][0]["steps"][1]["follow_through_pts"]=2
    assert saved_draft({**row,"rubric_draft":bad}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("strictness", [0, 25, 50, 100])
async def test_one_assignment_combines_teacher_awards_defaults_and_deductions(strictness):
    from backend.services.math_grading_context import prepare_math_plans, plans_from_snapshots, MATH_GRADING_ENGINE_VERSION
    from backend.tests.test_teacher_mixed_ledger import mixed_candidate, arithmetic_decision
    from backend.skills.part_scoring import settle_parts
    rows = {}
    for i, award in enumerate((0, 4, None), 1):
        text = "列式2分，计算结果8分。"
        if award is not None:
            text += f"计算结果沿用错值但本步正确保留{award}分。"
        row = row_with_policy(text, strictness)
        row.update(q_id=f"q{i}", number=str(i))
        raw = proposed(points=(2, 8))
        if award is not None:
            raw["parts"][0]["steps"][1]["follow_through_pts"] = award
        _, draft = generated_rubric(row, RubricDefinition(part_rubric=raw))
        rows[row["q_id"]] = {**row, "rubric_draft": json.loads(draft.model_dump_json())}
    row, raw = mixed_candidate()
    row.update(q_id="q4", number="4", rubric_generation_preferences={"follow_through_strictness": strictness})
    raw["parts"][0].pop("steps")
    _, draft = generated_rubric(row, RubricDefinition(part_rubric=raw))
    rows["q4"] = {**row, "rubric_draft": json.loads(draft.model_dump_json())}
    plans = await prepare_math_plans(rows, object())  # No model available on reload.
    for key, award in (("q1", 0), ("q2", 4), ("q3", 8 * (100-strictness)/100)):
        assert [step.follow_through_pts for step in plans[key].part_rubric.parts[0].steps] == [2 * (100-strictness)/100, award]
    assert not plans["q4"].part_rubric.parts[0].steps
    assert settle_parts(plans["q4"].part_rubric, [arithmetic_decision()], "10+x=17\nx=6", row["stem"])[0] == 9
    manifest = {"math_grading_engine_version": MATH_GRADING_ENGINE_VERSION,
                "math_rubric_plans": {key: plan.model_dump(mode="json") for key, plan in plans.items()}}
    restored = plans_from_snapshots(manifest, [], for_grading=True)
    assert {key: plan.part_rubric for key, plan in restored.items()} == {key: plan.part_rubric for key, plan in plans.items()}


@pytest.mark.parametrize("value", [-1, 101, 1.5, True, "50"])
def test_upload_policy_rejects_invalid_or_coerced_slider_values(value):
    with pytest.raises(ValidationError): QuestionScorePolicy(follow_through_strictness=value)


@pytest.mark.asyncio
@pytest.mark.parametrize("strictness", [0, 25, 100])
async def test_submitted_preference_survives_worker_artifacts_and_task_reload(monkeypatch, strictness):
    from unittest.mock import AsyncMock
    from backend.tests.test_incremental_question_preparation import (
        inc, api, row, persisted, mock_supplied_rule_interpretation,
        _seed_task, _queue_question_preparation, _RecoveryRegistry, _claim, task_facade,
    )
    owner, task = _seed_task()
    _, queued, _ = await _queue_question_preparation(owner, task, follow_through_strictness=strictness)
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: _RecoveryRegistry())
    operation = _claim(owner, queued["job_id"], "strictness-roundtrip")
    inputs = api._rehydrate_question_preparation_inputs(operation)
    assert inputs["score_policy"].follow_through_strictness == strictness
    mock_supplied_rule_interpretation(monkeypatch)
    monkeypatch.setattr(inc, "_extract_problems_call", AsyncMock(return_value=inc._rows([
        row(1, criterion="Full credit for a correct explanation.", reference_answer="Supplied answer.")
    ])))
    await api.run_durable_question_preparation(operation)
    saved, rows = persisted(owner, operation)
    assert saved.status == "done", (saved.error_code, saved.checkpoint)
    actual = task_facade.get_task(task_id=task, owner_id=owner, full=True)["problem_data"]["q1"]
    from backend.db import assignment_repository
    question = assignment_repository.list_questions(task, teacher_id=owner)[0]
    assert actual["rubric_generation_preferences"] == {"follow_through_strictness":strictness}
    for snapshot in (rows["q1"], question):
        draft = saved_draft(snapshot)
        assert draft is not None
        assert draft.plan.part_rubric.follow_through_strictness == strictness
        assert draft.plan.part_rubric.parts[0].steps[0].follow_through_pts == 10*(100-strictness)/100
