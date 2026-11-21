import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.api import experts
from backend.auth import create_token
from backend.config import settings
from backend.db.models import ByokConnectionRecord, ProviderConfigRecord, UserRecord
from backend.db.provider_repository import get_provider_config, load_adaptive_states
from backend.db.session import session_scope
from backend.llm.concurrency import quota_key
from backend.llm.model_discovery import ModelCatalog, discover_models
from backend.llm.providers import endpoint_key
from backend.main import app
from backend.services.grading_input_security import provider_configuration_fingerprint


def _teacher(owner):
    with session_scope() as session:
        session.add(UserRecord(
            id=owner, username=owner, email=f"{owner}@test.local", role="teacher",
            password_hash="hash", is_active=True,
        ))
    return {"Authorization": f"Bearer {create_token(owner, 'teacher')}"}


def test_catalog_connects_once_and_materializes_owner_scoped_model_and_effort(monkeypatch):
    client = TestClient(app)
    owner = "byok-owner"
    headers = _teacher(owner)
    other = _teacher("other-byok-owner")
    models = [{"id": "gpt-5.4", "reasoning_efforts": ["low", "high"]},
              {"id": "gpt-4.1", "reasoning_efforts": []}]
    discover_calls = []

    def discovery(*args):
        discover_calls.append(args)
        return ModelCatalog(models, "ready")

    monkeypatch.setattr(experts, "discover_models", discovery)
    created = client.post("/experts/connections", headers=headers, json={
        "provider_type": "openai", "api_key": "synthetic-connection-key", "rpm": 6,
    })
    assert created.status_code == 201, created.json()
    assert "synthetic-connection-key" not in created.text
    assert created.json()["models"] == models
    connection_id = created.json()["id"]
    endpoint = f"/experts/connections/{connection_id}/model"

    assert client.get("/experts/connections", headers=other).json() == []
    assert client.post(endpoint, headers=other, json={"model": "gpt-5.4"}).status_code == 404
    assert client.post(endpoint, headers=headers, json={
        "model": "gpt-4.1", "reasoning_effort": "high",
    }).json()["detail"]["code"] == "byok_effort_unsupported"
    assert client.post(endpoint, headers=headers, json={
        "model": "not-listed",
    }).json()["detail"]["code"] == "byok_model_not_listed"

    def choose(model, effort=None):
        result = client.post(endpoint, headers=headers, json={"model": model, "reasoning_effort": effort})
        assert result.status_code == 200, result.json()
        return result.json()["provider_id"]

    explicit = choose("gpt-5.4", "high")
    default = choose("gpt-5.4")
    second = choose("gpt-4.1")
    assert len({explicit, default, second}) == 3
    assert choose("gpt-5.4", "high") == explicit
    assert len(discover_calls) == 1
    with session_scope() as session:
        stored_connection = session.get(ByokConnectionRecord, connection_id)
        assert "synthetic-connection-key" not in stored_connection.encrypted_api_key
        assert session.get(ProviderConfigRecord, explicit).encrypted_api_key is None

    loaded = [get_provider_config(owner, provider_id, master_key=settings.provider_encryption_key)
              for provider_id in (explicit, default, second)]
    assert [item.config.reasoning_effort for item in loaded] == ["high", None, None]
    assert {item.config.api_key for item in loaded} == {"synthetic-connection-key"}
    assert len({quota_key(item.config, endpoint_key(item.config)) for item in loaded}) == 1
    source = load_adaptive_states({"group": (owner, f"connection:{connection_id}")})["group"]
    assert source["enabled"] and source["revision"] == loaded[0].connection_updated_at
    fingerprint = provider_configuration_fingerprint(owner_id=owner, selected_provider_ids=[explicit])
    assert fingerprint != provider_configuration_fingerprint(owner_id=owner, selected_provider_ids=[default])

    rotated = client.put(f"/experts/connections/{connection_id}", headers=headers,
                         json={"api_key": "replacement-key", "rpm": 6})
    assert rotated.status_code == 200
    assert fingerprint != provider_configuration_fingerprint(owner_id=owner, selected_provider_ids=[explicit])
    assert "replacement-key" not in rotated.text


def test_missing_catalog_preserves_manual_model_ids_without_synthetic_efforts(monkeypatch):
    client = TestClient(app)
    headers = _teacher("byok-fallback")
    monkeypatch.setattr(experts, "discover_models", lambda *_: ModelCatalog([], "unavailable"))
    saved = client.post("/experts/connections", headers=headers,
                        json={"provider_type": "openai", "api_key": "synthetic-fallback-key"})
    assert saved.json()["catalog_status"] == "unavailable"
    assert saved.json()["manual_model_efforts"]["gpt-5.5"] == ["none", "low", "medium", "high", "xhigh"]
    path = f"/experts/connections/{saved.json()['id']}/model"
    assert client.post(path, headers=headers, json={"model": "gpt-5.5", "reasoning_effort": "xhigh"}).status_code == 200
    assert client.post(path, headers=headers, json={
        "model": "org/provider-alias-high", "reasoning_effort": "high",
    }).json()["detail"]["code"] == "byok_effort_unsupported"
    route = client.post(path, headers=headers, json={"model": "org/provider-alias-high"})
    provider_id = route.json()["provider_id"]
    available = client.get("/experts/stage-options", headers=headers).json()
    selected = next(entry for entry in available if entry["provider_id"] == provider_id)
    assert selected["model"] == "org/provider-alias-high"
    assert selected["reasoning_effort"] is None
    assert client.delete(f"/experts/connections/{saved.json()['id']}", headers=headers).status_code == 200
    assert get_provider_config("byok-fallback", provider_id, master_key=settings.provider_encryption_key) is None


@pytest.mark.parametrize(("provider_type", "base_url", "protocol", "payload", "path", "key_header", "expected"), [
    ("openai", None, "openai_chat_completions", {"data": [{"id": "gpt-5.4"}, {"id": "text-embedding-3-small"}]},
     "/v1/models", "authorization", {"gpt-5.4": ["none", "low", "medium", "high", "xhigh"]}),
    ("deepseek", None, "openai_chat_completions", {"data": [
        {"id": "deepseek-flash", "effort": {"supported_levels": ["high", "new", "high"]}},
        {"id": "deepseek-v4-pro", "effort": {"supported_levels": []}},
        {"id": "new-id", "effort": {"supported_levels": ["low", "max"]}},
        {"id": "bad", "effort": {"supported_levels": "high"}},
    ]}, "/v1/models", "authorization", {"deepseek-flash": ["high"], "deepseek-v4-pro": [],
                                         "new-id": ["low", "max"], "bad": []}),
    ("deepseek", "https://relay.example.com/v1", "openai_chat_completions", {"data": [
        {"id": "deepseek-flash", "effort": {"supported_levels": ["low", "max"]}},
    ]}, "/v1/models", "authorization", {"deepseek-flash": []}),
    ("anthropic", None, "anthropic_messages", {"data": [{"id": "claude-example",
       "capabilities": {"effort": {"high": {"supported": True}, "max": {"supported": False}}}}]},
     "/v1/models", "x-api-key", {"claude-example": ["high"]}),
    ("gemini", None, "gemini_generate_content", {"models": [{"name": "models/gemini-3.1-pro-preview",
       "supportedGenerationMethods": ["generateContent"]}, {"name": "models/embedding", "supportedGenerationMethods": ["embedContent"]}]},
     "/v1beta/models", "x-goog-api-key", {"gemini-3.1-pro-preview": ["low", "medium", "high"]}),
    ("openai", "https://openrouter.ai/api/v1", "openai_chat_completions", {"data": [{"id": "anthropic/claude",
       "reasoning": {"supported_efforts": ["high", "none", "bad"], "mandatory": True}}]},
     "/api/v1/models", "authorization", {"anthropic/claude": ["high"]}),
    ("openai", "https://relay.example.com/v1", "openai_chat_completions", {"data": [{"id": "gpt-5.4"}]},
     "/v1/models", "authorization", {"gpt-5.4": []}),
])
def test_catalog_reads_native_paths_and_only_confirmed_metadata(
    monkeypatch, provider_type, base_url, protocol, payload, path, key_header, expected,
):
    def capture(request):
        assert request.method == "GET"
        assert request.url.path == path
        assert "synthetic-key" in request.headers[key_header]
        assert "synthetic-key" not in str(request.url)
        return httpx.Response(200, json=payload)

    real_client = httpx.Client
    client = lambda **kwargs: real_client(transport=httpx.MockTransport(capture))
    monkeypatch.setattr("backend.llm.model_discovery.httpx.Client", client)
    monkeypatch.setattr("backend.llm.model_discovery.build_safe_provider_catalog_client", lambda *_args, **kwargs: client())
    result = discover_models(provider_type, base_url, protocol, "synthetic-key")
    assert result.status == "ready"
    assert {entry["id"]: entry["reasoning_efforts"] for entry in result.models} == expected


@pytest.mark.parametrize(("provider_type", "kwargs", "expected"), [
    ("openai", {"model": "gpt-5.4"}, {"reasoning_effort": "high"}),
    ("anthropic", {"model": "claude-sonnet-5-5"}, {"output_config": {"effort": "high"}}),
    ("openai", {"model": "anthropic/claude-example", "base_url": "https://openrouter.ai/api/v1"},
     {"reasoning": {"effort": "high"}}),
])
def test_explicit_wire_parameter_and_working_default(provider_type, kwargs, expected):
    from langchain_core.messages import HumanMessage
    from backend.llm.providers import SafeRelayProvider, build_provider
    from backend.models import ProviderConfig

    def payload(effort):
        base = ProviderConfig(provider_type=provider_type, api_key="synthetic-key", reasoning_effort=effort, **kwargs)
        provider = build_provider(base)
        messages = [HumanMessage(content="fixture")]
        if isinstance(provider, SafeRelayProvider):
            return provider._request_parts(messages)[1]
        return provider._build_client_sync()._get_request_payload(messages)

    configured = payload("high")
    default = payload(None)
    assert all(configured[name] == value for name, value in expected.items())
    assert all(name not in default for name in expected)
    assert "synthetic-key" not in json.dumps(configured)


def test_gemini_effort_is_serializable_with_max_output_tokens():
    from backend.llm.providers import build_provider
    from backend.models import ProviderConfig

    provider = build_provider(ProviderConfig(provider_type="gemini", api_key="synthetic-key",
                                             model="gemini-3.1-pro-preview", reasoning_effort="high"))
    options = provider._generation_kwargs(512)
    request_config = provider._build_client_sync()._prepare_params(stop=None, **options)
    assert request_config.thinking_config.thinking_level == "HIGH"
    assert request_config.max_output_tokens == 512


@pytest.mark.parametrize(("provider", "model", "choices"), [
    ("openai", "gpt-5.2", "none low medium high xhigh"),
    ("openai", "gpt-5.4-nano-2026-03-17", "none low medium high xhigh"),
    ("openai", "gpt-5.6", "none low medium high xhigh max"),
    ("openai", "gpt-5.6-sol-preview", ""),
    ("gemini", "gemini-4-hydrogen", "low medium high xhigh"),
    ("gemini", "gemini-4-argon", "low medium high"),
    ("gemini", "gemini-2.5-pro", "low medium high"),
    ("gemini", "gemini-2.5-flash-lite", "none low medium high"),
    ("deepseek", "deepseek-flash", "low high max"),
    ("zhipu", "glm-5.3-flash", "low high max"),
    ("moonshot", "kimi-k3.1-pro", "low high max"),
    ("moonshot", "kimi-k2.5", ""),
    ("qwen", "qwen3.8-flash", "none low medium xhigh"),
    ("qwen", "kimi-k3.1-pro", "low medium high max"),
    ("tencent", "hy3", "none low high"),
    ("doubao", "doubao-seed-2-1-lite-260915", "none low medium high"),
    ("doubao", "ep-private-alias", ""),
    ("minimax", "MiniMax-M3.1-Flash", "low medium high xhigh max"),
    ("minimax", "MiniMax-M3", ""),
    ("anthropic", "claude-arbitrary", ""),
])
def test_exact_documented_efforts_for_listed_and_manual_models(provider, model, choices):
    from backend.llm.model_discovery import _entries
    from backend.llm.official_reasoning import manual_model_efforts
    from backend.llm.provider_catalog import PROVIDER_CATALOG_BY_TYPE
    protocol = PROVIDER_CATALOG_BY_TYPE[provider].wire_protocol
    catalog = {"models": [{"name": f"models/{model}"}]} if provider == "gemini" else {"data": [{"id": model}]}
    expected = choices.split()
    assert _entries(catalog, provider, protocol, official=True, openrouter=False) == [
        {"id": model, "reasoning_efforts": expected},
    ]
    assert list(manual_model_efforts(provider, None, protocol).get(model, ())) == expected
    assert manual_model_efforts(provider, "https://relay.example.com/v1", protocol) == {}
