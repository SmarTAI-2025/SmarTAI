"""Expiry reclaims real guest objects without resetting admission or teachers."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import time

import pytest
from sqlalchemy import select

from backend.config import settings
from backend.db.frontier_demo_models import FrontierDemoDailyUsageRecord
from backend.db.models import AccountClosureRecord, AdminUsageEventRecord, UserRecord
from backend.db.session import session_scope
from backend.services import account_closure, frontier_demo
from backend.storage.local import LocalStorage
from backend.tests.test_frontier_demo_session import enabled, headers, issue, task


def release_claim(owner_id):
    with session_scope() as session:
        session.scalar(select(AccountClosureRecord).where(
            AccountClosureRecord.user_id == owner_id)).lease_until = 0


def test_expiry_is_absolute_restart_safe_and_excludes_registered_accounts(enabled, monkeypatch):
    expired, fresh, disabled = issue(enabled), issue(enabled), issue(enabled)
    now = time.time()
    with session_scope() as session:
        session.get(UserRecord, expired["user"]["id"]).created_at = now - frontier_demo.SESSION_SECONDS
        session.get(UserRecord, disabled["user"]["id"]).is_active = False
        for suffix, fields in [("email", {"email": "teacher@example.edu"}),
                               ("password", {"password_hash": "registered-hash"}),
                               ("admin", {"role": "admin"})]:
            session.add(UserRecord(id="frontier_demo_" + suffix, username=suffix,
                password_hash="" if suffix != "password" else "registered-hash",
                role=fields.get("role", "teacher"), email=fields.get("email"),
                created_at=now - 8000, updated_at=now))
    monkeypatch.setattr(settings, "frontier_demo_enabled", False)
    # Independent writers see the persisted queue, not a process-local list.
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(lambda _: frontier_demo.queue_cleanup(), range(2))) == 2
    with session_scope() as session:
        assert {row.user_id for row in session.scalars(select(AccountClosureRecord))} == {
            expired["user"]["id"], disabled["user"]["id"]}
        assert session.get(UserRecord, fresh["user"]["id"]).is_active
        assert not session.get(UserRecord, expired["user"]["id"]).is_active
        assert session.scalar(select(FrontierDemoDailyUsageRecord)).sessions == 3
    assert frontier_demo.queue_cleanup() == 0


@pytest.mark.asyncio
async def test_ended_guest_drains_task_files_and_usage_without_touching_other_owners(enabled, tmp_path, monkeypatch):
    from backend.db import file_repository
    from backend.db.models import AssignmentRecord, CourseRecord, StoredFileRecord
    from backend.services import task_deletion
    from backend.tests.test_source_cleanup import _finish_task_delete, _task_delete_worker
    first, second = issue(enabled), issue(enabled)
    created, other = task(enabled, first), task(enabled, second)
    owner = first["user"]["id"]
    storage = LocalStorage(tmp_path / "objects")
    monkeypatch.setattr(account_closure, "get_storage", lambda: storage)
    monkeypatch.setattr(task_deletion, "get_storage", lambda: storage)
    stored = file_repository.save_file(storage=storage, owner_id=owner, kind="problem_source",
        original_name="sample.pdf", content=b"synthetic sample", content_type="application/pdf",
        assignment_id=created["task_id"])
    storage.save(f"users/{second['user']['id']}/keep.txt", b"other guest")
    assert enabled.post("/auth/frontier-demo-session/end", headers=headers(first["token"])).status_code == 200
    assert account_closure.process_one_closure(frontier_only=True, raise_errors=True)
    await _finish_task_delete(_task_delete_worker(), created["task_id"])
    release_claim(owner)
    assert account_closure.process_one_closure(frontier_only=True, raise_errors=True)
    with session_scope() as session:
        assert session.get(UserRecord, owner) is None
        assert session.get(AssignmentRecord, created["task_id"]) is None
        assert session.scalar(select(CourseRecord).where(CourseRecord.teacher_id == owner)) is None
        assert session.scalar(select(StoredFileRecord).where(StoredFileRecord.owner_id == owner)) is None
        assert session.scalar(select(AdminUsageEventRecord).where(AdminUsageEventRecord.user_id == owner)) is None
        assert session.scalar(select(AdminUsageEventRecord).where(AdminUsageEventRecord.user_id.like("closed_%"))) is None
        assert session.scalar(select(AccountClosureRecord)) is None
        assert session.get(AssignmentRecord, other["task_id"]).deletion_requested_at is None
        assert session.scalar(select(FrontierDemoDailyUsageRecord)).sessions == 2
    assert not storage.exists(stored.storage_key)
    assert storage.open(f"users/{second['user']['id']}/keep.txt").read() == b"other guest"
    assert enabled.post("/auth/frontier-demo-session", headers=headers(first["token"])).status_code == 401


def test_failed_object_delete_retains_durable_retry_until_verified_empty(enabled, tmp_path, monkeypatch):
    guest = issue(enabled)
    owner = guest["user"]["id"]
    storage = LocalStorage(tmp_path)
    storage.save(f"users/{owner}/orphan", b"synthetic")
    monkeypatch.setattr(account_closure, "get_storage", lambda: storage)
    frontier_demo.queue_cleanup(owner_id=owner)
    original = storage.delete
    monkeypatch.setattr(storage, "delete", lambda key: (_ for _ in ()).throw(OSError("synthetic")))
    account_closure.process_one_closure(frontier_only=True)
    with session_scope() as session:
        assert session.get(UserRecord, owner) is not None
        pending = session.scalar(select(AccountClosureRecord))
        assert pending.error_code == "cleanup_retry_required"
        assert not session.get(UserRecord, owner).is_active
    monkeypatch.setattr(storage, "delete", original)
    release_claim(owner)
    account_closure.process_one_closure(frontier_only=True, raise_errors=True)
    assert storage.list_keys("") == []
    with session_scope() as session:
        assert session.get(UserRecord, owner) is None
        assert session.scalar(select(AccountClosureRecord)) is None
        assert session.scalar(select(FrontierDemoDailyUsageRecord)).sessions == 1


def test_guest_worker_does_not_process_formal_closure_or_delete_misclassified_user(enabled):
    with session_scope() as session:
        session.add(UserRecord(id="formal", username="formal", role="teacher", password_hash="hash", created_at=1, updated_at=1))
        session.add(AccountClosureRecord(id="closure", user_id="formal", mode="normal", status="pending", created_at=1))
    assert not account_closure.process_one_closure(frontier_only=True)
    with session_scope() as session:
        session.get(AccountClosureRecord, "closure").mode = "frontier_demo"
    with pytest.raises(RuntimeError, match="invalid_frontier_cleanup_identity"):
        account_closure.process_one_closure(frontier_only=True, raise_errors=True)
    with session_scope() as session:
        assert session.get(UserRecord, "formal") is not None


@pytest.mark.asyncio
async def test_public_cleanup_loop_scans_at_start_and_retries_pending_work(monkeypatch):
    calls = []
    monkeypatch.setattr(frontier_demo, "queue_cleanup", lambda: calls.append("scan"))
    monkeypatch.setattr(account_closure, "process_one_closure", lambda **kwargs: calls.append(kwargs))
    async def stop(_):
        raise asyncio.CancelledError
    monkeypatch.setattr(account_closure.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await account_closure.account_closure_loop(frontier_only=True)
    assert calls == ["scan", {"frontier_only": True}]
