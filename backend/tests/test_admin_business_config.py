"""No provider/SMTP calls: temporary SQLite, synthetic accounts and fake sender."""
import hashlib
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from backend.api.admin_business_config import router
from backend.auth import create_token
from backend.config import settings
from backend.db.business_config_models import BusinessConfigRecord, UserStorageConfigRecord
from backend.db.models import AdminAuditLogRecord, EmailVerificationRequestRecord, PasswordResetRateEventRecord, PasswordResetRequestRecord, UserRecord
from backend.db.session import session_scope
from backend.services.business_config import DEFAULTS, read_business_config
from backend.services.email_registration import RegistrationError, request_registration, resend_registration, verify_registration
from backend.services.password_reset import PasswordResetError, request_password_reset


class Sender:
    def __init__(self):
        self.messages = []
    def send(self, to, subject, text, html):
        self.messages.append((to, text))


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "allowed_email_domains", "example.edu")
    monkeypatch.setattr(settings, "public_frontend_url", "http://localhost:5173")
    monkeypatch.setattr(settings, "email_verification_resend_seconds", 60)
    monkeypatch.setattr(settings, "email_verification_hourly_email_limit", 5)
    monkeypatch.setattr(settings, "email_verification_hourly_ip_limit", 20)
    monkeypatch.setattr(settings, "unfinished_source_quota_bytes", 536870912)
    monkeypatch.setattr(settings, "knowledge_storage_quota_bytes", 536870912)
    with session_scope() as session:
        for identity, role in (("manager", "admin"), ("teacher", "teacher"), ("other", "teacher")):
            session.add(UserRecord(id=identity, username=identity, email=f"{identity}@example.edu", role=role,
                                   password_hash="synthetic-unused-hash", is_active=True))
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def headers(identity="manager", key=None):
    return {"Authorization": "Bearer " + create_token(identity, "admin" if identity == "manager" else "teacher", auth_version=0),
            "Idempotency-Key": key or uuid.uuid4().hex}


def save(client, changes, *, user=None, version=None, global_version=None, key=None):
    path = "/admin/business-config" + (f"/users/{user}" if user else "")
    snapshot = client.get(path, headers=headers()).json()
    payload = {"changes": changes, "expected_version": snapshot["version"] if version is None else version, "reason": "quota review"}
    if user:
        payload["expected_global_version"] = snapshot["global_version"] if global_version is None else global_version
    return client.patch(path, json=payload, headers=headers(key=key))


def test_unset_configuration_preserves_settings_and_never_mutates_singleton(client, monkeypatch):
    monkeypatch.setattr(settings, "knowledge_storage_quota_bytes", 27)
    snapshot = client.get("/admin/business-config", headers=headers()).json()
    assert snapshot["version"] == 0
    for key, default in DEFAULTS.items():
        assert snapshot["fields"][key]["effective"] == getattr(settings, key, default)
        assert snapshot["fields"][key]["override"] is None
    assert snapshot["fields"]["knowledge_storage_quota_bytes"]["source"] == "settings"
    assert save(client, {"knowledge_storage_quota_bytes": 5}).status_code == 200
    assert settings.knowledge_storage_quota_bytes == 27
    assert save(client, {"knowledge_storage_quota_bytes": None}).json()["fields"]["knowledge_storage_quota_bytes"]["effective"] == 27


def test_authorization_and_required_reason_key(client):
    for method in ("get", "patch"):
        for path in ("/admin/business-config", "/admin/business-config/users/teacher"):
            kwargs = {} if method == "get" else {"json": {"changes": {"knowledge_storage_quota_bytes": 0}, "expected_version": 0, "expected_global_version": 0, "reason": "test"}}
            assert getattr(client, method)(path, headers=headers("teacher"), **kwargs).status_code == 403
            assert getattr(client, method)(path, **kwargs).status_code == 401
    payload = {"changes": {"knowledge_storage_quota_bytes": 1}, "expected_version": 0, "reason": "   "}
    assert client.patch("/admin/business-config", json=payload, headers=headers()).status_code == 422
    payload["reason"] = "review"
    h = headers(); del h["Idempotency-Key"]
    assert client.patch("/admin/business-config", json=payload, headers=h).status_code == 400


@pytest.mark.parametrize("changes", [
    {"allowed_email_domains": " "}, {"allowed_email_domains": "example.edu,"},
    {"allowed_email_domains": "*,example.edu"}, {"allowed_email_domains": "*.example.edu"},
    {"allowed_email_domains": "https://example.edu"}, {"allowed_email_domains": "a@example.edu"},
    {"allowed_email_domains": "bad_domain.edu"}, {"allowed_email_domains": "127.0.0.1"},
    {"allowed_email_domains": "localhost"}, {"allowed_email_domains": "example.edu:443"},
    {"email_verification_resend_seconds": 0}, {"email_verification_resend_seconds": 3601},
    {"email_verification_hourly_email_limit": 101}, {"email_verification_hourly_ip_limit": 1001},
    {"knowledge_storage_quota_bytes": -1}, {"knowledge_storage_quota_bytes": 1099511627777},
    {"unfinished_source_quota_bytes": True}, {"unfinished_source_quota_bytes": "42"},
    {"unfinished_source_quota_bytes": 1.5}, {"email_verification_hourly_ip_limit": ""},
    {"jwt_secret": "never"}, {"shared_pool_enabled": True}, {},
])
def test_invalid_settings_are_rejected_without_partial_save(client, changes):
    assert save(client, changes).status_code == 422
    assert client.get("/admin/business-config", headers=headers()).json()["version"] == 0
    with session_scope() as session:
        assert session.query(AdminAuditLogRecord).count() == 0


def test_user_scope_only_accepts_storage_and_requires_parent_version(client):
    assert save(client, {"allowed_email_domains": "*"}, user="teacher").status_code == 422
    response = client.patch("/admin/business-config/users/teacher", json={"changes": {"knowledge_storage_quota_bytes": 3}, "expected_version": 0, "reason": "review"}, headers=headers())
    assert response.status_code == 422
    assert client.get("/admin/business-config/users/missing", headers=headers()).status_code == 404


def test_version_audit_idempotency_and_no_aba_after_inheritance(client):
    key = uuid.uuid4().hex
    first = save(client, {"allowed_email_domains": " ExAmPlE.EDU., 例子.中国 "}, key=key)
    assert first.status_code == 200, first.text
    assert first.json()["fields"]["allowed_email_domains"]["effective"] == "example.edu,xn--fsqu00a.xn--fiqs8s"
    repeated = save(client, {"allowed_email_domains": " ExAmPlE.EDU., 例子.中国 "}, version=0, key=key)
    assert repeated.json() == first.json()
    assert save(client, {"allowed_email_domains": "*"}, version=1, key=key).status_code == 409
    assert save(client, {"allowed_email_domains": "*"}, version=0).status_code == 409
    assert save(client, {"allowed_email_domains": None}).json()["version"] == 2
    assert save(client, {"allowed_email_domains": "*"}, version=0).status_code == 409
    with session_scope() as session:
        records = session.scalars(select(AdminAuditLogRecord).order_by(AdminAuditLogRecord.created_at)).all()
        assert len(records) == 2
        assert records[0].actor_id == "manager"
        assert records[0].reason == "quota review"
        assert records[0].before_state == {"version": 0, "overrides": {}}
        assert records[0].after_state["version"] == 1
        assert records[1].after_state["overrides"] == {}


def test_two_writers_cannot_lose_an_update(client):
    def change(value):
        return client.patch("/admin/business-config", json={"changes": {"knowledge_storage_quota_bytes": value}, "expected_version": 0, "reason": "race"}, headers=headers()).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(change, [10, 20])) == [200, 409]
    with session_scope() as session:
        assert session.query(AdminAuditLogRecord).count() == 1


def test_user_version_rejects_changed_parent_and_inheritance_order(client):
    assert save(client, {"knowledge_storage_quota_bytes": 10}).status_code == 200
    result = save(client, {"knowledge_storage_quota_bytes": 4}, user="teacher")
    assert result.json()["fields"]["knowledge_storage_quota_bytes"]["source"] == "user_override"
    assert result.json()["usage"]["knowledge_storage_quota_bytes"]["limit_bytes"] == 4
    assert save(client, {"knowledge_storage_quota_bytes": 8}).status_code == 200
    assert save(client, {"knowledge_storage_quota_bytes": 9}, user="teacher", global_version=1).status_code == 409
    assert save(client, {"knowledge_storage_quota_bytes": None}, user="teacher").json()["fields"]["knowledge_storage_quota_bytes"] == {"effective": 8, "source": "global_override", "override": None, "bounds": [0, 1099511627776]}
    assert save(client, {"knowledge_storage_quota_bytes": None}).status_code == 200
    assert client.get("/admin/business-config/users/teacher", headers=headers()).json()["fields"]["knowledge_storage_quota_bytes"]["effective"] == 536870912


def request(email, sender, username=None, ip="127.0.0.1"):
    return request_registration(username=username or uuid.uuid4().hex, email=email, password="synthetic-password", source_ip=ip, sender=sender)


def test_registration_rules_apply_at_request_resend_and_verification(client):
    sender = Sender()
    assert save(client, {"allowed_email_domains": "new.edu"}).status_code == 200
    for email in ("a@badnew.edu", "a@new.edu.evil", "a@example.edu"):
        with pytest.raises(RegistrationError, match="registration_email_domain_not_allowed"):
            request(email, sender)
    initial = request("alice@sub.new.edu", sender)
    token = re.search(r"#token=([^\s]+)", sender.messages[0][1]).group(1)
    with session_scope() as session:
        row = session.get(EmailVerificationRequestRecord, initial["request_id"])
        expiry = row.expires_at
        row.resend_available_at = 0
    assert save(client, {"allowed_email_domains": ""}).status_code == 200
    with pytest.raises(RegistrationError, match="registration_email_domain_not_allowed"):
        resend_registration(request_id=initial["request_id"], source_ip="127.0.0.1", sender=sender)
    with pytest.raises(RegistrationError, match="registration_email_domain_not_allowed"):
        verify_registration(token)
    with session_scope() as session:
        assert session.get(EmailVerificationRequestRecord, initial["request_id"]).expires_at == expiry
        assert session.get(UserRecord, "teacher").is_active
    assert save(client, {"allowed_email_domains": "*"}).status_code == 200
    assert verify_registration(token)["status"] == "registered"
    assert save(client, {"allowed_email_domains": ""}).status_code == 200
    assert verify_registration(token)["status"] == "already_verified"


def test_registration_limits_reuse_existing_counts_and_frozen_token_deadlines(client, monkeypatch):
    sender = Sender()
    first = request("new@example.edu", sender)
    with session_scope() as session:
        row = session.get(EmailVerificationRequestRecord, first["request_id"])
        row.resend_available_at = 0
        expiry = row.expires_at
    assert save(client, {"email_verification_hourly_email_limit": 1}).status_code == 200
    with pytest.raises(RegistrationError, match="registration_rate_limited"):
        resend_registration(request_id=first["request_id"], source_ip="other-ip", sender=sender)
    with pytest.raises(RegistrationError, match="registration_rate_limited"):
        request("new@example.edu", sender)
    assert save(client, {"email_verification_hourly_ip_limit": 1}).status_code == 200
    with pytest.raises(RegistrationError, match="registration_rate_limited"):
        request("second@example.edu", sender)
    assert save(client, {"email_verification_resend_seconds": 120}).status_code == 200
    created = request("third@example.edu", sender, ip="another-ip")
    assert created["resend_after_seconds"] == 120
    with session_scope() as session:
        assert session.get(EmailVerificationRequestRecord, first["request_id"]).expires_at == expiry
        row = session.get(EmailVerificationRequestRecord, created["request_id"])
        assert row.resend_available_at - row.created_at == 120
    assert len(sender.messages) == 2


def test_reset_preserves_legacy_then_keeps_recovery_independent_of_registration(client, monkeypatch):
    sender = Sender()
    with session_scope() as session:
        session.get(UserRecord, "teacher").email = "teacher@old.edu"
    # No new configuration: exactly the old domain eligibility.
    assert request_password_reset(email="teacher@old.edu", source_ip="ip", sender=sender)["status"] == "reset_link_requested"
    assert sender.messages == []
    assert save(client, {"allowed_email_domains": "new.edu", "email_verification_resend_seconds": 1, "email_verification_hourly_email_limit": 1}).status_code == 200
    callbacks = []
    known = request_password_reset(email="teacher@old.edu", source_ip="ip", sender=sender, delivery_scheduler=callbacks.append)
    unknown = request_password_reset(email="unknown@old.edu", source_ip="ip", sender=sender, delivery_scheduler=callbacks.append)
    assert known == unknown
    assert sender.messages == []
    assert len(callbacks) == 2
    for callback in callbacks:
        callback()
    assert len(sender.messages) == 1
    with session_scope() as session:
        assert session.query(PasswordResetRateEventRecord).count() == 2
        expiry = session.query(PasswordResetRequestRecord).one().expires_at
    assert save(client, {"allowed_email_domains": None}).status_code == 200
    for email in ("teacher@old.edu", "unknown@old.edu"):
        with pytest.raises(PasswordResetError, match="password_reset_rate_limited"):
            request_password_reset(email=email, source_ip="ip", sender=sender)
    with session_scope() as session:
        assert session.query(PasswordResetRequestRecord).one().expires_at == expiry
        session.query(PasswordResetRateEventRecord).update({"created_at": time.time() - 4000})
    request_password_reset(email="teacher@old.edu", source_ip="ip", sender=sender)
    assert len(sender.messages) == 2


def test_reset_new_limit_and_cooldown_apply_to_existing_window(client, monkeypatch):
    sender = Sender()
    request_password_reset(email="teacher@example.edu", source_ip="ip", sender=sender)
    assert save(client, {"email_verification_resend_seconds": 120, "email_verification_hourly_ip_limit": 1}).status_code == 200
    with pytest.raises(PasswordResetError) as error:
        request_password_reset(email="teacher@example.edu", source_ip=None, sender=sender)
    assert error.value.retry_after > 60
    with pytest.raises(PasswordResetError):
        request_password_reset(email="other@example.edu", source_ip="ip", sender=sender)
    assert len(sender.messages) == 1


def test_storage_owner_override_lowering_zero_release_and_reset(client, tmp_path):
    from backend.db.knowledge_storage_repository import reserve_upload, knowledge_storage_usage
    from backend.db.source_storage_repository import reserve_source_upload, release_source_reservation, source_quota_usage
    from backend.db.models import CourseRecord, AssignmentRecord
    from backend.domain.errors import SourceStorageQuotaExceeded, KnowledgeStorageQuotaExceeded
    with session_scope() as session:
        session.add(CourseRecord(id="course", name="Test", teacher_id="teacher")); session.flush()
        session.add(AssignmentRecord(id="task", course_id="course", teacher_id="teacher", name="Test", status="draft", version=1))
    def source(content):
        return reserve_source_upload(file_id=uuid.uuid4().hex, file_owner_id="teacher", kind="problem_source", original_name="test.pdf",
            storage_backend="local", storage_key=uuid.uuid4().hex, content_type="application/pdf",
            requested_bytes=len(content), sha256=hashlib.sha256(content).hexdigest(), assignment_id="task", submission_revision_id=None)
    def knowledge(content, owner="teacher"):
        return reserve_upload(owner_id=owner, original_name="test.txt", size_bytes=len(content), sha256=hashlib.sha256(content).hexdigest())
    assert save(client, {"unfinished_source_quota_bytes": 10, "knowledge_storage_quota_bytes": 10}).status_code == 200
    original = source(b"123456")
    saved = knowledge(b"123456")
    assert save(client, {"unfinished_source_quota_bytes": 0, "knowledge_storage_quota_bytes": 0}, user="teacher").status_code == 200
    assert source_quota_usage("teacher").used_bytes == knowledge_storage_usage("teacher").used_bytes == 6
    assert source_quota_usage("teacher").limit_bytes == knowledge_storage_usage("teacher").limit_bytes == 0
    with pytest.raises(SourceStorageQuotaExceeded): source(b"x")
    with pytest.raises(KnowledgeStorageQuotaExceeded): knowledge(b"x")
    # A duplicate knowledge reservation adds no bytes, and release is allowed.
    assert knowledge(b"123456").entry.id == saved.entry.id
    assert knowledge(b"x", "other").created
    assert release_source_reservation(original.id)
    assert source_quota_usage("teacher").used_bytes == 0
    assert save(client, {"unfinished_source_quota_bytes": None, "knowledge_storage_quota_bytes": None}, user="teacher").status_code == 200
    assert source(b"x")
    assert knowledge(b"x").created
    assert source_quota_usage("teacher").limit_bytes == knowledge_storage_usage("teacher").limit_bytes == 10


def test_owner_delete_cascades_only_user_settings_and_clear_tables_restores_environment(client):
    assert save(client, {"allowed_email_domains": "new.edu", "knowledge_storage_quota_bytes": 7}).status_code == 200
    assert save(client, {"knowledge_storage_quota_bytes": 3}, user="teacher").status_code == 200
    with session_scope() as session:
        session.execute(delete(UserRecord).where(UserRecord.id == "teacher"))
    with session_scope() as session:
        assert session.get(UserStorageConfigRecord, "teacher") is None
        assert session.get(BusinessConfigRecord, "global") is not None
        session.execute(delete(UserStorageConfigRecord)); session.execute(delete(BusinessConfigRecord))
    with session_scope() as session:
        config = read_business_config(session)
        assert config.values["allowed_email_domains"] == "example.edu"
        assert not config.registration_rules_managed
        assert config.global_version == 0


def test_running_second_process_reads_committed_updates_without_restart(client, tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    code = """import sys
from backend.db.session import session_scope
from backend.services.business_config import read_business_config
for line in sys.stdin:
    with session_scope() as session:
        c = read_business_config(session, 'teacher')
        print(c.values['knowledge_storage_quota_bytes'], flush=True)
"""
    env = {"PATH": os.environ["PATH"], "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
           "SMARTAI_DATABASE_URL": os.environ["SMARTAI_DATABASE_URL"],
           "SMARTAI_DATABASE_HEAVY": "OFF", "SMARTAI_KNOWLEDGE_STORAGE_QUOTA_BYTES": "536870912"}
    process = subprocess.Popen([sys.executable, "-u", "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, cwd=tmp_path, env=env)
    def read():
        import selectors
        process.stdin.write("read\n"); process.stdin.flush()
        with selectors.DefaultSelector() as poll:
            poll.register(process.stdout, selectors.EVENT_READ)
            assert poll.select(10), "Configuration reader did not respond"
            return int(process.stdout.readline())
    try:
        assert read() == 536870912
        assert save(client, {"knowledge_storage_quota_bytes": 30}).status_code == 200
        assert read() == 30
        assert save(client, {"knowledge_storage_quota_bytes": 10}, user="teacher").status_code == 200
        assert read() == 10
        assert save(client, {"knowledge_storage_quota_bytes": None}, user="teacher").status_code == 200
        assert read() == 30
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait(timeout=5)
        process.stdout.close(); process.stderr.close()


def test_admin_assisted_reset_cannot_bypass_live_email_limits(client, monkeypatch):
    from backend.api import admin
    sender = Sender()
    monkeypatch.setattr(admin, "get_email_sender", lambda: sender)
    client.app.include_router(admin.router)
    assert save(client, {"email_verification_hourly_email_limit": 1}).status_code == 200
    request_password_reset(email="teacher@example.edu", source_ip="same-ip", sender=sender)
    response = client.post("/admin/users/teacher/password-reset", headers=headers(), json={"reason": "support"})
    assert response.status_code == 202
    assert response.json()["delivery"] == "unconfirmed"
    assert len(sender.messages) == 1
    with session_scope() as session:
        assert session.query(PasswordResetRateEventRecord).count() == 1


def test_sqlite_migration_upgrade_downgrade_upgrade(client, tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    url = f"sqlite:///{tmp_path / 'business-migration.db'}"
    monkeypatch.setenv("SMARTAI_DATABASE_URL", url)
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "backend/db/migrations"))
    engine = create_engine(url)
    try:
        command.upgrade(config, "head")
        assert {"business_configuration", "user_storage_configuration"} <= set(inspect(engine).get_table_names())
        assert inspect(engine).get_foreign_keys("user_storage_configuration")[0]["options"]["ondelete"] == "CASCADE"
        command.downgrade(config, "0022_account_closures")
        assert "business_configuration" not in inspect(engine).get_table_names()
        command.upgrade(config, "head")
        assert "business_configuration" in inspect(engine).get_table_names()
    finally:
        engine.dispose()
