"""Material repair sees the same written-rule contract as the first request."""
import json
from copy import deepcopy

import pytest

from backend.agents.ingest_agent import generate_missing_question_materials
from backend.services.part_rubric import TEACHER_RULE_INSTRUCTION
from backend.tests.test_question_material_format_repair import Provider
from backend.tests.test_teacher_frozen_allocations import make_row, proposed


def schemas(provider):
    first = json.loads(provider.calls[0][0].content.split("\nRubric definition schema:\n", 1)[1])
    repair_text = provider.calls[1][-1].content
    repaired, _ = json.JSONDecoder().raw_decode(repair_text[repair_text.index("\n{") + 1:])
    return first, repaired


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["criterion", "reference_answer"])
@pytest.mark.parametrize("allow_partial,first_failure", [(False, "json"), (False, "schema"), (True, "json")])
async def test_written_first_request_and_repair_share_definition_and_server_identity(target, allow_partial, first_failure):
    row = make_row("列式3分，计算结果7分。")
    if target == "reference_answer":
        row["reference_answer"] = ""
    candidate = dict(target_id=f"q1:{target}", q_id="q1", target=target,
                     text_value="列式3分，计算结果7分。" if target == "criterion" else "10+x=17，x=7。",
                     rubric_definition={"part_rubric": proposed()})
    for key in ("id", "label", "stem"):
        candidate["rubric_definition"]["part_rubric"].pop(key)
    malformed = deepcopy(candidate)
    malformed["rubric_definition"]["part_rubric"]["parts"][0]["steps"][0]["origin"] = "ai"
    first_raw = json.dumps({"candidates": [malformed]}) if first_failure == "schema" else "not JSON"
    provider = Provider([first_raw, json.dumps({"candidates": [candidate]})])
    result = await generate_missing_question_materials({"q1": row},
        [{key: candidate[key] for key in ("target_id", "q_id", "target")}], 3, provider,
        allow_partial=allow_partial)
    assert len(result) == 1 and len(provider.calls) == 2
    first, repaired = schemas(provider)
    assert repaired["$defs"]["RubricDefinition"] == {key: value for key, value in first.items() if key != "$defs"}
    assert repaired["$defs"]["PartRubric"] == first["$defs"]["PartRubric"]
    assert "part_rubric" in repaired["$defs"]["RubricDefinition"]["required"]
    assert "routes" not in repaired["$defs"]["RubricDefinition"]["properties"]
    assert "MathRubricRoute" not in repaired["$defs"]
    assert not {"id", "label", "stem"}.intersection(repaired["$defs"]["PartRubric"]["required"])
    assert TEACHER_RULE_INSTRUCTION in provider.calls[0][0].content
    assert result[0].rubric_draft["plan"]["part_rubric"]["id"] == "q1"


@pytest.mark.asyncio
async def test_written_repair_still_accepts_requested_non_scoring_sibling():
    math = make_row("列式3分，计算结果7分。")
    other = dict(q_id="q2", type="写作题", stem="Write a greeting.", max_score=10)
    candidates = [
        dict(target_id="q1:criterion", q_id="q1", target="criterion", text_value=math["criterion"],
             rubric_definition={"part_rubric": proposed()}),
        dict(target_id="q2:reference_answer", q_id="q2", target="reference_answer", text_value="Hello."),
    ]
    provider = Provider(["not JSON", json.dumps({"candidates": candidates})])
    result = await generate_missing_question_materials({"q1": math, "q2": other},
        [{key: candidate[key] for key in ("target_id", "q_id", "target")} for candidate in candidates], 3, provider)
    assert len(result) == 2 and len(provider.calls) == 2
    assert result[1].text_value == "Hello." and result[1].rubric_draft is None


@pytest.mark.asyncio
async def test_non_written_material_repair_keeps_legacy_compatible_shape():
    candidate = dict(target_id="q1:reference_answer", q_id="q1", target="reference_answer", text_value="Hello.")
    provider = Provider(["not JSON", json.dumps({"candidates": [candidate]})])
    result = await generate_missing_question_materials({"q1": dict(type="写作题", stem="Write a greeting.", max_score=10)},
        [{key: candidate[key] for key in ("target_id", "q_id", "target")}], 3, provider)
    assert result[0].text_value == "Hello." and len(provider.calls) == 2
    _, repaired = schemas(provider)
    assert "routes" in repaired["$defs"]["RubricDefinition"]["properties"]
    assert TEACHER_RULE_INSTRUCTION not in provider.calls[0][0].content


@pytest.mark.asyncio
@pytest.mark.parametrize("provenance", [False, True])
async def test_teacher_instruction_is_not_added_to_ai_authored_rules(provenance):
    row = make_row("列式3分，计算结果7分。" if provenance else "")
    if provenance:
        row["ai_completion_provenance"] = {"criterion": {"source_kind": "ai_generated"}}
    candidate = dict(target_id="q1:criterion", q_id="q1", target="criterion", text_value="AI rubric",
                     rubric_definition={"part_rubric": proposed()})
    provider = Provider([json.dumps({"candidates": [candidate]})])
    result = await generate_missing_question_materials({"q1": row},
        [{key: candidate[key] for key in ("target_id", "q_id", "target")}], 3, provider)
    assert len(result) == 1 and len(provider.calls) == 1
    assert TEACHER_RULE_INSTRUCTION not in provider.calls[0][0].content
