"""Real generation/parsing/persistence contracts with fixed provider responses.

These tests prove call counts and policy preservation, not real AI accuracy.
"""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agents.ingest_agent import generate_missing_question_materials
from backend.models import ProblemInfo
from backend.services.math_grading_context import prepare_math_plans
from backend.services.question_preparation_artifacts import ArtifactProblemV1
from backend.services.rubric_drafts import RubricDefinition, generated_rubric, saved_draft
from backend.tests.test_math_rubric_plan import ReplayProvider, explanation, question


def choice_question(**changes):
    return dict(q_id="q1", number="1", type="计算题", stem="计算2+2，选出结果：A.3 B.4 C.5 D.6",
                criterion="", max_score=4, reference_answer="", **changes)


def choice_definition():
    return dict(routes=[dict(id="answer", description="正确选项及结果", applies_when="所有作答",
        mode="points", items=[dict(id="option", description="选择B（4）得4分，否则0分；不要求过程",
        points=4, source="ai", source_quote="", default_adjustments=[])])],
        follow_through="只要求选项，无过程分。", numerical_policy="结果为4。", advisories=[])


async def generate_choice():
    provider = ReplayProvider({"candidates": [
        dict(target_id="q1:criterion", q_id="q1", target="criterion", rubric_definition=choice_definition()),
        dict(target_id="q1:reference_answer", q_id="q1", target="reference_answer", text_value="B，结果为4。"),
    ]})
    candidates = await generate_missing_question_materials({"q1": choice_question()}, [
        {"target_id": f"q1:{key}", "q_id": "q1", "target": key} for key in ("criterion", "reference_answer")
    ], 6, provider, generation_language="zh", allow_partial=True)
    assert len(provider.messages) == 1
    assert len(candidates) == 2
    row = choice_question()
    for candidate in candidates:
        row[candidate.target] = candidate.text_value
        row.setdefault("ai_completion_provenance", {})[candidate.target] = {"source_kind": "ai_generated"}
        if candidate.rubric_draft:
            row["rubric_draft"] = candidate.rubric_draft
    return row


@pytest.mark.asyncio
async def test_generated_response_round_trip_binds_without_any_grading_preparation_call():
    row = await generate_choice()
    row = ArtifactProblemV1.model_validate_json(ArtifactProblemV1(**row).model_dump_json()).model_dump(mode="json")
    assert "选择B" in row["criterion"] and row["reference_answer"] == "B，结果为4。"
    registry = SimpleNamespace(pick_default=AsyncMock(side_effect=AssertionError("No second compiler")))
    for strictness in (0, 50, 100):
        plans = await prepare_math_plans({"q1": row}, registry,
            grading_setup={"strictness": strictness, "allow_partial_credit": True})
        assert plans["q1"].strictness == strictness
        assert plans["q1"].routes[0].items[0].points == 4
        assert plans["q1"].effective_criterion.count("Authenticated teacher grading preferences:") == 1
    registry.pick_default.assert_not_called()


@pytest.mark.asyncio
async def test_content_changes_invalidate_but_renumbering_and_display_settings_do_not():
    row = await generate_choice()
    def problem(value):
        parsed = ProblemInfo.model_validate(value)
        parsed._math_rubric_source = {"presentation": {
            "rubric_draft": row["rubric_draft"], "ai_completion_provenance": row["ai_completion_provenance"],
        }}
        return parsed
    original = problem(row)
    for setup in ({"strictness": 0}, {"allow_partial_credit": False}, {"feedback_language": "en"},
                  {"feedback_tone": "encouraging", "feedback_detail": "brief"}, {"expert_ids": ["new"]}):
        assert saved_draft(original, grading_setup=setup) is not None
    assert saved_draft(problem({**row, "q_id": "q5", "number": "第五题"})) is not None
    for key, value in (("stem", "Choose 3+3."), ("reference_answer", "C"), ("criterion", "每错扣1分"),
                       ("max_score", 8), ("type", "概念题")):
        assert saved_draft(problem({**row, key: value})) is None
    assert saved_draft(original, grading_setup={"teacher_notes": "漏选得2分"}) is None


def test_complete_teacher_rubric_is_adopted_and_source_rules_are_not_rewritten():
    row = question()
    definition = RubricDefinition.model_validate({k: v for k, v in explanation().items() if k != "effective_criterion"})
    text, draft = generated_rubric(row, definition)
    assert text == row["criterion"]
    assert draft.plan.original_criterion == row["criterion"]
    assert [item.source for item in draft.plan.routes[0].items] == ["provided", "provided"]
    changed = definition.model_dump()
    changed["routes"][0]["items"][0]["points"] = 5
    with pytest.raises(ValueError):
        generated_rubric(row, RubricDefinition.model_validate(changed))


@pytest.mark.asyncio
async def test_manual_question_compiles_once_then_rebinds_changed_strictness():
    provider = ReplayProvider(explanation())
    raw = question()
    registry = SimpleNamespace(pick_default=lambda: provider)
    first = (await prepare_math_plans({"q1": raw}, registry, grading_setup={"strictness": 0}))["q1"]
    second = (await prepare_math_plans({"q1": raw}, registry, grading_setup={"strictness": 100}))["q1"]
    assert len(provider.messages) == 1
    assert first.strictness == 0 and second.strictness == 100
    assert first.plan_id != second.plan_id and first.routes == second.routes
    assert '"strictness"' not in provider.messages[0][1].content


from backend.tests.test_task_grading_retry import case, start  # noqa: F401
from backend.tests.test_grading_checkpoints import prepare, stop


@pytest.mark.asyncio
async def test_concurrent_preparation_persists_once_and_reuses_after_configuration_change(case, monkeypatch):
    import asyncio
    from backend.db import assignment_repository, workflow_repository
    from backend.services import grading_adapter, math_rubric_plan

    calls = []
    async def compile_plan(problem, provider, **kwargs):
        calls.append(problem.q_id)
        await asyncio.sleep(0)
        return math_rubric_plan.fallback_math_rubric_plan(
            problem, reason="model_selected_holistic", bind_preferences=False)
    monkeypatch.setattr(math_rubric_plan, "prepare_math_rubric_plan", compile_plan)
    registry = SimpleNamespace(pick_default=lambda: SimpleNamespace(ainvoke=AsyncMock()))
    run_id = start(case, "draft-cache")["job_id"]
    checkpoints, questions, _, _ = prepare(case, run_id)
    versions = {q.q_id: q.version for q in questions}
    async def load():
        return await checkpoints.run(prepare_math_plans(
            grading_adapter.build_problem_store(questions), registry))
    first, concurrent = await asyncio.gather(load(), load())
    assert calls == ["q1", "q2"]
    assert first == concurrent
    persisted = assignment_repository.get_questions_by_assignment(case[0])
    assert all(q.source["presentation"]["rubric_draft"] for q in persisted)
    assert {q.q_id: q.version for q in persisted} == versions
    stop(case, run_id)
    flow = workflow_repository.get_live_workflow(case[0], owner_id=case[1])
    workflow_repository.update_workflow(case[0], owner_id=case[1], grading_setup={
        **flow.grading_setup, "strictness": 100,
    })
    next_id = start(case, "changed-strictness")["job_id"]
    other, questions, _, _ = prepare(case, next_id)
    rebound = await other.run(prepare_math_plans(
        grading_adapter.build_problem_store(questions), registry, grading_setup={"strictness": 100}))
    assert calls == ["q1", "q2"]
    assert all(plan.strictness == 100 for plan in rebound.values())


@pytest.mark.asyncio
async def test_stop_during_compilation_cannot_write_a_draft(case, monkeypatch):
    from backend.db import assignment_repository
    from backend.domain.errors import LeaseLost
    from backend.services import grading_adapter, math_rubric_plan

    run_id = start(case, "stop-before-cache")["job_id"]
    checkpoints, questions, _, _ = prepare(case, run_id)
    async def compile_plan(problem, provider, **kwargs):
        stop(case, run_id)
        return math_rubric_plan.fallback_math_rubric_plan(
            problem, reason="model_selected_holistic", bind_preferences=False)
    monkeypatch.setattr(math_rubric_plan, "prepare_math_rubric_plan", compile_plan)
    with pytest.raises(LeaseLost):
        await checkpoints.run(prepare_math_plans(grading_adapter.build_problem_store(questions),
            SimpleNamespace(pick_default=lambda: SimpleNamespace(ainvoke=AsyncMock()))))
    assert all(not (q.source.get("presentation") or {}).get("rubric_draft")
               for q in assignment_repository.get_questions_by_assignment(case[0]))
