"""Stop between samples, resume across workers, and hand unfinished work to review."""
import asyncio
from types import SimpleNamespace

import pytest

from backend.db import grading_repository as grades, workflow_repository as workflows
from backend.db import submission_repository
from backend.domain.errors import VersionConflict, ResultNotReleasable
from backend.models import ExpertResult, ProblemInfo, StudentAnswerInfo, TaskGradingSetup
from backend.services import grading_runs, task_facade
from backend.services.grading_checkpoints import GradingCheckpoints, CheckpointWriteError
from backend.services.task_execution import stop_task_run, continue_manual_grading
from backend.tests.test_task_grading_retry import case, start  # noqa: F401


def prepare(case, run_id, worker="checkpoint-test"):
    grades.claim_lease(run_id, worker_id=worker, lease_seconds=120)
    setup = workflows.get_run_setup(run_id)
    questions = grading_runs._questions_for_run(assignment_id=case[0], frozen_setup=setup)
    frozen = grades.list_frozen_submissions(run_id)
    revisions = [(submission_repository.get_revision(revision_id=row.id, actor_id=case[1]), row.student_id)
                 for row in frozen]
    return GradingCheckpoints(run_id=run_id, worker_id=worker, questions=questions,
                             frozen_revisions=revisions), questions, revisions, setup


def stop(case, run_id):
    return stop_task_run(task_id=case[0], owner_id=case[1], job_id=run_id,
        expected_revision=workflows.get_live_workflow(case[0], owner_id=case[1]).workflow_revision)


def retry(case, key):
    return task_facade.start_task_grading(task_id=case[0], owner_id=case[1], request_id=key,
        expected_workflow_revision=workflows.get_live_workflow(case[0], owner_id=case[1]).workflow_revision,
        retry_scope="failed_only")["job_id"]


def test_stop_preserves_each_sample_and_resume_only_calls_missing_samples(case, monkeypatch):
    flow = workflows.get_live_workflow(case[0], owner_id=case[1])
    config = {**flow.grading_setup, "multi_sample_n": 3, "aggregation_method": "weighted_average"}
    workflows.update_workflow(case[0], owner_id=case[1], grading_setup=config)
    run_id = start(case, "sample-start")["job_id"]
    checkpoints, questions, revisions, _ = prepare(case, run_id)
    provider_id = config["selected_provider_ids"][0]
    student_id = revisions[0][1]
    expert = ExpertResult(provider="model#sample1", provider_id=provider_id, score=8,
                          max_score=10, confidence=.9, comment="Completed sample", steps=[])
    checkpoints.save_expert(student_id, "q1", provider_id, 1, expert)
    checkpoints.save_expert(student_id, "q1", provider_id, 2, expert.model_copy(update={
        "error_kind": "transient_llm", "score": 0, "comment": "Network timeout"}))
    stop(case, run_id)
    rows = {row.q_id: row for row in grades.list_results_for_run(run_id)}
    assert rows["q1"].ai_score == 8
    assert rows["q1"].ai_synthesis_method == "degraded_to_single"
    assert [e["error_kind"] for e in rows["q1"].ai_expert_results] == [None, "transient_llm", "operation_cancelled"]
    assert rows["q2"].ai_score is None
    assert task_facade._grading_progress(run_id, case[1])["saved_units"] == 1
    with pytest.raises(CheckpointWriteError):
        checkpoints.save_expert(student_id, "q2", provider_id, 1, expert)
    resumed = retry(case, "sample-resume")
    resumed_checkpoints, _, _, _ = prepare(case, resumed, "second-worker")
    # Stopping the new worker before any new model response must keep inherited work.
    stop(case, resumed)
    third = retry(case, "sample-resume-again")
    third_checkpoints, _, _, _ = prepare(case, third, "third-worker")
    provider = SimpleNamespace(provider_id=provider_id, model="model")
    registry = SimpleNamespace(list_available=lambda: [provider], get=lambda key: provider)
    calls = []
    class Skill:
        name = "checkpoint-test"
        def __init__(self, *args, **kwargs):
            pass
        async def grade(self, problem, answer, student_id):
            calls.append(problem.q_id)
            return expert.model_copy(update={"provider": "model", "score": 10})
    from backend.agents import multi_expert, grading_agent
    monkeypatch.setattr(multi_expert, "get_skill_for_type", lambda _type: Skill)
    question = questions[0]
    from backend.progress.tracker import ProgressReporter
    from backend.progress.grading_activity import GradingActivity
    from backend.services.execution_control import provider_key
    reporter = ProgressReporter(third, 1, 2)
    reporter.grading_activity = GradingActivity(reporter, 2)
    asyncio.run(third_checkpoints.seed_activity(reporter.grading_activity, registry, TaskGradingSetup.model_validate(config)))
    model = reporter.grading_activity.models[provider_key(provider)]
    assert model["succeeded"] == 1 and model["completion_times"] == []
    result = asyncio.run(third_checkpoints.run(grading_agent._grade_and_save_answer(
        problem=ProblemInfo(q_id="q1", number="1", type=question.type, stem=question.stem,
                            criterion=question.criterion, max_score=10),
        answer=StudentAnswerInfo(q_id="q1", number="1", type=question.type, content="answer"),
        student_id=student_id, registry=registry, multi_sample_n=3, reporter=reporter,
        aggregation_method="weighted_average", grading_setup=TaskGradingSetup.model_validate(config))))
    assert calls == ["q1", "q1"]  # Successful sample 1 is never sent again.
    assert model["succeeded"] == 3 and len(model["completion_times"]) == 2
    assert result.synthesis_method == "multi_sample"
    assert len(grades.list_results_for_run(third)) == 1  # Saved before batch completion.
    stop(case, third)
    assert len(grades.list_results_for_run(third)) == 2


def test_manual_handoff_requires_only_missing_score_to_be_filled_and_confirmed(case):
    run_id = start(case, "manual-start")["job_id"]
    checkpoints, _, revisions, setup = prepare(case, run_id)
    student = revisions[0][1]
    provider = setup.setup["selected_provider_ids"][0]
    checkpoints.save_expert(student, "q1", provider, 0,
        ExpertResult(provider=provider, provider_id=provider, score=8, confidence=.9, comment="Saved"))
    stop(case, run_id)
    revision = workflows.get_live_workflow(case[0], owner_id=case[1]).workflow_revision
    with pytest.raises(VersionConflict):
        continue_manual_grading(task_id=case[0], owner_id=case[1], job_id=run_id, expected_revision=revision - 1)
    continue_manual_grading(task_id=case[0], owner_id=case[1], job_id=run_id, expected_revision=revision)
    assert task_facade.finalization(task_id=case[0], owner_id=case[1])["remaining_review_count"] == 1
    assert len(task_facade.task_results(task_id=case[0], owner_id=case[1])["results"][0]["corrections"]) == 2
    rows = sorted(grades.list_results_for_run(run_id), key=lambda row: row.q_id, reverse=True)
    with pytest.raises(ResultNotReleasable, match="unresolved_failures"):
        grades.release(run_id, teacher_id=case[1])
    missing = next(row for row in rows if row.q_id == "q2")
    grades.add_teacher_review(missing.id, teacher_id=case[1], new_score=8,
                              new_comment="Manually checked", confirm=True)
    grades.release(run_id, teacher_id=case[1])
    finalization = task_facade.finalization(task_id=case[0], owner_id=case[1])
    assert finalization["ready_for_confirmation"]
    assert finalization["required_review_count"] == finalization["confirmed_required_count"] == 1


def test_changed_model_config_does_not_mix_old_partial_votes(case, monkeypatch):
    run_id = start(case, "before-config-change")["job_id"]
    checkpoints, _, revisions, setup = prepare(case, run_id)
    provider = setup.setup["selected_provider_ids"][0]
    checkpoints.save_expert(revisions[0][1], "q1", provider, 0,
        ExpertResult(provider=provider, provider_id=provider, score=0, confidence=0, comment="A valid zero"))
    stop(case, run_id)
    monkeypatch.setattr("backend.services.grading_input_security.provider_configuration_fingerprint",
                        lambda **kwargs: "changed-model")
    resumed = retry(case, "after-config-change")
    assert workflows.get_run_setup(resumed).input_manifest["reused_experts"] == []


def test_old_stopped_run_without_saved_matrix_can_continue_manually(case):
    from sqlalchemy import delete
    from backend.db.models import GradeResultRecord
    from backend.db.session import session_scope
    run_id = start(case, "legacy-pause")["job_id"]
    stop(case, run_id)
    # Simulate the pre-checkpoint version: paused metadata, no persisted grades.
    with session_scope() as session:
        session.execute(delete(GradeResultRecord).where(GradeResultRecord.grading_run_id == run_id))
    continue_manual_grading(task_id=case[0], owner_id=case[1], job_id=run_id,
        expected_revision=workflows.get_live_workflow(case[0], owner_id=case[1]).workflow_revision)
    rows = grades.list_results_for_run(run_id)
    assert len(rows) == 2 and all(row.ai_score is None for row in rows)
    assert task_facade.finalization(task_id=case[0], owner_id=case[1])["remaining_review_count"] == 2


def test_live_stop_after_one_sample_survives_before_whole_question_completes(case, monkeypatch):
    flow = workflows.get_live_workflow(case[0], owner_id=case[1])
    config = {**flow.grading_setup, "multi_sample_n": 3, "aggregation_method": "weighted_average"}
    workflows.update_workflow(case[0], owner_id=case[1], grading_setup=config)
    run_id = start(case, "live-stop")["job_id"]
    provider_id = config["selected_provider_ids"][0]
    provider = SimpleNamespace(provider_id=provider_id, model="test-model")
    registry = SimpleNamespace(list_available=lambda: [provider], get=lambda key: provider,
                               count=lambda: 1, select=lambda *args, **kwargs: registry)
    from backend.agents import multi_expert
    from backend.services.execution_control import cancel_local

    async def scenario():
        saved = asyncio.Event()
        started = []
        class Skill:
            name = "paused-model"
            def __init__(self, *args, **kwargs):
                pass
            async def grade(self, problem, answer, student_id):
                started.append(problem.q_id)
                if len(started) == 1:
                    return ExpertResult(provider=provider_id, score=7, confidence=.9, comment="Saved")
                await asyncio.Event().wait()
        monkeypatch.setattr(multi_expert, "get_skill_for_type", lambda _type: Skill)
        original_save = GradingCheckpoints.save_expert
        def save(self, *args):
            original_save(self, *args)
            saved.set()
        monkeypatch.setattr(GradingCheckpoints, "save_expert", save)
        worker = asyncio.create_task(grading_runs.process_run(run_id=run_id, worker_id="live-worker", registry=registry))
        await asyncio.wait_for(saved.wait(), timeout=3)
        stop(case, run_id)
        cancel_local(run_id)
        await asyncio.wait_for(worker, timeout=3)
        rows = grades.list_results_for_run(run_id)
        assert len(rows) == 2
        assert sum(row.ai_score is not None for row in rows) == 1
        assert sum(e.get("error_kind") == "operation_cancelled" for row in rows for e in row.ai_expert_results) == 5
        assert task_facade._grading_progress(run_id, case[1])["completed_units"] == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("value", [0, 8])
@pytest.mark.parametrize("saved_count", [1, 2])
def test_paused_valid_scores_release_without_teacher_confirmation(case, value, saved_count):
    flow = workflows.get_live_workflow(case[0], owner_id=case[1])
    config = {**flow.grading_setup, "multi_sample_n": 2, "aggregation_method": "weighted_average"}
    workflows.update_workflow(case[0], owner_id=case[1], grading_setup=config)
    run_id = start(case, "release-valid-paused")["job_id"]
    checkpoints, questions, revisions, _ = prepare(case, run_id)
    provider = config["selected_provider_ids"][0]
    for question in questions:
        for sample in range(1, saved_count + 1):
            checkpoints.save_expert(revisions[0][1], question.q_id, provider, sample,
                ExpertResult(provider=provider, provider_id=provider, score=value, confidence=0,
                             comment="A valid score with uncertain confidence"))
    stop(case, run_id)
    assert grades.get_run(run_id).status == "cancelled"
    continue_manual_grading(task_id=case[0], owner_id=case[1], job_id=run_id,
        expected_revision=workflows.get_live_workflow(case[0], owner_id=case[1]).workflow_revision)
    final = task_facade.finalization(task_id=case[0], owner_id=case[1])
    assert final["remaining_review_count"] == 0 and final["ready_for_confirmation"]
    assert all(row.ai_score == value and not row.teacher_review for row in grades.list_results_for_run(run_id))
    assert grades.release(run_id, teacher_id=case[1]).released_at is not None


@pytest.mark.parametrize("status", ["cancelled", "failed", "partial_failed"])
def test_failed_grading_manual_handoff_survives_task_reads_and_teacher_edit(case, status):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.api import tasks
    from backend.db.models import GradingRunRecord
    from backend.db.session import session_scope
    app = FastAPI()
    app.include_router(tasks.router)
    app.dependency_overrides[tasks.require_teacher] = lambda: SimpleNamespace(id=case[1])
    client = TestClient(app)
    run_id = start(case, "failed-manual")["job_id"]
    if status == "partial_failed":
        checkpoints, _, revisions, setup = prepare(case, run_id)
        provider = setup.setup["selected_provider_ids"][0]
        checkpoints.save_expert(revisions[0][1], "q1", provider, 0,
            ExpertResult(provider=provider, provider_id=provider, score=8, confidence=.9, comment="Saved"))
    stop(case, run_id)
    if status != "cancelled":
        with session_scope() as session:
            row = session.get(GradingRunRecord, run_id)
            row.status, row.error_message = status, "provider_unavailable"
            flow = session.get(workflows.AssignmentWorkflowRecord, case[0])
            # Match terminal worker projection, including partial_failed's null marker.
            flow.presentation_status = "error" if status == "failed" else "graded"
            flow.last_failed_job_id = run_id if status == "failed" else None
            flow.error_code = None
    url = f"/tasks/{case[0]}"
    before = client.get(url).json()
    assert before["status"] == ("grading_paused" if status == "cancelled" else "error")
    response = client.post(url + "/continue", json={"job_id": run_id, "manual": True,
        "expected_workflow_revision": before["workflow_revision"]})
    assert response.status_code == 200 and response.json()["status"] == "manual_review"
    remaining = 1 if status == "partial_failed" else 2
    for _ in range(2):  # Fresh requests / polling must not resurrect the old failure.
        for data in (client.get(url).json(), client.get(url + "/state").json(),
                     client.get("/tasks/").json()[case[0]]):
            assert data["status"] == "graded", data
            assert data["error"] is None and data["last_failed_job_id"] is None
        assert client.get(url + "/finalization").json()["remaining_review_count"] == remaining
    results = client.get(url + "/result").json()["results"]
    assert len(results[0]["corrections"]) == 2
    student = results[0]["student_id"]
    for q_id in (["q2"] if status == "partial_failed" else ["q1", "q2"]):
        current = client.get(url).json()
        edited = client.put(url + f"/reviews/{student}/{q_id}", json={
            "teacher_score": 0, "teacher_comment": "Manually checked", "confirm": True,
            "expected_workflow_revision": current["workflow_revision"]})
        assert edited.status_code == 200, edited.text
        assert client.get(url).json()["status"] == "graded"
    assert client.get(url + "/finalization").json()["remaining_review_count"] == 0
    # A new run must not inherit the earlier manual handoff and hide its failure.
    next_run = start(case, "new-failure-after-manual")["job_id"]
    stop(case, next_run)
    assert client.get(url).json()["status"] == "grading_paused"
