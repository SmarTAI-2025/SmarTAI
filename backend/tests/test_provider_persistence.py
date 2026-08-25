import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.db.models import ProviderConfigRecord, UserRecord
from backend.auth import create_token
from backend.db.provider_repository import (
    DefaultProviderNotEnabled,
    DefaultProviderReplacementRequired,
    delete_provider_config,
    get_default_provider_id,
    list_provider_configs,
    set_default_provider_id,
    set_provider_enabled,
    update_provider_config,
    upsert_provider_config,
)
from backend.db.session import session_scope
from backend.models import ProviderConfig
from backend.config import settings
from backend.main import app


def test_provider_api_key_is_encrypted_and_round_trips():
    from backend.security.secrets import decrypt_secret, encrypt_secret

    master_key = "test-master-key-that-is-long-enough"
    encrypted = encrypt_secret("sk-user-secret", master_key=master_key, associated_data="user-1/provider-1")

    assert "sk-user-secret" not in encrypted.ciphertext
    assert decrypt_secret(encrypted, master_key=master_key, associated_data="user-1/provider-1") == "sk-user-secret"


def test_provider_api_key_rejects_wrong_associated_data():
    from backend.security.secrets import decrypt_secret, encrypt_secret

    encrypted = encrypt_secret("secret", master_key="test-master-key-that-is-long-enough", associated_data="owner-a")
    with pytest.raises(ValueError):
        decrypt_secret(encrypted, master_key="test-master-key-that-is-long-enough", associated_data="owner-b")


def test_provider_config_persists_encrypted_key_and_is_owner_scoped():
    owner = "provider-owner-a"
    with session_scope() as session:
        session.add(UserRecord(id=owner, username=owner, email=f"{owner}@test.local", role="teacher",
                               password_hash="hash", is_active=True, created_at=1, updated_at=1))

    config = ProviderConfig(provider_type="openai", api_key="sk-persisted", model="test-model",
                            base_url="https://example.test/v1")
    saved = upsert_provider_config(owner, config, master_key="test-master-key-that-is-long-enough")

    with session_scope() as session:
        row = session.get(ProviderConfigRecord, saved.id)
        assert row is not None
        assert "sk-persisted" not in row.encrypted_api_key

    loaded = list_provider_configs(owner, master_key="test-master-key-that-is-long-enough")
    assert len(loaded) == 1
    assert loaded[0].config.api_key == "sk-persisted"
    assert list_provider_configs("provider-owner-b", master_key="test-master-key-that-is-long-enough") == []


def test_expert_endpoints_require_identity_and_only_list_current_users_keys():
    owner = "demo_expertowner"
    with session_scope() as session:
        session.add(UserRecord(id=owner, username=owner, email=f"{owner}@test.local", role="teacher",
                               password_hash="hash", is_active=True, created_at=1, updated_at=1))

    client = TestClient(app)
    headers = {"Authorization": f"Bearer {create_token(owner, 'teacher')}"}
    response = client.post("/experts/keys", headers=headers, json={
        "provider_type": "openai",
        "api_key": "sk-api-only",
        "model": "api-model",
        "base_url": "https://api.openai.com/v1",
    })
    assert response.status_code == 200

    listed = client.get("/experts/available", headers=headers)
    assert listed.status_code == 200
    assert len(listed.json()) == 1
    assert "api_key" not in listed.json()[0]
    assert "sk-api-only" not in listed.text

    assert client.get("/experts/available").status_code == 401


def test_owner_default_provider_is_stable_explicit_and_owner_scoped():
    owner = "provider-default-owner"
    other_owner = "provider-default-other"
    master_key = "provider-default-master-key-0123456789abcdef"
    with session_scope() as session:
        for owner_id in (owner, other_owner):
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
    first = upsert_provider_config(
        owner,
        ProviderConfig(provider_type="openai", api_key="sk-first", model="first"),
        master_key=master_key,
    )
    second = upsert_provider_config(
        owner,
        ProviderConfig(provider_type="gemini", api_key="sk-second", model="second"),
        master_key=master_key,
    )

    assert get_default_provider_id(owner) == first.id
    assert set_default_provider_id(owner, second.id) == second.id
    assert get_default_provider_id(owner) == second.id
    with pytest.raises(DefaultProviderNotEnabled):
        set_default_provider_id(other_owner, second.id)
    with pytest.raises(DefaultProviderReplacementRequired):
        set_provider_enabled(owner, second.id, False)
    with pytest.raises(DefaultProviderReplacementRequired):
        delete_provider_config(owner, second.id)

    assert set_default_provider_id(owner, first.id) == first.id
    assert delete_provider_config(owner, second.id) is True
    assert get_default_provider_id(owner) == first.id
    assert set_provider_enabled(owner, first.id, False) is True
    assert get_default_provider_id(owner) is None


def test_expert_api_marks_and_changes_owner_default(monkeypatch):
    owner = "provider-default-api-owner"
    master_key = "provider-default-api-key-0123456789abcdef"
    with session_scope() as session:
        session.add(UserRecord(
            id=owner,
            username=owner,
            email=f"{owner}@test.local",
            role="teacher",
            password_hash="hash",
            is_active=True,
            created_at=1,
            updated_at=1,
        ))
    monkeypatch.setattr(settings, "provider_encryption_key", master_key)
    headers = {"Authorization": f"Bearer {create_token(owner, 'teacher')}"}
    client = TestClient(app)
    first = client.post("/experts/keys", headers=headers, json={
        "provider_type": "openai", "api_key": "sk-first", "model": "first",
    }).json()["provider_id"]
    second = client.post("/experts/keys", headers=headers, json={
        "provider_type": "gemini", "api_key": "sk-second", "model": "second",
    }).json()["provider_id"]

    listed = client.get("/experts/available", headers=headers).json()
    assert {item["provider_id"]: item["is_default"] for item in listed} == {
        first: True,
        second: False,
    }
    changed = client.put(
        "/experts/default", headers=headers, json={"provider_id": second},
    )
    assert changed.status_code == 200
    assert changed.json() == {
        "status": "success", "provider_id": second, "is_default": True,
    }
    blocked = client.post(
        "/experts/select",
        headers=headers,
        json={"provider_id": second, "enabled": False},
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "default_provider_replacement_required"


@pytest.mark.parametrize(
    ("runtime_environment", "shows_internal_name"),
    [("development", True), ("production", False)],
)
def test_missing_provider_master_key_rejects_create_without_writing(
    monkeypatch,
    runtime_environment,
    shows_internal_name,
):
    owner = f"missing-master-key-{runtime_environment}"
    api_key = f"sk-must-not-persist-{runtime_environment}"
    with session_scope() as session:
        session.add(UserRecord(
            id=owner,
            username=owner,
            email=f"{owner}@test.local",
            role="teacher",
            password_hash="hash",
            is_active=True,
            created_at=1,
            updated_at=1,
        ))

    monkeypatch.setattr(settings, "runtime_environment", runtime_environment)
    monkeypatch.setattr(settings, "provider_encryption_key", "")
    client = TestClient(app)
    response = client.post(
        "/experts/keys",
        headers={"Authorization": f"Bearer {create_token(owner, 'teacher')}"},
        json={
            "provider_type": "openai",
            "api_key": api_key,
            "model": "api-model",
            "base_url": "https://api.openai.com/v1",
        },
    )

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["code"] == "provider_encryption_not_configured"
    assert (
        "SMARTAI_PROVIDER_ENCRYPTION_KEY" in detail["message"]
    ) is shows_internal_name
    if runtime_environment == "production":
        lowered_message = detail["message"].lower()
        assert "smartai_" not in lowered_message
        assert "32" not in lowered_message
        assert "secret" not in lowered_message
        assert "administrator" in lowered_message
        assert "not saved" in lowered_message
    assert api_key not in response.text
    with session_scope() as session:
        assert list(session.scalars(select(ProviderConfigRecord).where(
            ProviderConfigRecord.owner_id == owner,
        ))) == []


def test_missing_provider_master_key_rejects_update_without_changing_record(monkeypatch):
    owner = "missing-master-key-update"
    master_key = "update-provider-master-key-0123456789abcdef"
    original = ProviderConfig(
        provider_type="openai",
        api_key="sk-original",
        model="original-model",
        base_url="https://api.openai.com/v1",
    )
    with session_scope() as session:
        session.add(UserRecord(
            id=owner,
            username=owner,
            email=f"{owner}@test.local",
            role="teacher",
            password_hash="hash",
            is_active=True,
            created_at=1,
            updated_at=1,
        ))
    stored = upsert_provider_config(owner, original, master_key=master_key)

    monkeypatch.setattr(settings, "runtime_environment", "development")
    monkeypatch.setattr(settings, "provider_encryption_key", "")
    client = TestClient(app)
    replacement_key = "sk-replacement-must-not-persist"
    response = client.put(
        f"/experts/{stored.id}",
        headers={"Authorization": f"Bearer {create_token(owner, 'teacher')}"},
        json={
            "api_key": replacement_key,
            "model": "replacement-model",
            "base_url": "https://api.openai.com/v1",
        },
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "provider_encryption_not_configured"
    assert replacement_key not in response.text
    loaded = list_provider_configs(owner, master_key=master_key)
    assert len(loaded) == 1
    assert loaded[0].config.api_key == "sk-original"
    assert loaded[0].config.model == "original-model"


def test_missing_provider_master_key_never_hides_existing_byok_or_uses_shared_pool(
    monkeypatch,
):
    owner = "missing-master-key-read"
    master_key = "read-provider-master-key-0123456789abcdef"
    with session_scope() as session:
        session.add(UserRecord(
            id=owner,
            username=owner,
            email=f"{owner}@test.local",
            role="teacher",
            password_hash="hash",
            is_active=True,
            created_at=1,
            updated_at=1,
        ))
    upsert_provider_config(
        owner,
        ProviderConfig(
            provider_type="openai",
            api_key="sk-existing",
            model="existing-model",
            base_url="https://api.openai.com/v1",
        ),
        master_key=master_key,
    )

    monkeypatch.setattr(settings, "runtime_environment", "development")
    monkeypatch.setattr(settings, "provider_encryption_key", "")
    monkeypatch.setattr(settings, "shared_pool_enabled", True)
    monkeypatch.setattr(settings, "openai_api_key", "sk-shared-must-not-be-used")
    client = TestClient(app)
    response = client.get(
        "/experts/available",
        headers={"Authorization": f"Bearer {create_token(owner, 'teacher')}"},
    )

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["code"] == "provider_encryption_not_configured"
    assert "SMARTAI_PROVIDER_ENCRYPTION_KEY" in detail["message"]
    assert "sk-existing" not in response.text
    assert "sk-shared-must-not-be-used" not in response.text


def test_missing_provider_master_key_keeps_shared_pool_for_owner_without_byok(
    monkeypatch,
):
    owner = "missing-master-key-no-record"
    with session_scope() as session:
        session.add(UserRecord(
            id=owner,
            username=owner,
            email=f"{owner}@test.local",
            role="teacher",
            password_hash="hash",
            is_active=True,
            created_at=1,
            updated_at=1,
        ))

    monkeypatch.setattr(settings, "runtime_environment", "development")
    monkeypatch.setattr(settings, "provider_encryption_key", "")
    monkeypatch.setattr(settings, "shared_pool_enabled", True)
    monkeypatch.setattr(settings, "openai_api_key", "sk-shared-test-only")
    monkeypatch.setattr(settings, "openai_model", "shared-test-model")
    monkeypatch.setattr(settings, "openai_api_base", "https://api.openai.com/v1")
    client = TestClient(app)
    response = client.get(
        "/experts/available",
        headers={"Authorization": f"Bearer {create_token(owner, 'teacher')}"},
    )

    assert response.status_code == 200
    providers = response.json()
    assert len(providers) == 1
    assert providers[0]["is_shared"] is True
    assert providers[0]["model"] == "shared-test-model"
    assert "sk-shared-test-only" not in response.text


def test_grading_provider_fingerprint_changes_without_exposing_secret():
    from backend.services.grading_input_security import (
        provider_configuration_fingerprint,
    )

    owner = "provider-fingerprint-owner"
    with session_scope() as session:
        session.add(UserRecord(
            id=owner, username=owner, email=f"{owner}@test.local",
            role="teacher", password_hash="hash", is_active=True,
            created_at=1, updated_at=1,
        ))
    original_config = ProviderConfig(
        provider_type="openai", api_key="secret-before", model="gpt-test",
        base_url="https://api.openai.com/v1",
    )
    stored = upsert_provider_config(
        owner, original_config,
        master_key="test-master-key-that-is-long-enough",
    )
    before = provider_configuration_fingerprint(
        owner_id=owner, selected_provider_ids=[stored.id],
    )
    update_provider_config(
        owner,
        stored.id,
        original_config.model_copy(update={"api_key": "secret-after"}),
        master_key="test-master-key-that-is-long-enough",
    )
    after = provider_configuration_fingerprint(
        owner_id=owner, selected_provider_ids=[stored.id],
    )

    assert before != after
    assert "secret-before" not in before
    assert "secret-after" not in after
