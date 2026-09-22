"""Synthetic checks for completion, recognition-retry and replacement retention."""
from __future__ import annotations

import pytest
from sqlalchemy import delete

from backend.db import file_repository, source_storage_repository, workflow_repository
from backend.db.models import GradingRunRecord, SubmissionAnswerRecord
from backend.db.session import session_scope
from backend.db.source_outcome_repository import WorkflowSourceItemRecord, WorkflowSourceOutcomeRecord
from backend.services import source_cleanup
from backend.storage.local import LocalStorage
from backend.tests.test_task_finalization_contract import _prepared_task
from backend.tests.test_source_cleanup import _worker, _replacement_worker, _drain


def _source(seeded, storage, *, kind="submission_source", name="answer.txt"):
    return file_repository.save_file(storage=storage, owner_id=seeded["owner_id"],
        kind=kind, original_name=name, content=name.encode(), content_type="text/plain",
        assignment_id=seeded["task_id"])


def _recognition(seeded, source, *, status, container=None, suffix="old", created_at=1):
    with session_scope() as session:
        operation_id = "retention-recognition-" + suffix
        operation = workflow_repository.WorkflowOperationRecord(
            id=operation_id, assignment_id=seeded["task_id"], owner_id=seeded["owner_id"],
            operation_type="submission_recognition", input_hash=suffix,
            status="completed", attempt=1, artifact_refs=[container.id] if container else [],
            checkpoint={"container_file_id": container.id} if container else {})
        session.add(operation)
        session.flush()
        source_id = "retention-source-" + suffix
        session.add(WorkflowSourceItemRecord(id=source_id, owner_id=seeded["owner_id"],
            assignment_id=seeded["task_id"], operation_id=operation_id, attempt=1,
            order_index=0, stored_file_id=source.id, created_at=created_at))
        session.flush()
        if status is not None:
            session.add(WorkflowSourceOutcomeRecord(source_id=source_id, status=status,
                matched_answer_count=2 if status == "parsed" else 0,
                unknown_question_ids=[], retryable=status != "parsed"))
    return operation_id


def _enqueue(seeded, source_ids):
    with session_scope() as session:
        run = session.get(GradingRunRecord, seeded["run_id"])
        return source_storage_repository.enqueue_finalized_source_cleanup_in_session(
            session, assignment_id=seeded["task_id"], owner_id=seeded["owner_id"],
            grading_run_id=run.id, final_result_version=1, finalized_at=run.completed_at,
            source_file_ids=source_ids)


@pytest.mark.parametrize("status", [None, "parse_failed", "identity_conflict", "no_matching_answer"])
@pytest.mark.parametrize("archive", [False, True])
def test_completion_retains_unresolved_recognition_source_and_archive(tmp_path, status, archive):
    seeded = _prepared_task()
    storage = LocalStorage(tmp_path / "sources")
    source = _source(seeded, storage)
    container = _source(seeded, storage, kind="submission_container", name="class.zip") if archive else None
    _recognition(seeded, source, status=status, container=container)
    target = container or source
    assert _enqueue(seeded, (target.id,)) is None
    current = file_repository.get_file(file_id=target.id, owner_id=seeded["owner_id"])
    assert current.availability_status == "available"
    assert storage.exists(target.storage_key)


def test_latest_successful_recognition_allows_completion_cleanup(tmp_path):
    seeded = _prepared_task()
    storage = LocalStorage(tmp_path / "sources")
    source = _source(seeded, storage)
    _recognition(seeded, source, status="parse_failed")
    _recognition(seeded, source, status="parsed", suffix="retry", created_at=2)
    assert _enqueue(seeded, (source.id,)) is not None


@pytest.mark.asyncio
async def test_queued_cleanup_rechecks_missing_structured_input_before_delete(tmp_path, monkeypatch):
    seeded = _prepared_task()
    storage = LocalStorage(tmp_path / "sources")
    monkeypatch.setattr(source_cleanup, "get_storage", lambda: storage)
    source = _source(seeded, storage)
    operation_id = _enqueue(seeded, (source.id,))
    assert operation_id is not None
    with session_scope() as session:
        session.execute(delete(SubmissionAnswerRecord))
    worker = _worker()
    assert await worker.poll_once() == 1
    await _drain(worker)
    current = file_repository.get_file(file_id=source.id, owner_id=seeded["owner_id"])
    assert current.availability_status == "available"
    assert storage.exists(source.storage_key)
    assert workflow_repository.get_operation(operation_id, owner_id=seeded["owner_id"]).status == "superseded"


@pytest.mark.asyncio
async def test_explicit_replacement_can_retire_a_failed_old_source(tmp_path, monkeypatch):
    seeded = _prepared_task()
    storage = LocalStorage(tmp_path / "sources")
    monkeypatch.setattr(source_cleanup, "get_storage", lambda: storage)
    old = _source(seeded, storage, name="wrong-old.txt")
    new = _source(seeded, storage, name="replacement.txt")
    operation_id = _recognition(seeded, old, status="parse_failed")
    assert _enqueue(seeded, (old.id,)) is None
    with session_scope() as session:
        cleanup_id = source_storage_repository.enqueue_replaced_source_cleanup_in_session(
            session, assignment_id=seeded["task_id"], owner_id=seeded["owner_id"],
            producer_operation_id=operation_id, producer_operation_attempt=1,
            source_file_ids=(old.id,), keep_file_ids=(new.id,))
    assert cleanup_id is not None
    worker = _replacement_worker()
    assert await worker.poll_once() == 1
    await _drain(worker)
    assert not storage.exists(old.storage_key)
    assert storage.exists(new.storage_key)
    current = file_repository.get_file(file_id=old.id, owner_id=seeded["owner_id"])
    assert current.availability_reason == "replaced"


@pytest.mark.parametrize("malformation", ["not_an_object", "missing_revision"])
def test_incomplete_frozen_manifest_retains_source_without_interrupting_completion(tmp_path, malformation):
    from backend.db.models import GradingRunSubmissionRecord
    from backend.db.workflow_repository import GradingRunSetupRecord
    from sqlalchemy import select
    seeded = _prepared_task()
    storage = LocalStorage(tmp_path / "sources")
    source = _source(seeded, storage)
    with session_scope() as session:
        revisions = list(session.scalars(select(GradingRunSubmissionRecord.submission_revision_id)
            .where(GradingRunSubmissionRecord.grading_run_id == seeded["run_id"])))
        manifest = [] if malformation == "not_an_object" else {
            "questions": [{"q_id": "q-required"}, {"q_id": "q-optional"}],
            "submission_revision_ids": [*revisions, "lost-frozen-revision"],
            "source_file_ids": [source.id],
        }
        session.add(GradingRunSetupRecord(grading_run_id=seeded["run_id"],
            assignment_id=seeded["task_id"], owner_id=seeded["owner_id"],
            setup={}, input_manifest=manifest, fingerprint="synthetic-malformed"))
    assert _enqueue(seeded, (source.id,)) is None
    assert storage.exists(source.storage_key)
