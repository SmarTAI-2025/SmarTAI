from __future__ import annotations

import io
import json
import time
import uuid
import zipfile
from types import SimpleNamespace

import pytest

from backend.agents.ingest_agent import (
    SubmissionSourceInput,
    parse_student_answer_sources,
)
from backend.db import (
    assignment_repository,
    source_outcome_repository,
    workflow_repository,
)
from backend.db.file_repository import list_files
from backend.db.models import AssignmentRecord, CourseRecord, UserRecord
from backend.db.session import session_scope
from backend.services import submission_source_pipeline, task_facade
from backend.storage import get_storage
from backend.tools.file_processing import RawUploadSource


def _seed_task() -> tuple[str, str]:
    suffix = uuid.uuid4().hex[:10]
    owner_id = f"teacher_{suffix}"
    course_id = f"course_{suffix}"
    task_id = f"assignment_{suffix}"
    with session_scope() as session:
        session.add(UserRecord(
            id=owner_id,
            username=owner_id,
            password_hash="hash",
            role="teacher",
            is_active=True,
        ))
        session.flush()
        session.add(CourseRecord(
            id=course_id,
            name="Course",
            code=f"C-{suffix}",
            teacher_id=owner_id,
        ))
        session.flush()
        session.add(AssignmentRecord(
            id=task_id,
            course_id=course_id,
            teacher_id=owner_id,
            name="Assignment",
            status="draft",
            version=1,
        ))
    workflow_repository.ensure_workflow(assignment_id=task_id, owner_id=owner_id)
    return owner_id, task_id


def _create_operation(owner_id: str, task_id: str, *, input_hash: str | None = None):
    operation, created = workflow_repository.create_operation(
        assignment_id=task_id,
        owner_id=owner_id,
        operation_type="submission_recognition",
        input_hash=input_hash or uuid.uuid4().hex,
    )
    assert created is True
    return operation


def _raw_source() -> RawUploadSource:
    return RawUploadSource(
        filename="student.txt",
        content=b"student answer",
        content_type="text/plain",
    )


def _zip_sources(items: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in items.items():
            archive.writestr(name, content)
    return buffer.getvalue()


class _Registry:
    provider = SimpleNamespace(provider_id="test-provider")

    def pick_default(self):
        return self.provider

    def get(self, provider_id):
        return self.provider if provider_id == self.provider.provider_id else None

    def list_configs(self):
        return [{"provider_id": self.provider.provider_id, "enabled": True}]

    def pick_vision(self, _provider):
        return None


@pytest.mark.asyncio
async def test_register_failure_compensates_saved_file_metadata_and_object(monkeypatch):
    owner_id, task_id = _seed_task()
    operation = _create_operation(owner_id, task_id)
    saved = []
    real_save_file = submission_source_pipeline.save_file

    def capture_save_file(**kwargs):
        stored = real_save_file(**kwargs)
        saved.append(stored)
        return stored

    def fail_registration(**_kwargs):
        raise RuntimeError("injected_register_failure")

    monkeypatch.setattr(submission_source_pipeline, "save_file", capture_save_file)
    monkeypatch.setattr(
        submission_source_pipeline.source_outcome_repository,
        "register_source",
        fail_registration,
    )

    with pytest.raises(RuntimeError, match="submission_source_persistence_failed"):
        await submission_source_pipeline._persist_and_register(
            raw=_raw_source(),
            owner_id=owner_id,
            task_id=task_id,
            job_id=operation.id,
            job_attempt=operation.attempt,
            order_index=0,
        )

    assert len(saved) == 1
    assert list_files(owner_id=owner_id, assignment_id=task_id) == []
    assert not get_storage().exists(saved[0].storage_key)
    assert source_outcome_repository.list_sources(
        operation_id=operation.id,
        owner_id=owner_id,
        attempt=operation.attempt,
    ) == []


@pytest.mark.asyncio
async def test_same_attempt_replay_reuses_source_and_stored_file():
    owner_id, task_id = _seed_task()
    operation = _create_operation(owner_id, task_id)

    first_source_id, first_file_id = await submission_source_pipeline._persist_and_register(
        raw=_raw_source(),
        owner_id=owner_id,
        task_id=task_id,
        job_id=operation.id,
        job_attempt=operation.attempt,
        order_index=0,
    )
    replay_source_id, replay_file_id = await submission_source_pipeline._persist_and_register(
        raw=_raw_source(),
        owner_id=owner_id,
        task_id=task_id,
        job_id=operation.id,
        job_attempt=operation.attempt,
        order_index=0,
    )

    assert (replay_source_id, replay_file_id) == (first_source_id, first_file_id)
    assert [item.id for item in list_files(
        owner_id=owner_id,
        assignment_id=task_id,
    )] == [first_file_id]
    assert [item.id for item in source_outcome_repository.list_sources(
        operation_id=operation.id,
        owner_id=owner_id,
        attempt=operation.attempt,
    )] == [first_source_id]


@pytest.mark.asyncio
async def test_archive_checkpoint_failure_and_read_failure_cleans_unlinked_container(
    monkeypatch,
):
    owner_id, task_id = _seed_task()
    operation = _create_operation(owner_id, task_id)
    saved = []
    real_save_file = submission_source_pipeline.save_file
    real_get_operation = workflow_repository.get_operation
    reads = 0

    def capture_save_file(**kwargs):
        stored = real_save_file(**kwargs)
        saved.append(stored)
        return stored

    def fail_checkpoint(*_args, **_kwargs):
        raise RuntimeError("injected_checkpoint_failure")

    def fail_recovery_read(*args, **kwargs):
        nonlocal reads
        reads += 1
        if reads == 1:
            return real_get_operation(*args, **kwargs)
        raise RuntimeError("injected_recovery_read_failure")

    monkeypatch.setattr(submission_source_pipeline, "save_file", capture_save_file)
    monkeypatch.setattr(
        submission_source_pipeline.workflow_repository,
        "save_operation_checkpoint",
        fail_checkpoint,
    )
    monkeypatch.setattr(
        submission_source_pipeline.workflow_repository,
        "get_operation",
        fail_recovery_read,
    )

    with pytest.raises(RuntimeError, match="injected_checkpoint_failure"):
        await submission_source_pipeline._persist_archive_container(
            content=_zip_sources({"student.txt": b"answer"}),
            filename="submissions.zip",
            content_type="application/zip",
            owner_id=owner_id,
            task_id=task_id,
            job_id=operation.id,
            job_attempt=operation.attempt,
        )

    assert len(saved) == 1
    assert list_files(owner_id=owner_id, assignment_id=task_id) == []
    assert not get_storage().exists(saved[0].storage_key)


@pytest.mark.asyncio
async def test_committed_archive_checkpoint_survives_ambiguous_failure(monkeypatch):
    owner_id, task_id = _seed_task()
    operation = _create_operation(owner_id, task_id)
    real_checkpoint = workflow_repository.save_operation_checkpoint
    real_get_operation = workflow_repository.get_operation
    reads = 0

    def commit_then_raise(*args, **kwargs):
        real_checkpoint(*args, **kwargs)
        raise RuntimeError("injected_checkpoint_ack_loss")

    def fail_recovery_read(*args, **kwargs):
        nonlocal reads
        reads += 1
        if reads == 1:
            return real_get_operation(*args, **kwargs)
        raise RuntimeError("injected_recovery_read_failure")

    monkeypatch.setattr(
        submission_source_pipeline.workflow_repository,
        "save_operation_checkpoint",
        commit_then_raise,
    )
    monkeypatch.setattr(
        submission_source_pipeline.workflow_repository,
        "get_operation",
        fail_recovery_read,
    )

    with pytest.raises(RuntimeError, match="injected_checkpoint_ack_loss"):
        await submission_source_pipeline._persist_archive_container(
            content=_zip_sources({"student.txt": b"answer"}),
            filename="submissions.zip",
            content_type="application/zip",
            owner_id=owner_id,
            task_id=task_id,
            job_id=operation.id,
            job_attempt=operation.attempt,
        )

    persisted_operation = real_get_operation(operation.id, owner_id=owner_id)
    assert len(persisted_operation.artifact_refs) == 1
    stored = list_files(owner_id=owner_id, assignment_id=task_id)
    assert [item.id for item in stored] == persisted_operation.artifact_refs
    assert get_storage().exists(stored[0].storage_key)


@pytest.mark.asyncio
async def test_new_attempt_automatically_links_matching_prior_source():
    owner_id, task_id = _seed_task()
    input_hash = uuid.uuid4().hex
    operation = _create_operation(owner_id, task_id, input_hash=input_hash)
    first_source_id, first_file_id = await submission_source_pipeline._persist_and_register(
        raw=_raw_source(),
        owner_id=owner_id,
        task_id=task_id,
        job_id=operation.id,
        job_attempt=operation.attempt,
        order_index=0,
    )
    workflow_repository.update_operation(
        operation.id,
        owner_id=owner_id,
        expected_attempt=operation.attempt,
        status="error",
        error_code="submission_parse_failed",
        completed_at=time.time(),
    )
    retried_operation, created = workflow_repository.create_operation(
        assignment_id=task_id,
        owner_id=owner_id,
        operation_type="submission_recognition",
        input_hash=input_hash,
    )
    assert created is True
    assert retried_operation.id == operation.id
    assert retried_operation.attempt == operation.attempt + 1

    retried_source_id, retried_file_id = (
        await submission_source_pipeline._persist_and_register(
            raw=_raw_source(),
            owner_id=owner_id,
            task_id=task_id,
            job_id=retried_operation.id,
            job_attempt=retried_operation.attempt,
            order_index=0,
        )
    )
    retried_source = source_outcome_repository.get_source(
        retried_source_id,
        owner_id=owner_id,
    )

    assert retried_source.retry_of_source_id == first_source_id
    assert retried_file_id != first_file_id
    assert len(list_files(owner_id=owner_id, assignment_id=task_id)) == 2


@pytest.mark.asyncio
async def test_oversized_model_field_fails_only_its_source(monkeypatch):
    async def fake_invoke(_provider, messages):
        prompt = messages[-1].content
        is_oversized = "oversized.txt" in prompt
        return SimpleNamespace(content=json.dumps({
            "stu_id": "S" * 161 if is_oversized else "S002",
            "stu_name": "Student Two",
            "stu_ans": [{
                "q_id": "q1",
                "number": "1",
                "type": "short",
                "content": "answer",
                "flag": [],
            }],
        }))

    monkeypatch.setattr("backend.agents.ingest_agent.ainvoke_with_retry", fake_invoke)
    results = await parse_student_answer_sources(
        [
            SubmissionSourceInput(
                source_id="source-oversized",
                stored_file_id="file-oversized",
                filename="oversized.txt",
                content_type="text/plain",
                text="answer",
            ),
            SubmissionSourceInput(
                source_id="source-valid",
                stored_file_id="file-valid",
                filename="valid.txt",
                content_type="text/plain",
                text="answer",
            ),
        ],
        {"q1": {"q_id": "q1", "number": "1", "type": "short", "stem": "Q1"}},
        SimpleNamespace(provider_id="test"),
    )

    assert results[0].status == "parse_failed"
    assert results[0].stable_error_code == "submission_model_field_too_long"
    assert results[0].failure_phase == "structured_parse"
    assert results[0].retryable is False
    assert results[0].student is None
    assert results[1].status == "parsed"
    assert results[1].stable_error_code is None
    assert results[1].student is not None
    assert results[1].student["stu_id"] == "S002"


@pytest.mark.asyncio
async def test_terminal_operation_without_outcome_projects_source_as_failed():
    owner_id, task_id = _seed_task()
    operation = _create_operation(owner_id, task_id)
    source_id, _file_id = await submission_source_pipeline._persist_and_register(
        raw=_raw_source(),
        owner_id=owner_id,
        task_id=task_id,
        job_id=operation.id,
        job_attempt=operation.attempt,
        order_index=0,
    )
    workflow_repository.update_workflow(
        task_id,
        owner_id=owner_id,
        bump_revision=False,
        parse_job_id=operation.id,
    )
    workflow_repository.update_operation(
        operation.id,
        owner_id=owner_id,
        expected_attempt=operation.attempt,
        status="error",
        error_code="submission_outcome_persistence_failed",
        completed_at=time.time(),
    )

    task = task_facade.get_task(task_id=task_id, owner_id=owner_id)

    assert task["submission_source_summary"] == {
        "uploaded": 1,
        "parsed": 0,
        "failed": 1,
        "identity_needs_review": 0,
        "pending": 0,
    }
    assert len(task["submission_sources"]) == 1
    source = task["submission_sources"][0]
    assert source["source_id"] == source_id
    assert source["status"] == "failed"
    assert source["internal_status"] == "parse_failed"
    assert source["reason_code"] == "submission_outcome_persistence_failed"
    assert source["failure_phase"] == "outcome_persistence"


@pytest.mark.asyncio
async def test_outcome_and_finalizer_failures_still_leave_terminal_failed_projection(
    monkeypatch,
):
    owner_id, task_id = _seed_task()
    assignment_repository.add_question(
        task_id,
        teacher_id=owner_id,
        q_id="q1",
        order_index=0,
        type="short",
        stem="Question one",
        criterion="",
        max_score=10,
    )
    queued = task_facade.queue_task_submission_parsing(
        task_id=task_id,
        owner_id=owner_id,
        filename="student.txt",
        content=b"student answer",
        content_type="text/plain",
        registry=_Registry(),
        recognition_provider_id="test-provider",
    )

    async def fake_invoke(_provider, _messages):
        return SimpleNamespace(content=json.dumps({
            "stu_id": "S003",
            "stu_name": "Student Three",
            "stu_ans": [{
                "q_id": "q1",
                "number": "1",
                "type": "short",
                "content": "answer",
                "flag": [],
            }],
        }))

    def fail_outcome(**_kwargs):
        raise RuntimeError("injected_outcome_write_failure")

    def fail_finalizer(**_kwargs):
        raise RuntimeError("injected_finalizer_failure")

    monkeypatch.setattr("backend.agents.ingest_agent.ainvoke_with_retry", fake_invoke)
    monkeypatch.setattr(
        task_facade.source_outcome_repository,
        "record_outcome",
        fail_outcome,
    )
    monkeypatch.setattr(
        task_facade.source_outcome_repository,
        "finalize_pending_sources_as_failed",
        fail_finalizer,
    )

    await task_facade.run_task_submission_parsing(
        task_id=task_id,
        owner_id=owner_id,
        job_id=queued["job_id"],
        filename="student.txt",
        content=b"student answer",
        content_type="text/plain",
        registry=_Registry(),
        job_attempt=queued["_job_attempt"],
        identity_mode="filename",
        roster_entries=None,
        recognition_provider_id="test-provider",
        replace_confirmed=False,
        claimed_workflow_revision=queued["workflow_revision"],
    )

    operation = workflow_repository.get_operation(
        queued["job_id"],
        owner_id=owner_id,
    )
    assert operation.status == "error"
    assert operation.error_code == "submission_outcome_persistence_failed"
    assert source_outcome_repository.list_source_results(
        operation_id=operation.id,
        owner_id=owner_id,
        attempt=operation.attempt,
    )[0].outcome is None

    task = task_facade.get_task(task_id=task_id, owner_id=owner_id)
    assert task["submission_source_summary"]["failed"] == 1
    assert task["submission_source_summary"]["pending"] == 0
    assert task["submission_sources"][0]["status"] == "failed"
    assert (
        task["submission_sources"][0]["reason_code"]
        == "submission_outcome_persistence_failed"
    )
    assert task["submission_sources"][0]["failure_phase"] == "outcome_persistence"


@pytest.mark.asyncio
async def test_archive_member_persistence_failure_is_terminal_and_does_not_skip_siblings(
    monkeypatch,
):
    owner_id, task_id = _seed_task()
    assignment_repository.add_question(
        task_id,
        teacher_id=owner_id,
        q_id="q1",
        order_index=0,
        type="short",
        stem="Question one",
        criterion="",
        max_score=10,
    )
    archive = _zip_sources({
        "first.txt": b"first answer",
        "broken.txt": b"broken answer",
        "third.txt": b"third answer",
    })
    queued = task_facade.queue_task_submission_parsing(
        task_id=task_id,
        owner_id=owner_id,
        filename="submissions.zip",
        content=archive,
        content_type="application/zip",
        registry=_Registry(),
        recognition_provider_id="test-provider",
    )
    real_persist = submission_source_pipeline._persist_and_register
    injected = False

    async def fail_one_member(**kwargs):
        nonlocal injected
        raw = kwargs["raw"]
        if raw.filename == "broken.txt" and raw.content is not None and not injected:
            injected = True
            raise RuntimeError("injected_member_persistence_failure")
        return await real_persist(**kwargs)

    async def fake_invoke(_provider, messages):
        prompt = messages[-1].content
        student_id = "S001" if "first.txt" in prompt else "S003"
        return SimpleNamespace(content=json.dumps({
            "stu_id": student_id,
            "stu_name": f"Student {student_id}",
            "stu_ans": [{
                "q_id": "q1",
                "number": "1",
                "type": "short",
                "content": "answer",
                "flag": [],
            }],
        }))

    monkeypatch.setattr(
        submission_source_pipeline,
        "_persist_and_register",
        fail_one_member,
    )
    monkeypatch.setattr("backend.agents.ingest_agent.ainvoke_with_retry", fake_invoke)

    await task_facade.run_task_submission_parsing(
        task_id=task_id,
        owner_id=owner_id,
        job_id=queued["job_id"],
        filename="submissions.zip",
        content=archive,
        content_type="application/zip",
        registry=_Registry(),
        job_attempt=queued["_job_attempt"],
        identity_mode="filename",
        roster_entries=None,
        recognition_provider_id="test-provider",
        replace_confirmed=False,
        claimed_workflow_revision=queued["workflow_revision"],
    )

    operation = workflow_repository.get_operation(queued["job_id"], owner_id=owner_id)
    rows = source_outcome_repository.list_source_results(
        operation_id=operation.id,
        owner_id=owner_id,
        attempt=operation.attempt,
    )
    assert injected is True
    assert operation.status == "done"
    assert len(rows) == 3
    assert [row.source.original_name for row in rows] == [
        "first.txt",
        "submissions.zip :: broken.txt",
        "third.txt",
    ]
    assert [row.outcome.status for row in rows] == [
        "parsed",
        "parse_failed",
        "parsed",
    ]
    assert rows[1].outcome.stable_error_code == "submission_source_persistence_failed"
    assert rows[1].outcome.failure_phase == "source_persistence"
    task = task_facade.get_task(task_id=task_id, owner_id=owner_id)
    assert task["submission_source_summary"] == {
        "uploaded": 3,
        "parsed": 2,
        "failed": 1,
        "identity_needs_review": 0,
        "pending": 0,
    }
