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


async def generate_choice(kind="计算题", definition=None):
    provider = ReplayProvider({"candidates": [
        dict(target_id="q1:criterion", q_id="q1", target="criterion", rubric_definition=definition or choice_definition()),
        dict(target_id="q1:reference_answer", q_id="q1", target="reference_answer", text_value="B，结果为4。"),
    ]})
    raw = {**choice_question(), "type": kind}
    candidates = await generate_missing_question_materials({"q1": raw}, [
        {"target_id": f"q1:{key}", "q_id": "q1", "target": key} for key in ("criterion", "reference_answer")
    ], 6, provider, generation_language="zh", allow_partial=True)
    assert len(provider.messages) == 1
    assert len(candidates) == 2
    row = raw
    for candidate in candidates:
        row[candidate.target] = candidate.text_value
        row.setdefault("ai_completion_provenance", {})[candidate.target] = {"source_kind": "ai_generated"}
        if candidate.rubric_draft:
            row["rubric_draft"] = candidate.rubric_draft
    return row


@pytest.mark.asyncio
@pytest.mark.parametrize("batched", [False, True])
@pytest.mark.parametrize("order", [("criterion", "reference_answer"), ("reference_answer", "criterion")])
async def test_incremental_completion_keeps_draft_matching_final_materials(monkeypatch, order, batched):
    from backend.agents import incremental_question_preparation as inc
    from backend.tests.test_incremental_question_preparation import setup, persisted

    owner, _, operation, pipeline = await setup(monkeypatch)
    raw = inc._rows([choice_question()])["q1"]
    monkeypatch.setattr(pipeline, "_project_base", lambda: {"q1": deepcopy(raw)})
    definition = choice_definition()
    candidates = [dict(
        target_id=f"q1:{target}", q_id="q1", target=target,
        text_value="B，结果为4。" if target == "reference_answer" else "",
        rubric_definition=definition,
    ) for target in order]
    responses = ([{"candidates": candidates}] if batched else [{"candidates": [candidate]} for candidate in candidates])
    provider = ReplayProvider(*responses)
    pipeline.provider = provider
    targets = [dict(target_id=f"q1:{target}", q_id="q1", target=target) for target in order]
    identity = inc._base_identity(raw)
    await pipeline._generate("generation:" + identity, identity, raw, targets)
    assert not pipeline.failures, pipeline.failures
    assert len(provider.messages) == (1 if batched else 2)
    _, rows = persisted(owner, operation)
    assert rows["q1"]["reference_answer"] == "B，结果为4。"
    assert "选择B" in rows["q1"]["criterion"]
    registry = SimpleNamespace(pick_default=AsyncMock(side_effect=AssertionError("No repeated compilation")))
    assert saved_draft(rows["q1"]) is not None
    plans = await prepare_math_plans(rows, registry)
    assert plans["q1"].original_criterion == rows["q1"]["criterion"]
    registry.pick_default.assert_not_called()
    # Rehydration/arrival order cannot select a different stale field snapshot.
    pipeline.units = dict(reversed(list(pipeline.units.items())))
    assert saved_draft(pipeline.project()["q1"]) == saved_draft(rows["q1"])
    # Never make an obsolete plan current by merely updating its fingerprint.
    old_stem = raw["stem"]
    raw["stem"] += "请另外说明理由。"
    pipeline._base_cache_key = None
    assert saved_draft(pipeline.project()["q1"]) is None
    raw["stem"] = old_stem


@pytest.mark.asyncio
@pytest.mark.parametrize("criterion,text,correct,expected", [
    ("全部选对得4分，否则0分。例外：少选且无错选得两分。", "A", False, 2),
    ("全部选对得4分，否则0分。例外：少选且无错选得2分。", "A", False, 2),
    ("全部选对得四分，否则零分。少选但没有错误选项得两分。", "A", False, 2),
    ("少选且无错选得两分。全部选对得4分，否则0分。", "A", False, 2),
    ("全部选对得4分，否则0分。乙类作答按另一张表给分。", "A", False, 2),
    ("每空答对得两分，否则零分。两空分别计分。", "4；5", False, 2),
    ("第一空答案正确得2分，否则0分。第二空答案正确得2分，否则0分。", "4；5", False, 2),
    ("选项正确得两分，否则零分。解释正确得两分。", "A；解释正确", False, 2),
    ("答案正确得满分，否则得零分。", "AB", True, 4),
    ("答案正确得满分，否则得零分。", "A", False, 0),
    ("全部选对得四分，否则得零分。", "AB", True, 4),
    ("全部选对得四分，否则得零分。", "A", False, 0),
])
async def test_teacher_policy_scope_survives_generation_persistence_reuse_and_grading(
    monkeypatch, criterion, text, correct, expected,
):
    from backend.models import StudentAnswerInfo
    from backend.services.math_grading_context import math_plans_context
    from backend.skills import math_grading
    from backend.skills.concept import ConceptSkill

    monkeypatch.setattr(math_grading.kb_tool, "retrieve_for_grading", AsyncMock(return_value=[]))
    monkeypatch.setattr(ConceptSkill, "_verify", AsyncMock(return_value=[]))
    row = {**choice_question(), "type": "概念题", "criterion": criterion,
           "material_provenance": {"criterion": {"source_kind": "teacher_provided"}}}
    definition = dict(routes=[dict(id="teacher", description="教师完整政策", applies_when="所有作答",
        mode="holistic")], follow_through="遵循教师原文及其例外。", numerical_policy="遵循教师原文。")
    generator = ReplayProvider({"candidates": [dict(target_id="q1:reference_answer", q_id="q1",
        target="reference_answer", text_value="AB", rubric_definition=definition)]})
    candidates = await generate_missing_question_materials({"q1": row}, [dict(
        target_id="q1:reference_answer", q_id="q1", target="reference_answer")], 6, generator,
        generation_language="zh", allow_partial=True)
    assert len(candidates) == 1 and candidates[0].rubric_draft
    row.update(reference_answer="AB", rubric_draft=candidates[0].rubric_draft,
               ai_completion_provenance={"reference_answer": {"source_kind": "ai_generated"}})
    row = ArtifactProblemV1.model_validate_json(ArtifactProblemV1(**row).model_dump_json()).model_dump(mode="json")
    assert row["criterion"] == criterion and saved_draft(row) is not None
    registry = SimpleNamespace(pick_default=AsyncMock(side_effect=AssertionError("No second compiler")))
    plans = await prepare_math_plans({"q1": row}, registry)
    provider = ReplayProvider(dict(route_id="teacher", route_reason="应用完整教师政策", score=2,
        comment="逐项核对", rationale="按教师规则计分", confidence=.9, claims=[dict(
            id="s1", quote=text, verdict="supported" if correct else "refuted", reason="判断是否全部正确")]))
    with math_plans_context(plans):
        result = await ConceptSkill(provider).grade(ProblemInfo.model_validate(row),
            StudentAnswerInfo(q_id="q1", number="1", type="概念题", content=text))
    assert not result.error_kind
    assert result.score == expected == sum(step.score for step in result.steps)
    assert len(generator.messages) == len(provider.messages) == 1
    registry.pick_default.assert_not_called()



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
        from backend.services.math_grading_context import get_math_plan, math_plans_context, problem_from_question
        persisted = SimpleNamespace(**{k: row.get(k) for k in (
            "q_id", "number", "type", "stem", "criterion", "reference_answer", "test_cases", "max_score",
        )}, order_index=0, source={"presentation": {
            "rubric_draft": row["rubric_draft"], "ai_completion_provenance": row["ai_completion_provenance"],
        }})
        with math_plans_context(plans):
            assert get_math_plan(problem_from_question(persisted)).plan_id == plans["q1"].plan_id
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
    versions = {q.q_id: (q.version, q.updated_at) for q in questions}
    async def load():
        return await checkpoints.run(prepare_math_plans(
            grading_adapter.build_problem_store(questions), registry))
    first, concurrent = await asyncio.gather(load(), load())
    assert calls == ["q1", "q2"]
    assert first == concurrent
    persisted = assignment_repository.get_questions_by_assignment(case[0])
    assert all(q.source["presentation"]["rubric_draft"] for q in persisted)
    assert {q.q_id: (q.version, q.updated_at) for q in persisted} == versions
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


@pytest.mark.asyncio
@pytest.mark.parametrize("definition", [{"routes": "broken"}, {**choice_definition(), "routes": [{**choice_definition()["routes"][0], "items": [{**choice_definition()["routes"][0]["items"][0], "points": 3}]}]}])
async def test_optional_broken_definition_preserves_valid_reference_answer(definition):
    raw = choice_question()
    raw["criterion"] = "正确选项得4分，其他0分。"
    provider = ReplayProvider({"candidates": [dict(target_id="q1:reference_answer", q_id="q1",
        target="reference_answer", text_value="B，4。", rubric_definition=definition)]})
    result = await generate_missing_question_materials({"q1": raw}, [dict(
        target_id="q1:reference_answer", q_id="q1", target="reference_answer")], 6, provider,
        generation_language="zh", allow_partial=True)
    assert len(result) == 1 and result[0].text_value == "B，4。"
    if isinstance(definition["routes"], str):
        assert result[0].rubric_draft is None
    else:
        draft = result[0].rubric_draft
        assert draft["plan"]["original_criterion"] == raw["criterion"]
        assert draft["plan"]["compilation_status"] == "holistic"
        assert not draft["plan"]["routes"][0]["items"]
        completed = {**raw, "reference_answer": result[0].text_value, "rubric_draft": draft,
                     "ai_completion_provenance": {"reference_answer": {"source_kind": "ai_generated"}}}
        plans = await prepare_math_plans({"q1": completed},
            SimpleNamespace(pick_default=AsyncMock(side_effect=AssertionError("No repeated interpretation"))))
        assert plans["q1"].original_criterion == raw["criterion"]
    assert len(provider.messages) == 1


@pytest.mark.asyncio
async def test_wrong_target_identity_cannot_drop_a_valid_sibling():
    provider = ReplayProvider({"candidates": [
        dict(target_id="q1:criterion", q_id="unrelated", target="criterion", rubric_definition=choice_definition()),
        dict(target_id="q1:reference_answer", q_id="q1", target="reference_answer", text_value="B，4。"),
    ]})
    result = await generate_missing_question_materials({"q1": choice_question()}, [dict(
        target_id=f"q1:{field}", q_id="q1", target=field) for field in ("criterion", "reference_answer")],
        6, provider, generation_language="zh", allow_partial=True)
    assert [candidate.target for candidate in result] == ["reference_answer"]
    assert len(provider.messages) == 1


@pytest.mark.asyncio
async def test_generation_receives_the_complete_teacher_policy_tail():
    raw = choice_question()
    raw["criterion"] = "教师背景说明。" * 1500 + "末尾政策：选错不得分，禁止追加过程分。"
    provider = ReplayProvider({"candidates": [dict(target_id="q1:reference_answer", q_id="q1",
        target="reference_answer", text_value="B，4。") ]})
    await generate_missing_question_materials({"q1": raw}, [dict(target_id="q1:reference_answer", q_id="q1",
        target="reference_answer")], 6, provider, generation_language="zh", allow_partial=True)
    assert raw["criterion"] in provider.messages[0][1].content


@pytest.mark.asyncio
async def test_caching_a_draft_does_not_make_finished_inputs_new_billable_work(case, monkeypatch):
    from backend.db import grading_repository as grades, workflow_repository as workflows
    from backend.models import Correction
    from backend.services import grading_adapter, math_rubric_plan, task_facade

    async def compile_plan(problem, provider, **kwargs):
        return math_rubric_plan.fallback_math_rubric_plan(
            problem, reason="model_selected_holistic", bind_preferences=False)
    monkeypatch.setattr(math_rubric_plan, "prepare_math_rubric_plan", compile_plan)
    run_id = start(case, "metadata-idempotency")["job_id"]
    checkpoints, questions, revisions, _ = prepare(case, run_id)
    await checkpoints.run(prepare_math_plans(grading_adapter.build_problem_store(questions),
        SimpleNamespace(pick_default=lambda: SimpleNamespace(ainvoke=AsyncMock()))))
    for _, student in revisions:
        for q in questions:
            checkpoints.save_correction(student, Correction(q_id=q.q_id, type=q.type,
                score=8, max_score=10, confidence=1, comment="Synthetic accepted answer", steps=[]))
    grades.mark_completed(run_id, worker_id="checkpoint-test", completed=len(questions)*len(revisions), failed=0)
    flow = workflows.get_live_workflow(case[0], owner_id=case[1])
    result = task_facade.start_task_grading(task_id=case[0], owner_id=case[1],
        expected_workflow_revision=flow.workflow_revision)
    assert result["status"] == "already_done"
    assert len(grades.list_runs_for_assignment(case[0], actor_id=case[1])) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["计算题", "概念题"])
@pytest.mark.parametrize("correct", [False, True])
@pytest.mark.parametrize("bare_rule", [False, True])
async def test_generated_binary_rubric_enforces_zero_or_full(monkeypatch, kind, correct, bare_rule):
    from backend.services.math_grading_context import math_plans_context
    from backend.skills.base import get_skill_for_type
    from backend.skills import math_grading
    from backend.models import StudentAnswerInfo

    monkeypatch.setattr(math_grading.kb_tool, "retrieve_for_grading", AsyncMock(return_value=[]))
    definition = choice_definition()
    if bare_rule:
        definition["routes"][0]["items"][0]["description"] = "选B得4分，否则0分"
    row = await generate_choice(kind, definition)
    registry = SimpleNamespace(pick_default=AsyncMock(side_effect=AssertionError("No second compiler")))
    plans = await prepare_math_plans({"q1": row}, registry)
    problem = ProblemInfo.model_validate(row)
    problem._math_rubric_source = {"presentation": {"ai_completion_provenance": row["ai_completion_provenance"]}}
    selected = "B" if correct else "A"
    provider = ReplayProvider(dict(route_id="answer", route_reason="按唯一选项计分", comment="按选项计分",
        claims=[dict(id="s1", quote=selected, verdict="supported" if correct else "refuted",
                     reason="选项正确" if correct else "选项错误", item_ids=["option"])],
        awards=[dict(item_id="option", score=2, claim_ids=["s1"], reason="模型错误给出中间分")]))
    with math_plans_context(plans):
        result = await get_skill_for_type(kind)(provider).grade(problem,
            StudentAnswerInfo(q_id="q1", number="1", type=kind, content=selected))
    assert not result.error_kind
    assert result.score == (4 if correct else 0)
    assert sum(step.score for step in result.steps) == result.score
    assert len(provider.messages) == 1
    registry.pick_default.assert_not_called()
