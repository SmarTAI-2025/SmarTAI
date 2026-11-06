"""Draft restoration is an owner-scoped read, never a recognition preflight."""
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import update

from backend.api import task_preparation
from backend.db import file_repository, workflow_repository
from backend.db.models import StoredFileRecord
from backend.db.session import session_scope
from backend.storage.local import LocalStorage
from backend.tests.test_task_background_workflows import _seed_task


@pytest.fixture
def draft_source(tmp_path, monkeypatch):
    owner, task = _seed_task()
    storage = LocalStorage(tmp_path / "draft-files")
    monkeypatch.setattr(task_preparation, "get_storage", lambda: storage)
    stored = file_repository.save_file(storage=storage, owner_id=owner,
        assignment_id=task, kind="problem_source", original_name="questions.txt",
        content=b"1. Add 1 and 2", content_type="text/plain")
    def forbidden(*args, **kwargs):
        pytest.fail("Draft restoration dispatched recognition or modified sources")
    monkeypatch.setattr(task_preparation, "_select_source", forbidden)
    monkeypatch.setattr(task_preparation, "_resolve_recognition_provider", forbidden)
    return owner, task, stored, storage


def check(owner, task, stored, **kwargs):
    return task_preparation.check_problem_draft_reference(task,
        stored_file_id=stored.id, current=SimpleNamespace(id=owner), **kwargs)


def test_available_reference_is_read_only(draft_source):
    owner, task, stored, _ = draft_source
    before = workflow_repository.get_live_workflow(task, owner_id=owner).workflow_revision
    assert check(owner, task, stored) == {"available": True, "filename": "questions.txt", "prepared": False}
    assert workflow_repository.get_live_workflow(task, owner_id=owner).workflow_revision == before
    assert file_repository.get_file(file_id=stored.id, owner_id=owner) == stored


def test_other_owner_and_other_task_are_uniform_not_found(draft_source):
    owner, task, stored, _ = draft_source
    other_owner, other_task = _seed_task()
    assert check(other_owner, task, stored).status_code == 404
    assert check(other_owner, other_task, stored).status_code == 404


@pytest.mark.parametrize("state", ["cleanup_pending", "unavailable"])
def test_tombstoned_reference_is_never_available(draft_source, state):
    owner, task, stored, _ = draft_source
    with session_scope() as session:
        session.execute(update(StoredFileRecord).where(StoredFileRecord.id == stored.id).values(availability_status=state))
    assert check(owner, task, stored).status_code == 404


def test_missing_object_and_empty_object_require_reselection(draft_source):
    owner, task, stored, storage = draft_source
    storage.delete(stored.storage_key)
    assert check(owner, task, stored).status_code == 404
    storage.save(stored.storage_key, b"")
    assert check(owner, task, stored).status_code == 404


def test_storage_outage_is_retryable_not_deleted(draft_source, monkeypatch):
    owner, task, stored, storage = draft_source
    def unavailable(*args):
        raise OSError("private-storage-detail")
    monkeypatch.setattr(storage, "open", unavailable)
    with pytest.raises(HTTPException) as caught:
        check(owner, task, stored)
    assert caught.value.status_code == 503
    assert "private-storage-detail" not in str(caught.value.detail)


def test_expired_preflight_keeps_original_without_extending_expiry(draft_source, monkeypatch):
    owner, task, stored, _ = draft_source
    expires = time.time() - 1
    operation = SimpleNamespace(assignment_id=task, operation_type="problem_source", status="ready", expires_at=expires,
        payload={"base_workflow_revision": 0, "source_ref": {"stored_file_id": stored.id}})
    monkeypatch.setattr(workflow_repository, "get_operation", lambda *a, **k: operation)
    assert check(owner, task, stored, prepared_id="prepared")["prepared"] is False
    assert operation.expires_at == expires
    operation.expires_at = time.time() + 60
    assert check(owner, task, stored, prepared_id="prepared")["prepared"] is True
    operation.payload["base_workflow_revision"] = 99
    assert check(owner, task, stored, prepared_id="prepared")["prepared"] is False


def test_unrelated_file_kind_cannot_be_reused(draft_source):
    owner, task, stored, _ = draft_source
    with session_scope() as session:
        session.execute(update(StoredFileRecord).where(StoredFileRecord.id == stored.id).values(kind="analysis_report"))
    assert check(owner, task, stored).status_code == 404
