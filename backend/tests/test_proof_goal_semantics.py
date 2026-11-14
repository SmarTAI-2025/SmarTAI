"""Source-goal acceptance is a separate semantic decision, never quote matching alone."""
import json
import pytest
from backend.services import proof_goals as goals
from backend.tests.test_question_material_format_repair import Provider
from backend.tests.test_proof_judgment_review import material


def outline(targets):
    return goals.ProofOutline.model_validate({"parts": [{"part_id": "p", "goals": [
        {"target": text, "source_id": "question:1", "points": 10 / len(targets)} for text in targets]}]})


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["inconsistent", "uncertain"])
async def test_literal_givens_cannot_be_saved_as_requested_results(verdict):
    source = "Let x>0. Prove x*x>0."
    proposal = outline(["Let x>0.", "Prove x*x>0."])
    _, plan = material()
    # This proposal satisfies every deterministic quote/budget/coverage check.
    goals.compile_proof_goals(plan.part_rubric, proposal, {"p": {"question:1": source}}, language="en")
    provider = Provider([proposal.model_dump_json(), json.dumps({"parts": [{"part_id":"p",
        "verdict":verdict, "reason":"The first funded item is an assumption, not a requested result."}]})])
    with pytest.raises(goals.ProofGoalsUnverified):
        await goals.prepare_source_goals(provider, source, plan.part_rubric, language="en")
    assert len(provider.calls) == 2
    request = json.loads(provider.calls[1][-1].content)
    assert request["source"]["question_sources"] == {"question:1": source}
    assert request["proposal"] == proposal.model_dump(mode="json")


@pytest.mark.asyncio
async def test_independently_requested_dependent_goals_are_accepted():
    source = "Construct a witness. Use it to prove existence."
    proposal = outline(["Construct a witness.", "Use it to prove existence."])
    _, plan = material()
    provider = Provider([proposal.model_dump_json(), json.dumps({"parts":[{"part_id":"p",
        "verdict":"consistent", "reason":"Both are explicitly requested despite their dependence."}]})])
    accepted = await goals.prepare_source_goals(provider, source, plan.part_rubric, language="en")
    assert len(provider.calls) == 2 and len(accepted.parts[0].deductions) == 6
    assert {d.exclusive_group for d in accepted.parts[0].deductions} == {"goal1", "goal2"}


@pytest.mark.asyncio
async def test_wrong_audit_identity_is_not_approval_and_total_calls_are_bounded():
    _, plan = material()
    proposal = outline(["Prove x=x."])
    wrong = json.dumps({"parts":[{"part_id":"sibling", "verdict":"consistent", "reason":"ok"}]})
    provider = Provider([proposal.model_dump_json(), wrong, wrong])
    with pytest.raises(goals.ProofGoalsUnverified):
        await goals.prepare_source_goals(provider, "Prove x=x.", plan.part_rubric, language="en")
    assert len(provider.calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_partial", [False, True])
async def test_generation_never_adopts_an_unverified_goal_but_keeps_valid_siblings(allow_partial):
    from backend.agents.ingest_agent import generate_missing_question_materials
    from backend.agents.incremental_question_preparation import _rows
    source = "Let x>0. Prove x*x>0."
    proposal = outline(["Let x>0.", "Prove x*x>0."])
    raw = {"id":"r", "label":"1", "stem":source, "total_pts":10,
        "parts":[{"id":"p", "total_pts":10, "alternative":{"deduct_per_error":1},
                  "steps":[{"id":"s", "total_pts":10, "criterion":"Prove the requested result."}]}]}
    targets = [{"q_id":"q1", "target":key, "target_id":"q1:"+key} for key in ["reference_answer", "criterion"]]
    initial = {"candidates":[{**targets[0], "text_value":"Since x>0, multiplying by x gives x*x>0."},
                             {**targets[1], "rubric_definition":{"part_rubric":raw}}]}
    provider = Provider([json.dumps(initial), proposal.model_dump_json(), json.dumps({"parts":[{
        "part_id":"p", "verdict":"inconsistent", "reason":"The first item is a given."}]})])
    rows = _rows([dict(number="1", type="推理题", stem=source, max_score=10)])
    feedback = {}
    if allow_partial:
        result = await generate_missing_question_materials(rows, targets, 1, provider,
            allow_partial=True, repair_feedback=feedback)
        assert [candidate.target for candidate in result] == ["reference_answer"]
        assert "q1:criterion" in feedback
    else:
        with pytest.raises(goals.ProofGoalsUnverified):
            await generate_missing_question_materials(rows, targets, 1, provider)
    assert len(provider.calls) == 3
