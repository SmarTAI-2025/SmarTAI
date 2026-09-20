from __future__ import annotations

import asyncio
import io
import json
import time
import uuid
import zipfile
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from backend.agents.ingest_agent import (
    SubmissionSourceInput,
    parse_student_answer_sources,
)
from backend.db import (
    assignment_repository,
    source_outcome_repository,
    source_storage_repository,
    workflow_repository,
)
from backend.db.file_repository import get_file, list_files
from backend.db.models import (
    AssignmentRecord,
    CourseRecord,
    SourceStorageReservationRecord,
    UserRecord,
)
from backend.db.session import session_scope
from backend.config import settings
from backend.domain.errors import LeaseLost
from backend.domain.source_storage import SOURCE_REPLACEMENT_CLEANUP_OPERATION
from backend.services import (
    source_cleanup,
    source_files,
    submission_source_pipeline,
    task_facade,
)
from backend.services.workflow_worker import WorkflowWorker
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


async def _drain_replacement_worker(worker: WorkflowWorker) -> None:
    for _ in range(500):
        if worker.in_flight_count == 0:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("source replacement cleanup worker did not drain")


def _replacement_cleanup_worker(worker_id: str) -> WorkflowWorker:
    return WorkflowWorker(
        handlers={
            SOURCE_REPLACEMENT_CLEANUP_OPERATION: (
                source_cleanup.run_source_replacement_cleanup
            ),
        },
        worker_id=worker_id,
        lease_seconds=60,
        heartbeat_seconds=60,
        poll_seconds=60,
        claim_batch_size=10,
        max_in_flight=1,
        shutdown_seconds=1,
    )


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
async def test_expired_submission_worker_cannot_publish_a_raw_source():
    owner_id, task_id = _seed_task()
    queued = _create_operation(owner_id, task_id)
    running = workflow_repository.claim_operation(
        queued.id,
        owner_id=owner_id,
        worker_id="expired-submission-worker",
        lease_seconds=60,
    )
    assert running.lease_token
    with session_scope() as session:
        operation = session.get(
            workflow_repository.WorkflowOperationRecord, running.id
        )
        assert operation is not None
        operation.lease_expires_at = time.time() - 1

    with pytest.raises(RuntimeError, match="submission_source_persistence_failed"):
        await submission_source_pipeline._persist_and_register(
            raw=_raw_source(),
            owner_id=owner_id,
            task_id=task_id,
            job_id=running.id,
            job_attempt=running.attempt,
            order_index=0,
            operation_lease_token=running.lease_token,
        )

    assert list_files(owner_id=owner_id, assignment_id=task_id) == []
    assert source_storage_repository.source_quota_usage(owner_id).used_bytes == 0
    with session_scope() as session:
        assert session.query(SourceStorageReservationRecord).count() == 0
    assert source_outcome_repository.list_sources(
        operation_id=running.id,
        owner_id=owner_id,
        attempt=running.attempt,
    ) == []


@pytest.mark.asyncio
async def test_lease_loss_between_publication_and_registration_preserves_recovery(
    monkeypatch,
):
    owner_id, task_id = _seed_task()
    queued = _create_operation(owner_id, task_id)
    running = workflow_repository.claim_operation(
        queued.id,
        owner_id=owner_id,
        worker_id="registration-gap-worker",
        lease_seconds=60,
    )
    assert running.lease_token
    real_register = source_outcome_repository.register_source

    def expire_then_register(**kwargs):
        with session_scope() as session:
            operation = session.get(
                workflow_repository.WorkflowOperationRecord, running.id
            )
            assert operation is not None
            operation.lease_expires_at = time.time() - 1
        return real_register(**kwargs)

    monkeypatch.setattr(
        submission_source_pipeline.source_outcome_repository,
        "register_source",
        expire_then_register,
    )

    with pytest.raises(RuntimeError, match="submission_source_persistence_failed"):
        await submission_source_pipeline._persist_and_register(
            raw=_raw_source(),
            owner_id=owner_id,
            task_id=task_id,
            job_id=running.id,
            job_attempt=running.attempt,
            order_index=0,
            operation_lease_token=running.lease_token,
        )

    preserved = list_files(owner_id=owner_id, assignment_id=task_id)
    assert len(preserved) == 1
    assert source_storage_repository.source_quota_usage(
        owner_id
    ).used_bytes == len(_raw_source().content)
    assert source_outcome_repository.list_sources(
        operation_id=running.id,
        owner_id=owner_id,
        attempt=running.attempt,
    ) == []
    monkeypatch.setattr(
        submission_source_pipeline.source_outcome_repository,
        "register_source",
        real_register,
    )
    replacement = workflow_repository.claim_operation(
        running.id,
        owner_id=owner_id,
        worker_id="registration-gap-replacement-worker",
        lease_seconds=60,
    )
    source_id, file_id = await submission_source_pipeline._persist_and_register(
        raw=_raw_source(),
        owner_id=owner_id,
        task_id=task_id,
        job_id=replacement.id,
        job_attempt=replacement.attempt,
        order_index=0,
        operation_lease_token=replacement.lease_token,
    )
    assert file_id == preserved[0].id
    assert [item.id for item in source_outcome_repository.list_sources(
        operation_id=replacement.id,
        owner_id=owner_id,
        attempt=replacement.attempt,
    )] == [source_id]


@pytest.mark.asyncio
async def test_stale_worker_cannot_recover_another_workers_source_registration(
    monkeypatch,
):
    owner_id, task_id = _seed_task()
    queued = _create_operation(owner_id, task_id)
    running = workflow_repository.claim_operation(
        queued.id,
        owner_id=owner_id,
        worker_id="stale-registration-worker",
        lease_seconds=60,
    )
    assert running.lease_token
    real_register = source_outcome_repository.register_source

    def new_worker_wins_then_old_worker_loses(**kwargs):
        with session_scope() as session:
            operation = session.get(
                workflow_repository.WorkflowOperationRecord, running.id
            )
            assert operation is not None
            operation.lease_expires_at = time.time() - 1
        replacement = workflow_repository.claim_operation(
            running.id,
            owner_id=owner_id,
            worker_id="replacement-registration-worker",
            lease_seconds=60,
        )
        real_register(
            **{
                **kwargs,
                "expected_lease_token": replacement.lease_token,
            }
        )
        raise LeaseLost("old worker lost registration race")

    monkeypatch.setattr(
        submission_source_pipeline.source_outcome_repository,
        "register_source",
        new_worker_wins_then_old_worker_loses,
    )

    with pytest.raises(LeaseLost):
        await submission_source_pipeline._persist_and_register(
            raw=_raw_source(),
            owner_id=owner_id,
            task_id=task_id,
            job_id=running.id,
            job_attempt=running.attempt,
            order_index=0,
            operation_lease_token=running.lease_token,
        )

    sources = source_outcome_repository.list_sources(
        operation_id=running.id,
        owner_id=owner_id,
        attempt=running.attempt,
    )
    assert len(sources) == 1
    assert [item.id for item in list_files(
        owner_id=owner_id,
        assignment_id=task_id,
    )] == [sources[0].stored_file_id]


@pytest.mark.asyncio
async def test_stale_worker_cannot_delete_replacement_workers_unlinked_source():
    owner_id, task_id = _seed_task()
    queued = _create_operation(owner_id, task_id)
    stale = workflow_repository.claim_operation(
        queued.id,
        owner_id=owner_id,
        worker_id="stale-unlinked-source-worker",
        lease_seconds=60,
    )
    assert stale.lease_token
    with session_scope() as session:
        operation = session.get(
            workflow_repository.WorkflowOperationRecord, stale.id
        )
        assert operation is not None
        operation.lease_expires_at = time.time() - 1
    replacement = workflow_repository.claim_operation(
        stale.id,
        owner_id=owner_id,
        worker_id="replacement-unlinked-source-worker",
        lease_seconds=60,
    )
    assert replacement.lease_token
    raw = _raw_source()
    replacement_file = submission_source_pipeline.save_file(
        storage=get_storage(),
        owner_id=owner_id,
        kind="submission_source",
        original_name=raw.filename,
        content=raw.content,
        content_type=raw.content_type,
        storage_prefix=(
            f"assignments/{task_id}/submission-sources/"
            f"{replacement.id}/{replacement.attempt}"
        ),
        assignment_id=task_id,
        fence_operation_id=replacement.id,
        fence_operation_attempt=replacement.attempt,
        fence_lease_token=replacement.lease_token,
    )

    with pytest.raises(RuntimeError, match="submission_source_persistence_failed"):
        await submission_source_pipeline._persist_and_register(
            raw=raw,
            owner_id=owner_id,
            task_id=task_id,
            job_id=stale.id,
            job_attempt=stale.attempt,
            order_index=0,
            operation_lease_token=stale.lease_token,
        )

    preserved = list_files(owner_id=owner_id, assignment_id=task_id)
    assert [item.id for item in preserved] == [replacement_file.id]
    assert get_storage().exists(replacement_file.storage_key)
    assert source_storage_repository.source_quota_usage(
        owner_id
    ).used_bytes == len(raw.content)

    source_id, stored_file_id = await submission_source_pipeline._persist_and_register(
        raw=raw,
        owner_id=owner_id,
        task_id=task_id,
        job_id=replacement.id,
        job_attempt=replacement.attempt,
        order_index=0,
        operation_lease_token=replacement.lease_token,
    )
    assert stored_file_id == replacement_file.id
    assert [item.id for item in source_outcome_repository.list_sources(
        operation_id=replacement.id,
        owner_id=owner_id,
        attempt=replacement.attempt,
    )] == [source_id]


@pytest.mark.asyncio
async def test_expired_submission_worker_cannot_reuse_registered_source():
    owner_id, task_id = _seed_task()
    queued = _create_operation(owner_id, task_id)
    source_id, file_id = await submission_source_pipeline._persist_and_register(
        raw=_raw_source(),
        owner_id=owner_id,
        task_id=task_id,
        job_id=queued.id,
        job_attempt=queued.attempt,
        order_index=0,
    )
    running = workflow_repository.claim_operation(
        queued.id,
        owner_id=owner_id,
        worker_id="expired-source-reuse-worker",
        lease_seconds=60,
    )
    assert running.lease_token
    with session_scope() as session:
        operation = session.get(
            workflow_repository.WorkflowOperationRecord, running.id
        )
        assert operation is not None
        operation.lease_expires_at = time.time() - 1

    with pytest.raises(LeaseLost):
        await submission_source_pipeline._persist_and_register(
            raw=_raw_source(),
            owner_id=owner_id,
            task_id=task_id,
            job_id=running.id,
            job_attempt=running.attempt,
            order_index=0,
            operation_lease_token=running.lease_token,
        )

    assert [item.id for item in list_files(
        owner_id=owner_id,
        assignment_id=task_id,
    )] == [file_id]
    assert [item.id for item in source_outcome_repository.list_sources(
        operation_id=running.id,
        owner_id=owner_id,
        attempt=running.attempt,
    )] == [source_id]


@pytest.mark.asyncio
async def test_live_submission_worker_publishes_and_checkpoints_archive_container():
    owner_id, task_id = _seed_task()
    queued = _create_operation(owner_id, task_id)
    running = workflow_repository.claim_operation(
        queued.id,
        owner_id=owner_id,
        worker_id="live-archive-worker",
        lease_seconds=60,
    )
    assert running.lease_token

    stored = await submission_source_pipeline._persist_archive_container(
        content=_zip_sources({"student.txt": b"answer"}),
        filename="submissions.zip",
        content_type="application/zip",
        owner_id=owner_id,
        task_id=task_id,
        job_id=running.id,
        job_attempt=running.attempt,
        operation_lease_token=running.lease_token,
    )

    current = workflow_repository.get_operation(running.id, owner_id=owner_id)
    assert current.artifact_refs == [stored.id]
    assert current.checkpoint["container_file_id"] == stored.id
    assert get_storage().exists(stored.storage_key)
    assert source_storage_repository.source_quota_usage(
        owner_id
    ).used_bytes == stored.size_bytes


@pytest.mark.asyncio
async def test_expired_submission_worker_cannot_publish_archive_container():
    owner_id, task_id = _seed_task()
    queued = _create_operation(owner_id, task_id)
    running = workflow_repository.claim_operation(
        queued.id,
        owner_id=owner_id,
        worker_id="expired-archive-worker",
        lease_seconds=60,
    )
    assert running.lease_token
    with session_scope() as session:
        operation = session.get(
            workflow_repository.WorkflowOperationRecord, running.id
        )
        assert operation is not None
        operation.lease_expires_at = time.time() - 1

    with pytest.raises(LeaseLost):
        await submission_source_pipeline._persist_archive_container(
            content=_zip_sources({"student.txt": b"answer"}),
            filename="submissions.zip",
            content_type="application/zip",
            owner_id=owner_id,
            task_id=task_id,
            job_id=running.id,
            job_attempt=running.attempt,
            operation_lease_token=running.lease_token,
        )

    assert list_files(owner_id=owner_id, assignment_id=task_id) == []
    assert source_storage_repository.source_quota_usage(owner_id).used_bytes == 0
    with session_scope() as session:
        assert session.query(SourceStorageReservationRecord).count() == 0


@pytest.mark.asyncio
async def test_stale_worker_cannot_recover_another_workers_archive_checkpoint(
    monkeypatch,
):
    owner_id, task_id = _seed_task()
    queued = _create_operation(owner_id, task_id)
    running = workflow_repository.claim_operation(
        queued.id,
        owner_id=owner_id,
        worker_id="stale-archive-checkpoint-worker",
        lease_seconds=60,
    )
    assert running.lease_token
    real_checkpoint = workflow_repository.save_operation_checkpoint

    def new_worker_checkpoints_then_old_worker_loses(*args, **kwargs):
        with session_scope() as session:
            operation = session.get(
                workflow_repository.WorkflowOperationRecord, running.id
            )
            assert operation is not None
            operation.lease_expires_at = time.time() - 1
        replacement = workflow_repository.claim_operation(
            running.id,
            owner_id=owner_id,
            worker_id="replacement-archive-checkpoint-worker",
            lease_seconds=60,
        )
        real_checkpoint(
            *args,
            **{
                **kwargs,
                "expected_lease_token": replacement.lease_token,
            },
        )
        raise LeaseLost("old worker lost archive checkpoint race")

    monkeypatch.setattr(
        submission_source_pipeline.workflow_repository,
        "save_operation_checkpoint",
        new_worker_checkpoints_then_old_worker_loses,
    )

    with pytest.raises(LeaseLost):
        await submission_source_pipeline._persist_archive_container(
            content=_zip_sources({"student.txt": b"answer"}),
            filename="submissions.zip",
            content_type="application/zip",
            owner_id=owner_id,
            task_id=task_id,
            job_id=running.id,
            job_attempt=running.attempt,
            operation_lease_token=running.lease_token,
        )

    current = workflow_repository.get_operation(running.id, owner_id=owner_id)
    assert len(current.artifact_refs) == 1
    stored = list_files(owner_id=owner_id, assignment_id=task_id)
    assert [item.id for item in stored] == current.artifact_refs
    assert get_storage().exists(stored[0].storage_key)


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
async def test_restart_recovers_source_published_before_registration():
    owner_id, task_id = _seed_task()
    queued = _create_operation(owner_id, task_id)
    running = workflow_repository.claim_operation(
        queued.id,
        owner_id=owner_id,
        worker_id="source-publication-recovery-worker",
        lease_seconds=60,
    )
    assert running.lease_token
    raw = _raw_source()
    orphan = submission_source_pipeline.save_file(
        storage=get_storage(),
        owner_id=owner_id,
        kind="submission_source",
        original_name=raw.filename,
        content=raw.content,
        content_type=raw.content_type,
        storage_prefix=(
            f"assignments/{task_id}/submission-sources/"
            f"{running.id}/{running.attempt}"
        ),
        assignment_id=task_id,
        fence_operation_id=running.id,
        fence_operation_attempt=running.attempt,
        fence_lease_token=running.lease_token,
    )
    assert source_outcome_repository.list_sources(
        operation_id=running.id,
        owner_id=owner_id,
        attempt=running.attempt,
    ) == []

    source_id, stored_file_id = await submission_source_pipeline._persist_and_register(
        raw=raw,
        owner_id=owner_id,
        task_id=task_id,
        job_id=running.id,
        job_attempt=running.attempt,
        order_index=0,
        operation_lease_token=running.lease_token,
    )

    assert stored_file_id == orphan.id
    assert [item.id for item in list_files(
        owner_id=owner_id,
        assignment_id=task_id,
    )] == [orphan.id]
    assert [item.id for item in source_outcome_repository.list_sources(
        operation_id=running.id,
        owner_id=owner_id,
        attempt=running.attempt,
    )] == [source_id]
    assert source_storage_repository.source_quota_usage(
        owner_id
    ).used_bytes == len(raw.content)


@pytest.mark.asyncio
async def test_restart_recovers_container_published_before_checkpoint():
    owner_id, task_id = _seed_task()
    queued = _create_operation(owner_id, task_id)
    running = workflow_repository.claim_operation(
        queued.id,
        owner_id=owner_id,
        worker_id="container-publication-recovery-worker",
        lease_seconds=60,
    )
    assert running.lease_token
    content = _zip_sources({"student.txt": b"answer"})
    orphan = submission_source_pipeline.save_file(
        storage=get_storage(),
        owner_id=owner_id,
        kind="submission_container",
        original_name="submissions.zip",
        content=content,
        content_type="application/zip",
        storage_prefix=(
            f"assignments/{task_id}/submission-containers/"
            f"{running.id}/{running.attempt}"
        ),
        assignment_id=task_id,
        fence_operation_id=running.id,
        fence_operation_attempt=running.attempt,
        fence_lease_token=running.lease_token,
    )

    recovered = await submission_source_pipeline._persist_archive_container(
        content=content,
        filename="submissions.zip",
        content_type="application/zip",
        owner_id=owner_id,
        task_id=task_id,
        job_id=running.id,
        job_attempt=running.attempt,
        operation_lease_token=running.lease_token,
    )

    assert recovered.id == orphan.id
    assert [item.id for item in list_files(
        owner_id=owner_id,
        assignment_id=task_id,
    )] == [orphan.id]
    current = workflow_repository.get_operation(running.id, owner_id=owner_id)
    assert current.artifact_refs == [orphan.id]
    assert source_storage_repository.source_quota_usage(
        owner_id
    ).used_bytes == len(content)


@pytest.mark.asyncio
async def test_restart_recovers_archive_reference_published_before_registration():
    owner_id, task_id = _seed_task()
    queued = _create_operation(owner_id, task_id)
    running = workflow_repository.claim_operation(
        queued.id,
        owner_id=owner_id,
        worker_id="reference-publication-recovery-worker",
        lease_seconds=60,
    )
    assert running.lease_token
    container = await submission_source_pipeline._persist_archive_container(
        content=_zip_sources({"student.txt": b"answer"}),
        filename="submissions.zip",
        content_type="application/zip",
        owner_id=owner_id,
        task_id=task_id,
        job_id=running.id,
        job_attempt=running.attempt,
        operation_lease_token=running.lease_token,
    )
    raw = RawUploadSource(
        filename="student.txt",
        content=None,
        content_type="text/plain",
    )
    orphan = submission_source_pipeline.create_archive_member_reference(
        storage=get_storage(),
        source_file_id=container.id,
        owner_id=owner_id,
        assignment_id=task_id,
        member_name=raw.filename,
        fence_operation_id=running.id,
        fence_operation_attempt=running.attempt,
        fence_lease_token=running.lease_token,
    )

    source_id, stored_file_id = await submission_source_pipeline._persist_and_register(
        raw=raw,
        owner_id=owner_id,
        task_id=task_id,
        job_id=running.id,
        job_attempt=running.attempt,
        order_index=0,
        container_file=container,
        operation_lease_token=running.lease_token,
    )

    assert stored_file_id == orphan.id
    assert {item.id for item in list_files(
        owner_id=owner_id,
        assignment_id=task_id,
    )} == {container.id, orphan.id}
    assert [item.id for item in source_outcome_repository.list_sources(
        operation_id=running.id,
        owner_id=owner_id,
        attempt=running.attempt,
    )] == [source_id]
    # The archive is the sole charged original. Its bounded member pointer is
    # lifecycle-managed but zero quota, so a retry cannot double-charge bytes.
    assert source_storage_repository.source_quota_usage(
        owner_id
    ).used_bytes == container.size_bytes


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


@pytest.mark.asyncio
async def test_equal_size_zip_replacement_survives_old_delete_retry_and_keeps_preview(
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
    old_member_bytes = b"\x89PNG\r\n\x1a\nold-preview"
    new_member_bytes = b"\x89PNG\r\n\x1a\nnew-preview"
    old_archive = _zip_sources({"student.png": old_member_bytes})
    new_archive = _zip_sources({"student.png": new_member_bytes})
    assert len(old_archive) == len(new_archive)
    assert old_archive != new_archive
    monkeypatch.setattr(
        settings, "unfinished_source_quota_bytes", len(old_archive)
    )

    async def fake_extract(*_args, **_kwargs):
        return "answer"

    async def fake_invoke(_provider, _messages):
        return SimpleNamespace(content=json.dumps({
            "stu_id": "S001",
            "stu_name": "Student S001",
            "stu_ans": [{
                "q_id": "q1",
                "number": "1",
                "type": "short",
                "content": "answer",
                "flag": [],
            }],
        }))

    monkeypatch.setattr(
        submission_source_pipeline, "extract_text_from_upload", fake_extract
    )
    monkeypatch.setattr("backend.agents.ingest_agent.ainvoke_with_retry", fake_invoke)

    async def parse_archive(content: bytes, *, replace_confirmed: bool) -> str:
        queued = task_facade.queue_task_submission_parsing(
            task_id=task_id,
            owner_id=owner_id,
            filename="submissions.zip",
            content=content,
            content_type="application/zip",
            registry=_Registry(),
            recognition_provider_id="test-provider",
            replace_confirmed=replace_confirmed,
        )
        await task_facade.run_task_submission_parsing(
            task_id=task_id,
            owner_id=owner_id,
            job_id=queued["job_id"],
            filename="submissions.zip",
            content=content,
            content_type="application/zip",
            registry=_Registry(),
            job_attempt=queued["_job_attempt"],
            identity_mode="filename",
            roster_entries=None,
            recognition_provider_id="test-provider",
            replace_confirmed=replace_confirmed,
            claimed_workflow_revision=queued["workflow_revision"],
        )
        return str(queued["job_id"])

    first_job_id = await parse_archive(old_archive, replace_confirmed=False)
    first_operation = workflow_repository.get_operation(
        first_job_id, owner_id=owner_id
    )
    first_source = source_outcome_repository.list_sources(
        operation_id=first_job_id,
        owner_id=owner_id,
        attempt=first_operation.attempt,
    )[0]
    old_member = get_file(
        file_id=first_source.stored_file_id, owner_id=owner_id
    )
    old_container = next(
        item
        for item in list_files(owner_id=owner_id, assignment_id=task_id)
        if item.kind == "submission_container"
        and item.availability_status == "available"
    )
    assert old_member is not None
    assert old_member.kind == "submission_archive_member"
    assert old_member.source_quota_bytes == 0
    assert source_storage_repository.source_quota_usage(
        owner_id
    ).used_bytes == len(old_archive)

    queued_replacement = task_facade.queue_task_submission_parsing(
        task_id=task_id,
        owner_id=owner_id,
        filename="submissions.zip",
        content=new_archive,
        content_type="application/zip",
        registry=_Registry(),
        recognition_provider_id="test-provider",
        replace_confirmed=True,
    )
    old_container_pending = get_file(file_id=old_container.id, owner_id=owner_id)
    old_member_pending = get_file(file_id=old_member.id, owner_id=owner_id)
    assert old_container_pending is not None
    assert old_member_pending is not None
    assert old_container_pending.availability_status == "cleanup_pending"
    assert old_member_pending.availability_status == "cleanup_pending"

    storage = get_storage()
    real_delete = storage.delete

    def fail_old_delete(_key: str) -> None:
        raise OSError("private provider detail")

    monkeypatch.setattr(storage, "delete", fail_old_delete)
    monkeypatch.setattr(source_cleanup, "get_storage", lambda: storage)
    monkeypatch.setattr(settings, "source_cleanup_retry_base_seconds", 1)
    monkeypatch.setattr(settings, "source_cleanup_retry_max_seconds", 1)
    first_cleanup_worker = _replacement_cleanup_worker("zip-cleanup-failing")
    assert await first_cleanup_worker.poll_once() == 1
    await _drain_replacement_worker(first_cleanup_worker)
    usage_while_retrying = source_storage_repository.source_quota_usage(owner_id)
    assert usage_while_retrying.used_bytes == 2 * len(old_archive)
    assert usage_while_retrying.retrying_cleanup_bytes == len(old_archive)

    await task_facade.run_task_submission_parsing(
        task_id=task_id,
        owner_id=owner_id,
        job_id=queued_replacement["job_id"],
        filename="submissions.zip",
        content=new_archive,
        content_type="application/zip",
        registry=_Registry(),
        job_attempt=queued_replacement["_job_attempt"],
        identity_mode="filename",
        roster_entries=None,
        recognition_provider_id="test-provider",
        replace_confirmed=True,
        claimed_workflow_revision=queued_replacement["workflow_revision"],
    )
    replacement_operation = workflow_repository.get_operation(
        queued_replacement["job_id"], owner_id=owner_id
    )
    replacement_source = source_outcome_repository.list_sources(
        operation_id=replacement_operation.id,
        owner_id=owner_id,
        attempt=replacement_operation.attempt,
    )[0]
    replacement_member = get_file(
        file_id=replacement_source.stored_file_id, owner_id=owner_id
    )
    assert replacement_member is not None
    assert replacement_member.kind == "submission_archive_member"
    assert replacement_member.source_quota_bytes == 0
    descriptor = source_files.describe_source_files(
        task_id=task_id, owner_id=owner_id, storage=storage
    )["submission_sources"][replacement_source.id]
    assert descriptor["status"] == "available"
    assert descriptor["preview_kind"] == "image"
    preview = source_files.read_source_file_content(
        task_id=task_id,
        file_id=replacement_member.id,
        owner_id=owner_id,
        storage=storage,
    )
    assert preview.content == new_member_bytes
    assert source_storage_repository.source_quota_usage(
        owner_id
    ).used_bytes == 2 * len(old_archive)

    monkeypatch.setattr(storage, "delete", real_delete)
    with session_scope() as session:
        cleanup_operation = session.scalar(
            select(workflow_repository.WorkflowOperationRecord).where(
                workflow_repository.WorkflowOperationRecord.assignment_id
                == task_id,
                workflow_repository.WorkflowOperationRecord.owner_id == owner_id,
                workflow_repository.WorkflowOperationRecord.operation_type
                == SOURCE_REPLACEMENT_CLEANUP_OPERATION,
            )
        )
        assert cleanup_operation is not None
        assert cleanup_operation.status == "pending"
        cleanup_operation.expires_at = 0.0
    retry_worker = _replacement_cleanup_worker("zip-cleanup-retry")
    assert await retry_worker.poll_once() == 1
    await _drain_replacement_worker(retry_worker)

    old_container_deleted = get_file(file_id=old_container.id, owner_id=owner_id)
    old_member_deleted = get_file(file_id=old_member.id, owner_id=owner_id)
    assert old_container_deleted is not None
    assert old_member_deleted is not None
    assert old_container_deleted.availability_status == "unavailable"
    assert old_member_deleted.availability_status == "unavailable"
    assert not storage.exists(old_container.storage_key)
    assert not storage.exists(old_member.storage_key)
    assert storage.exists(replacement_member.storage_key)
    assert source_storage_repository.source_quota_usage(
        owner_id
    ).used_bytes == len(new_archive)
