"""Complete only uniquely determined budgets before the existing validation."""
import json
from copy import deepcopy

import pytest

from backend.services.rubric_drafts import (
    RubricDefinition, generated_rubric, source_bound_definition,
)
from backend.tests.test_question_material_format_repair import Provider


def sample(total=5, amounts=(2, 1, 2)):
    row = dict(q_id="q1", number="Q1", type="计算题", max_score=total,
               stem="Evaluate the integral of x exp(x²) from 0 to 1.",
               reference_answer="(e - 1) / 2", criterion="",
               question_structure={"shared_stem": "Evaluate the integral of x exp(x²) from 0 to 1.",
                                   "subparts": []})
    part = dict(id="p1", total_pts=total, alternative={"deduct_per_error": 0.1},
                steps=[dict(id=f"s{i}", criterion=criterion, total_pts=points)
                       for i, (criterion, points) in enumerate(zip(
                           ("Substitute u = x²", "Transform the bounds", "Evaluate (e - 1) / 2"), amounts))])
    return row, {"part_rubric": {"total_pts": total, "parts": [part]}}


def accepted(row, raw):
    bound = source_bound_definition(raw, row)
    _, draft = generated_rubric(row, RubricDefinition.model_validate(bound))
    return draft.plan.part_rubric


@pytest.mark.parametrize("missing_part,missing_step", [(True, None), (False, 0), (False, 1),
                                                       (False, 2), (True, 1)])
def test_demo_missing_points_are_recovered_without_changing_rules_or_input(missing_part, missing_step):
    row, raw = sample()
    part = raw["part_rubric"]["parts"][0]
    if missing_part:
        del part["total_pts"]
    if missing_step is not None:
        del part["steps"][missing_step]["total_pts"]
    before = deepcopy((row, raw))
    rules = accepted(row, raw)
    assert rules.total_pts == rules.parts[0].total_pts == 5
    assert [step.total_pts for step in rules.parts[0].steps] == [2, 1, 2]
    assert [step.criterion for step in rules.parts[0].steps] == [step["criterion"] for step in part["steps"]]
    assert (row, raw) == before


@pytest.mark.parametrize("as_strings", [False, True])
def test_difference_uses_decimal_arithmetic(as_strings):
    row, raw = sample(0.6, (0.1, 0.2, 0.3))
    part = raw["part_rubric"]["parts"][0]
    del part["steps"][2]["total_pts"]
    if as_strings:
        part["total_pts"] = "0.6"
        part["steps"][0]["total_pts"] = "0.1"
        part["steps"][1]["total_pts"] = "0.2"
    assert [step.total_pts for step in accepted(row, raw).parts[0].steps] == [0.1, 0.2, 0.3]


def test_existing_flat_singleton_normalization_can_complete_one_step():
    row, raw = sample()
    part = raw["part_rubric"].pop("parts")[0]
    raw["part_rubric"].update(steps=part["steps"], alternative=part["alternative"])
    del part["steps"][0]["total_pts"]
    assert [s.total_pts for s in accepted(row, raw).parts[0].steps] == [2, 1, 2]


def test_only_step_has_the_known_part_budget():
    row, raw = sample(amounts=(5,))
    del raw["part_rubric"]["parts"][0]["steps"][0]["total_pts"]
    assert accepted(row, raw).parts[0].steps[0].total_pts == 5


def multipart():
    from backend.services.question_structure import build_major_question_structure
    row, raw = sample(8, (2, 1, 2))
    row["stem"] = "(a) Evaluate the integral.\n(b) Solve x + 1 = 2."
    row["question_structure"] = build_major_question_structure(row, major_order=0).model_dump()
    part = raw["part_rubric"]["parts"][0]
    part.update(total_pts=5, label="(a)")
    raw["part_rubric"]["parts"].append(dict(id="p2", label="(b)", total_pts=3,
        alternative={"deduct_per_error": 1}, steps=[dict(id="s1", criterion="Solve x = 1", total_pts=3)]))
    return row, raw


def test_step_difference_uses_own_part_budget_not_whole_question():
    row, raw = multipart()
    del raw["part_rubric"]["parts"][0]["steps"][2]["total_pts"]
    rules = accepted(row, raw)
    assert [part.total_pts for part in rules.parts] == [5, 3]
    assert [step.total_pts for step in rules.parts[0].steps] == [2, 1, 2]


def test_missing_multipart_budget_is_not_inferred():
    row, raw = multipart()
    del raw["part_rubric"]["parts"][0]["total_pts"]
    with pytest.raises(ValueError):
        accepted(row, raw)


@pytest.mark.parametrize("known", [0, -1, 5, 6, None, True, "bad", float("inf"), float("nan")])
def test_invalid_or_nonpositive_difference_is_not_completed(known):
    row, raw = sample()
    steps = raw["part_rubric"]["parts"][0]["steps"]
    steps[0]["total_pts"] = known
    del steps[1]["total_pts"]
    bound = source_bound_definition(raw, row)
    assert "total_pts" not in bound["part_rubric"]["parts"][0]["steps"][1]
    with pytest.raises(ValueError):
        accepted(row, raw)


@pytest.mark.parametrize("damage", ["two_missing", "explicit_null", "wrong_total", "bad_step", "missing_criterion"])
def test_completion_preserves_schema_and_budget_errors(damage):
    row, raw = sample()
    part = raw["part_rubric"]["parts"][0]
    if damage == "two_missing":
        del part["steps"][0]["total_pts"]
        del part["steps"][1]["total_pts"]
    elif damage == "explicit_null":
        part["steps"][0]["total_pts"] = None
    elif damage == "wrong_total":
        part["total_pts"] = 6
    elif damage == "bad_step":
        part["steps"][0] = None
        del part["steps"][1]["total_pts"]
    else:
        del part["steps"][0]["criterion"]
        del part["steps"][0]["total_pts"]
    with pytest.raises(ValueError):
        accepted(row, raw)


@pytest.mark.parametrize("maximum", [None, 0, -1, True, float("inf"), "bad"])
def test_unknown_source_maximum_does_not_supply_singleton_budget(maximum):
    row, raw = sample()
    row["max_score"] = maximum
    row["criterion_source"] = "ai"
    del raw["part_rubric"]["parts"][0]["total_pts"]
    bound = source_bound_definition(raw, row)
    assert "total_pts" not in bound["part_rubric"]["parts"][0]


def test_conflicting_parent_total_does_not_supply_singleton_budget():
    row, raw = sample()
    raw["part_rubric"]["total_pts"] = 6
    del raw["part_rubric"]["parts"][0]["total_pts"]
    with pytest.raises(ValueError):
        accepted(row, raw)


def test_missing_question_structure_does_not_establish_singleton_scope():
    row, raw = sample()
    del row["question_structure"]
    del raw["part_rubric"]["parts"][0]["total_pts"]
    with pytest.raises(ValueError):
        accepted(row, raw)


@pytest.mark.parametrize("kind", ["steps", "deductions"])
def test_valid_rules_are_unchanged_and_deduction_rates_are_never_inferred(kind):
    row, raw = sample()
    part = raw["part_rubric"]["parts"][0]
    if kind == "deductions":
        del part["steps"]
        part["deductions"] = [dict(id="d", criterion="An independent error", deduct_pts=1)]
    before = deepcopy(raw)
    expected = deepcopy(raw)
    expected["part_rubric"].update(id="q1", label="Q1", stem=row["stem"], nonblank_floor={"pts": 0})
    expected["part_rubric"]["parts"][0].update(label=None, stem=None)
    assert source_bound_definition(raw, row) == expected
    assert raw == before
    if kind == "deductions":
        del part["deductions"][0]["deduct_pts"]
        bound = source_bound_definition(raw, row)
        assert "deduct_pts" not in bound["part_rubric"]["parts"][0]["deductions"][0]
        with pytest.raises(ValueError):
            RubricDefinition.model_validate(bound)


def test_teacher_allocation_still_rejects_an_arithmetically_possible_difference():
    from backend.tests.test_teacher_frozen_allocations import make_row, proposed
    row = make_row("列式3分、计算结果7分")
    raw = {"part_rubric": proposed(points=(4, 6))}
    del raw["part_rubric"]["parts"][0]["steps"][0]["total_pts"]
    with pytest.raises(ValueError, match="requires"):
        accepted(row, raw)


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_partial", [True, False])
async def test_generation_accepts_missing_budgets_in_one_model_call(allow_partial):
    from backend.agents.ingest_agent import generate_missing_question_materials
    row, raw = sample()
    part = raw["part_rubric"]["parts"][0]
    del part["total_pts"]
    del part["steps"][1]["total_pts"]
    provider = Provider([json.dumps({"candidates": [dict(target_id="q1:criterion", rubric_definition=raw)]})])
    feedback = {}
    candidates = await generate_missing_question_materials({"q1": row},
        [dict(q_id="q1", target_id="q1:criterion", target="criterion")], 6, provider,
        allow_partial=allow_partial, repair_feedback=feedback)
    assert len(candidates) == len(provider.calls) == 1
    assert feedback == {}
    assert [s["total_pts"] for s in candidates[0].rubric_draft["plan"]["part_rubric"]["parts"][0]["steps"]] == [2, 1, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["part", "step", "both"])
async def test_teacher_rubric_compiler_uses_the_same_completion(missing):
    from backend.services.question_structure import build_major_question_structure
    from backend.services.rubric_drafts import compile_parts_draft
    from backend.tests.test_teacher_frozen_allocations import make_row, proposed
    row = make_row("列式3分、计算结果7分")
    row["question_structure"] = build_major_question_structure(row, major_order=0).model_dump()
    raw = {"part_rubric": proposed()}
    part = raw["part_rubric"]["parts"][0]
    if missing in {"part", "both"}:
        del part["total_pts"]
    if missing in {"step", "both"}:
        del part["steps"][1]["total_pts"]
    provider = Provider([json.dumps(raw)])
    draft = await compile_parts_draft(row, provider)
    assert len(provider.calls) == 1
    assert [s.total_pts for s in draft.plan.part_rubric.parts[0].steps] == [3, 7]


@pytest.mark.asyncio
@pytest.mark.parametrize("recovers", [True, False])
async def test_ambiguous_missing_steps_keep_targeted_retries_and_existing_results(monkeypatch, recovers):
    from unittest.mock import AsyncMock
    from backend.domain.errors import ValidationError
    from backend.tests.test_incremental_question_preparation import inc, persisted, setup
    owner, _, operation, pipeline = await setup(monkeypatch)
    row, raw = sample(10, (4, 2, 4))
    del row["question_structure"]
    row = inc._rows([row])["q1"]
    monkeypatch.setattr(inc, "_extract_problems_call", AsyncMock(return_value={"q1": row}))
    invalid = deepcopy(raw)
    for step in invalid["part_rubric"]["parts"][0]["steps"][:2]:
        del step["total_pts"]
    del raw["part_rubric"]["parts"][0]["steps"][1]["total_pts"]
    def reply(value):
        return json.dumps({"candidates": [dict(target_id="q1:criterion", rubric_definition=value)]})
    pipeline.provider = Provider([reply(invalid), reply(raw)] if recovers else [reply(invalid)] * 3)
    if recovers:
        await pipeline.run()
    else:
        with pytest.raises(ValidationError):
            await pipeline.run()
    assert len(pipeline.provider.calls) == (2 if recovers else 3)
    requests = [json.loads(call[1].content.split("\n", 1)[1]) for call in pipeline.provider.calls]
    assert "total_pts" in requests[1]["validation_feedback"]["q1:criterion"]
    assert all(r["requested_targets"] == [dict(q_id="q1", target_id="q1:criterion", target="criterion")] for r in requests)
    saved, rows = persisted(owner, operation)
    assert rows["q1"]["reference_answer"] == row["reference_answer"]
    assert saved.checkpoint["preparation_counts"]["rubrics"] == int(recovers)
