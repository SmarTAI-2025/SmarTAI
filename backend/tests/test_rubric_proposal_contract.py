"""Synthetic regression for observed misplaced metadata; no saved raw response."""
import json
from copy import deepcopy

import pytest

from backend.agents.ingest_agent import generate_missing_question_materials
from backend.services.rubric_drafts import (
    RubricDefinition, generated_rubric, source_bound_definition, written_proposal_schema,
)
from backend.tests.test_rubric_missing_points import sample, accepted
from backend.tests.test_question_material_format_repair import Provider


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [True, False])
async def test_misplaced_source_metadata_keeps_scores_without_regeneration(partial):
    row, raw = sample()
    raw["part_rubric"]["parts"][0].update(rubric_origin="teacher", teacher_gaps=[])
    before = deepcopy(raw)
    provider = Provider([json.dumps({"candidates": [{"target_id": "q1:criterion", "rubric_definition": raw}]})])
    result = await generate_missing_question_materials({"q1": row},
        [dict(target_id="q1:criterion", q_id="q1", target="criterion")], 6, provider, allow_partial=partial)
    assert len(result) == len(provider.calls) == 1
    rules = result[0].rubric_draft["plan"]["part_rubric"]
    assert rules["rubric_origin"] == "ai"
    assert [s["total_pts"] for s in rules["parts"][0]["steps"]] == [2, 1, 2]
    assert raw == before
    sent = json.loads(provider.calls[0][0].content.split("Rubric definition schema:\n")[1])
    expected = written_proposal_schema()
    expected["$defs"]["PartRubric"]["properties"]["parts"].update(minItems=1, maxItems=1)
    assert sent == expected
    for name, fields in {"PartRubric": ["rubric_origin", "teacher_rubric", "id", "stem"],
                         "RubricPart": ["origin_overrides", "subpart_id"]}.items():
        assert not set(fields) & sent["$defs"][name]["properties"].keys()


def test_advisories_move_to_question_without_overwriting_or_mutating_fields():
    row, raw = sample()
    raw["part_rubric"]["teacher_gaps"] = ["First advisory"]
    raw["part_rubric"]["parts"][0]["teacher_gaps"] = ["First advisory", "Second advisory"]
    before = deepcopy(raw)
    bound = source_bound_definition(raw, row)
    assert bound["part_rubric"]["teacher_gaps"] == ["First advisory", "Second advisory"]
    assert raw == before
    assert generated_rubric(row, RubricDefinition.model_validate(bound))[1]


@pytest.mark.parametrize("damage", [
    {"rubric_origin": {"unexpected": "object"}}, {"teacher_gaps": "not a list"},
    {"teacher_gaps": [3]}, {"teacher_gaps": [str(i) for i in range(21)]},
    {"score": 5}, {"total_pts": 4}, {"alternative": {"deduct_per_error": -1}},
])
def test_metadata_recovery_does_not_hide_bad_types_unknown_scores_or_budget_conflicts(damage):
    row, raw = sample()
    raw["part_rubric"]["parts"][0].update(damage)
    with pytest.raises(ValueError):
        accepted(row, raw)


def test_conflicting_wrapper_rules_still_fail():
    row, raw = sample()
    raw["total_pts"] = 9
    with pytest.raises(ValueError):
        accepted(row, raw)


@pytest.mark.asyncio
async def test_repair_reuses_the_simplified_contract_and_does_not_ignore_scoring_errors():
    row, raw = sample()
    broken = deepcopy(raw)
    broken["part_rubric"]["parts"][0]["steps"][0]["total_pts"] = -1
    responses = [json.dumps({"candidates": [{"target_id": "q1:criterion", "rubric_definition": r}]})
                 for r in (broken, raw)]
    provider = Provider(responses)
    candidates = await generate_missing_question_materials({"q1": row},
        [dict(target_id="q1:criterion", q_id="q1", target="criterion")], 6, provider)
    assert len(candidates) == 1 and len(provider.calls) == 2
    repair = provider.calls[1][-1].content
    assert '"rubric_origin"' not in repair and '"origin_overrides"' not in repair
    assert '"deduct_per_error"' in repair and '"alternative"' in repair
    assert '"minItems": 1' in repair and '"maxItems": 1' in repair
