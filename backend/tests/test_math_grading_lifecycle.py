"""Frozen rubric plans survive retries without mixing old scoring engines."""
import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError

from backend.db import grading_repository as grades, workflow_repository as workflows
from backend.db.models import AssignmentQuestionRecord, GradingRunEventRecord
from backend.db.session import session_scope
from backend.domain.errors import VersionConflict
from backend.models import Correction, ExpertResult, ProblemInfo, StepScore, TaskGradingSetup
from backend.services import grading_adapter, math_grading_context as context
from backend.services import math_rubric_plan as rubric
from backend.services import task_facade
from backend.services.grading_checkpoints import CheckpointWriteError, MATH_TRACE_EVENT, saved_experts
from backend.tests.test_grading_checkpoints import prepare, retry, stop
from backend.tests.test_postgres_integration import pg_database  # noqa: F401
from backend.tests.test_task_grading_retry import case, finish, start  # noqa: F401


def problem(**changes):
    return ProblemInfo(
        **{"q_id": "q1", "number": "1", "type": "计算题", "stem": "Evaluate 2+2.",
           "criterion": "Method 8, result 2.", "max_score": 10, **changes},
    )


def registry():
    provider = SimpleNamespace(provider_id="chosen", model="fake", ainvoke=lambda *_: None)
    return SimpleNamespace(pick_default=lambda: provider, list_available=lambda: [provider])


def old_run(run_id):
    """Represent a real pre-upgrade run, without fabricating a current plan."""
    with session_scope() as session:
        row = session.get(workflows.GradingRunSetupRecord, run_id)
        row.input_manifest = {
            key: value for key, value in row.input_manifest.items()
            if key not in {"math_grading_engine_version", "math_rubric_plans", "math_rubric_input_fingerprints"}
        }
        session.execute(delete(GradingRunEventRecord).where(
            GradingRunEventRecord.grading_run_id == run_id,
            GradingRunEventRecord.message.in_([context.ENGINE_EVENT, context.PLAN_EVENT]),
        ))
        for event in session.scalars(select(GradingRunEventRecord).where(
            GradingRunEventRecord.grading_run_id == run_id,
            GradingRunEventRecord.message == "grading_expert_checkpoint",
        )):
            event.payload = {
                key: value for key, value in event.payload.items()
                if key not in {"math_grading_engine_version", "math_plan_id"}
            }


def final_math_correction(question, plan, provider_id="provider"):
    from backend.skills.math_grading import MathAssessment, TRACE_VERSION

    original = MathAssessment(route_id=plan.routes[0].id, route_reason="Original rubric",
        claims=[{"id": "s1", "quote": "4", "verdict": "supported", "reason": "Stated result."}],
        score=8, confidence=.9, rationale="Original scoring evidence.", comment="Original.")
    revised = original.model_copy(update={"score": 9, "rationale": "Rechecked scoring evidence."})
    trace = {"version": TRACE_VERSION, "plan_id": plan.plan_id,
             "assessment": revised.model_dump(mode="json"),
             "original_assessment": original.model_dump(mode="json"),
             "evidence": [{"claim_id": "s1", "status": "unknown"}],
             "targeted_review": {"provider": "reviewer", "reason": "Credit follows the teacher rule."}}
    step = StepScore(step_no=1, desc=revised.rationale, score=9, is_correct=False)
    experts = [ExpertResult(provider=f"model#sample{index}", provider_id=provider_id,
        score=9, max_score=10, confidence=.9, comment="Rechecked.", steps=[step],
        raw_output=json.dumps(trace)) for index in (1, 2)]
    return Correction(q_id=question.q_id, type=question.type, score=9, max_score=10,
        confidence=.9, comment="Rechecked.", steps=[step], expert_results=experts,
        synthesis_method="multi_sample")


@pytest.mark.asyncio
@pytest.mark.parametrize("samples", [1, 2])
@pytest.mark.parametrize("residual_concern", [True, False])
async def test_actual_math_review_advisory_survives_projection_and_teacher_confirmation(
    case, monkeypatch, samples, residual_concern,
):
    from backend.agents.grading_agent import _grade_single_answer
    from backend.domain.grading_review import review_state
    from backend.models import StudentAnswerInfo
    from backend.skills import math_grading as skill
    from backend.tests.test_multi_expert_robustness import _FakeProvider, _FakeRegistry

    run_id = start(case, f"residual-review-{samples}-{residual_concern}")["job_id"]
    checkpoints, questions, revisions, setup = prepare(case, run_id)
    question, student = questions[0], revisions[0][1]
    p = context.problem_from_question(question)
    plan = rubric.fallback_math_rubric_plan(p)
    checkpoints.save_math_plan(p, plan)
    draft = skill.MathAssessment(
        route_id="holistic", route_reason="Use the whole original rubric.",
        claims=[skill.Claim(id="s1", quote="4", verdict="supported", reason="The result is stated.")],
        score=8, confidence=.95, rationale="The conclusion earns credit; the omitted condition needs checking.",
        comment="8 points; check the omitted condition.", needs_review=True,
    )
    reviewed = draft.model_copy(update={"needs_review": residual_concern})
    calls = []

    async def model_call(_provider, **kwargs):
        calls.append(kwargs["output_model"])
        if kwargs["output_model"] is skill.ExpertReview:
            result = skill.ExpertReview(revisions=[skill.ExpertRevision(
                expert_index=index, source_quote="4", reason="Checked the stated conclusion.",
                assessment=reviewed,
            ) for index in range(samples)])
        else:
            assert kwargs["output_model"] is skill.MathAssessment
            result = draft if len(calls) <= samples else reviewed
        return result.model_copy(deep=True), SimpleNamespace(content="{}", duration_ms=1)

    monkeypatch.setattr(skill, "structured_llm_call", model_call)
    monkeypatch.setattr(skill.kb_tool, "retrieve_for_grading", AsyncMock(return_value=[]))
    provider = _FakeProvider(setup.setup["selected_provider_ids"][0])
    with context.math_plans_context({p.q_id: plan}):
        correction = await _grade_single_answer(
            problem=p, answer=StudentAnswerInfo(q_id=p.q_id, number=p.number, type=p.type, content="4"),
            student_id=student, registry=_FakeRegistry([provider]), multi_sample_n=samples,
            aggregation_method="weighted_average",
        )
    assert len(calls) == samples + 1
    assert correction.score == 8 and correction.confidence == .95
    assert correction.review_reasons == (["math_model_review"] if residual_concern else [])
    checkpoints.save_correction(student, correction)
    grades.mark_completed(run_id, worker_id="checkpoint-test", completed=1, failed=1)
    result, = grades.list_results_for_run(run_id)
    public = task_facade._serialize_correction(result)
    assert public["score"] == 8 and public["confidence"] == .95
    assert all(expert["raw_output"] is None for expert in public["expert_results"])
    assert review_state(public) == (residual_concern, False)
    assert not task_facade._correction_review_blocks_finalization(result)
    response = task_facade.update_correction_review(
        task_id=case[0], owner_id=case[1], display_student_id=student, q_id=p.q_id,
        teacher_score=8, teacher_comment=correction.comment, confirm=True,
        expected_revision=workflows.get_live_workflow(case[0], owner_id=case[1]).workflow_revision,
    )
    assert review_state(response["correction"]) == (False, True)
    assert response["correction"]["initial_review_reasons"] == public["initial_review_reasons"]


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("ai_original", [True, False])
def test_question_advisory_projects_once_without_mutating_source_or_input_identity(case, language, ai_original):
    flow = workflows.get_live_workflow(case[0], owner_id=case[1])
    workflows.update_workflow(case[0], owner_id=case[1], grading_setup={
        **flow.grading_setup, "feedback_language": language,
    })
    with session_scope() as session:
        question = session.scalar(select(AssignmentQuestionRecord).where(
            AssignmentQuestionRecord.assignment_id == case[0], AssignmentQuestionRecord.q_id == "q1",
        ))
        source = deepcopy(question.source or {})
        presentation = source.setdefault("presentation", {})
        presentation.update(review_status="confirmed", max_score_review_status="confirmed")
        if ai_original:
            presentation["ai_completion_provenance"] = {"criterion": {"source_kind": "ai_generated"}}
        question.source = source
    run_id = start(case, f"rubric-advice-{language}-{ai_original}")["job_id"]
    checkpoints, questions, _, frozen = prepare(case, run_id)
    original_manifest = deepcopy(frozen.input_manifest)
    q = questions[0]
    original_source = deepcopy(q.source)
    p = context.problem_from_question(q)
    plan = rubric.fallback_math_rubric_plan(p)
    checkpoints.save_math_plan(p, plan)

    public = task_facade.get_task(task_id=case[0], owner_id=case[1])
    projected = public["problem_data"][q.q_id]
    advisory, = [item for item in projected["preparation_issues"] if item["code"] == "math_rubric_advisory"]
    assert projected["review_status"] == "confirmed"
    assert advisory["severity"] == "warning" and advisory["field"] == "rubric"
    message = advisory["details"]["message"]
    if language == "zh":
        assert "建议核对本题评分标准" in message
        assert "无需额外确认" not in message and "AI interpretation:" not in message
    else:
        assert "review the rubric" in message
        assert "does not require confirmation" not in message
    assert set(advisory["details"]) == {"message"}
    assert not task_facade._math_rubric_advisory(q, plan.model_copy(update={"advisories": ()}), language)
    assert public["problem_data"][questions[1].q_id]["preparation_issues"] == []
    assert task_facade._run_snapshot_payload(task_id=case[0], owner_id=case[1], run_id=run_id)["problem_data"][q.q_id] == projected
    assert workflows.get_run_setup(run_id).input_manifest == original_manifest
    assert q.source == original_source
    assert rubric.math_rubric_fingerprint(context.problem_from_question(q)) == plan.input_fingerprint
    with session_scope() as session:
        assert session.get(AssignmentQuestionRecord, q.id).source == original_source
    assert task_facade._question_review_blockers(questions) == []


@pytest.mark.parametrize("change", ["question", "provenance", "setup", "run", "old_engine"])
def test_rubric_advisory_never_leaks_from_obsolete_inputs_or_engine(case, change):
    run_id = start(case, f"rubric-advice-identity-{change}")["job_id"]
    checkpoints, questions, _, _ = prepare(case, run_id)
    q = questions[0]
    plan = rubric.fallback_math_rubric_plan(context.problem_from_question(q))
    checkpoints.save_math_plan(context.problem_from_question(q), plan)
    if change in {"question", "provenance"}:
        with session_scope() as session:
            row = session.get(AssignmentQuestionRecord, q.id)
            if change == "question":
                row.criterion += " Only the final result earns credit."
            else:
                source = deepcopy(row.source or {})
                source.setdefault("presentation", {})["ai_completion_provenance"] = {"criterion": {}}
                row.source = source
    elif change == "setup":
        flow = workflows.get_live_workflow(case[0], owner_id=case[1])
        workflows.update_workflow(case[0], owner_id=case[1], grading_setup={
            **flow.grading_setup, "teacher_notes": "Different grading preference.",
        })
    elif change == "run":
        workflows.update_workflow(case[0], owner_id=case[1], grading_job_id=None, presentation_status="grading_setup")
    else:
        old_run(run_id)
    public = task_facade.get_task(task_id=case[0], owner_id=case[1])
    assert not any(item["code"] == "math_rubric_advisory"
                   for item in public["problem_data"][q.q_id]["preparation_issues"])
    if change != "old_engine":
        snapshot = task_facade._run_snapshot_payload(task_id=case[0], owner_id=case[1], run_id=run_id)
        assert any(item["code"] == "math_rubric_advisory"
                   for item in snapshot["problem_data"][q.q_id]["preparation_issues"])


def test_question_advisory_explains_actual_ai_additions_and_distinct_scoring_routes():
    p = problem()
    plan = rubric._build_plan(rubric._question_input(p), rubric._Explanation(
        effective_criterion="按原有过程8分、结果2分；替代解法采用单独约定的扣分方法。",
        routes=(
            rubric.MathRubricRoute(id="main", description="原有解法", applies_when="适用原有评分项",
                mode="points", items=(
                    rubric.MathRubricItem(id="method", description="过程", points=8, source="provided"),
                    rubric.MathRubricItem(id="result", description="结果", points=2, source="provided"),
                )),
            rubric.MathRubricRoute(id="alternative", description="替代方法", applies_when="另一种有效解法",
                mode="deductions", minimum_score=6, minimum_condition="nonblank",
                items=(rubric.MathRubricItem(id="error", description="独立错误", points=1, source="ai"),)),
        ),
        follow_through="同一错误只在首次发生处扣分。",
        numerical_policy="必须保留单位。",
        advisories=("替代方法规则由 AI 补充，请按需修改原标准。",),
    ))
    question = SimpleNamespace(q_id=p.q_id, source={})
    issue, = task_facade._math_rubric_advisory(question, plan, "zh")
    message = issue["details"]["message"]
    assert message == plan.advisories[0]
    assert "input_fingerprint" not in message and plan.plan_id not in message


def test_public_projection_hides_only_math_trace_without_mutating_expert():
    question = SimpleNamespace(q_id="q1", id="question", type="计算题", max_score=10)
    correction = final_math_correction(question, rubric.fallback_math_rubric_plan(problem()))
    original = correction.model_dump(mode="json")
    ordinary = correction.expert_results[0].model_copy(update={"raw_output": "ordinary model output"})
    correction.expert_results.append(ordinary)
    result = grading_adapter.correction_to_result(
        run_id="run", revision_id="revision", question=question, student_id="student",
        correction=correction,
    )
    assert [expert["raw_output"] for expert in result.ai_expert_results] == [
        None, None, "ordinary model output",
    ]
    assert correction.expert_results[0].model_dump(mode="json") == original["expert_results"][0]
    assert result.ai_score == correction.score
    assert result.ai_steps == [step.model_dump() for step in correction.steps]


def test_final_math_trace_is_private_and_preserves_revised_samples(case):
    from backend.progress.grading_activity import public_event

    run_id = start(case, "final-math-trace")["job_id"]
    checkpoints, questions, revisions, setup = prepare(case, run_id)
    question, student = questions[0], revisions[0][1]
    plan = rubric.fallback_math_rubric_plan(context.problem_from_question(question))
    checkpoints.save_math_plan(context.problem_from_question(question), plan)
    correction = final_math_correction(question, plan, setup.setup["selected_provider_ids"][0])
    checkpoints.save_correction(student, correction)
    event, = [row for row in grades.list_events(run_id) if row["message"] == MATH_TRACE_EVENT]
    assert public_event(event) is None
    assert event["payload"]["math_grading_engine_version"] == context.MATH_GRADING_ENGINE_VERSION
    assert event["payload"]["student_id"] == student
    assert event["payload"]["q_id"] == question.q_id
    assert event["payload"]["score"] == 9
    for index, saved in enumerate(event["payload"]["experts"]):
        expert = correction.expert_results[index]
        assert saved["provider"] == f"model#sample{index + 1}"
        assert saved["provider_id"] == expert.provider_id
        assert saved["math_plan_id"] == plan.plan_id
        assert saved["trace"] == json.loads(expert.raw_output)
        assert saved["trace"]["original_assessment"]["score"] == 8
        assert saved["trace"]["assessment"]["score"] == 9
        assert saved["trace"]["targeted_review"]["reason"] == "Credit follows the teacher rule."
    persisted, = grades.list_results_for_run(run_id)
    assert persisted.ai_score == 9
    assert all(expert["raw_output"] is None for expert in persisted.ai_expert_results)
    assert all(expert.raw_output is not None for expert in correction.expert_results)


@pytest.mark.parametrize("failure", ["storage", "lease"])
def test_final_trace_failure_cannot_publish_or_change_a_result(case, monkeypatch, failure):
    run_id = start(case, f"final-trace-{failure}")["job_id"]
    checkpoints, questions, revisions, _ = prepare(case, run_id)
    question, student = questions[0], revisions[0][1]
    plan = rubric.fallback_math_rubric_plan(context.problem_from_question(question))
    checkpoints.save_math_plan(context.problem_from_question(question), plan)
    correction = final_math_correction(question, plan)
    if failure == "lease":
        stop(case, run_id)
    else:
        def fail(*args, **kwargs):
            raise OperationalError("event storage unavailable", {}, RuntimeError("database failed"))
        # Fail after the result insert has flushed, while creating its trace.
        # The transaction must roll back both instead of publishing a score.
        monkeypatch.setattr(grades, "_new_event_id", fail)
    previous = [row.model_dump() for row in grades.list_results_for_run(run_id)]
    with pytest.raises(CheckpointWriteError, match="question_checkpoint_failed"):
        checkpoints.save_correction(student, correction)
    assert [row.model_dump() for row in grades.list_results_for_run(run_id)] == previous
    assert not [row for row in grades.list_events(run_id) if row["message"] == MATH_TRACE_EVENT]


def test_one_plan_precedes_all_students_and_is_reused_without_another_call(monkeypatch):
    calls = []
    original = problem()

    async def compile_plan(value, provider, **kwargs):
        calls.append((value.model_dump(), provider.provider_id))
        return rubric.fallback_math_rubric_plan(value)

    monkeypatch.setattr(rubric, "prepare_math_rubric_plan", compile_plan)

    async def exercise():
        plans = await context.prepare_math_plans({"q1": original}, registry())
        with context.math_plans_context(plans):
            # This is also the direct grade_batch/adapter nesting contract.
            nested = await context.prepare_math_plans({"q1": original}, registry())
            returned = await asyncio.gather(*[
                asyncio.sleep(0, result=context.get_math_plan(original).plan_id)
                for _ in range(20)
            ])
            assert returned == [nested["q1"].plan_id] * 20
            changed = problem(criterion="Result only, 10 points.")
            assert context.get_math_plan(changed) is None
        assert context.get_math_plan(original) is None

    asyncio.run(exercise())
    assert len(calls) == 1
    assert "student" not in calls[0][0]


def test_context_is_request_local_and_returned_plan_cannot_mutate_shared_rules():
    original = problem()
    first = rubric.fallback_math_rubric_plan(original)
    second_problem = problem(criterion="Result only, 10 points.")
    second = rubric.fallback_math_rubric_plan(second_problem)

    async def read(plan, value):
        with context.math_plans_context({"q1": plan}):
            await asyncio.sleep(0)
            found = context.get_math_plan(value)
            assert found.plan_id == plan.plan_id
            assert found is not plan
            return found.original_criterion

    async def exercise():
        return await asyncio.gather(read(first, original), read(second, second_problem))

    assert asyncio.run(exercise()) == [original.criterion, second_problem.criterion]
    assert context.get_math_plan(original) is None


def test_internal_provenance_survives_adapter_without_changing_problem_json(monkeypatch):
    source = {"presentation": {
        "ai_completion_provenance": {"criterion": {"status": "generated"}},
        "question_structure": {"shared_stem": "", "subparts": [{"label": "a", "stem": "Evaluate 2+2."}]},
    }}
    question = SimpleNamespace(
        id="test-question", version=1, q_id="q1", number="1", order_index=0, type="计算题", stem="Evaluate 2+2.",
        criterion="Method 8, result 2.", reference_answer=None, test_cases=None, max_score=10, source=source,
    )
    seen = []

    async def compile_plan(value, provider, **kwargs):
        seen.append(value._math_rubric_source)
        return rubric.fallback_math_rubric_plan(value)

    monkeypatch.setattr(rubric, "prepare_math_rubric_plan", compile_plan)
    raw = grading_adapter.build_problem_store([question])
    plans = asyncio.run(context.prepare_math_plans(raw, registry()))
    canonical = context.problem_from_question(question)
    assert seen == [source]
    assert plans["q1"].input_fingerprint == rubric.math_rubric_fingerprint(canonical)
    assert plans["q1"].input_fingerprint != rubric.math_rubric_fingerprint(problem())
    assert "_math_rubric_source" not in canonical.model_dump()
    assert "_math_rubric_source" not in ProblemInfo.model_json_schema()["properties"]
    with context.math_plans_context(plans):
        assert context.get_math_plan(canonical).plan_id == plans["q1"].plan_id


def test_plan_preparation_uses_selected_primary_and_skips_nonmath(monkeypatch):
    called = []
    primary = SimpleNamespace(ainvoke=lambda *_: None)
    selected = SimpleNamespace(get=lambda provider_id: primary if provider_id == "primary" else None)
    setup = TaskGradingSetup(selected_provider_ids=["primary"], primary_provider_id="primary")

    async def compile_plan(value, provider, **kwargs):
        called.append((value.q_id, provider, kwargs["grading_setup"]))
        return rubric.fallback_math_rubric_plan(value)

    monkeypatch.setattr(rubric, "prepare_math_rubric_plan", compile_plan)
    values = {
        "q1": problem(type="concept"),
        "q2": problem(q_id="q2", type="编程题"),
        "q3": problem(q_id="q3", type="选择题"),
    }
    plans = asyncio.run(context.prepare_math_plans(values, selected, grading_setup=setup))
    assert set(plans) == {"q1"}
    assert [(qid, selected) for qid, selected, _ in called] == [("q1", primary)]
    assert called[0][2]["strictness"] == 50
    assert asyncio.run(context.prepare_math_plans({}, selected)) == {}
    assert len(called) == 1


@pytest.mark.parametrize("error", [ValueError("invalid plan"), RuntimeError("provider failed")])
def test_failed_interpretation_retains_original_rubric_without_blocking(monkeypatch, error):
    async def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(rubric, "prepare_math_rubric_plan", fail)
    original = problem(criterion="Give 7 points for a valid alternative method.")
    plan = asyncio.run(context.prepare_math_plans({"q1": original}, registry()))["q1"]
    assert plan.original_criterion == original.criterion
    assert original.criterion in plan.effective_criterion


@pytest.mark.parametrize("error", [
    asyncio.CancelledError(),
    CheckpointWriteError("storage failed"),
    OperationalError("write failed", {}, RuntimeError()),
])
def test_cancellation_and_storage_failure_are_never_rubric_fallback(monkeypatch, error):
    async def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(rubric, "prepare_math_rubric_plan", fail)
    with pytest.raises(type(error)):
        asyncio.run(context.prepare_math_plans({"q1": problem()}, registry()))


@pytest.mark.parametrize("link", ["__cause__", "__context__"])
def test_wrapped_storage_failure_is_not_mistaken_for_model_fallback(monkeypatch, link):
    from backend.tools.structured_llm import TransientLLMError

    storage_error = OperationalError("quota database unavailable", {}, RuntimeError("database failed"))
    classified = TransientLLMError("quota database unavailable")
    setattr(classified, link, storage_error)
    # Some wrappers retain the earlier exception again; traversal is bounded.
    storage_error.__context__ = classified

    async def fail(*args, **kwargs):
        raise classified

    monkeypatch.setattr(rubric, "prepare_math_rubric_plan", fail)
    with pytest.raises(TransientLLMError) as captured:
        asyncio.run(context.prepare_math_plans({"q1": problem()}, registry()))
    assert captured.value is classified


def test_paid_plan_survives_stop_and_multiple_failed_only_retries(case, monkeypatch):
    run_id = start(case, "freeze-plan")["job_id"]
    checkpoints, questions, revisions, setup = prepare(case, run_id)
    calls = []

    async def compile_plan(value, provider, **kwargs):
        calls.append(value.q_id)
        return rubric.fallback_math_rubric_plan(value)

    monkeypatch.setattr(rubric, "prepare_math_rubric_plan", compile_plan)
    plans = asyncio.run(checkpoints.run(context.prepare_math_plans(
        grading_adapter.build_problem_store(questions), registry(),
    )))
    student = revisions[0][1]
    provider = setup.setup["selected_provider_ids"][0]
    checkpoints.save_expert(student, "q1", provider, 0,
        ExpertResult(provider=provider, provider_id=provider, score=8, confidence=.9, comment="method"))
    assert saved_experts(run_id)[0]["math_plan_id"] == plans["q1"].plan_id
    assert len([event for event in grades.list_events(run_id) if event["message"] == context.PLAN_EVENT]) == 2
    stop(case, run_id)
    for index in range(2):
        resumed = retry(case, f"resume-plan-{index}")
        restored, restored_questions, _, _ = prepare(case, resumed, f"worker-{index}")
        actual = asyncio.run(restored.run(context.prepare_math_plans(
            grading_adapter.build_problem_store(restored_questions), registry(),
        )))
        assert {qid: value.model_dump() for qid, value in actual.items()} == {
            qid: value.model_dump() for qid, value in plans.items()
        }
        assert restored.get(student, "q1", provider, 0).score == 8
        assert not [event for event in grades.list_events(resumed) if event["message"] == context.PLAN_EVENT]
        stop(case, resumed)
    assert calls == ["q1", "q2"]


def test_plan_save_is_lease_fenced_and_cannot_change_mid_run(case):
    run_id = start(case, "plan-lease")["job_id"]
    checkpoints, questions, _, _ = prepare(case, run_id)
    original = context.problem_from_question(questions[0])
    plan = rubric.fallback_math_rubric_plan(original)
    checkpoints.save_math_plan(original, plan)
    with pytest.raises(CheckpointWriteError, match="already_frozen"):
        checkpoints.save_math_plan(original, plan.model_copy(update={"plan_id": "changed"}))
    stop(case, run_id)
    other = context.problem_from_question(questions[1])
    with pytest.raises(CheckpointWriteError, match="checkpoint_failed"):
        checkpoints.save_math_plan(other, rubric.fallback_math_rubric_plan(other))
    assert "q2" not in checkpoints.math_plans


def test_empty_student_batch_never_prepares_or_calls_a_model(case, monkeypatch):
    from backend.db import assignment_repository

    async def should_not_run(*args, **kwargs):
        raise AssertionError("an empty batch cannot trigger rubric interpretation")

    monkeypatch.setattr(rubric, "prepare_math_rubric_plan", should_not_run)
    questions = assignment_repository.get_questions_by_assignment(case[0])
    assert asyncio.run(grading_adapter.run_grading(
        run_id="unused", assignment_id=case[0], teacher_id=case[1], questions=questions,
        frozen_revisions=[], registry=registry(),
    )) == []


@pytest.mark.parametrize("only_reused_answer", [False, True])
def test_missing_pending_answers_do_not_prepare_rules_or_hide_missing_results(
    case, monkeypatch, only_reused_answer,
):
    from backend.db import assignment_repository

    async def should_not_prepare(*args, **kwargs):
        raise AssertionError("no pending answer row should prepare a rubric")

    async def no_corrections(**kwargs):
        return [{"student_id": "student", "corrections": []}]

    monkeypatch.setattr(context, "prepare_math_plans", should_not_prepare)
    monkeypatch.setattr(grading_adapter, "grade_batch", no_corrections)
    monkeypatch.setattr(grading_adapter.settings, "e2e_fake_provider", False)
    answers = [SimpleNamespace(q_id="q1", number="1", type="concept", content="already graded", flag=[])] if only_reused_answer else []
    revision = SimpleNamespace(id="revision", answers=answers)
    outcomes = asyncio.run(grading_adapter.run_grading(
        run_id="unused", assignment_id=case[0], teacher_id=case[1],
        questions=assignment_repository.get_questions_by_assignment(case[0]),
        frozen_revisions=[(revision, "student")], registry=registry(),
        reused_pairs={("revision", "q1")} if only_reused_answer else None,
    ))
    assert {row.q_id for row in outcomes[0].results} == ({"q2"} if only_reused_answer else {"q1", "q2"})
    assert all(row.ai_score is None and row.result_status == "failed" for row in outcomes[0].results)


def test_failed_only_old_engine_recomputes_math_but_keeps_nonmath_success(case, monkeypatch):
    with session_scope() as session:
        q2 = session.scalar(select(AssignmentQuestionRecord).where(
            AssignmentQuestionRecord.assignment_id == case[0], AssignmentQuestionRecord.q_id == "q2",
        ))
        q2.type = "编程题"
    first = start(case, "old-engine")["job_id"]
    finish(case, first, monkeypatch, ("q1",))
    old_run(first)
    resumed = retry(case, "new-engine")
    manifest = workflows.get_run_setup(resumed).input_manifest
    assert manifest["math_grading_engine_version"] == context.MATH_GRADING_ENGINE_VERSION
    assert [row["q_id"] for row in manifest["reused_results"]] == ["q2"]
    assert manifest["reused_experts"] == []
    assert manifest["math_rubric_plans"] == {}
    original = grades.list_results_for_run(first)
    assert next(row for row in original if row.q_id == "q2").ai_score == 8
    assert finish(case, resumed, monkeypatch, ())["question_ids_by_student"] == {
        original[0].student_id: {"q1"},
    }


def test_old_success_cannot_short_circuit_failed_only_as_already_done(case, monkeypatch):
    first = start(case, "all-old-success")["job_id"]
    finish(case, first, monkeypatch, ())
    old_run(first)
    resumed = retry(case, "replace-old-math")
    assert resumed != first
    assert workflows.get_run_setup(resumed).input_manifest["reused_results"] == []
    assert len(grades.list_results_for_run(first)) == 2


def test_untouched_old_queued_run_can_adopt_current_engine(case, monkeypatch):
    run_id = start(case, "old-queued")["job_id"]
    old_run(run_id)
    finish(case, run_id, monkeypatch, ())
    assert grades.get_run(run_id).status == "completed"
    version_events = [event for event in grades.list_events(run_id) if event["message"] == context.ENGINE_EVENT]
    assert [event["payload"]["version"] for event in version_events] == [context.MATH_GRADING_ENGINE_VERSION]


def test_existing_old_run_rows_cannot_mix_with_new_execution(case, monkeypatch):
    run_id = start(case, "existing-old")["job_id"]
    finish(case, run_id, monkeypatch, ("q2",))
    old_run(run_id)
    setup = workflows.get_run_setup(run_id)
    from backend.services.grading_runs import _questions_for_run
    questions = _questions_for_run(assignment_id=case[0], frozen_setup=setup)
    with pytest.raises(VersionConflict, match="engine changed"):
        context.ensure_run_math_version(
            run_id=run_id, worker_id="unowned", questions=questions, frozen_setup=setup,
        )


def test_old_expert_or_another_plan_never_becomes_a_resumed_vote(case):
    run_id = start(case, "vote-version")["job_id"]
    checkpoints, questions, revisions, setup = prepare(case, run_id)
    provider = setup.setup["selected_provider_ids"][0]
    original = context.problem_from_question(questions[0])
    plan = rubric.fallback_math_rubric_plan(original)
    checkpoints.save_math_plan(original, plan)
    expert = ExpertResult(provider=provider, provider_id=provider, score=10, confidence=1, comment="old")
    for sample, plan_id, version in [
        (0, plan.plan_id, "old"),
        (1, "another-plan", context.MATH_GRADING_ENGINE_VERSION),
        (2, plan.plan_id, context.MATH_GRADING_ENGINE_VERSION),
    ]:
        grades.record_event(run_id, worker_id="checkpoint-test", level="info",
            message="grading_expert_checkpoint", payload={
                "student_id": revisions[0][1], "q_id": "q1", "provider_id": provider,
                "sample_index": sample, "result": expert.model_dump(mode="json"),
                "math_plan_id": plan_id, "math_grading_engine_version": version,
            })
    assert [item["sample_index"] for item in saved_experts(run_id)] == [2]
