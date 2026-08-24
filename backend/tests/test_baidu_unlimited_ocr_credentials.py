from __future__ import annotations

import inspect as python_inspect
import logging
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select

from backend.api.ocr_providers import (
    get_baidu_unlimited_ocr_credential_verifier,
)
from backend.auth import create_token
from backend.config import settings
from backend.db.models import (
    OCRProviderCredentialRecord,
    ProviderConfigRecord,
    UserRecord,
)
from backend.db.ocr_provider_repository import (
    get_baidu_unlimited_ocr_credential,
    get_baidu_unlimited_ocr_credential_metadata,
    upsert_baidu_unlimited_ocr_credential,
)
from backend.db.session import session_scope
from backend.main import app
from backend.security.secrets import EncryptedSecret, decrypt_secret
from backend.services.baidu_unlimited_ocr_factory import (
    OCRCredentialNotFoundError,
    build_owner_baidu_unlimited_ocr_skill,
    get_owner_baidu_unlimited_ocr_credentials,
)


MASTER_KEY = "test-ocr-master-key-that-is-long-enough-0123456789"


def _add_teacher(owner_id: str) -> None:
    with session_scope() as session:
        session.add(UserRecord(
            id=owner_id,
            username=owner_id,
            email=f"{owner_id}@test.local",
            role="teacher",
            password_hash="hash",
            is_active=True,
            created_at=1,
            updated_at=1,
        ))


def _headers(owner_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_token(owner_id, 'teacher')}"}


def test_baidu_credentials_are_independently_encrypted_and_owner_scoped():
    owner = "baidu-owner-a"
    other_owner = "baidu-owner-b"
    _add_teacher(owner)
    _add_teacher(other_owner)

    metadata = upsert_baidu_unlimited_ocr_credential(
        owner,
        api_key="baidu-ak-owner-a",
        secret_key="baidu-sk-owner-a",
        master_key=MASTER_KEY,
    )

    with session_scope() as session:
        row = session.scalar(select(OCRProviderCredentialRecord).where(
            OCRProviderCredentialRecord.id == metadata.id,
        ))
        assert row is not None
        serialized = "|".join((
            row.encrypted_api_key,
            row.api_key_nonce,
            row.encrypted_secret_key,
            row.secret_key_nonce,
        ))
        assert "baidu-ak-owner-a" not in serialized
        assert "baidu-sk-owner-a" not in serialized
        assert row.api_key_nonce != row.secret_key_nonce
        encrypted_api_key = EncryptedSecret(
            row.encrypted_api_key,
            row.api_key_nonce,
            row.api_key_version,
        )
        for wrong_aad in (
            f"ocr-provider-credential:{owner}:{metadata.id}:secret-key",
            f"ocr-provider-credential:{other_owner}:{metadata.id}:api-key",
        ):
            with pytest.raises(ValueError):
                decrypt_secret(
                    encrypted_api_key,
                    master_key=MASTER_KEY,
                    associated_data=wrong_aad,
                )

    loaded = get_baidu_unlimited_ocr_credential(
        owner,
        metadata.id,
        master_key=MASTER_KEY,
    )
    assert loaded is not None
    assert loaded.api_key == "baidu-ak-owner-a"
    assert loaded.secret_key == "baidu-sk-owner-a"
    assert "baidu-ak-owner-a" not in repr(loaded)
    assert "baidu-sk-owner-a" not in repr(loaded)
    assert get_baidu_unlimited_ocr_credential(
        other_owner,
        metadata.id,
        master_key=MASTER_KEY,
    ) is None

    replaced = upsert_baidu_unlimited_ocr_credential(
        owner,
        api_key="baidu-ak-replaced",
        secret_key="baidu-sk-replaced",
        master_key=MASTER_KEY,
    )
    assert replaced.id == metadata.id
    with session_scope() as session:
        assert len(list(session.scalars(select(OCRProviderCredentialRecord)))) == 1


def test_ocr_api_crud_and_token_only_verification_are_redacted(monkeypatch):
    owner = "baidu-api-owner"
    other_owner = "baidu-api-other"
    _add_teacher(owner)
    _add_teacher(other_owner)
    monkeypatch.setattr(settings, "provider_encryption_key", MASTER_KEY)

    received: list[tuple[str, str]] = []

    async def fake_token_verifier(*, api_key: str, secret_key: str) -> None:
        received.append((api_key, secret_key))

    app.dependency_overrides[get_baidu_unlimited_ocr_credential_verifier] = (
        lambda: fake_token_verifier
    )
    client = TestClient(app)
    try:
        assert client.get("/ocr/providers/baidu-unlimited-ocr").status_code == 401
        stored = client.put(
            "/ocr/providers/baidu-unlimited-ocr",
            headers=_headers(owner),
            json={"api_key": "api-redacted-ak", "secret_key": "api-redacted-sk"},
        )
        assert stored.status_code == 200
        payload = stored.json()
        credential_id = payload["credential_id"]
        assert payload["status"] == "credentials_stored"
        assert payload["provider_type"] == "baidu_unlimited_ocr"
        assert payload["verification_status"] == "unverified"
        assert "api-redacted-ak" not in stored.text
        assert "api-redacted-sk" not in stored.text
        assert "cipher" not in stored.text.lower()
        assert "nonce" not in stored.text.lower()
        with session_scope() as session:
            assert list(session.scalars(select(ProviderConfigRecord))) == []

        current_read = client.get(
            "/ocr/providers/baidu-unlimited-ocr",
            headers=_headers(owner),
        )
        assert current_read.status_code == 200
        assert current_read.json()["credential_id"] == credential_id
        assert client.get(
            "/ocr/providers/baidu-unlimited-ocr",
            headers=_headers(other_owner),
        ).status_code == 404

        owner_read = client.get(
            f"/ocr/providers/baidu-unlimited-ocr/{credential_id}",
            headers=_headers(owner),
        )
        assert owner_read.status_code == 200
        assert owner_read.json()["credentials_configured"] is True

        for method, suffix in (
            ("get", ""),
            ("delete", ""),
            ("post", "/verify"),
        ):
            wrong_owner = getattr(client, method)(
                f"/ocr/providers/baidu-unlimited-ocr/{credential_id}{suffix}",
                headers=_headers(other_owner),
            )
            assert wrong_owner.status_code == 404
            assert wrong_owner.json()["detail"]["code"] == "ocr_credential_not_found"

        verified = client.post(
            f"/ocr/providers/baidu-unlimited-ocr/{credential_id}/verify",
            headers=_headers(owner),
        )
        assert verified.status_code == 200
        assert verified.json()["status"] == "credentials_verified"
        assert verified.json()["verification_scope"] == "credentials_only"
        assert verified.json()["service_readiness"] == "not_tested"
        assert "access_token" not in verified.text
        assert received == [("api-redacted-ak", "api-redacted-sk")]

        updated_read = client.get(
            f"/ocr/providers/baidu-unlimited-ocr/{credential_id}",
            headers=_headers(owner),
        )
        assert (
            updated_read.json()["verification_status"]
            == "credentials_verified"
        )

        removed = client.delete(
            f"/ocr/providers/baidu-unlimited-ocr/{credential_id}",
            headers=_headers(owner),
        )
        assert removed.status_code == 200
        assert removed.json()["status"] == "credentials_deleted"
        assert client.get(
            f"/ocr/providers/baidu-unlimited-ocr/{credential_id}",
            headers=_headers(owner),
        ).status_code == 404
    finally:
        app.dependency_overrides.pop(
            get_baidu_unlimited_ocr_credential_verifier,
            None,
        )


def test_verification_error_is_safely_projected_and_does_not_log_secrets(
    monkeypatch,
    caplog,
):
    owner = "baidu-api-failed-verify"
    _add_teacher(owner)
    monkeypatch.setattr(settings, "provider_encryption_key", MASTER_KEY)

    class VendorAuthError(RuntimeError):
        code = "provider_auth_failed"

    async def rejected_verifier(*, api_key: str, secret_key: str) -> None:
        raise VendorAuthError(
            f"vendor rejected api_key={api_key} secret_key={secret_key}"
        )

    metadata = upsert_baidu_unlimited_ocr_credential(
        owner,
        api_key="never-log-ak",
        secret_key="never-log-sk",
        master_key=MASTER_KEY,
    )
    app.dependency_overrides[get_baidu_unlimited_ocr_credential_verifier] = (
        lambda: rejected_verifier
    )
    client = TestClient(app)
    try:
        with caplog.at_level(logging.WARNING):
            response = client.post(
                f"/ocr/providers/baidu-unlimited-ocr/{metadata.id}/verify",
                headers=_headers(owner),
            )
        assert response.status_code == 422
        assert response.json() == {"detail": {"code": "provider_auth_failed"}}
        combined = response.text + caplog.text
        assert "never-log-ak" not in combined
        assert "never-log-sk" not in combined
        assert metadata.id not in caplog.text
    finally:
        app.dependency_overrides.pop(
            get_baidu_unlimited_ocr_credential_verifier,
            None,
        )


@pytest.mark.parametrize("verification_fails", [False, True])
def test_verification_does_not_write_result_after_concurrent_credential_replacement(
    monkeypatch,
    verification_fails,
):
    owner = f"baidu-concurrent-replacement-{verification_fails}"
    _add_teacher(owner)
    monkeypatch.setattr(settings, "provider_encryption_key", MASTER_KEY)
    initial = upsert_baidu_unlimited_ocr_credential(
        owner,
        api_key="old-ak-being-verified",
        secret_key="old-sk-being-verified",
        master_key=MASTER_KEY,
    )
    received: list[tuple[str, str]] = []

    class RejectedOldCredential(RuntimeError):
        code = "provider_auth_failed"

    async def replacing_verifier(*, api_key: str, secret_key: str) -> None:
        received.append((api_key, secret_key))
        upsert_baidu_unlimited_ocr_credential(
            owner,
            api_key="replacement-ak",
            secret_key="replacement-sk",
            master_key=MASTER_KEY,
        )
        # Make the optimistic precondition deterministic even on a platform
        # whose wall-clock resolution returns the same value twice.
        with session_scope() as session:
            row = session.get(OCRProviderCredentialRecord, initial.id)
            assert row is not None
            row.updated_at = initial.updated_at + 1.0
        if verification_fails:
            raise RejectedOldCredential("raw vendor response must stay private")

    app.dependency_overrides[get_baidu_unlimited_ocr_credential_verifier] = (
        lambda: replacing_verifier
    )
    try:
        response = TestClient(app).post(
            f"/ocr/providers/baidu-unlimited-ocr/{initial.id}/verify",
            headers=_headers(owner),
        )
    finally:
        app.dependency_overrides.pop(
            get_baidu_unlimited_ocr_credential_verifier,
            None,
        )

    assert response.status_code == 409
    assert response.json() == {"detail": {"code": "ocr_credential_changed"}}
    assert received == [("old-ak-being-verified", "old-sk-being-verified")]
    current = get_baidu_unlimited_ocr_credential(
        owner,
        initial.id,
        master_key=MASTER_KEY,
    )
    assert current is not None
    assert current.api_key == "replacement-ak"
    assert current.secret_key == "replacement-sk"
    assert current.metadata.verification_status == "unverified"


@pytest.mark.parametrize(
    "payload",
    [
        {"api_key": "must-not-echo-missing-field"},
        {
            "api_key": "must-not-echo-extra-field",
            "secret_key": "another-secret-value",
            "unexpected": "also-secret",
        },
        {"api_key": "a" * 513, "secret_key": "must-not-echo-too-long"},
    ],
)
def test_invalid_credential_payload_never_echoes_submitted_values(payload):
    owner = "baidu-api-invalid-payload"
    _add_teacher(owner)

    response = TestClient(app).put(
        "/ocr/providers/baidu-unlimited-ocr",
        headers=_headers(owner),
        json=payload,
    )

    assert response.status_code == 422
    for value in payload.values():
        assert str(value) not in response.text


def test_production_missing_master_key_does_not_persist_submitted_credentials(
    monkeypatch,
):
    owner = "baidu-api-no-master"
    _add_teacher(owner)
    monkeypatch.setattr(settings, "runtime_environment", "production")
    monkeypatch.setattr(settings, "provider_encryption_key", "")

    response = TestClient(app).put(
        "/ocr/providers/baidu-unlimited-ocr",
        headers=_headers(owner),
        json={"api_key": "must-not-save-ak", "secret_key": "must-not-save-sk"},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "provider_encryption_not_configured"
    lowered = response.text.lower()
    assert "smartai_" not in lowered
    assert "32" not in lowered
    assert "must-not-save-ak" not in response.text
    assert "must-not-save-sk" not in response.text
    assert get_baidu_unlimited_ocr_credential_metadata(owner, "missing") is None
    with session_scope() as session:
        assert list(session.scalars(select(OCRProviderCredentialRecord))) == []


def test_production_missing_master_key_blocks_verification_without_hiding_metadata(
    monkeypatch,
):
    owner = "baidu-api-existing-no-master"
    _add_teacher(owner)
    metadata = upsert_baidu_unlimited_ocr_credential(
        owner,
        api_key="existing-ak-not-visible",
        secret_key="existing-sk-not-visible",
        master_key=MASTER_KEY,
    )
    monkeypatch.setattr(settings, "runtime_environment", "production")
    monkeypatch.setattr(settings, "provider_encryption_key", "")

    client = TestClient(app)
    visible_metadata = client.get(
        f"/ocr/providers/baidu-unlimited-ocr/{metadata.id}",
        headers=_headers(owner),
    )
    response = client.post(
        f"/ocr/providers/baidu-unlimited-ocr/{metadata.id}/verify",
        headers=_headers(owner),
    )

    assert visible_metadata.status_code == 200
    assert visible_metadata.json()["credentials_configured"] is True
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "provider_encryption_not_configured"
    assert "existing-ak-not-visible" not in response.text
    assert "existing-sk-not-visible" not in response.text
    assert "smartai_" not in response.text.lower()


def test_owner_factory_requires_explicit_owner_and_credential_id():
    owner = "baidu-factory-owner"
    other_owner = "baidu-factory-other"
    _add_teacher(owner)
    _add_teacher(other_owner)
    metadata = upsert_baidu_unlimited_ocr_credential(
        owner,
        api_key="factory-ak",
        secret_key="factory-sk",
        master_key=MASTER_KEY,
    )

    loaded = get_owner_baidu_unlimited_ocr_credentials(
        owner,
        metadata.id,
        master_key=MASTER_KEY,
    )
    assert loaded.api_key == "factory-ak"
    assert loaded.secret_key == "factory-sk"
    with pytest.raises(OCRCredentialNotFoundError):
        get_owner_baidu_unlimited_ocr_credentials(
            other_owner,
            metadata.id,
            master_key=MASTER_KEY,
        )

    skill = build_owner_baidu_unlimited_ocr_skill(
        owner,
        metadata.id,
        master_key=MASTER_KEY,
        client_kwargs={"http_client": object()},
    )
    from backend.skills.ocr_ingest import BaiduUnlimitedOCRSkill

    assert isinstance(skill, BaiduUnlimitedOCRSkill)
    client_repr = repr(skill.client)
    assert "factory-ak" not in client_repr
    assert "factory-sk" not in client_repr
    assert "redacted" in client_repr
    assert "ExpertRegistry" not in python_inspect.getsource(
        build_owner_baidu_unlimited_ocr_skill
    )
    with pytest.raises(OCRCredentialNotFoundError):
        build_owner_baidu_unlimited_ocr_skill(
            other_owner,
            metadata.id,
            master_key=MASTER_KEY,
            client_kwargs={"http_client": object()},
        )


def test_ocr_credential_migration_contract(tmp_path, monkeypatch):
    repo_root = Path(__file__).resolve().parents[2]
    db_url = f"sqlite:///{(tmp_path / 'ocr-credentials.db').as_posix()}"
    config = Config(str(repo_root / "alembic.ini"))
    config.set_main_option(
        "script_location",
        str(repo_root / "backend/db/migrations"),
    )
    monkeypatch.setenv("SMARTAI_DATABASE_URL", db_url)
    monkeypatch.setenv("SMARTAI_DATABASE_HEAVY", "OFF")

    command.upgrade(config, "head")
    inspector = inspect(create_engine(db_url))
    columns = {
        column["name"]
        for column in inspector.get_columns("ocr_provider_credentials")
    }
    assert {
        "id",
        "owner_id",
        "provider_type",
        "encrypted_api_key",
        "api_key_nonce",
        "api_key_version",
        "encrypted_secret_key",
        "secret_key_nonce",
        "secret_key_version",
        "verification_status",
        "last_checked_at",
        "verification_error_code",
        "created_at",
        "updated_at",
    } == columns
    checks = {
        check["name"]: check["sqltext"]
        for check in inspector.get_check_constraints("ocr_provider_credentials")
    }
    check_names = set(checks)
    assert "ck_ocr_provider_credentials_provider_type" in check_names
    assert "ck_ocr_provider_credentials_verification_status" in check_names
    verification_check = checks[
        "ck_ocr_provider_credentials_verification_status"
    ]
    assert "credentials_verified" in verification_check
    assert "'verified'" not in verification_check
    uniques = {
        tuple(item["column_names"])
        for item in inspector.get_unique_constraints("ocr_provider_credentials")
    }
    assert ("owner_id", "provider_type") in uniques

    command.downgrade(config, "0010_provider_wire_protocol")
    assert "ocr_provider_credentials" not in set(
        inspect(create_engine(db_url)).get_table_names()
    )
