"""Regressions found by combined-product QA; real orchestration, no paid calls."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import OperationalError

from backend.agents import multi_expert as multi
from backend.db import grading_repository as grades, workflow_repository as workflows
from backend.domain.grading_review import review_state
from backend.services import grading_adapter, grading_runs, task_facade
from backend.services.math_grading_context import math_plans_context
from backend.services.task_execution import continue_manual_grading
from backend.skills import math_grading as math_skill
from backend.tests.test_grading_checkpoints import prepare, retry, stop
from backend.tests.test_math_grading import answer, assessment, expert_for, plan_for, problem
from backend.tests.test_multi_expert_robustness import _FakeProvider, _FakeRegistry
from backend.tests.test_postgres_integration import pg_database  # noqa: F401
from backend.tests.test_task_grading_retry import Registry, case, finish, start  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize("review_failure", [False, True])
async def test_equal_total_residual_item_dispute_is_advisory(case, monkeypatch, review_failure):
    p, a, plan = problem(), answer(), plan_for()
    run_id = start(case, "unresolved-items")["job_id"]
    _, questions, revisions, _ = prepare(case, run_id)
    student = revisions[0][1]
    experts = {key: expert_for(draft, plan, a.content).model_copy(update={"confidence": .95})
               for key, draft in zip(("one", "two"), (assessment(8, 0), assessment(6, 2)))}

    class CannedMath:
        name = "CannedMath"
        uses_math_evidence = True

        def __init__(self, provider, **kwargs):
            self.provider = provider

        async def grade(self, *args, **kwargs):
            return experts[self.provider.provider_id].model_copy(deep=True)

    call = AsyncMock(side_effect=RuntimeError("optional review unavailable")) if review_failure else AsyncMock(
        return_value=(math_skill.ExpertReview(revisions=[]), SimpleNamespace(duration_ms=1)))
    monkeypatch.setattr(multi, "get_skill_for_type", lambda _: CannedMath)
    monkeypatch.setattr(math_skill, "structured_llm_call", call)
    with math_plans_context({"q1": plan}):
        correction = await multi.run_multi_expert(
            problem=p, answer=a, student_id=student,
            registry=_FakeRegistry([_FakeProvider("one"), _FakeProvider("two")]),
            aggregation_method="weighted_average")
    row = grading_adapter.correction_to_result(run_id=run_id, revision_id=revisions[0][0].id,
        student_id=student, question=questions[0], correction=correction)
    grades.upsert_result(run_id, worker_id="checkpoint-test", grade_result=row)
    row, = grades.list_results_for_run(run_id)
    public = task_facade._serialize_correction(row)
    assert call.await_count == 1
    assert correction.score == sum(step.score for step in correction.steps) == 8
    assert correction.confidence == .95
    assert row.result_status == "needs_review"
    assert public["review_reasons"] == ["math_scoring_uncertainty"]
    assert review_state(public) == (True, False)
    assert not task_facade._correction_review_blocks_finalization(row)
    grades.mark_completed(run_id, worker_id="checkpoint-test", completed=1, failed=1)
    grades.add_teacher_review(row.id, teacher_id=case[1], new_score=8, new_comment="Checked", confirm=True)
    row, = grades.list_results_for_run(run_id)
    assert review_state(task_facade._serialize_correction(row)) == (False, True)


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapped", [False, True])
async def test_synthesis_does_not_hide_storage_failure(monkeypatch, wrapped):
    error = OperationalError("usage write", {}, RuntimeError("database unavailable"))
    if wrapped:
        wrapper = RuntimeError("provider wrapper")
        wrapper.__cause__ = error
        error = wrapper
    monkeypatch.setattr(multi, "structured_llm_call", AsyncMock(side_effect=error))
    p, a, plan = problem(), answer(), plan_for()
    with pytest.raises(type(error)) as raised:
        await multi._synthesize(problem=p, answer=a,
            expert_results=[expert_for(assessment(), plan, a.content)],
            synthesis_provider=_FakeProvider("one"))
    assert raised.value is error


def _manual_before_plan(case, *, confirmed=True):
    run = start(case, "pause-before-plan")["job_id"]
    stop(case, run)
    continue_manual_grading(task_id=case[0], owner_id=case[1], job_id=run,
        expected_revision=workflows.get_live_workflow(case[0], owner_id=case[1]).workflow_revision)
    first = next(row for row in grades.list_results_for_run(run) if row.q_id == "q1")
    assert first.ai_score is None
    grades.add_teacher_review(first.id, teacher_id=case[1], new_score=9,
                              new_comment="Teacher checked the actual work.", confirm=confirmed)
    return run


@pytest.mark.parametrize("confirmed", [True, False])
@pytest.mark.parametrize("save_path", ["batch", "batch_failed", "checkpoint", "pause"])
def test_manual_override_survives_retry_without_reusing_old_ai(case, monkeypatch, save_path, confirmed):
    from backend.models import Correction
    first = _manual_before_plan(case, confirmed=confirmed)
    resumed = retry(case, "finish-after-manual")
    frozen = workflows.get_run_setup(resumed)
    assert frozen.input_manifest["reused_results"] == []
    if save_path in {"batch", "batch_failed"}:
        finish(case, resumed, monkeypatch, failed=("q1",) if save_path == "batch_failed" else (), score=8)
    elif save_path == "checkpoint":
        checkpoints, _, revisions, _ = prepare(case, resumed)
        checkpoints.save_correction(revisions[0][1], Correction(q_id="q1", type="concept", score=8,
            max_score=10, confidence=1, comment="New AI judgment", steps=[], synthesis_method="single"))
        stop(case, resumed)
    else:
        stop(case, resumed)  # Still queued: no worker or math plan has run.
    current = next(row for row in grades.list_results_for_run(resumed) if row.q_id == "q1")
    assert current.ai_score == (None if save_path in {"pause", "batch_failed"} else 8)
    assert current.effective_score == 9
    assert current.teacher_review["confirmed"] is confirmed
    assert current.result_status == ("graded" if confirmed else "needs_review")
    assert current.review_reasons == ([] if confirmed else ["teacher_edit_pending_confirmation"])
    assert task_facade._correction_review_blocks_finalization(current) is (
        not confirmed and save_path in {"pause", "batch_failed"})
    assert current.effective_comment == "Teacher checked the actual work."
    original = next(row for row in grades.list_results_for_run(first) if row.q_id == "q1")
    assert original.ai_score is None and original.effective_score == 9


@pytest.mark.asyncio
async def test_equivalent_deduction_evidence_does_not_request_review(monkeypatch):
    p, a, plan = problem(), answer(), plan_for(mode="deductions")
    experts = []
    for index in range(2):
        claim_id, error_id = f"claim-{index}", f"error-{index}"
        draft = math_skill.MathAssessment(route_id="main", route_reason="Independent errors",
            claims=[math_skill.Claim(id=claim_id, quote=a.content, verdict="refuted", reason=f"Reason {index}")],
            deductions=[math_skill.Deduction(error_id=error_id, rule_id="error", claim_id=claim_id,
                                            reason=f"Different wording {index}")],
            confidence=.95, comment="One independent arithmetic error.")
        experts.append(expert_for(draft, plan, a.content))
    call = AsyncMock()
    monkeypatch.setattr(math_skill, "structured_llm_call", call)
    with math_plans_context({"q1": plan}), math_skill.math_review_scope():
        reviewed = await math_skill.review_math_experts(p, a, experts, _FakeProvider("one"))
    assert not call.called
    merged = multi._weighted_average_fallback(p, reviewed)
    assert merged.score == 9 and merged.review_reasons == []


@pytest.mark.asyncio
async def test_successful_item_review_clears_residual_advice(monkeypatch):
    p, a, plan = problem(), answer(), plan_for()
    experts = [expert_for(draft, plan, a.content) for draft in (assessment(8, 0), assessment(6, 2))]
    call = AsyncMock(return_value=(math_skill.ExpertReview(revisions=[math_skill.ExpertRevision(
        expert_index=1, source_quote=a.content, reason="Correct setup, final arithmetic error.",
        assessment=assessment(8, 0))]), SimpleNamespace(duration_ms=1)))
    monkeypatch.setattr(math_skill, "structured_llm_call", call)
    with math_plans_context({"q1": plan}), math_skill.math_review_scope():
        reviewed = await math_skill.review_math_experts(p, a, experts, _FakeProvider("one"))
    merged = multi._weighted_average_fallback(p, reviewed)
    assert call.await_count == 1 and merged.score == 8
    assert merged.review_reasons == []


@pytest.mark.asyncio
async def test_provider_only_synthesis_failure_still_uses_valid_votes(monkeypatch):
    monkeypatch.setattr(multi, "structured_llm_call", AsyncMock(side_effect=RuntimeError("provider unavailable")))
    p, a, plan = problem(), answer(), plan_for()
    result = await multi._synthesize(problem=p, answer=a,
        expert_results=[expert_for(assessment(), plan, a.content)], synthesis_provider=_FakeProvider("one"))
    assert result.score == 8 and result.synthesis_method == "weighted_average"


@pytest.mark.parametrize("wrapped", [False, True])
def test_synthesis_storage_failure_reaches_actual_run_boundary(case, monkeypatch, wrapped):
    from backend.agents import grading_agent
    from backend.models import ExpertResult
    from backend.services.grading_checkpoints import CheckpointWriteError

    error = OperationalError("usage write", {}, RuntimeError("database unavailable"))
    if wrapped:
        wrapper = RuntimeError("provider wrapper")
        wrapper.__context__ = error
        error = wrapper
    monkeypatch.setattr(multi, "structured_llm_call", AsyncMock(side_effect=error))

    async def synthesize_completed_votes(**kwargs):
        return await multi._synthesize(problem=kwargs["problem"], answer=kwargs["answer"],
            expert_results=[ExpertResult(provider="one", score=8, max_score=10, confidence=.95,
                                        comment="Valid expert vote.", steps=[])],
            synthesis_provider=_FakeProvider("one"))

    monkeypatch.setattr(grading_agent, "run_multi_expert", synthesize_completed_votes)
    class LocalRegistry(Registry):
        def get(self, key):
            return SimpleNamespace(provider_id=key, model="mock")

    run = start(case, "synthesis-storage-failure")["job_id"]
    with pytest.raises(CheckpointWriteError):
        asyncio.run(grading_runs.process_run(run_id=run, worker_id="critical-test", registry=LocalRegistry()))
    assert grades.get_run(run).status == "failed"
    assert grades.get_run(run).error_message == "grading_persistence_failed"
    assert grades.list_results_for_run(run) == []
    assert any(event["message"] == "run_failed" and event["payload"]["code"] == "grading_persistence_failed"
               for event in grades.list_events(run))


@pytest.mark.parametrize("interpreter_changed", [False, True])
def test_teacher_override_survives_failure_before_any_retry_result(case, monkeypatch, interpreter_changed):
    _manual_before_plan(case)
    second = retry(case, "retry-before-failure")
    prepare(case, second)
    grades.mark_failed(second, worker_id="checkpoint-test", error_message="provider unavailable")
    assert grades.list_results_for_run(second) == []
    if interpreter_changed:
        from backend.services import math_rubric_plan
        monkeypatch.setattr(math_rubric_plan, "INTERPRETATION_VERSION", "test-new-interpreter")
    third = retry(case, "retry-after-failure")
    finish(case, third, monkeypatch, failed=())
    result = next(row for row in grades.list_results_for_run(third) if row.q_id == "q1")
    assert result.ai_score == 8 and result.effective_score == 9 and result.teacher_review["confirmed"]


def test_legacy_ai_version_does_not_discard_current_teacher_override(case, monkeypatch):
    from backend.tests.test_math_grading_lifecycle import old_run
    first = _manual_before_plan(case)
    old_run(first)
    resumed = retry(case, "retry-old-engine")
    manifest = workflows.get_run_setup(resumed).input_manifest
    assert manifest["reused_results"] == [] and manifest["reused_experts"] == []
    finish(case, resumed, monkeypatch, failed=())
    result = next(row for row in grades.list_results_for_run(resumed) if row.q_id == "q1")
    assert result.ai_score == 8 and result.effective_score == 9


@pytest.mark.parametrize("change", ["stem", "answer", "structure", "provenance", "full_batch"])
def test_changed_work_or_explicit_full_batch_does_not_inherit_confirmation(case, monkeypatch, change):
    from copy import deepcopy
    from sqlalchemy import select
    from backend.db import submission_repository as submissions
    from backend.db.models import AssignmentQuestionRecord
    from backend.db.session import session_scope
    from backend.domain.errors import VersionConflict

    first = _manual_before_plan(case)
    if change in {"stem", "structure", "provenance"}:
        with session_scope() as session:
            row = session.scalar(select(AssignmentQuestionRecord).where(
                AssignmentQuestionRecord.assignment_id == case[0], AssignmentQuestionRecord.q_id == "q1"))
            if change == "stem":
                row.stem += " New requirement."
            else:
                source = deepcopy(row.source or {})
                presentation = source.setdefault("presentation", {})
                if change == "structure":
                    presentation["question_structure"] = {
                        "major_order": 1, "shared_stem": "New shared premise", "subparts": []}
                else:
                    presentation["ai_completion_provenance"] = {"criterion": {"source": "ai"}}
                row.source = source
    elif change == "answer":
        old_revision = grades.list_frozen_submissions(first)[0]
        revision = submissions.get_revision(revision_id=old_revision.id, actor_id=case[1])
        submissions.add_revision(submission_id=revision.submission_id, student_id=old_revision.student_id,
            source="online", answers=[{**a.model_dump(), "content": "Changed answer"} for a in revision.answers])
    if change in {"stem", "answer"}:
        with pytest.raises(VersionConflict, match="Grading inputs changed"):
            retry(case, "invalid-old-confirmation")
        resumed = start(case, "changed-new-batch")["job_id"]
    elif change == "full_batch":
        resumed = start(case, "fresh-whole-batch")["job_id"]
    else:
        resumed = retry(case, "changed-source")
    finish(case, resumed, monkeypatch, failed=())
    result = next(row for row in grades.list_results_for_run(resumed) if row.q_id == "q1")
    assert result.ai_score == result.effective_score == 8 and result.teacher_review is None
