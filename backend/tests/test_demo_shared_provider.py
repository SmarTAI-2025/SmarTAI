"""Deployment-only shared provider selection; no model or network calls."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from backend.config import Settings, settings
from backend.domain.errors import ValidationError
from backend.llm import registry as registry_module
from backend.llm.registry import ExpertRegistry
from backend.llm.providers import build_provider
from backend.models import ProviderConfig
from backend.rag import embedder as embedder_module
from backend.services.grading_input_security import (
    _shared_provider_row,
    provider_configuration_fingerprint,
)


VENDORS = ("gemini", "openai", "zhipu", "anthropic", "deepseek", "moonshot", "qwen")
COMPATIBLE_VENDORS = ("openai", "zhipu", "deepseek", "moonshot", "qwen")
DUMMY_KEY = "test-only-generic-shared-key"
MODEL = "demo-shared-model"
RELAY_URL = "https://relay.example.com/v1"


@pytest.fixture
def configured_pool(monkeypatch):
    monkeypatch.setattr(settings, "shared_pool_enabled", True)
    monkeypatch.setattr(settings, "shared_provider", "openai")
    monkeypatch.setattr(settings, "shared_api_key", DUMMY_KEY)
    monkeypatch.setattr(settings, "shared_model", MODEL)
    monkeypatch.setattr(settings, "shared_base_url", RELAY_URL)
    monkeypatch.setattr(settings, "shared_wire_protocol", "openai_responses")
    monkeypatch.setattr(settings, "shared_reasoning_effort", "high")
    for vendor in VENDORS:
        monkeypatch.setattr(settings, f"{vendor}_api_key", "")
    built: list[ProviderConfig] = []

    def fake_provider(config):
        built.append(config)
        return SimpleNamespace(
            provider_id=f"{config.provider_type}:{config.model}",
            provider_type=config.provider_type,
            model=config.model,
            config=config,
            supports_vision=config.provider_type in {"openai", "gemini", "anthropic"},
        )

    monkeypatch.setattr(registry_module, "build_provider", fake_provider)
    return built


def test_generic_shared_environment_variables_resolve_one_provider(monkeypatch):
    for suffix, value in {
        "PROVIDER": "openai",
        "API_KEY": DUMMY_KEY,
        "MODEL": MODEL,
        "BASE_URL": RELAY_URL,
        "WIRE_PROTOCOL": "openai_responses",
        "REASONING_EFFORT": "high",
    }.items():
        monkeypatch.setenv(f"SMARTAI_SHARED_{suffix}", value)
    configured = Settings(_env_file=None).configured_shared_provider()

    assert configured is not None
    assert configured.provider_type == "openai"
    assert configured.api_key == DUMMY_KEY
    assert configured.model == MODEL
    assert configured.base_url == RELAY_URL
    assert configured.wire_protocol == "openai_responses"
    assert configured.reasoning_effort == "high"


@pytest.mark.parametrize("vendor", ["openai", "gemini"])
def test_shared_calls_obey_deployed_concurrency_limit(monkeypatch, vendor):
    monkeypatch.setenv("SMARTAI_MAX_CONCURRENT_LLM_PER_PROVIDER", "2")
    configured = Settings(
        _env_file=None, shared_provider=vendor, shared_api_key=DUMMY_KEY,
        shared_model=MODEL, shared_wire_protocol="auto", shared_base_url="",
    ).configured_shared_provider()
    provider = build_provider(configured)
    monkeypatch.setattr(settings, "http_proxy", "")
    monkeypatch.setattr(settings, "https_proxy", "")

    async def exercise():
        active = peak = started = 0
        limit_reached = asyncio.Event()
        release = asyncio.Event()

        class Client:
            async def ainvoke(self, _messages):
                nonlocal active, peak, started
                active += 1
                started += 1
                peak = max(peak, active)
                if active == 2:
                    limit_reached.set()
                try:
                    await release.wait()
                    return SimpleNamespace(content="ok", response_metadata={}, usage_metadata={})
                finally:
                    active -= 1

        provider._client = Client()
        calls = [asyncio.create_task(provider.ainvoke([])) for _ in range(5)]
        try:
            await asyncio.wait_for(limit_reached.wait(), timeout=2)
            await asyncio.sleep(0)
            assert started == 2
            assert peak == 2
        finally:
            release.set()
            await asyncio.gather(*calls)
        assert started == 5
        assert peak == 2

    asyncio.run(exercise())


@pytest.mark.parametrize("vendor", COMPATIBLE_VENDORS)
def test_compatible_shared_vendor_auto_uses_chat_protocol(vendor):
    configured = Settings(
        _env_file=None, shared_provider=vendor, shared_api_key=DUMMY_KEY,
        shared_model=MODEL, shared_wire_protocol="auto", shared_reasoning_effort="",
    ).configured_shared_provider()

    assert configured is not None
    assert configured.provider_type == vendor
    assert configured.wire_protocol == "openai_chat_completions"
    assert configured.reasoning_effort is None


@pytest.mark.parametrize("vendor", ["gemini", "anthropic"])
def test_native_shared_vendor_auto_preserves_native_protocol(vendor):
    configured = Settings(
        _env_file=None, shared_provider=vendor, shared_api_key=DUMMY_KEY,
        shared_model=MODEL, shared_wire_protocol="auto", shared_reasoning_effort="",
    ).configured_shared_provider()

    assert configured is not None
    assert configured.provider_type == vendor
    assert configured.wire_protocol is None
    assert configured.reasoning_effort is None


def test_explicit_shared_selection_replaces_all_legacy_keys(configured_pool, monkeypatch):
    for vendor in VENDORS:
        monkeypatch.setattr(settings, f"{vendor}_api_key", f"test-only-legacy-{vendor}")
    registry = ExpertRegistry(shared_owner_id="demo-test-owner")

    assert len(configured_pool) == 1
    assert configured_pool[0].provider_type == "openai"
    assert configured_pool[0].api_key == DUMMY_KEY
    assert configured_pool[0].wire_protocol == "openai_responses"
    assert registry.pick_default().provider_id == f"openai:{MODEL}"
    assert registry.uses_shared_pool() is True
    listed = registry.list_configs()
    assert len(listed) == 1
    assert listed[0]["model"] == MODEL
    assert listed[0]["scope"] == "shared"
    assert listed[0]["editable"] is False
    assert DUMMY_KEY not in json.dumps(listed)
    assert "api_key" not in listed[0]
    assert registry.list_enabled_configs() == []

    def forbidden_embedding_client(*_args, **_kwargs):
        raise AssertionError("the selected shared relay must not receive embeddings")

    monkeypatch.setattr(embedder_module, "OpenAICompatibleEmbedder", forbidden_embedding_client)
    assert isinstance(embedder_module.pick_embedder(registry), embedder_module.BM25Embedder)
    selected = registry.select([f"openai:{MODEL}"], primary_provider_id=f"openai:{MODEL}")
    assert selected.list_enabled_configs() == []
    assert isinstance(embedder_module.pick_embedder(selected), embedder_module.BM25Embedder)


@pytest.mark.parametrize("field", ["shared_api_key", "shared_model"])
def test_incomplete_explicit_selection_never_falls_back_to_legacy(
    configured_pool, monkeypatch, field,
):
    monkeypatch.setattr(settings, field, "")
    monkeypatch.setattr(settings, "gemini_api_key", "test-only-legacy-gemini")

    assert settings.configured_shared_provider() is None
    registry = ExpertRegistry(shared_owner_id="demo-test-owner")
    assert configured_pool == []
    assert registry.list_available() == []
    assert registry.uses_shared_pool() is False
    with pytest.raises(ValidationError) as error:
        provider_configuration_fingerprint(
            owner_id="demo-test-owner", selected_provider_ids=[f"gemini:{settings.gemini_model}"],
        )
    assert error.value.code == "provider_not_enabled"


def test_empty_shared_provider_preserves_legacy_environment_selection(
    configured_pool, monkeypatch,
):
    monkeypatch.setattr(settings, "shared_provider", "")
    monkeypatch.setattr(settings, "gemini_api_key", "test-only-legacy-gemini")
    monkeypatch.setattr(settings, "zhipu_api_key", "test-only-legacy-zhipu")

    assert settings.configured_shared_provider() is None
    registry = ExpertRegistry(shared_owner_id="demo-test-owner")
    assert {config.provider_type for config in configured_pool} == {"gemini", "zhipu"}
    assert all(config.wire_protocol is None for config in configured_pool)
    assert all(config.reasoning_effort is None for config in configured_pool)
    assert len(registry.list_available()) == 2


def test_shared_kill_switch_still_prevents_generic_environment_seeding(
    configured_pool, monkeypatch,
):
    monkeypatch.setattr(settings, "shared_pool_enabled", False)
    registry = ExpertRegistry(shared_owner_id="demo-test-owner")

    assert configured_pool == []
    assert registry.list_available() == []
    assert registry.uses_shared_pool() is False


def test_shared_protocol_and_reasoning_do_not_change_byok_defaults(configured_pool):
    byok = ProviderConfig(
        provider_type="openai", api_key="test-only-owner-key", model="owner-model",
    )
    registry = ExpertRegistry(seed_from_settings=False)
    registry.register(byok, provider_id="owner-record")

    assert byok.wire_protocol is None
    assert byok.reasoning_effort is None
    assert configured_pool == [byok]
    assert registry.uses_shared_pool() is False


@pytest.mark.parametrize(("field", "new_value"), [
    ("shared_base_url", "https://other-relay.example.com/v1"),
    ("shared_wire_protocol", "openai_chat_completions"),
    ("shared_reasoning_effort", "low"),
    ("shared_api_key", "test-only-rotated-shared-key"),
])
def test_generic_shared_fingerprint_tracks_invocation_configuration(
    configured_pool, monkeypatch, field, new_value,
):
    selected_id = f"openai:{MODEL}"

    def fingerprint():
        return provider_configuration_fingerprint(
            owner_id="demo-test-owner", selected_provider_ids=[selected_id],
        )

    before = fingerprint()
    assert len(before) == 64
    assert DUMMY_KEY not in json.dumps(_shared_provider_row(selected_id))
    monkeypatch.setattr(settings, field, new_value)
    assert fingerprint() != before


def test_generic_provider_switch_invalidates_previous_selected_model(
    configured_pool, monkeypatch,
):
    previous = f"openai:{MODEL}"
    monkeypatch.setattr(settings, "shared_provider", "zhipu")
    monkeypatch.setattr(settings, "shared_model", "glm-demo-model")
    # Even a surviving legacy key for the old choice cannot rescue that choice.
    monkeypatch.setattr(settings, "openai_api_key", "test-only-legacy-openai")
    monkeypatch.setattr(settings, "openai_model", MODEL)

    with pytest.raises(ValidationError) as error:
        provider_configuration_fingerprint(
            owner_id="demo-test-owner", selected_provider_ids=[previous],
        )
    assert error.value.code == "provider_not_enabled"
    fingerprint = provider_configuration_fingerprint(
        owner_id="demo-test-owner", selected_provider_ids=["zhipu:glm-demo-model"],
    )
    assert len(fingerprint) == 64


def test_generic_zhipu_default_does_not_inherit_legacy_endpoint(
    configured_pool, monkeypatch,
):
    monkeypatch.setattr(settings, "shared_provider", "zhipu")
    monkeypatch.setattr(settings, "shared_base_url", "")
    monkeypatch.setattr(settings, "zhipu_api_base", "https://legacy-a.example.com/v1")
    selected_id = f"zhipu:{MODEL}"

    def fingerprint():
        return provider_configuration_fingerprint(
            owner_id="demo-test-owner", selected_provider_ids=[selected_id],
        )

    configured = settings.configured_shared_provider()
    assert configured is not None
    assert configured.base_url == "https://open.bigmodel.cn/api/paas/v4"
    before = fingerprint()

    monkeypatch.setattr(settings, "zhipu_api_base", "https://legacy-b.example.com/v1")
    assert settings.configured_shared_provider() == configured
    assert fingerprint() == before

    monkeypatch.setattr(settings, "shared_base_url", RELAY_URL)
    assert settings.configured_shared_provider().base_url == RELAY_URL
    assert fingerprint() != before
