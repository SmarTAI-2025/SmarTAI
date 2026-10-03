"""Automatic concurrency is an API policy, including for legacy BYOK rows."""

import pytest
from fastapi.testclient import TestClient

from backend.auth import create_token
from backend.config import settings
from backend.db.models import UserRecord
from backend.db.provider_repository import get_provider_config, upsert_provider_config
from backend.db.session import session_scope
from backend.main import app
from backend.models import ProviderConfig


def _owner(owner_id):
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
    return {"Authorization": f"Bearer {create_token(owner_id, 'teacher')}"}


@pytest.mark.parametrize(
    ("rpm", "legacy_concurrency", "expected"),
    [(0, "omitted", 50), (10, None, 20), (30, 5, 50), (1, 50, 2)],
)
def test_create_derives_and_persists_concurrency(rpm, legacy_concurrency, expected):
    owner_id = f"auto-create-{rpm}"
    headers = _owner(owner_id)
    payload = {
        "provider_type": "openai",
        "api_key": "sk-automatic-private",
        "model": "automatic-model",
        "rpm": rpm,
    }
    if legacy_concurrency != "omitted":
        payload["max_concurrent"] = legacy_concurrency

    client = TestClient(app)
    response = client.post("/experts/keys", headers=headers, json=payload)
    assert response.status_code == 200, response.text
    provider_id = response.json()["provider_id"]
    stored = get_provider_config(
        owner_id, provider_id, master_key=settings.provider_encryption_key,
    )
    assert stored.config.max_concurrent == expected
    assert stored.config.rpm == rpm

    listed = client.get("/experts/available", headers=headers)
    assert listed.status_code == 200
    assert listed.json()[0]["max_concurrent"] == expected
    assert listed.json()[0]["concurrency_mode"] == "automatic"
    assert "api_key" not in listed.json()[0]
    assert "scheduling_owner" not in listed.json()[0]
    assert payload["api_key"] not in listed.text


@pytest.mark.parametrize(
    ("rpm", "legacy_concurrency", "expected"),
    [(10, "omitted", 20), (30, None, 50), (0, 5, 50)],
)
def test_update_replaces_legacy_manual_concurrency_without_requiring_key(
    rpm, legacy_concurrency, expected,
):
    owner_id = f"auto-update-{rpm}"
    headers = _owner(owner_id)
    saved = upsert_provider_config(
        owner_id,
        ProviderConfig(
            provider_type="openai",
            api_key="sk-retained-private",
            model="original",
            max_concurrent=5,
            rpm=2,
        ),
        master_key=settings.provider_encryption_key,
    )
    payload = {"model": "updated", "rpm": rpm}
    if legacy_concurrency != "omitted":
        payload["max_concurrent"] = legacy_concurrency

    response = TestClient(app).put(
        f"/experts/{saved.id}", headers=headers, json=payload,
    )
    assert response.status_code == 200, response.text
    updated = get_provider_config(
        owner_id, saved.id, master_key=settings.provider_encryption_key,
    )
    assert updated.config.max_concurrent == expected
    assert updated.config.rpm == rpm
    assert updated.config.api_key == "sk-retained-private"
    assert updated.config.api_key not in response.text


def test_legacy_row_exposes_automatic_estimate_without_mutating_frozen_record():
    owner_id = "auto-existing-legacy"
    headers = _owner(owner_id)
    saved = upsert_provider_config(
        owner_id,
        ProviderConfig(
            provider_type="openai",
            api_key="sk-legacy-private",
            model="legacy",
            max_concurrent=5,
            rpm=10,
        ),
        master_key=settings.provider_encryption_key,
    )

    listed = TestClient(app).get("/experts/available", headers=headers)
    assert listed.status_code == 200
    assert listed.json()[0]["max_concurrent"] == 20
    assert listed.json()[0]["concurrency_mode"] == "automatic"
    assert "sk-legacy-private" not in listed.text
    assert "scheduling_owner" not in listed.json()[0]
    stored = get_provider_config(
        owner_id, saved.id, master_key=settings.provider_encryption_key,
    )
    assert stored.config.max_concurrent == 5


@pytest.mark.parametrize("invalid_concurrency", [0, 51])
def test_invalid_legacy_concurrency_is_rejected_without_saving(invalid_concurrency):
    owner_id = f"auto-invalid-{invalid_concurrency}"
    headers = _owner(owner_id)
    client = TestClient(app)
    response = client.post("/experts/keys", headers=headers, json={
        "provider_type": "openai",
        "api_key": "sk-invalid-private",
        "model": "invalid-model",
        "max_concurrent": invalid_concurrency,
    })
    assert response.status_code == 422
    assert "sk-invalid-private" not in response.text
    assert client.get("/experts/available", headers=headers).json() == []


def test_runtime_scheduling_owner_is_private_and_not_a_frozen_config_field():
    from backend.llm.registry import ExpertRegistry

    registry = ExpertRegistry(seed_from_settings=False, shared_owner_id="runtime-owner")
    config = ProviderConfig(
        provider_type="openai", api_key="sk-owner-private", model="owner-model", rpm=10,
        wire_protocol="openai_chat_completions",
    )
    frozen_config = config.model_dump(mode="json")
    provider_id = registry.register(config)
    runtime_config = registry.get(provider_id).config

    assert runtime_config.scheduling_owner == "runtime-owner"
    assert config.scheduling_owner is None
    assert runtime_config.model_dump(mode="json") == frozen_config
    assert "scheduling_owner" not in runtime_config.model_dump()
    assert "runtime-owner" not in repr(runtime_config)
    assert "scheduling_owner" not in registry.list_configs()[0]
