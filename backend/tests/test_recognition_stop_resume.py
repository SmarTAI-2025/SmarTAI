"""Real persisted stop/start/retry boundaries; no external provider calls."""
import asyncio
from types import SimpleNamespace

import pytest

from backend.api import task_preparation, tasks
from backend.db import workflow_repository as flows
from backend.db.session import session_scope
from backend.services import task_facade
from backend.services.task_execution import stop_task_run
from backend.tests.test_task_background_workflows import _BackgroundTasks, _Registry, _seed_task
from backend.tests.test_question_preparation_recovery import (
    _RecoveryRegistry, _queue_question_preparation, _claim, _install_incremental_preparer,
)


def assert_started(response):
    assert isinstance(response, dict), getattr(response, "body", response)
    assert response["status"] == "started", response
    return response["job_id"]


def stop(owner, task, job):
    workflow = flows.get_live_workflow(task, owner_id=owner)
    assert stop_task_run(task_id=task, owner_id=owner, job_id=job,
                         expected_revision=workflow.workflow_revision)["status"] == "stopped"
    return flows.get_live_workflow(task, owner_id=owner).workflow_revision


@pytest.mark.asyncio
@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize("changed_model", [False, True])
async def test_questions_stop_continue_twice_then_complete(monkeypatch, running, changed_model):
    owner, task = _seed_task()
    _, first, _ = await _queue_question_preparation(owner, task)
    job = first["job_id"]
    registry = _RecoveryRegistry("changed-model") if changed_model else _RecoveryRegistry()
    for index in range(2):
        if running:
            _claim(owner, job, f"stopped-{index}")
        revision = stop(owner, task, job)
        response = await task_preparation.retry_question_preparation(
            task_id=task, job_id=job,
            request=task_preparation.RetryQuestionPreparationRequest(
                expected_workflow_revision=revision, use_current_configuration=True,
                acknowledge_possible_duplicate_call=True),
            background_tasks=_BackgroundTasks(), current=SimpleNamespace(id=owner), registry=registry)
        job = assert_started(response)
        workflow = flows.get_live_workflow(task, owner_id=owner)
        assert workflow.active_job_id == job and workflow.last_failed_job_id is None
    calls = []
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: registry)
    _install_incremental_preparer(monkeypatch, calls, question_count=1)
    await task_preparation.run_durable_question_preparation(_claim(owner, job, "finish"))
    assert flows.get_operation(job, owner_id=owner).status == "done"
    assert calls == ["q1"]


@pytest.mark.asyncio
async def test_question_stop_does_not_allow_retry_after_a_real_task_edit():
    owner, task = _seed_task()
    _, first, _ = await _queue_question_preparation(owner, task)
    revision = stop(owner, task, first["job_id"])
    flows.update_workflow(task, owner_id=owner, expected_revision=revision, bump_revision=True,
                          problem_file_name="changed.txt")
    workflow = flows.get_live_workflow(task, owner_id=owner)
    response = await task_preparation.retry_question_preparation(
        task_id=task, job_id=first["job_id"],
        request=task_preparation.RetryQuestionPreparationRequest(expected_workflow_revision=workflow.workflow_revision),
        background_tasks=_BackgroundTasks(), current=SimpleNamespace(id=owner), registry=_RecoveryRegistry())
    assert response.status_code == 409
    assert "stale_revision" in response.body.decode()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_model", [False, True])
async def test_questions_stop_then_fresh_configuration_start(monkeypatch, changed_model):
    owner, task = _seed_task()
    _, first, _ = await _queue_question_preparation(owner, task)
    revision = stop(owner, task, first["job_id"])
    registry = _RecoveryRegistry("changed-model") if changed_model else _RecoveryRegistry()
    # Upload/configuration re-preflights the retained source at the current revision.
    source = await task_preparation.preflight_problem_source(task_id=task, file=None,
        library_material_id=None, stored_file_id=None, inline_text="1. (a) Explain A. (b) Explain B.",
        structure_mode="organized", role="problem", extraction_hint="", save_to_library=False,
        recognition_provider_id="test-provider", current=SimpleNamespace(id=owner), registry=registry)
    response = await task_preparation._start_question_preparation(task_id=task,
        request=task_preparation.StartQuestionPreparationRequest(source_tokens=[source["source_token"]],
            expected_workflow_revision=revision, recognition_provider_id="test-provider"),
        background_tasks=_BackgroundTasks(), current=SimpleNamespace(id=owner), registry=registry,
        allow_prepared_source_reuse=False)
    assert assert_started(response) != first["job_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize("from_configuration", [False, True])
@pytest.mark.parametrize("changed_model", [False, True])
async def test_submissions_stop_then_continue_or_start_from_saved_configuration(running, from_configuration, changed_model):
    owner, task = _seed_task(with_question=True)
    first = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename="answer.txt", content=b"answer", content_type="text/plain", registry=_Registry(),
        expected_workflow_revision=0)
    job = first["job_id"]
    registry = _RecoveryRegistry("changed-model") if changed_model else _Registry()
    for index in range(2):
        if running:
            _claim(owner, job, f"stopped-{index}")
        revision = stop(owner, task, job)
        operation = flows.get_operation(job, owner_id=owner)
        if from_configuration:
            response = await tasks.parse_submissions_endpoint(task_id=task, file=None,
                stored_file_id=operation.payload["input_file_id"], identity_mode="filename", roster_file=None,
                recognition_provider_id="test-provider", replace_confirmed=False,
                expected_workflow_revision=revision, acknowledge_possible_duplicate_call=True,
                background_tasks=_BackgroundTasks(), current=SimpleNamespace(id=owner), registry=registry)
        else:
            response = await tasks.retry_submission_recognition_endpoint(task_id=task, job_id=job,
                request=tasks.RetrySubmissionRecognitionRequest(expected_workflow_revision=revision,
                    acknowledge_possible_duplicate_call=True),
                background_tasks=_BackgroundTasks(), current=SimpleNamespace(id=owner), registry=registry)
        job = assert_started(response)
        workflow = flows.get_live_workflow(task, owner_id=owner)
        assert workflow.active_job_id == job
        assert task_facade.get_task(task_id=task, owner_id=owner)["status"] == "parsing_submissions"


@pytest.mark.asyncio
@pytest.mark.parametrize("inflight", [False, True])
async def test_questions_stop_preserves_saved_results_and_requires_inflight_ack(monkeypatch, inflight):
    owner, task = _seed_task()
    _, first, _ = await _queue_question_preparation(owner, task, source_text="1. First.\n2. Second.")
    calls = []
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: _RecoveryRegistry())
    _install_incremental_preparer(monkeypatch, calls,
        question_count=2, cancel_after_question=None if inflight else "q1",
        cancel_after_question_started="q2" if inflight else None)
    with pytest.raises(asyncio.CancelledError):
        await task_preparation.run_durable_question_preparation(_claim(owner, first["job_id"], "first"))
    revision = stop(owner, task, first["job_id"])
    request = task_preparation.RetryQuestionPreparationRequest(expected_workflow_revision=revision)
    async def retry():
        return await task_preparation.retry_question_preparation(task_id=task, job_id=first["job_id"],
            request=request, background_tasks=_BackgroundTasks(), current=SimpleNamespace(id=owner),
            registry=_RecoveryRegistry())
    if inflight:
        rejected = await retry()
        assert rejected.status_code == 409
        assert "provider_submit_uncertain" in rejected.body.decode()
        request.acknowledge_possible_duplicate_call = True
    job = assert_started(await retry())
    _install_incremental_preparer(monkeypatch, calls, question_count=2)
    await task_preparation.run_durable_question_preparation(_claim(owner, job, "finish"))
    assert flows.get_operation(job, owner_id=owner).status == "done"
    assert calls == ["q1", "q2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("from_configuration", [False, True])
async def test_submissions_stopped_during_ocr_requires_ack_then_starts(from_configuration):
    owner, task = _seed_task(with_question=True)
    first = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename="answer.txt", content=b"answer", content_type="text/plain", registry=_Registry(),
        expected_workflow_revision=0)
    job = first["job_id"]
    operation = flows.get_operation(job, owner_id=owner)
    with session_scope() as session:
        record = session.get(flows.WorkflowOperationRecord, job)
        record.checkpoint = {**(record.checkpoint or {}), "ocr_inflight_source_id": "source-1"}
    revision = stop(owner, task, job)
    async def restart(ack):
        if from_configuration:
            return await tasks.parse_submissions_endpoint(task_id=task, file=None,
                stored_file_id=operation.payload["input_file_id"], identity_mode="filename", roster_file=None,
                recognition_provider_id="test-provider", replace_confirmed=False,
                expected_workflow_revision=revision, acknowledge_possible_duplicate_call=ack,
                background_tasks=_BackgroundTasks(), current=SimpleNamespace(id=owner), registry=_Registry())
        return await tasks.retry_submission_recognition_endpoint(task_id=task, job_id=job,
            request=tasks.RetrySubmissionRecognitionRequest(expected_workflow_revision=revision,
                acknowledge_possible_duplicate_call=ack), background_tasks=_BackgroundTasks(),
            current=SimpleNamespace(id=owner), registry=_Registry())
    rejected = await restart(False)
    assert rejected.status_code == 409
    assert "provider_submit_uncertain" in rejected.body.decode()
    assert assert_started(await restart(True)) != job


@pytest.mark.asyncio
async def test_stopped_questions_can_handoff_saved_results_to_manual_review(monkeypatch):
    owner, task = _seed_task()
    _, first, _ = await _queue_question_preparation(owner, task, source_text="1. First.\n2. Second.")
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: _RecoveryRegistry())
    calls = []
    _install_incremental_preparer(monkeypatch, calls, question_count=2, cancel_after_question="q1")
    with pytest.raises(asyncio.CancelledError):
        await task_preparation.run_durable_question_preparation(_claim(owner, first["job_id"], "first"))
    revision = stop(owner, task, first["job_id"])
    response = await task_preparation.manually_complete_question_preparation(task, first["job_id"],
        task_preparation.ManualQuestionPreparationRequest(expected_workflow_revision=revision),
        current=SimpleNamespace(id=owner))
    assert isinstance(response, dict), getattr(response, "body", response)
    assert response["status"] == "ready_for_review"
    assert response["first_question_id"] == "q2"
    assert calls == ["q1"]

