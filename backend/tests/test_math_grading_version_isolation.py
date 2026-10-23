"""Persisted v1 work cannot become v2 evidence; teacher judgments stay separate."""
import asyncio
from copy import deepcopy

import pytest
from sqlalchemy import delete, select

from backend.db import grading_repository as grades, workflow_repository as workflows
from backend.db.models import AssignmentQuestionRecord, GradingRunEventRecord
from backend.db.session import session_scope
from backend.domain.errors import VersionConflict
from backend.models import Correction, ExpertResult
from backend.services import grading_adapter, math_grading_context as context
from backend.services import math_rubric_plan as rubric
from backend.services.grading_checkpoints import (
    GradingCheckpoints, saved_experts, saved_math_plans,
)
from backend.tests.test_grading_checkpoints import prepare, retry, stop
from backend.tests.test_math_grading_lifecycle import registry
from backend.tests.test_postgres_integration import pg_database  # noqa: F401
from backend.tests.test_task_grading_retry import case, finish, start  # noqa: F401


def v1_plan(question):
    """The deployed v1 shape/hash, without v2 fields or a current-plan cast."""
    problem = context.problem_from_question(question)
    inputs = rubric._question_input(problem)
    inputs.update(schema_version="math_rubric_v1", interpretation_version="math_rubric_interpretation_v2")
    payload = {
        "schema_version": "math_rubric_v1",
        "input_fingerprint": rubric._content_hash(inputs),
        "max_score": problem.max_score, "original_criterion": problem.criterion,
        "effective_criterion": problem.criterion or "Use the complete original rule.",
        "routes": [{"id": "holistic", "description": "Original v1 rule",
                    "applies_when": "All answers", "mode": "holistic", "items": [],
                    "minimum_score": 0, "minimum_condition": "always"}],
        "follow_through": "Apply the teacher rule.",
        "numerical_policy": "Preserve the original precision requirements.", "advisories": [],
    }
    return {**payload, "plan_id": rubric._content_hash(payload)}


def persist_v1_state(run_id, questions):
    """Restore historical private snapshots in the disposable test database."""
    plans = {q.q_id: v1_plan(q) for q in questions if context.is_math_problem(q)}
    with session_scope() as session:
        setup = session.get(workflows.GradingRunSetupRecord, run_id)
        setup.input_manifest = {
            **setup.input_manifest, "math_grading_engine_version": "math_evidence_v1",
            "math_rubric_plans": plans,
            "math_rubric_input_fingerprints": {qid: p["input_fingerprint"] for qid, p in plans.items()},
        }
        for event in session.scalars(select(GradingRunEventRecord).where(
            GradingRunEventRecord.grading_run_id == run_id,
        )):
            if event.message == context.ENGINE_EVENT:
                event.payload = {"version": "math_evidence_v1"}
            elif event.message == context.PLAN_EVENT and event.payload["q_id"] in plans:
                event.payload = {**event.payload, "engine_version": "math_evidence_v1",
                                 "plan": plans[event.payload["q_id"]]}
            elif event.message == "grading_expert_checkpoint" and event.payload["q_id"] in plans:
                event.payload = {**event.payload, "math_grading_engine_version": "math_evidence_v1",
                                 "math_plan_id": plans[event.payload["q_id"]]["plan_id"]}
    return plans


@pytest.mark.parametrize("confirmed", [False, True])
def test_v1_completed_run_regrades_math_and_preserves_teacher_override(case, monkeypatch, confirmed):
    from backend.db import assignment_repository

    with session_scope() as session:
        session.scalar(select(AssignmentQuestionRecord).where(
            AssignmentQuestionRecord.assignment_id == case[0], AssignmentQuestionRecord.q_id == "q2",
        )).type = "选择题"
    first = start(case, "v1-completed")["job_id"]
    finish(case, first, monkeypatch, (), score=8)
    original = next(row for row in grades.list_results_for_run(first) if row.q_id == "q1")
    grades.add_teacher_review(original.id, teacher_id=case[1], new_score=9,
                              new_comment="Preserve this human judgment", confirm=confirmed)
    questions = assignment_repository.get_questions_by_assignment(case[0])
    persist_v1_state(first, questions)
    assert saved_math_plans(first) == {}

    resumed = retry(case, "v2-retry")
    assert resumed != first
    manifest = workflows.get_run_setup(resumed).input_manifest
    assert manifest["math_grading_engine_version"] == "math_evidence_v2"
    assert manifest["math_rubric_plans"] == {} and manifest["reused_experts"] == []
    assert [row["q_id"] for row in manifest["reused_results"]] == ["q2"]
    assert manifest["teacher_review_overrides"][0]["teacher_review"]["confirmed"] is confirmed
    calls = finish(case, resumed, monkeypatch, (), score=6)
    assert calls["question_ids_by_student"] == {original.student_id: {"q1"}}
    current = {row.q_id: row for row in grades.list_results_for_run(resumed)}
    assert current["q1"].ai_score == 6 and current["q1"].effective_score == 9
    assert current["q1"].teacher_review["confirmed"] is confirmed
    assert current["q2"].ai_score == 8
    historical = next(row for row in grades.list_results_for_run(first) if row.q_id == "q1")
    assert historical.ai_score == 8 and historical.effective_score == 9


def test_v1_paused_vote_is_excluded_but_new_v2_plan_and_vote_resume(case, monkeypatch):
    first = start(case, "v1-paused")["job_id"]
    checkpoints, questions, revisions, setup = prepare(case, first)
    student = revisions[0][1]
    provider = setup.setup["selected_provider_ids"][0]
    checkpoints.save_expert(student, "q1", provider, 0,
        ExpertResult(provider=provider, score=9, max_score=10, confidence=.9, comment="v1 vote"))
    old_plans = persist_v1_state(first, questions)
    fresh = GradingCheckpoints(run_id=first, worker_id="checkpoint-test", questions=questions,
                              frozen_revisions=revisions)
    assert fresh.get(student, "q1", provider, 0) is None and saved_experts(first) == []
    assert fresh.get_math_plan(context.problem_from_question(questions[0])) is None
    with pytest.raises(VersionConflict, match="engine changed"):
        context.ensure_run_math_version(run_id=first, worker_id="checkpoint-test",
            questions=questions, frozen_setup=workflows.get_run_setup(first))
    stop(case, first)
    assert all(row.ai_score is None for row in grades.list_results_for_run(first))

    calls = []

    async def compile_plan(problem, _provider, **kwargs):
        calls.append(problem.q_id)
        return rubric.fallback_math_rubric_plan(problem)

    monkeypatch.setattr(rubric, "prepare_math_rubric_plan", compile_plan)
    second = retry(case, "v2-after-old-pause")
    current, questions, _, _ = prepare(case, second, "v2-worker")
    plans = asyncio.run(current.run(context.prepare_math_plans(
        grading_adapter.build_problem_store(questions), registry(),
    )))
    assert all(plan.schema_version == "math_rubric_v2" for plan in plans.values())
    assert all(plan.plan_id != old_plans[qid]["plan_id"] for qid, plan in plans.items())
    current.save_expert(student, "q1", provider, 0,
        ExpertResult(provider=provider, score=7, max_score=10, confidence=.9, comment="v2 vote"))
    stop(case, second)
    third = retry(case, "v2-resume")
    restored, questions, _, _ = prepare(case, third, "v2-next-worker")
    actual = asyncio.run(restored.run(context.prepare_math_plans(
        grading_adapter.build_problem_store(questions), registry(),
    )))
    assert {qid: p.model_dump() for qid, p in actual.items()} == {
        qid: p.model_dump() for qid, p in plans.items()}
    assert restored.get(student, "q1", provider, 0).score == 7
    assert calls == ["q1", "q2"]


@pytest.mark.parametrize("storage", ["manifest", "event"])
def test_current_engine_stamp_cannot_restore_a_v1_schema_plan(case, storage):
    run_id = start(case, "mixed-plan-schema")["job_id"]
    checkpoints, questions, revisions, _ = prepare(case, run_id)
    question, student = questions[0], revisions[0][1]
    problem = context.problem_from_question(question)
    plan = rubric.fallback_math_rubric_plan(problem)
    checkpoints.save_math_plan(problem, plan)
    assert saved_math_plans(run_id)[question.q_id].plan_id == plan.plan_id
    checkpoints.save_correction(student, Correction(q_id=question.q_id, type=question.type,
        score=8, max_score=10, confidence=.9, comment="Saved current result", steps=[]))
    with session_scope() as session:
        if storage == "manifest":
            frozen = session.get(workflows.GradingRunSetupRecord, run_id)
            frozen.input_manifest = {**deepcopy(frozen.input_manifest),
                "math_rubric_plans": {question.q_id: v1_plan(question)}}
            session.execute(delete(GradingRunEventRecord).where(
                GradingRunEventRecord.grading_run_id == run_id,
                GradingRunEventRecord.message == context.PLAN_EVENT,
            ))
        else:
            event = session.scalar(select(GradingRunEventRecord).where(
                GradingRunEventRecord.grading_run_id == run_id,
                GradingRunEventRecord.message == context.PLAN_EVENT,
            ))
            event.payload = {**event.payload, "plan": v1_plan(question)}
    frozen = workflows.get_run_setup(run_id)
    assert frozen.input_manifest["math_grading_engine_version"] == "math_evidence_v2"
    assert saved_math_plans(run_id) == {}
    with pytest.raises(VersionConflict, match="plan is missing"):
        context.ensure_run_math_version(run_id=run_id, worker_id="checkpoint-test",
            questions=questions, frozen_setup=frozen)
