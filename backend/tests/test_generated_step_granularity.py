"""Deterministic generation bounds, source exceptions, and frozen-plan compatibility."""
import json
from decimal import Decimal

import pytest

from backend.agents.incremental_question_preparation import _rows
from backend.services import part_rubric
from backend.services.rubric_drafts import RubricAllocationError, RubricDefinition, generated_rubric, saved_draft
from backend.tests.test_proof_rubric import audit, initial, rubric, STEM
from backend.tests.test_question_material_format_repair import Provider
from backend.tests.test_manual_proof_completion import complete


def proposal(groups, *, stem="Compute the requested results.", criterion=""):
    budgets = [float(sum(Decimal(str(p)) for p in points)) for points in groups]
    total = sum(budgets)
    if len(groups) > 1:
        stem += "\n" + "\n".join(f"({chr(97+i)}) Compute result {i+1}." for i in range(len(groups)))
    row = _rows([dict(number="1", type="计算题", stem=stem, criterion=criterion, max_score=total)])["q1"]
    labels = [p["label"] for p in (row.get("question_structure") or {}).get("subparts", [])]
    assert len(labels) == (len(groups) if len(groups) > 1 else 0)
    definition = RubricDefinition(part_rubric=dict(id="r", label="1", stem=stem, total_pts=total, parts=[dict(
        id=f"p{i+1}", label=labels[i] if labels else None, total_pts=budgets[i],
        steps=[dict(id=f"s{j+1}", criterion=f"Establish result {i+1}.{j+1}.", total_pts=points)
               for j, points in enumerate(group)], alternative=dict(deduct_per_error=1))
        for i, group in enumerate(groups)]))
    return row, definition


@pytest.mark.parametrize("groups", [
    [[.25] * 8], [[1] * 9], [[1] * 12], [[.5] * 8, [.5] * 8],
    [[.5] * 8, [1] * 9], [[.1, .2, .3]],
])
def test_either_bound_is_sufficient_and_applies_to_each_subquestion(groups):
    row, definition = proposal(groups)
    _, draft = generated_rubric(row, definition)
    assert [[s.total_pts for s in p.steps] for p in draft.plan.part_rubric.parts] == groups


@pytest.mark.parametrize("groups,bad_part", [
    ([[.5] * 9], "p1"), ([[1] * 8 + [.99]], "p1"),
    ([[2] * 8 + [.5]], "p1"), ([[.5] * 8, [.5] * 9], "(b)"),
])
def test_only_exceeding_both_bounds_requests_local_merge(groups, bad_part):
    row, definition = proposal(groups)
    before = definition.model_dump_json()
    with pytest.raises(RubricAllocationError) as error:
        generated_rubric(row, definition)
    assert str(error.value).startswith(bad_part + ":")
    assert "at most 8 steps OR" in str(error.value)
    assert "all other subquestions" in str(error.value)
    assert definition.model_dump_json() == before


@pytest.mark.parametrize("stem", [
    "按教师指定的九步评分，每步0.5分。Compute the results.",
    "Compute the results. Award 0.5 points for each of the nine listed steps.",
])
def test_explicit_teacher_scoring_in_question_is_exempt(stem):
    row, definition = proposal([[.5] * 9], stem=stem)
    _, draft = generated_rubric(row, definition)
    assert len(draft.plan.part_rubric.parts[0].steps) == 9


@pytest.mark.parametrize("criterion", ["按教师指定的九个步骤逐项给分。", "Use the teacher's nine scoring steps."])
def test_provided_teacher_criterion_is_exempt(criterion):
    row, definition = proposal([[.5] * 9], criterion=criterion)
    _, draft = generated_rubric(row, definition)
    assert draft.plan.part_rubric.teacher_rubric == criterion
    assert len(draft.plan.part_rubric.parts[0].steps) == 9


@pytest.mark.parametrize("stem", [
    "（4.5分）Compute the results.", "Compute the results. (4.5 points)",
    "本题满分 4.5 分。Compute the results.", "Compute the results. 本题满分 4.5 分",
    "1. (4.5 points) Compute the results.", "第1题（4.5分）Compute the results.",
    "Total: 4.5 points. Compute the results.",
])
def test_question_total_does_not_exempt_ai_steps(stem):
    row, definition = proposal([[.5] * 9], stem=stem)
    with pytest.raises(RubricAllocationError, match="at most 8 steps OR"):
        generated_rubric(row, definition)


def test_model_cannot_invent_teacher_exception():
    row, definition = proposal([[.5] * 9])
    definition = definition.model_copy(update={"part_rubric": definition.part_rubric.model_copy(update={
        "teacher_rubric": "invented teacher rule", "rubric_origin": "teacher"})})
    with pytest.raises(RubricAllocationError, match="at most 8 steps OR"):
        generated_rubric(row, definition)


def test_old_frozen_rubric_loads_and_sibling_completion_does_not_reinterpret_it(monkeypatch):
    row, definition = proposal([[.5] * 9])
    with monkeypatch.context() as m:
        m.setattr(part_rubric, "validate_generated_step_granularity", lambda *_: None)
        text, draft = generated_rubric(row, definition)
    completed = {**row, "criterion": text, "rubric_draft": draft.model_dump(mode="json"),
                 "ai_completion_provenance": {"criterion": {"source_kind": "ai_generated"}}}
    assert saved_draft(completed) is not None
    assert generated_rubric(completed, definition)[1].plan.part_rubric == draft.plan.part_rubric


@pytest.mark.asyncio
@pytest.mark.parametrize("repaired", [True, False])
async def test_manual_generation_merges_excessive_steps_and_keeps_accepted_answer(monkeypatch, repaired):
    rules = rubric();part = rules.parts[0]
    small_steps = [part.steps[0].model_copy(update={"id": f"s{i+1}", "criterion": f"Establish goal {i+1}.",
                   "total_pts": points}) for i, points in enumerate([1] * 8 + [1.5, .5])]
    excessive = rules.model_copy(update={"parts": (part.model_copy(update={"steps": tuple(small_steps)}),)})
    provider = Provider([initial(excessive), initial(reference=False), audit()] if repaired else
                        [initial(excessive), initial(excessive, reference=False), initial(excessive, reference=False)])
    job, data = await complete(monkeypatch, provider)
    assert job.status == "done"
    if not repaired:
        assert job.payload["skipped_target_ids"] == ["q1:criterion"]
        assert data.reference_answer and not data.criterion and saved_draft(data) is None
        assert len(provider.calls) == 3
        return
    assert not job.payload["skipped_target_ids"]
    assert len(provider.calls) == 3  # No audit call for the invalid numerical proposal.
    retry = json.loads(provider.calls[1][-1].content.removeprefix("[Generation request]\n"))
    assert [t["target_id"] for t in retry["requested_targets"]] == ["q1:criterion"]
    assert retry["known_problems"][0]["existing_reference_answer"] == data.reference_answer
    assert "at most 8 steps OR" in retry["validation_feedback"]["q1:criterion"]
    frozen = saved_draft(data).plan.part_rubric
    assert [(s.id, s.total_pts) for s in frozen.parts[0].steps] == [(s.id, s.total_pts) for s in rubric().parts[0].steps]


@pytest.mark.asyncio
async def test_editing_only_alternative_keeps_old_fine_grained_main_steps(monkeypatch):
    from backend.services.rubric_drafts import compile_parts_draft
    row, definition = proposal([[.5] * 9])
    with monkeypatch.context() as m:
        m.setattr(part_rubric, "validate_generated_step_granularity", lambda *_: None)
        text, prior = generated_rubric(row, definition)
    row = {**row, "criterion": text, "ai_completion_provenance": {"criterion": {"source_kind": "ai_generated"}}}
    raw = prior.plan.part_rubric.model_dump(mode="json")
    raw["parts"][0]["alternative"]["deduct_per_error"] = 2
    response = json.dumps({"part_rubric": raw})
    provider = Provider([response, response])
    revised = await compile_parts_draft(row, provider, previous=prior, alternative_ids={"p1"})
    assert revised.plan.part_rubric.parts[0].steps == prior.plan.part_rubric.parts[0].steps
    assert revised.plan.part_rubric.parts[0].alternative.deduct_per_error == 2
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_alternative_selection_without_prior_does_not_exempt_new_steps():
    from backend.domain.errors import ValidationError
    from backend.services.rubric_drafts import compile_parts_draft
    row, definition = proposal([[.5] * 9])
    response = json.dumps({"part_rubric": definition.part_rubric.model_dump(mode="json")})
    provider = Provider([response, response, response])
    with pytest.raises(ValidationError) as error:
        await compile_parts_draft(row, provider, alternative_ids={"p1"})
    assert error.value.code == "invalid_structured_rubric"
