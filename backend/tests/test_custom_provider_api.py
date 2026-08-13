from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.api.experts import verify_provider, verify_provider_vision
from backend.auth import create_token
from backend.config import settings
from backend.db.models import UserRecord
from backend.db.provider_repository import (
    get_provider_config,
    set_provider_enabled,
    set_provider_verification,
    set_provider_vision_verification,
    upsert_provider_config,
)
from backend.db.session import session_scope
from backend.llm.endpoint_policy import CUSTOM_PROVIDER_RISK_ACK_VERSION, ResolvedEndpoint
from backend.llm.providers import LLMResponse
from backend.main import app
from backend.models import ProviderConfig, User


def _teacher(owner_id: str) -> dict[str, str]:
    with session_scope() as session:
        session.add(UserRecord(
            id=owner_id, username=owner_id, email=f"{owner_id}@test.local",
            role="teacher", password_hash="hash", is_active=True,
            created_at=1, updated_at=1,
        ))
    return {"Authorization": f"Bearer {create_token(owner_id, 'teacher')}"}


def _payload(base_url: str = "https://api.llm.ustc.edu.cn/v1") -> dict:
    return {
        "provider_type": "openai_compatible",
        "api_key": "sk-private-custom-key",
        "model": "school-model",
        "base_url": base_url,
        "display_name": "School relay",
        "risk_ack_version": CUSTOM_PROVIDER_RISK_ACK_VERSION,
    }


def _public_resolution(base_url: str, **_kwargs) -> ResolvedEndpoint:
    return ResolvedEndpoint(base_url, base_url.split("/")[2], ("93.184.216.34",))


def test_custom_api_and_catalog_fail_closed_when_flag_is_off(monkeypatch):
    headers = _teacher("custom-api-off")
    monkeypatch.setattr(settings, "custom_provider_endpoints_enabled", False)
    client = TestClient(app)

    catalog = client.get("/experts/catalog", headers=headers)
    response = client.post("/experts/keys", headers=headers, json=_payload())

    assert all(not item.get("custom") for item in catalog.json())
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "custom_provider_endpoints_disabled"


def test_ustc_is_plain_custom_and_cannot_enable_before_text_verification(monkeypatch):
    headers = _teacher("custom-api-ustc")
    monkeypatch.setattr(settings, "custom_provider_endpoints_enabled", True)
    monkeypatch.setattr("backend.api.experts.resolve_public_endpoint", _public_resolution)
    client = TestClient(app)

    response = client.post("/experts/keys", headers=headers, json=_payload())
    assert response.status_code == 200, response.text
    provider_id = response.json()["provider_id"]
    listed = client.get("/experts/available", headers=headers).json()

    assert len(listed) == 1
    assert listed[0]["provider_type"] == "openai_compatible"
    assert listed[0]["base_url"] == "https://api.llm.ustc.edu.cn/v1"
    assert listed[0]["enabled"] is False
    assert listed[0]["verification_status"] == "unverified"
    assert listed[0]["vision_verification_status"] == "unverified"
    assert listed[0]["risk_ack_version"] == CUSTOM_PROVIDER_RISK_ACK_VERSION
    assert "api_key" not in listed[0]
    assert "sk-private-custom-key" not in str(listed)

    enable = client.post(
        "/experts/select", headers=headers,
        json={"provider_id": provider_id, "enabled": True},
    )
    assert enable.status_code == 409
    assert enable.json()["detail"]["code"] == "provider_endpoint_not_verified"


def test_custom_provider_is_owner_scoped_for_all_mutations(monkeypatch):
    owner_headers = _teacher("custom-api-owner")
    other_headers = _teacher("custom-api-other")
    monkeypatch.setattr(settings, "custom_provider_endpoints_enabled", True)
    monkeypatch.setattr("backend.api.experts.resolve_public_endpoint", _public_resolution)
    client = TestClient(app)

    created = client.post("/experts/keys", headers=owner_headers, json=_payload())
    provider_id = created.json()["provider_id"]

    assert client.get("/experts/available", headers=other_headers).json() == []
    update = client.put(
        f"/experts/{provider_id}", headers=other_headers,
        json={
            "model": "school-model",
            "base_url": "https://api.llm.ustc.edu.cn/v1",
            "risk_ack_version": CUSTOM_PROVIDER_RISK_ACK_VERSION,
        },
    )
    assert update.status_code == 404
    assert client.post(
        f"/experts/{provider_id}/verify", headers=other_headers
    ).status_code == 404
    assert client.delete(
        f"/experts/{provider_id}", headers=other_headers
    ).json()["status"] == "not_found"


def test_update_invalidates_both_capabilities_and_disables_custom(monkeypatch):
    owner_id = "custom-api-update"
    headers = _teacher(owner_id)
    monkeypatch.setattr(settings, "custom_provider_endpoints_enabled", True)
    monkeypatch.setattr(settings, "custom_provider_verification_cooldown_seconds", 0)
    monkeypatch.setattr("backend.api.experts.resolve_public_endpoint", _public_resolution)
    client = TestClient(app)
    provider_id = client.post(
        "/experts/keys", headers=headers, json=_payload()
    ).json()["provider_id"]

    assert set_provider_verification(
        owner_id, provider_id, verification_status="verified", checked_at=2
    )
    assert set_provider_vision_verification(
        owner_id, provider_id, verification_status="verified", checked_at=3
    )
    assert set_provider_enabled(owner_id, provider_id, True)

    updated = client.put(
        f"/experts/{provider_id}", headers=headers,
        json={
            "model": "school-model-v2",
            "base_url": "https://relay.example.com/v2",
            "risk_ack_version": CUSTOM_PROVIDER_RISK_ACK_VERSION,
        },
    )
    assert updated.status_code == 200, updated.text
    stored = get_provider_config(
        owner_id, provider_id, master_key=settings.provider_encryption_key
    )
    assert stored is not None
    assert stored.config.enabled is False
    assert stored.verification_status == "unverified"
    assert stored.vision_verification_status == "unverified"


class _FakeCustomProvider:
    provider_type = "openai_compatible"
    supports_vision = False

    def __init__(self, *, text: str = "SMARTAI_TEXT_PROBE_OK", vision: str = "S003"):
        self.text = text
        self.vision = vision

    async def ainvoke(self, _messages):
        return LLMResponse(
            content=self.text,
            provider="openai_compatible:test",
            model="test",
            duration_ms=1,
        )

    async def ainvoke_vision(self, _prompt, _images):
        return LLMResponse(
            content=self.vision,
            provider="openai_compatible:test",
            model="test",
            duration_ms=1,
        )


class _FakeRegistry:
    def __init__(self, provider, *, shared=False):
        self.provider = provider
        self.shared = shared
        self.registered = []

    def get(self, _provider_id, *, include_unverified=False):
        assert include_unverified is True
        return self.provider

    def uses_shared_pool(self):
        return self.shared

    def register(self, config, **kwargs):
        self.registered.append((config, kwargs))
        return kwargs.get("provider_id", "registered")


def _stored_custom(owner_id: str) -> tuple[User, str]:
    _teacher(owner_id)
    record = upsert_provider_config(
        owner_id,
        ProviderConfig(
            provider_type="openai_compatible",
            api_key="private",
            model="test",
            base_url="https://relay.example.com/v1",
            endpoint_identity="https://relay.example.com/v1",
            enabled=False,
        ),
        master_key=settings.provider_encryption_key,
        risk_ack_version=CUSTOM_PROVIDER_RISK_ACK_VERSION,
    )
    return User(id=owner_id, username=owner_id), record.id


@pytest.mark.asyncio
async def test_text_then_vision_verification_persists_separate_capabilities(monkeypatch):
    current, provider_id = _stored_custom("custom-api-verify-success")
    provider = _FakeCustomProvider()
    registry = _FakeRegistry(provider)
    monkeypatch.setattr(settings, "custom_provider_endpoints_enabled", True)
    monkeypatch.setattr(settings, "custom_provider_verification_cooldown_seconds", 0)

    text_result = await verify_provider(provider_id, current=current, registry=registry)
    after_text = get_provider_config(
        current.id, provider_id, master_key=settings.provider_encryption_key
    )
    assert text_result["verification_status"] == "verified"
    assert after_text is not None
    assert after_text.verification_status == "verified"
    assert after_text.vision_verification_status == "unverified"

    vision_result = await verify_provider_vision(
        provider_id, current=current, registry=registry
    )
    after_vision = get_provider_config(
        current.id, provider_id, master_key=settings.provider_encryption_key
    )
    assert vision_result["vision_verification_status"] == "verified"
    assert after_vision is not None
    assert after_vision.verification_status == "verified"
    assert after_vision.vision_verification_status == "verified"


@pytest.mark.asyncio
async def test_protocol_mismatch_fails_text_verification_with_safe_code(monkeypatch):
    current, provider_id = _stored_custom("custom-api-verify-mismatch")
    monkeypatch.setattr(settings, "custom_provider_endpoints_enabled", True)
    monkeypatch.setattr(settings, "custom_provider_verification_cooldown_seconds", 0)

    with pytest.raises(HTTPException) as exc_info:
        await verify_provider(
            provider_id,
            current=current,
            registry=_FakeRegistry(_FakeCustomProvider(text="almost ok")),
        )

    assert getattr(exc_info.value, "status_code", None) == 422
    assert exc_info.value.detail["code"] == "provider_endpoint_protocol_mismatch"
    stored = get_provider_config(
        current.id, provider_id, master_key=settings.provider_encryption_key
    )
    assert stored is not None
    assert stored.verification_status == "failed"
    assert stored.verification_error_code == "provider_endpoint_protocol_mismatch"


@pytest.mark.asyncio
async def test_vision_verification_requires_verified_text(monkeypatch):
    current, provider_id = _stored_custom("custom-api-vision-before-text")
    monkeypatch.setattr(settings, "custom_provider_endpoints_enabled", True)

    with pytest.raises(HTTPException) as exc_info:
        await verify_provider_vision(
            provider_id,
            current=current,
            registry=_FakeRegistry(_FakeCustomProvider()),
        )

    assert getattr(exc_info.value, "status_code", None) == 409
    assert exc_info.value.detail["code"] == "provider_endpoint_not_verified"


def test_first_custom_config_is_not_registered_into_transient_shared_pool(monkeypatch):
    owner_id = "custom-api-first-byok"
    _teacher(owner_id)
    current = User(id=owner_id, username=owner_id)
    registry = _FakeRegistry(_FakeCustomProvider(), shared=True)
    monkeypatch.setattr(settings, "custom_provider_endpoints_enabled", True)
    monkeypatch.setattr(settings, "custom_provider_verification_cooldown_seconds", 0)
    monkeypatch.setattr("backend.api.experts.resolve_public_endpoint", _public_resolution)

    from backend.api.experts import AddKeyRequest, add_key

    result = add_key(AddKeyRequest(**_payload()), current=current, registry=registry)

    assert result["provider_id"]
    assert registry.registered == []
    stored = get_provider_config(
        owner_id,
        result["provider_id"],
        master_key=settings.provider_encryption_key,
    )
    assert stored is not None
    assert stored.config.provider_type == "openai_compatible"
