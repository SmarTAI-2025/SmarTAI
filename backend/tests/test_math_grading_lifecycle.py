"""Frozen rubric plans survive retries without mixing old scoring engines."""
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError

from backend.db import grading_repository as grades, workflow_repository as workflows
from backend.db.models import AssignmentQuestionRecord, GradingRunEventRecord
from backend.db.session import session_scope
from backend.domain.errors import VersionConflict
from backend.models import ExpertResult, ProblemInfo, TaskGradingSetup
from backend.services import grading_adapter, math_grading_context as context
from backend.services import math_rubric_plan as rubric
from backend.services.grading_checkpoints import CheckpointWriteError, saved_experts
from backend.tests.test_grading_checkpoints import prepare, retry, stop
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
        q_id="q1", number="1", order_index=0, type="计算题", stem="Evaluate 2+2.",
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
        "q1": problem(type="short"),
        "q2": problem(q_id="q2", type="编程题"),
        "q3": problem(q_id="q3", type="选择题"),
    }
    plans = asyncio.run(context.prepare_math_plans(values, selected, grading_setup=setup))
    assert set(plans) == {"q1"}
    assert called == [("q1", primary, setup)]
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
    answers = [SimpleNamespace(q_id="q1", number="1", type="short", content="already graded", flag=[])] if only_reused_answer else []
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
        q2.type = "选择题"
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
