from __future__ import annotations

import io
import zipfile

import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers

from backend.api import tasks
from backend.agents.ingest_agent import SubmissionSourceParseResult
from backend.db import file_repository, source_outcome_repository, workflow_repository
from backend.services import task_facade
from backend.services.workflow_worker import LeasedOperation
from backend.storage import get_storage
from backend.tests.test_task_background_workflows import _BackgroundTasks, _Registry, _seed_task


def _archive_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("student.txt", "answer")
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_submission_queue_persists_archive_and_has_no_request_task():
    owner_id, task_id = _seed_task(with_question=True)
    background = _BackgroundTasks()
    upload = UploadFile(
        file=io.BytesIO(b"archive-bytes"), filename="answers.zip",
        headers=Headers({"content-type": "application/zip"}),
    )

    response = await tasks.parse_submissions_endpoint(
        task_id=task_id, background_tasks=background, file=upload,
        identity_mode="filename", roster_file=None,
        recognition_provider_id=None, replace_confirmed=False,
        current=type("User", (), {"id": owner_id})(), registry=_Registry(),
    )

    assert response["status"] == "started"
    assert background.calls == []
    operation = workflow_repository.get_operation(response["job_id"], owner_id=owner_id)
    assert operation.status == "pending"
    assert set(operation.payload) == {
        "input_file_id", "source_ids", "base_workflow_revision", "identity_mode",
        "roster_entries", "roster_name", "recognition_provider_id",
        "replace_confirmed",
    }
    sources = source_outcome_repository.list_sources(
        operation_id=operation.id, owner_id=owner_id, attempt=operation.attempt,
    )
    assert sources == []
    stored = file_repository.get_file(
        file_id=operation.payload["input_file_id"], owner_id=owner_id
    )
    assert stored is not None
    assert stored.id in operation.artifact_refs
    with get_storage().open(stored.storage_key) as stream:
        assert stream.read() == b"archive-bytes"


@pytest.mark.asyncio
async def test_fresh_submission_worker_recovers_archive_and_commits_once(monkeypatch):
    owner_id, task_id = _seed_task(with_question=True)
    content = _archive_bytes()
    queued = task_facade.queue_task_submission_parsing(
        task_id=task_id, owner_id=owner_id, filename="answers.zip",
        content=content, content_type="application/zip",
        registry=_Registry(),
    )
    calls = {"parse": 0}

    async def fake_parse(sources, problems, provider, **kwargs):
        calls["parse"] += 1
        source = sources[0]
        student = {
            "stu_id": "student-1", "stu_name": "Student One",
            "stu_ans": [{"q_id": "q1", "content": "answer", "flag": []}],
            "source_filename": source.filename,
            "source_id": source.source_id,
            "stored_file_id": source.stored_file_id,
            "identity_status": "matched",
        }
        return [SubmissionSourceParseResult(
            source_id=source.source_id,
            stored_file_id=source.stored_file_id,
            filename=source.filename,
            status="parsed",
            student=student,
            student_candidate="student-1",
            matched_answer_count=1,
            unknown_question_ids=(),
            stable_error_code=None,
            failure_phase=None,
            retryable=False,
        )]

    monkeypatch.setattr(task_facade, "parse_student_answer_sources", fake_parse)
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _owner: _Registry())

    claimed = workflow_repository.claim_operation(
        queued["job_id"], owner_id=owner_id, worker_id="fresh-worker", lease_seconds=60,
    )
    await task_facade.run_durable_submission_recognition(
        LeasedOperation(claimed, worker_id="fresh-worker", lease_seconds=60)
    )

    operation = workflow_repository.get_operation(queued["job_id"], owner_id=owner_id)
    assert calls == {"parse": 1}
    assert operation.status == "done"
    assert operation.lease_token is None


@pytest.mark.asyncio
async def test_submission_worker_reuses_result_artifact_without_provider_call(monkeypatch):
    owner_id, task_id = _seed_task(with_question=True)
    queued = task_facade.queue_task_submission_parsing(
        task_id=task_id, owner_id=owner_id, filename="student.txt",
        content=b"answer", content_type="text/plain",
        registry=_Registry(),
    )
    operation = workflow_repository.get_operation(queued["job_id"], owner_id=owner_id)
    source = source_outcome_repository.list_sources(
        operation_id=operation.id,
        owner_id=owner_id,
        attempt=operation.attempt,
    )[0]
    result = SubmissionSourceParseResult(
        source_id=source.id,
        stored_file_id=source.stored_file_id,
        filename=source.original_name,
        status="parsed",
        student={
            "stu_id": "student-1", "stu_name": "Student One",
            "stu_ans": [{"q_id": "q1", "content": "answer", "flag": []}],
            "source_filename": source.original_name,
            "source_id": source.id,
            "stored_file_id": source.stored_file_id,
            "identity_status": "matched",
        },
        student_candidate="student-1",
        matched_answer_count=1,
        unknown_question_ids=(),
        stable_error_code=None,
        failure_phase=None,
        retryable=False,
    )
    artifact = file_repository.save_file(
        storage=get_storage(), owner_id=owner_id,
        kind="submission_recognition_result",
        original_name=f"{operation.id}-attempt-{operation.attempt}-parsed.json",
        content=task_facade._serialize_submission_results([result]),
        content_type="application/json", assignment_id=task_id,
    )
    operation = workflow_repository.save_operation_checkpoint(
        operation.id,
        owner_id=owner_id,
        expected_attempt=operation.attempt,
        expected_checkpoint_revision=operation.checkpoint_revision,
        stage="submissions_parsed",
        checkpoint={"parsed_artifact_id": artifact.id},
        artifact_refs=[*operation.artifact_refs, artifact.id],
    )

    async def unexpected(*args, **kwargs):
        raise AssertionError("completed provider stage was repeated")

    monkeypatch.setattr(task_facade, "prepare_submission_sources", unexpected)
    monkeypatch.setattr(task_facade, "parse_student_answer_sources", unexpected)
    claimed = workflow_repository.claim_operation(
        operation.id, owner_id=owner_id, worker_id="fresh-worker", lease_seconds=60,
    )
    await task_facade.run_durable_submission_recognition(
        LeasedOperation(claimed, worker_id="fresh-worker", lease_seconds=60)
    )

    completed = workflow_repository.get_operation(operation.id, owner_id=owner_id)
    outcome = source_outcome_repository.get_outcome(source.id, owner_id=owner_id)
    assert completed.status == "done"
    assert completed.checkpoint["parsed_artifact_id"] == artifact.id
    assert outcome is not None
    assert outcome.artifact_file_id == artifact.id


def test_stale_submission_lease_cannot_commit_or_fail_current_attempt():
    owner_id, task_id = _seed_task(with_question=True)
    queued = task_facade.queue_task_submission_parsing(
        task_id=task_id, owner_id=owner_id, filename="student.txt",
        content=b"answer", content_type="text/plain",
        registry=_Registry(),
    )
    old = workflow_repository.claim_operation(
        queued["job_id"], owner_id=owner_id, worker_id="old", lease_seconds=60,
    )
    workflow_repository.release_operation(
        old.id, owner_id=owner_id, worker_id="old", lease_token=old.lease_token,
    )
    current = workflow_repository.claim_operation(
        old.id, owner_id=owner_id, worker_id="current", lease_seconds=60,
    )

    from backend.domain.errors import LeaseLost
    assignment = task_facade.assignment_repository.get_assignment(task_id, actor_id=owner_id)
    with pytest.raises(LeaseLost):
        task_facade._commit_imported_submissions(
            task_id=task_id, owner_id=owner_id, course_id=assignment.course_id,
            students=[], expected_workflow_revision=1,
            operation_id=old.id, expected_operation_attempt=old.attempt,
            expected_lease_token=old.lease_token,
        )
    with pytest.raises(LeaseLost):
        task_facade._fail_operation(
            task_id, owner_id, old.id, old.attempt, "submission_parse_failed",
            expected_lease_token=old.lease_token,
        )
    assert workflow_repository.get_operation(
        old.id, owner_id=owner_id
    ).lease_token == current.lease_token
    source = source_outcome_repository.list_sources(
        operation_id=old.id, owner_id=owner_id, attempt=old.attempt,
    )[0]
    assert source_outcome_repository.get_outcome(source.id, owner_id=owner_id) is None


@pytest.mark.asyncio
async def test_submission_failure_records_fenced_source_outcome(monkeypatch):
    owner_id, task_id = _seed_task(with_question=True)
    queued = task_facade.queue_task_submission_parsing(
        task_id=task_id, owner_id=owner_id, filename="answers.zip",
        content=b"archive-bytes", content_type="application/zip",
        registry=_Registry(),
    )
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _owner: _Registry())
    claimed = workflow_repository.claim_operation(
        queued["job_id"], owner_id=owner_id,
        worker_id="fresh-worker", lease_seconds=60,
    )
    await task_facade.run_durable_submission_recognition(
        LeasedOperation(claimed, worker_id="fresh-worker", lease_seconds=60)
    )

    source = source_outcome_repository.list_sources(
        operation_id=queued["job_id"], owner_id=owner_id, attempt=1,
    )[0]
    outcome = source_outcome_repository.get_outcome(source.id, owner_id=owner_id)
    operation = workflow_repository.get_operation(queued["job_id"], owner_id=owner_id)
    assert operation.status == "error"
    assert outcome is not None
    assert outcome.status == "parse_failed"
    assert outcome.stable_error_code == "submission_source_content_type_mismatch"
    assert outcome.failure_phase == "source_read"
    assert outcome.retryable is False
