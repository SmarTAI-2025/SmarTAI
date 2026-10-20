"""Final evidence and public scores must become durable together."""
import pytest

from backend.db import grading_repository as grades
from backend.services import math_grading_context as context, math_rubric_plan as rubric
from backend.services.grading_checkpoints import MATH_TRACE_EVENT
from backend.tests.test_grading_checkpoints import prepare, stop
from backend.tests.test_math_grading_lifecycle import final_math_correction
from backend.tests.test_postgres_integration import pg_database  # noqa: F401
from backend.tests.test_task_grading_retry import case, start  # noqa: F401


def test_stop_cannot_restore_pre_review_score_after_final_trace_is_persisted(case, monkeypatch):
    run_id = start(case, "atomic-final-review")["job_id"]
    checkpoints, questions, revisions, setup = prepare(case, run_id)
    question, student = questions[0], revisions[0][1]
    plan = rubric.fallback_math_rubric_plan(context.problem_from_question(question))
    checkpoints.save_math_plan(context.problem_from_question(question), plan)
    provider = setup.setup["selected_provider_ids"][0]
    correction = final_math_correction(question, plan, provider)
    correction.expert_results = correction.expert_results[:1]
    correction.synthesis_method = "single"
    checkpoints.save_expert(student, question.q_id, provider, 0,
                            correction.expert_results[0].model_copy(update={"score": 8}))

    actual_upsert = grades.upsert_result
    observed = []

    def save_and_stop(*args, **kwargs):
        # This was the real inter-transaction window: a final score of 9 was
        # already durable in the trace, while stop could only restore 8.
        events = [e for e in grades.list_events(run_id) if e["message"] == MATH_TRACE_EVENT]
        if events:
            stop(case, run_id)
            observed.append(next(r.ai_score for r in grades.list_results_for_run(run_id)
                                 if r.q_id == question.q_id))
            return
        actual_upsert(*args, **kwargs)
        stop(case, run_id)
        observed.append(next(r.ai_score for r in grades.list_results_for_run(run_id)
                             if r.q_id == question.q_id))

    monkeypatch.setattr(grades, "upsert_result", save_and_stop)
    checkpoints.save_correction(student, correction)
    assert observed == [9]
    event, = [e for e in grades.list_events(run_id) if e["message"] == MATH_TRACE_EVENT]
    assert event["payload"]["score"] == observed[0]
