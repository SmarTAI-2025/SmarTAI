from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.api.experts import verify_provider
from backend.auth import create_token
from backend.config import settings
from backend.db.models import UserRecord
from backend.db.provider_repository import get_provider_config, list_provider_configs, upsert_provider_config
from backend.db.session import session_scope
from backend.llm.endpoint_policy import ResolvedEndpoint
from backend.llm.providers import LLMResponse
from backend.main import app
from backend.models import ProviderConfig, User


@pytest.mark.parametrize(('body', 'expected'), [
    ({'code': 400, 'message': 'User location is not supported for the API use.'}, 'expert_verification_region_unsupported'),
    ({'code': 400, 'details': [{'reason': 'API_KEY_INVALID'}]}, 'expert_verification_auth_failed'),
    ({'code': 400, 'message': 'private upstream body'}, 'expert_verification_provider_error'),
    ({'code': 404}, 'expert_verification_model_not_found'),
    ({'code': 429}, 'expert_verification_rate_limited'),
])
def test_google_structured_verification_errors_are_safe(body, expected):
    from backend.api.experts import _verification_error_code

    errors = pytest.importorskip('google.genai.errors')
    error = errors.ClientError(body['code'], {'error': body, 'sensitive': 'must-not-be-returned'})
    wrapper = RuntimeError('SDK wrapper')
    wrapper.__cause__ = error
    assert _verification_error_code(wrapper) == expected


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


@pytest.mark.parametrize("editing", [False, True])
@pytest.mark.parametrize("invalid_key", [
    "secret-中文", "secret，fullwidth", "secret\u200binvisible",
    "secret\r\nheader", "secret\x00null", "secret\ttab", "secret\x7fdel",
])
def test_byok_save_rejects_header_unsafe_keys_without_mutating_config(
    monkeypatch, caplog, editing, invalid_key,
):
    owner_id = "key-validation-owner"
    headers = _teacher(owner_id)
    monkeypatch.setattr("backend.api.experts.resolve_public_endpoint", _public_resolution)
    client = TestClient(app)
    body = _payload()
    if editing:
        created = client.post("/experts/keys", headers=headers, json=body)
        assert created.status_code == 200
        provider_id = created.json()["provider_id"]
        before = get_provider_config(owner_id, provider_id, master_key=settings.provider_encryption_key)
    body["api_key"] = invalid_key
    response = (client.put(f"/experts/{provider_id}", headers=headers, json=body)
                if editing else client.post("/experts/keys", headers=headers, json=body))

    assert response.status_code == 422
    assert response.json()["detail"] == {"code": "provider_api_key_invalid"}
    assert invalid_key not in response.text + caplog.text
    stored = list_provider_configs(owner_id, master_key=settings.provider_encryption_key)
    if editing:
        assert len(stored) == 1
        assert stored[0].config.api_key == "sk-private-relay-key"
        assert stored[0].updated_at == before.updated_at
    else:
        assert stored == []


def test_existing_byok_valid_key_replacement_and_blank_preservation(monkeypatch):
    owner_id = "key-replacement-owner"
    headers = _teacher(owner_id)
    monkeypatch.setattr("backend.api.experts.resolve_public_endpoint", _public_resolution)
    client = TestClient(app)
    body = _payload()
    created = client.post("/experts/keys", headers=headers, json=body)
    provider_id = created.json()["provider_id"]
    replacement = "synthetic_Aa09+/=._-"
    for key in (f"  {replacement}\n", None, ""):
        response = client.put(f"/experts/{provider_id}", headers=headers, json={**body, "api_key": key})
        assert response.status_code == 200
        assert get_provider_config(owner_id, provider_id, master_key=settings.provider_encryption_key).config.api_key == replacement


@pytest.mark.parametrize("operation", ["verify", "verify-image"])
def test_legacy_invalid_byok_key_reports_local_error_without_provider_call(monkeypatch, operation):
    from unittest.mock import AsyncMock
    from backend.llm.providers import SafeRelayProvider

    owner_id = "legacy-invalid-key-owner"
    headers = _teacher(owner_id)
    stored = upsert_provider_config(owner_id, ProviderConfig(**{
        **_payload(), "api_key": "secret-中文",
    }), master_key=settings.provider_encryption_key)
    invoke = AsyncMock(side_effect=AssertionError("invalid key must not invoke provider"))
    monkeypatch.setattr(SafeRelayProvider, "ainvoke", invoke)
    monkeypatch.setattr(SafeRelayProvider, "ainvoke_vision", invoke)
    response = TestClient(app).post(f"/experts/{stored.id}/{operation}", headers=headers)
    if operation == "verify":
        assert response.status_code == 502
        assert response.json()["detail"]["code"] == "provider_api_key_invalid"
        after = get_provider_config(owner_id, stored.id, master_key=settings.provider_encryption_key)
        assert after.verification_error_code == "provider_api_key_invalid"
    else:
        assert response.status_code == 200
        assert response.json()["image_reason"] == "provider_api_key_invalid"
    invoke.assert_not_called()


def test_verification_preserves_team_model_denial_without_raw_upstream_text(monkeypatch, caplog):
    import httpx
    from backend.llm.providers import SafeRelayProvider

    owner_id = "model-denied-owner"
    headers = _teacher(owner_id)
    stored = upsert_provider_config(owner_id, ProviderConfig(**_payload()), master_key=settings.provider_encryption_key)
    calls = []
    class Client:
        async def post(self, url, **kwargs):
            calls.append(url)
            return httpx.Response(403, json={"error": {
                "type": "team_model_access_denied", "param": "model",
                "message": "private-upstream-secret sk-private-relay-key",
            }})
    async def client(self):
        return Client()
    monkeypatch.setattr(SafeRelayProvider, "_relay_client", client)
    response = TestClient(app).post(f"/experts/{stored.id}/verify", headers=headers)

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "provider_model_access_denied"
    assert len(calls) == 1
    after = get_provider_config(owner_id, stored.id, master_key=settings.provider_encryption_key)
    assert after.verification_error_code == "provider_model_access_denied"
    assert "private-upstream-secret" not in response.text + caplog.text
    assert "sk-private-relay-key" not in response.text + caplog.text


def test_local_encoding_error_is_not_reported_as_provider_rejection():
    from backend.api.experts import _verification_error_code

    error = UnicodeEncodeError("ascii", "secret-中文", 7, 9, "ordinal not in range")
    wrapped = RuntimeError("wrapper")
    wrapped.__cause__ = error
    assert _verification_error_code(wrapped) == "expert_verification_encoding_error"


@pytest.mark.asyncio
async def test_tls_read_wait_timeout_is_not_a_certificate_failure():
    import asyncio
    import ssl
    from anyio.abc import ByteStream
    from anyio.streams.tls import TLSStream
    from backend.api.experts import _verification_error_code

    class WaitingStream(ByteStream):
        async def send(self, item):
            pass

        async def receive(self, max_bytes=65536):
            await asyncio.Event().wait()

        async def send_eof(self):
            pass

        async def aclose(self):
            pass

    # Exercise the real AnyIO SSL/cancellation chain without a socket or model call.
    with pytest.raises(TimeoutError) as raised:
        await asyncio.wait_for(TLSStream.wrap(WaitingStream(), hostname="relay.example"), 0.02)
    assert isinstance(raised.value.__cause__.__context__, ssl.SSLWantReadError)
    assert _verification_error_code(raised.value) == "expert_verification_timeout"


@pytest.mark.parametrize("operation", ["verify", "verify-image"])
def test_verification_persists_timeout_even_with_ssl_read_context(monkeypatch, operation):
    import asyncio
    import ssl
    from unittest.mock import AsyncMock
    from backend.llm.providers import SafeRelayProvider

    owner_id = "tls-timeout-owner"
    headers = _teacher(owner_id)
    stored = upsert_provider_config(owner_id, ProviderConfig(**_payload()), master_key=settings.provider_encryption_key)
    cancelled = asyncio.CancelledError()
    cancelled.__context__ = ssl.SSLWantReadError("read pending")
    timeout = TimeoutError()
    timeout.__cause__ = cancelled
    call = AsyncMock(side_effect=timeout)
    monkeypatch.setattr(SafeRelayProvider, "ainvoke", call)
    monkeypatch.setattr(SafeRelayProvider, "ainvoke_vision", call)
    wait_for = asyncio.wait_for
    deadlines = []
    async def observe_deadline(awaitable, timeout):
        deadlines.append(timeout)
        return await wait_for(awaitable, timeout)
    monkeypatch.setattr(asyncio, "wait_for", observe_deadline)
    response = TestClient(app).post(f"/experts/{stored.id}/{operation}", headers=headers)
    after = get_provider_config(owner_id, stored.id, master_key=settings.provider_encryption_key)
    if operation == "verify":
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "expert_verification_timeout"
        assert after.verification_error_code == "expert_verification_timeout"
    else:
        assert response.status_code == 200
        assert response.json()["image_capability_status"] == "inconclusive"
        assert after.config.image_reason == "expert_verification_timeout"
    call.assert_awaited_once()
    assert deadlines == [60]


def test_genuine_tls_failure_retains_its_safe_code():
    import ssl
    from backend.api.experts import _verification_error_code

    for cause in (ssl.SSLCertVerificationError("private certificate details"), ssl.SSLError("handshake failed")):
        wrapped = RuntimeError("transport wrapper")
        wrapped.__cause__ = cause
        assert _verification_error_code(wrapped) == "provider_endpoint_tls_failed"


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
        "tencent", "doubao", "minimax",
    }
    expected_protocols = {
        "openai": "openai_chat_completions",
        "zhipu": "openai_chat_completions",
        "deepseek": "openai_chat_completions",
        "moonshot": "openai_chat_completions",
        "qwen": "openai_chat_completions",
        "tencent": "openai_chat_completions",
        "doubao": "openai_chat_completions",
        "minimax": "openai_chat_completions",
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


@pytest.mark.parametrize("provider_type,model,base_url", [
    ("tencent", "hy-vision-2.0-instruct", "https://tokenhub.tencentmaas.com/v1"),
    ("doubao", "doubao-seed-2-1-pro-260915", "https://ark.cn-beijing.volces.com/api/v3"),
    ("minimax", "MiniMax-M3", "https://api.minimax.cn/v1"),
])
def test_guide_vendors_save_and_reload_without_a_probe(monkeypatch, provider_type, model, base_url):
    owner = f"guide-{provider_type}"
    headers = _teacher(owner)
    other_headers = _teacher(f"other-{provider_type}")
    monkeypatch.setattr(settings, "custom_provider_endpoints_enabled", False)
    client = TestClient(app)
    response = client.post("/experts/keys", headers=headers, json={
        "provider_type": provider_type, "model": model,
        "base_url": base_url, "api_key": "guide-synthetic-key",
    })
    assert response.status_code == 200, response.text
    # Each request rebuilds the owner registry from persisted configurations.
    listed = client.get("/experts/available", headers=headers).json()
    assert len(listed) == 1
    assert listed[0]["provider_type"] == provider_type
    assert listed[0]["model"] == model
    assert listed[0]["base_url"] == base_url
    assert listed[0]["verification_status"] == "unverified"
    assert "guide-synthetic-key" not in str(listed)
    assert all(item["provider_id"] != listed[0]["provider_id"] for item in client.get("/experts/available", headers=other_headers).json())


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
