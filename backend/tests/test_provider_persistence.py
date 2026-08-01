import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.db.models import ProviderConfigRecord, UserRecord
from backend.auth import create_token
from backend.db.provider_repository import (
    list_provider_configs,
    update_provider_config,
    upsert_provider_config,
)
from backend.db.session import session_scope
from backend.models import ProviderConfig
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
