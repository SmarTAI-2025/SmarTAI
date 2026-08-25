from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.api.experts import verify_provider
from backend.auth import create_token
from backend.config import settings
from backend.db.models import UserRecord
from backend.db.provider_repository import get_provider_config, upsert_provider_config
from backend.db.session import session_scope
from backend.llm.endpoint_policy import ResolvedEndpoint
from backend.llm.providers import LLMResponse
from backend.main import app
from backend.models import ProviderConfig, User


def _teacher(owner_id: str) -> dict[str, str]:
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


def _payload(base_url: str = "https://api.llm.ustc.edu.cn/v1") -> dict:
    return {
        "provider_type": "deepseek",
        "api_key": "sk-private-relay-key",
        "model": "school-model",
        "base_url": base_url,
        "display_name": "School DeepSeek",
    }


def _public_resolution(base_url: str, **_kwargs) -> ResolvedEndpoint:
    return ResolvedEndpoint(base_url, base_url.split("/")[2], ("93.184.216.34",))


def test_development_catalog_exposes_editable_vendor_base_url(monkeypatch):
    headers = _teacher("relay-api-catalog-dev")
    monkeypatch.setattr(settings, "runtime_environment", "development")
    monkeypatch.setattr(settings, "custom_provider_endpoints_enabled", False)

    catalog = TestClient(app).get("/experts/catalog", headers=headers).json()
    deepseek = next(item for item in catalog if item["provider_type"] == "deepseek")

    assert deepseek["default_base_url"] == "https://api.deepseek.com/v1"
    assert deepseek["base_url_editable"] is True
    assert all(item["provider_type"] != "openai_compatible" for item in catalog)


def test_catalog_describes_all_supported_vendors_without_frontend_hardcoding(monkeypatch):
    headers = _teacher("relay-api-catalog-all")
    monkeypatch.setattr(settings, "runtime_environment", "development")
    catalog = TestClient(app).get("/experts/catalog", headers=headers).json()

    assert {item["provider_type"] for item in catalog} == {
        "openai", "zhipu", "deepseek", "moonshot", "qwen", "anthropic", "gemini",
    }
    expected_protocols = {
        "openai": "openai_chat_completions",
        "zhipu": "openai_chat_completions",
        "deepseek": "openai_chat_completions",
        "moonshot": "openai_chat_completions",
        "qwen": "openai_chat_completions",
        "anthropic": "anthropic_messages",
        "gemini": "gemini_generate_content",
    }
    assert {
        item["provider_type"]: item["wire_protocol"] for item in catalog
    } == expected_protocols
    assert all(item["default_base_url"].startswith("https://") for item in catalog)
    assert all(item["custom_base_url_supported"] is True for item in catalog)
    assert all(item["custom_base_url_enabled"] is True for item in catalog)


def test_production_flag_hides_editing_and_rejects_non_official_url(monkeypatch):
    headers = _teacher("relay-api-catalog-prod")
    monkeypatch.setattr(settings, "runtime_environment", "production")
    monkeypatch.setattr(settings, "custom_provider_endpoints_enabled", False)
    client = TestClient(app)

    catalog = client.get("/experts/catalog", headers=headers).json()
    deepseek = next(item for item in catalog if item["provider_type"] == "deepseek")
    response = client.post("/experts/keys", headers=headers, json=_payload())

    assert deepseek["base_url_editable"] is False
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "custom_provider_endpoints_disabled"


def test_ustc_url_is_saved_as_enabled_deepseek_without_verification(monkeypatch):
    headers = _teacher("relay-api-ustc")
    monkeypatch.setattr(settings, "runtime_environment", "development")
    monkeypatch.setattr("backend.api.experts.resolve_public_endpoint", _public_resolution)
    client = TestClient(app)

    response = client.post("/experts/keys", headers=headers, json=_payload())
    assert response.status_code == 200, response.text
    provider_id = response.json()["provider_id"]
    listed = client.get("/experts/available", headers=headers).json()

    assert len(listed) == 1
    assert listed[0]["provider_id"] == provider_id
    assert listed[0]["provider_type"] == "deepseek"
    assert listed[0]["base_url"] == "https://api.llm.ustc.edu.cn/v1"
    assert listed[0]["enabled"] is True
    assert listed[0]["verification_status"] == "unverified"
    assert "api_key" not in listed[0]
    assert "sk-private-relay-key" not in str(listed)


@pytest.mark.parametrize(
    ("provider_type", "wire_protocol"),
    [
        ("openai", "openai_chat_completions"),
        ("zhipu", "openai_chat_completions"),
        ("deepseek", "openai_chat_completions"),
        ("moonshot", "openai_chat_completions"),
        ("qwen", "openai_chat_completions"),
        ("anthropic", "anthropic_messages"),
        ("gemini", "gemini_generate_content"),
        ("gemini", "openai_chat_completions"),
    ],
)
def test_native_and_cross_protocol_relay_configs_save_and_are_immediately_usable(
    monkeypatch,
    provider_type,
    wire_protocol,
):
    owner_id = f"relay-api-{provider_type}-{wire_protocol}"
    headers = _teacher(owner_id)
    monkeypatch.setattr(settings, "runtime_environment", "development")
    monkeypatch.setattr("backend.api.experts.resolve_public_endpoint", _public_resolution)
    client = TestClient(app)

    response = client.post(
        "/experts/keys",
        headers=headers,
        json={
            "provider_type": provider_type,
            "api_key": "private-key",
            "model": "relay-model",
            "base_url": "https://relay.example.com/v1",
            "wire_protocol": wire_protocol,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["wire_protocol"] == wire_protocol
    listed = client.get("/experts/available", headers=headers).json()
    assert len(listed) == 1
    assert listed[0]["enabled"] is True
    assert listed[0]["wire_protocol"] == wire_protocol
    assert listed[0]["verification_status"] == "unverified"


def test_automatic_names_keep_same_vendor_model_at_two_urls_visible(monkeypatch):
    headers = _teacher("relay-api-auto-names")
    monkeypatch.setattr(settings, "runtime_environment", "development")
    monkeypatch.setattr("backend.api.experts.resolve_public_endpoint", _public_resolution)
    client = TestClient(app)

    for hostname in ("relay-a.example.com", "relay-b.example.com"):
        response = client.post(
            "/experts/keys",
            headers=headers,
            json={
                "provider_type": "deepseek",
                "api_key": "private-key",
                "model": "same-model",
                "base_url": f"https://{hostname}/v1",
            },
        )
        assert response.status_code == 200, response.text

    listed = client.get("/experts/available", headers=headers).json()
    assert len(listed) == 2
    assert len({item["provider_id"] for item in listed}) == 2
    assert len({item["resolved_display_name"] for item in listed}) == 2
    assert {item["endpoint_identity"] for item in listed} == {
        "https://relay-a.example.com/v1",
        "https://relay-b.example.com/v1",
    }
    assert all("same-model" in item["resolved_display_name"] for item in listed)


def test_relay_provider_is_owner_scoped_for_all_mutations(monkeypatch):
    owner_headers = _teacher("relay-api-owner")
    other_headers = _teacher("relay-api-other")
    monkeypatch.setattr(settings, "runtime_environment", "development")
    monkeypatch.setattr("backend.api.experts.resolve_public_endpoint", _public_resolution)
    client = TestClient(app)

    created = client.post("/experts/keys", headers=owner_headers, json=_payload())
    provider_id = created.json()["provider_id"]

    assert client.get("/experts/available", headers=other_headers).json() == []
    update = client.put(
        f"/experts/{provider_id}",
        headers=other_headers,
        json={"model": "school-model", "base_url": "https://relay.example.com/v1"},
    )
    assert update.status_code == 404
    assert client.post(
        f"/experts/{provider_id}/verify", headers=other_headers
    ).status_code == 404
    assert client.delete(
        f"/experts/{provider_id}", headers=other_headers
    ).json()["status"] == "not_found"


def test_updating_relay_keeps_it_enabled_and_resets_optional_status(monkeypatch):
    owner_id = "relay-api-update"
    headers = _teacher(owner_id)
    monkeypatch.setattr(settings, "runtime_environment", "development")
    monkeypatch.setattr("backend.api.experts.resolve_public_endpoint", _public_resolution)
    client = TestClient(app)
    provider_id = client.post(
        "/experts/keys", headers=headers, json=_payload()
    ).json()["provider_id"]

    updated = client.put(
        f"/experts/{provider_id}",
        headers=headers,
        json={"model": "school-model-v2", "base_url": "https://relay.example.com/v2"},
    )

    assert updated.status_code == 200, updated.text
    stored = get_provider_config(
        owner_id, provider_id, master_key=settings.provider_encryption_key
    )
    assert stored is not None
    assert stored.config.enabled is True
    assert stored.config.provider_type == "deepseek"
    assert stored.config.base_url == "https://relay.example.com/v2"
    assert stored.verification_status == "unverified"


def test_update_can_return_from_protocol_override_to_follow_provider(monkeypatch):
    owner_id = "relay-api-reset-protocol"
    headers = _teacher(owner_id)
    monkeypatch.setattr(settings, "runtime_environment", "development")
    monkeypatch.setattr("backend.api.experts.resolve_public_endpoint", _public_resolution)
    client = TestClient(app)
    created = client.post(
        "/experts/keys",
        headers=headers,
        json={
            "provider_type": "anthropic",
            "api_key": "private-key",
            "model": "relay-model",
            "base_url": "https://relay.example.com/v1",
            "wire_protocol": "openai_chat_completions",
        },
    )
    assert created.status_code == 200, created.text
    provider_id = created.json()["provider_id"]

    updated = client.put(
        f"/experts/{provider_id}",
        headers=headers,
        json={
            "model": "claude-sonnet-4-20250514",
            "base_url": "https://api.anthropic.com",
            "wire_protocol": None,
        },
    )

    assert updated.status_code == 200, updated.text
    assert updated.json()["wire_protocol"] == "anthropic_messages"
    stored = get_provider_config(
        owner_id, provider_id, master_key=settings.provider_encryption_key
    )
    assert stored is not None
    assert stored.config.wire_protocol == "anthropic_messages"
    assert stored.config.base_url == "https://api.anthropic.com"


class _FakeProvider:
    provider_type = "deepseek"

    async def ainvoke(self, _messages):
        return LLMResponse(
            content="OK",
            provider="deepseek:test",
            model="test",
            duration_ms=1,
        )


class _FakeRegistry:
    def __init__(self, *, shared: bool = False):
        self.shared = shared
        self.registered = []

    def get(self, _provider_id):
        return _FakeProvider()

    def uses_shared_pool(self):
        return self.shared

    def register(self, config, **kwargs):
        self.registered.append((config, kwargs))
        return kwargs.get("provider_id", "registered")


@pytest.mark.asyncio
async def test_optional_connectivity_check_is_not_an_enablement_gate(monkeypatch):
    owner_id = "relay-api-verify"
    _teacher(owner_id)
    current = User(id=owner_id, username=owner_id)
    record = upsert_provider_config(
        owner_id,
        ProviderConfig(
            provider_type="deepseek",
            api_key="private",
            model="test",
            base_url="https://relay.example.com/v1",
            endpoint_identity="https://relay.example.com/v1",
            enabled=True,
        ),
        master_key=settings.provider_encryption_key,
    )
    monkeypatch.setattr(settings, "runtime_environment", "development")
    monkeypatch.setattr(settings, "custom_provider_verification_cooldown_seconds", 0)

    before = get_provider_config(
        owner_id, record.id, master_key=settings.provider_encryption_key
    )
    assert before is not None and before.config.enabled is True
    result = await verify_provider(
        record.id,
        current=current,
        registry=_FakeRegistry(),
    )

    assert result["verification_status"] == "verified"
    after = get_provider_config(
        owner_id, record.id, master_key=settings.provider_encryption_key
    )
    assert after is not None and after.config.enabled is True


def test_first_relay_config_is_not_registered_into_transient_shared_pool(monkeypatch):
    from backend.api.experts import AddKeyRequest, add_key

    owner_id = "relay-api-first-byok"
    _teacher(owner_id)
    current = User(id=owner_id, username=owner_id)
    registry = _FakeRegistry(shared=True)
    monkeypatch.setattr(settings, "runtime_environment", "development")
    monkeypatch.setattr("backend.api.experts.resolve_public_endpoint", _public_resolution)

    result = add_key(AddKeyRequest(**_payload()), current=current, registry=registry)

    assert result["provider_id"]
    assert registry.registered == []
    stored = get_provider_config(
        owner_id,
        result["provider_id"],
        master_key=settings.provider_encryption_key,
    )
    assert stored is not None
    assert stored.config.provider_type == "deepseek"
