from types import SimpleNamespace
import io
import json
import zipfile

import pytest
from fastapi import BackgroundTasks, FastAPI, UploadFile
from fastapi.testclient import TestClient

from backend.api import task_preparation, tasks
from backend.auth import require_teacher
from backend.db import workflow_repository
from backend.domain.errors import NotFound
from backend.services import task_facade, workflow_inputs
from backend.tests.test_question_preparation_recovery import _queue_question_preparation, _RecoveryRegistry
from backend.tests.test_task_background_workflows import _seed_task, _Registry


def fail(owner, task, queued, code="provider_timeout"):
    operation = workflow_repository.get_operation(queued["job_id"], owner_id=owner)
    assert task_facade._fail_operation(task, owner, operation.id, operation.attempt, code)
    return workflow_repository.get_live_workflow(task, owner_id=owner)


@pytest.mark.asyncio
async def test_question_input_read_is_owner_bound_allowlisted_and_does_not_replay():
    owner, task = _seed_task()
    source, queued, background = await _queue_question_preparation(owner, task)
    workflow = fail(owner, task, queued)
    snapshot = workflow_inputs.question_inputs(task_id=task, owner_id=owner)
    assert snapshot["workflow_revision"] == workflow.workflow_revision
    assert snapshot["input"]["sources"][0]["source_token"] == source["source_token"]
    assert snapshot["input"]["sources"][0]["inline_text"] == "1. (a) Explain A. (b) Explain B."
    assert snapshot["input"]["score_policy"]["uniform_max_score"] == 10
    assert "provider_configuration_fingerprint" not in json.dumps(snapshot)
    assert "checkpoint" not in snapshot
    assert background.calls == []
    before = workflow_repository.get_operation(queued["job_id"], owner_id=owner)
    workflow_inputs.question_inputs(task_id=task, owner_id=owner)
    after = workflow_repository.get_operation(before.id, owner_id=owner)
    assert (before.attempt, before.updated_at) == (after.attempt, after.updated_at)
    other, _ = _seed_task()
    with pytest.raises(NotFound):
        workflow_inputs.question_inputs(task_id=task, owner_id=other)


@pytest.mark.asyncio
async def test_explicit_current_configuration_retry_preserves_old_evidence():
    owner, task = _seed_task()
    source, queued, background = await _queue_question_preparation(owner, task)
    workflow = fail(owner, task, queued)
    changed = _RecoveryRegistry(model="changed-model")
    request = task_preparation.RetryQuestionPreparationRequest(
        expected_workflow_revision=workflow.workflow_revision,
        recognition_provider_id="test-provider", use_current_configuration=True,
    )
    result = await task_preparation.retry_question_preparation(task, queued["job_id"], request, BackgroundTasks(), SimpleNamespace(id=owner), changed)
    assert result["status"] == "started"
    assert result["job_id"] != queued["job_id"]
    original = workflow_repository.get_operation(queued["job_id"], owner_id=owner)
    current = workflow_repository.get_operation(result["job_id"], owner_id=owner)
    assert original.status == "error" and original.attempt == 1
    assert current.payload["source_tokens"] == [source["source_token"]]
    assert original.payload["provider_configuration_fingerprint"] != current.payload["provider_configuration_fingerprint"]
    assert background.calls == []


def queue_submission(owner, task):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("student.txt", "original answer")
    queued = task_facade.queue_task_submission_parsing(
        task_id=task, owner_id=owner, filename="answers.zip", content=buffer.getvalue(),
        content_type="application/zip", registry=_Registry(), identity_mode="roster",
        roster_entries=[{"stu_id": "S1", "stu_name": "Synthetic learner"}], roster_name="roster.csv",
    )
    return queued, buffer.getvalue()


@pytest.mark.asyncio
async def test_saved_zip_and_roster_restore_then_apply_current_options_without_upload():
    owner, task = _seed_task(with_question=True)
    queued, content = queue_submission(owner, task)
    workflow = fail(owner, task, queued)
    snapshot = workflow_inputs.submission_inputs(task_id=task, owner_id=owner)["input"]
    assert snapshot["identity_mode"] == "roster" and snapshot["roster_count"] == 1
    assert snapshot["roster_name"] == "roster.csv"
    _, restored = workflow_inputs.load_submission_input(task_id=task, owner_id=owner, stored_file_id=snapshot["stored_file_id"])
    assert restored == content
    result = await tasks.parse_submissions_endpoint(
        task_id=task, background_tasks=BackgroundTasks(), file=None,
        stored_file_id=snapshot["stored_file_id"], identity_mode="manual_review", roster_file=None,
        reuse_roster_from_job_id=queued["job_id"], recognition_provider_id="test-provider",
        replace_confirmed=False, expected_workflow_revision=workflow.workflow_revision,
        current=SimpleNamespace(id=owner), registry=_Registry(),
    )
    assert result["status"] == "started"
    operation = workflow_repository.get_operation(result["job_id"], owner_id=owner)
    assert operation.payload["identity_mode"] == "manual_review"
    assert operation.payload["roster_entries"] == []
    assert operation.payload["input_file_id"] == snapshot["stored_file_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("replace_roster", [False, True])
async def test_saved_roster_reused_and_new_roster_overrides_it(replace_roster):
    owner, task = _seed_task(with_question=True)
    queued, _ = queue_submission(owner, task)
    workflow = fail(owner, task, queued)
    snapshot = workflow_inputs.submission_inputs(task_id=task, owner_id=owner)["input"]
    result = await tasks.parse_submissions_endpoint(
        task_id=task, background_tasks=BackgroundTasks(), file=None, stored_file_id=snapshot["stored_file_id"],
        identity_mode="roster", roster_file=UploadFile(file=io.BytesIO(b"student_id,name\nS2,New learner\n"), filename="new.csv") if replace_roster else None,
        reuse_roster_from_job_id=queued["job_id"], recognition_provider_id="test-provider", replace_confirmed=False,
        expected_workflow_revision=workflow.workflow_revision, current=SimpleNamespace(id=owner), registry=_Registry(),
    )
    operation = workflow_repository.get_operation(result["job_id"], owner_id=owner)
    assert operation.payload["roster_name"] == ("new.csv" if replace_roster else "roster.csv")
    assert operation.payload["roster_entries"][0]["stu_id"] == ("S2" if replace_roster else "S1")


@pytest.mark.asyncio
async def test_uncertain_submission_requires_explicit_ack_and_creates_distinct_intent():
    owner, task = _seed_task(with_question=True)
    queued, _ = queue_submission(owner, task)
    workflow = fail(owner, task, queued, "provider_submit_uncertain")
    request = tasks.RetrySubmissionRecognitionRequest(expected_workflow_revision=workflow.workflow_revision)
    refused = await tasks.retry_submission_recognition_endpoint(task, queued["job_id"], request, BackgroundTasks(), SimpleNamespace(id=owner), _Registry())
    assert refused.status_code == 409
    assert "provider_submit_uncertain" in refused.body.decode()
    request.acknowledge_possible_duplicate_call = True
    restarted = await tasks.retry_submission_recognition_endpoint(task, queued["job_id"], request, BackgroundTasks(), SimpleNamespace(id=owner), _Registry())
    assert restarted["status"] == "started" and restarted["job_id"] != queued["job_id"]
    assert workflow_repository.get_operation(queued["job_id"], owner_id=owner).status == "error"


@pytest.mark.asyncio
async def test_mounted_read_routes_and_saved_inputs_isolate_users_tasks_and_revisions():
    owner, task = _seed_task(with_question=True)
    other, other_task = _seed_task(with_question=True)
    queued, _ = queue_submission(owner, task)
    workflow = fail(owner, task, queued)
    snapshot = workflow_inputs.submission_inputs(task_id=task, owner_id=owner)["input"]
    app = FastAPI()
    app.include_router(tasks.router)
    app.include_router(task_preparation.router)
    app.dependency_overrides[require_teacher] = lambda: SimpleNamespace(id=owner)
    client = TestClient(app)
    assert client.get(f"/tasks/{task}/submission-recognition/input").json()["input"] == snapshot
    assert client.get(f"/tasks/{task}/question-preparation/input").json()["input"] is None
    app.dependency_overrides[require_teacher] = lambda: SimpleNamespace(id=other)
    assert client.get(f"/tasks/{task}/submission-recognition/input").status_code == 404
    with pytest.raises(NotFound):
        workflow_inputs.load_submission_input(task_id=other_task, owner_id=owner, stored_file_id=snapshot["stored_file_id"])
    stale = await tasks.parse_submissions_endpoint(task_id=task, background_tasks=BackgroundTasks(), file=None,
        stored_file_id=snapshot["stored_file_id"], identity_mode="filename", roster_file=None,
        expected_workflow_revision=workflow.workflow_revision - 1, recognition_provider_id="test-provider", replace_confirmed=False,
        current=SimpleNamespace(id=owner), registry=_Registry())
    assert stale.status_code == 409
