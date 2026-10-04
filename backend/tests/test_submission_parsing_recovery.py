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
async def test_partial_batch_stays_failed_and_explicit_retry_calls_only_failed_source(monkeypatch):
    from types import SimpleNamespace
    owner, task = _seed_task(with_question=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("one.txt", "first answer")
        archive.writestr("two.txt", "second answer")
    reads, parsed = [], []
    round_number = 0

    async def read(**kw):
        reads.append(kw["filename"])
        return SimpleNamespace(text=kw["content"].decode(), recognition=None)

    async def parse(sources, *_args, **_kwargs):
        result = []
        for source in sources:
            parsed.append(source.filename)
            failed = source.filename == "two.txt" and round_number == 0
            student = None if failed else {"stu_id": source.filename, "stu_name": source.filename,
                "stu_ans": [{"q_id": "q1", "content": source.text, "flag": []}],
                "source_id": source.source_id, "stored_file_id": source.stored_file_id,
                "source_filename": source.filename, "identity_status": "matched"}
            result.append(SubmissionSourceParseResult(source.source_id, source.stored_file_id,
                source.filename, "parse_failed" if failed else "parsed", student,
                None if failed else source.filename, 0 if failed else 1, (),
                "provider_rate_limited" if failed else None, "recognition" if failed else None, failed))
        return result

    monkeypatch.setattr(task_facade, "read_question_source", read)
    monkeypatch.setattr(task_facade, "parse_student_answer_sources", parse)
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _owner: _Registry())
    first = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename="answers.zip", content=buf.getvalue(), content_type="application/zip", registry=_Registry())

    async def finish(job):
        row = workflow_repository.claim_operation(job, owner_id=owner, worker_id="test", lease_seconds=60)
        await task_facade.run_durable_submission_recognition(LeasedOperation(row, worker_id="test", lease_seconds=60))

    await finish(first["job_id"])
    snapshot = task_facade.get_task(task_id=task, owner_id=owner)
    assert snapshot["status"] == "error"
    assert snapshot["student_data"] == {}
    assert snapshot["submission_source_summary"]["parsed"] == 1
    assert snapshot["submission_source_summary"]["failed"] == 1
    round_number = 1
    retried = await tasks.retry_submission_recognition_endpoint(task_id=task, job_id=first["job_id"],
        request=tasks.RetrySubmissionRecognitionRequest(expected_workflow_revision=snapshot["workflow_revision"]),
        background_tasks=_BackgroundTasks(), current=SimpleNamespace(id=owner), registry=_Registry())
    await finish(retried["job_id"])
    assert reads == parsed == ["one.txt", "two.txt", "two.txt"]
    result = task_facade.get_task(task_id=task, owner_id=owner)
    assert result["status"] == "submissions_ready"
    assert len(result["student_data"]) == 2
    assert result["submission_source_summary"]["failed"] == 0


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
        "provider_configuration_fingerprint", "question_snapshot",
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
async def test_submission_retry_republishes_only_for_durable_worker():
    owner_id, task_id = _seed_task(with_question=True)
    queued = task_facade.queue_task_submission_parsing(
        task_id=task_id,
        owner_id=owner_id,
        filename="student.txt",
        content=b"answer",
        content_type="text/plain",
        registry=_Registry(),
    )
    operation = workflow_repository.get_operation(
        queued["job_id"], owner_id=owner_id
    )
    assert task_facade._fail_operation(
        task_id,
        owner_id,
        operation.id,
        operation.attempt,
        "provider_timeout",
    ) is True
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    background = _BackgroundTasks()

    retried = await tasks.retry_submission_recognition_endpoint(
        task_id=task_id,
        job_id=operation.id,
        request=tasks.RetrySubmissionRecognitionRequest(
            recognition_provider_id=None,
            expected_workflow_revision=workflow.workflow_revision,
        ),
        background_tasks=background,
        current=type("User", (), {"id": owner_id})(),
        registry=_Registry(),
    )

    assert retried["status"] == "started"
    assert retried["reused_original_upload"] is True
    assert background.calls == []
    retry_operation = workflow_repository.get_operation(
        retried["job_id"], owner_id=owner_id
    )
    assert retry_operation.status == "pending"
    assert retry_operation.payload["retry_from"] == {"operation_id": operation.id, "attempt": operation.attempt}
    assert workflow_repository.get_operation(operation.id, owner_id=owner_id).status == "error"
    assert retry_operation.payload["recognition_provider_id"] == "test-provider"


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
