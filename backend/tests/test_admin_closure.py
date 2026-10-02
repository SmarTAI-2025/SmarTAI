import pytest
from sqlalchemy import select
from backend.tests.test_admin_account_lifecycle import accounts, headers
from backend.db.auth_repository import AuthRepositoryError, register_without_invite
from backend.db.session import session_scope
from backend.db.models import AccountClosureRecord, BlockedRegistrationEmailRecord, UserRecord
from backend.services.account_closure import process_one_closure
from backend.auth import hash_password
from backend.storage.local import LocalStorage


@pytest.mark.parametrize("mode", ["normal", "blacklist"])
def test_closure_releases_expected_identity_only_after_storage_cleanup(mode, tmp_path, monkeypatch):
    client, user, admin, teacher = accounts()
    storage = LocalStorage(tmp_path / "uploads")
    storage.save(f"users/{user.id}/orphan.bin", b"discardable test bytes")
    monkeypatch.setattr("backend.services.account_closure.get_storage", lambda: storage)
    from backend.db.business_config_models import UserStorageConfigRecord
    from backend.db.models import AdminAuditLogRecord
    with session_scope() as session:
        session.add(UserStorageConfigRecord(owner_id=user.id, overrides={"knowledge_storage_quota_bytes": 0}, version=1, updated_at=1))
    request_headers = headers(admin, "close-once")
    body = {"mode": mode, "confirm_username": "teacher", "reason": "requested_or_abuse"}
    response = client.post(f"/admin/users/{user.id}/closure", json=body, headers=request_headers)
    assert response.status_code == 202, response.text
    assert client.post(f"/admin/users/{user.id}/closure", json=body, headers=request_headers).json() == response.json()
    assert client.get("/auth/me", headers=headers(teacher)).status_code == 401
    assert client.patch(f"/admin/users/{user.id}/access", json={"access": "normal"}, headers=headers(admin)).status_code == 409
    with pytest.raises(AuthRepositoryError):
        register_without_invite(username="teacher", email="new@example.edu", password_hash="unused")
    assert process_one_closure(raise_errors=True)
    status = client.get(f"/admin/closures/{response.json()['closure_id']}", headers=headers(admin)).json()
    assert status["status"] == "completed", status
    assert storage.list_keys("") == []
    with session_scope() as session:
        assert session.get(UserRecord, user.id) is None
        assert session.get(UserStorageConfigRecord, user.id) is None
        completed = session.scalar(select(AdminAuditLogRecord).where(AdminAuditLogRecord.action == "account_closure_completed"))
        assert completed.actor_id == "manager"
        assert completed.after_state == {"status": "completed", "mode": mode}
    assert not process_one_closure(raise_errors=True)
    if mode == "blacklist":
        with pytest.raises(AuthRepositoryError):
            register_without_invite(username="newname", email="TEACHER@EXAMPLE.EDU", password_hash="unused")
        register_without_invite(username="teacher", email="different@example.edu", password_hash="unused")
    else:
        register_without_invite(username="teacher", email="teacher@example.edu", password_hash="unused")


def test_storage_failure_keeps_identity_and_has_recoverable_closure(tmp_path, monkeypatch):
    client, user, admin, _ = accounts()
    storage = LocalStorage(tmp_path / "uploads")
    storage.save(f"users/{user.id}/orphan.bin", b"discardable")
    monkeypatch.setattr("backend.services.account_closure.get_storage", lambda: storage)
    real_delete = storage.delete
    monkeypatch.setattr(storage, "delete", lambda key: (_ for _ in ()).throw(OSError("synthetic failure")))
    response = client.post(f"/admin/users/{user.id}/closure", json={"mode": "normal", "confirm_username": "teacher"}, headers=headers(admin))
    assert response.status_code == 202
    process_one_closure()
    with session_scope() as session:
        row = session.scalar(select(AccountClosureRecord).where(AccountClosureRecord.user_id == user.id))
        assert row.status == "pending"
        assert row.error_code == "cleanup_retry_required"
        assert session.get(UserRecord, user.id) is not None
        row.lease_until = 0
    monkeypatch.setattr(storage, "delete", real_delete)
    process_one_closure()
    assert client.get(f"/admin/closures/{response.json()['closure_id']}", headers=headers(admin)).json()["status"] == "completed"


def test_wrong_confirmation_and_self_closure_make_no_changes():
    client, user, admin, teacher = accounts()
    assert client.post(f"/admin/users/{user.id}/closure", json={"mode": "normal", "confirm_username": "wrong"}, headers=headers(admin)).status_code == 400
    assert client.post("/admin/users/manager/closure", json={"mode": "normal", "confirm_username": "manager"}, headers=headers(admin)).status_code == 409
    assert client.get("/auth/me", headers=headers(teacher)).status_code == 200
    with session_scope() as session:
        assert session.scalar(select(AccountClosureRecord.id)) is None


@pytest.mark.asyncio
async def test_closure_drains_real_task_and_knowledge_workers_before_releasing_identity(tmp_path, monkeypatch):
    from backend.services import task_facade, task_deletion
    from backend.db import file_repository
    from backend.storage import get_storage
    from backend.services.knowledge_storage import KnowledgeStorageWorker
    from backend.tests.test_source_cleanup import _finish_task_delete, _task_delete_worker
    from backend.db.models import AssignmentRecord, StoredFileRecord, KnowledgeDocumentRecord, AdminUsageEventRecord
    client, user, admin, teacher = accounts()
    storage = LocalStorage(tmp_path / "dedicated")
    monkeypatch.setattr("backend.services.account_closure.get_storage", lambda: storage)
    monkeypatch.setattr(task_deletion, "get_storage", lambda: storage)
    monkeypatch.setattr("backend.api.knowledge.get_storage", lambda: storage)
    monkeypatch.setattr("backend.services.knowledge_storage.get_storage", lambda: storage)
    task = task_facade.create_task(owner_id=user.id, name="owned task", semester_id=None, course_id=None, idempotency_key="close-task")
    original = file_repository.save_file(storage=storage, owner_id=user.id, kind="problem_source", original_name="test.pdf", content=b"fake source", content_type="application/pdf", assignment_id=task["task_id"])
    uploaded = client.post("/knowledge/documents", headers=headers(teacher), files={"file": ("notes.txt", b"synthetic notes", "text/plain")})
    assert uploaded.status_code == 201, uploaded.text
    response = client.post(f"/admin/users/{user.id}/closure", json={"mode": "normal", "confirm_username": "teacher"}, headers=headers(admin))
    assert response.status_code == 202
    process_one_closure(raise_errors=True)
    with session_scope() as session:
        assert session.get(UserRecord, user.id) is not None
    await _finish_task_delete(_task_delete_worker(), task["task_id"])
    assert not storage.exists(original.storage_key)
    def release():
        with session_scope() as session:
            session.scalar(select(AccountClosureRecord).where(AccountClosureRecord.user_id == user.id)).lease_until = 0
    release(); process_one_closure(raise_errors=True)
    worker = KnowledgeStorageWorker(storage=storage)
    for _ in range(5):
        await worker.run_once(max_claims=10)
        release(); process_one_closure(raise_errors=True)
    with session_scope() as session:
        assert session.get(UserRecord, user.id) is None
        assert session.get(AssignmentRecord, task["task_id"]) is None
        from sqlalchemy import text
        remaining_files = list(session.scalars(select(StoredFileRecord).where(StoredFileRecord.owner_id == user.id)))
        assert not remaining_files, {"foreign_keys": session.scalar(text("PRAGMA foreign_keys")), "files": [(f.kind, f.assignment_id, f.knowledge_document_id) for f in remaining_files]}
        assert session.scalar(select(KnowledgeDocumentRecord).where(KnowledgeDocumentRecord.owner_id == user.id)) is None
        assert session.scalar(select(AdminUsageEventRecord).where(AdminUsageEventRecord.user_id == user.id)) is None
        assert session.scalar(select(AdminUsageEventRecord).where(AdminUsageEventRecord.user_id.like("closed_%"))) is not None
    assert storage.list_keys("") == []
    register_without_invite(username="teacher", email="teacher@example.edu", password_hash="unused")


def test_legacy_student_closure_does_not_cascade_other_teachers_work():
    client, user, admin, _ = accounts()
    with session_scope() as session:
        session.get(UserRecord, user.id).role = "student"
    response = client.post(f"/admin/users/{user.id}/closure", json={"mode": "normal", "confirm_username": "teacher"}, headers=headers(admin))
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "account_has_shared_student_work"
    with session_scope() as session:
        assert session.get(UserRecord, user.id).is_active
        assert session.scalar(select(AccountClosureRecord.id)) is None
