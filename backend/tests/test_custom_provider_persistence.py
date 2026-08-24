from __future__ import annotations

import pytest
from sqlalchemy import select

from backend.config import settings
from backend.db.models import ProviderConfigRecord, UserRecord
from backend.db.provider_repository import (
    list_provider_configs,
    set_provider_verification,
    update_provider_config,
    upsert_provider_config,
)
from backend.db.session import session_scope
from backend.llm.endpoint_policy import normalize_provider_endpoint
from backend.llm.registry import ExpertRegistry
from backend.models import ProviderConfig
from backend.services.grading_input_security import provider_configuration_fingerprint


MASTER_KEY = "relay-provider-test-master-key-0123456789"


def _owner(owner_id: str) -> None:
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


def _deepseek_config(
    base_url: str,
    *,
    model: str = "relay-model",
    enabled: bool = True,
    wire_protocol: str | None = None,
) -> ProviderConfig:
    canonical, identity = normalize_provider_endpoint(
        "deepseek",
        base_url,
        wire_protocol,
    )
    return ProviderConfig(
        provider_type="deepseek",
        api_key="sk-private-owner-key",
        model=model,
        base_url=canonical,
        endpoint_identity=identity,
        wire_protocol=wire_protocol,
        display_name="DeepSeek model",
        enabled=enabled,
    )


def test_same_owner_can_store_same_vendor_model_at_two_endpoints():
    _owner("relay-owner-a")
    first = upsert_provider_config(
        "relay-owner-a",
        _deepseek_config("https://relay-a.example.com/v1"),
        master_key=MASTER_KEY,
    )
    second = upsert_provider_config(
        "relay-owner-a",
        _deepseek_config("https://relay-b.example.com/v1"),
        master_key=MASTER_KEY,
    )

    assert first.id != second.id
    loaded = list_provider_configs("relay-owner-a", master_key=MASTER_KEY)
    assert {item.config.endpoint_identity for item in loaded} == {
        "https://relay-a.example.com/v1",
        "https://relay-b.example.com/v1",
    }


def test_ustc_deepseek_stays_owner_scoped_and_encrypted():
    _owner("relay-owner-b")
    record = upsert_provider_config(
        "relay-owner-b",
        _deepseek_config("https://api.llm.ustc.edu.cn/v1"),
        master_key=MASTER_KEY,
    )

    with session_scope() as session:
        row = session.get(ProviderConfigRecord, record.id)
        assert row is not None
        assert "sk-private-owner-key" not in row.encrypted_api_key
        assert row.provider_type == "deepseek"
        assert row.endpoint_identity == "https://api.llm.ustc.edu.cn/v1"
        assert row.enabled is True

    assert list_provider_configs("other-owner", master_key=MASTER_KEY) == []


def test_update_keeps_relay_enabled_and_resets_optional_connectivity_status():
    _owner("relay-owner-c")
    record = upsert_provider_config(
        "relay-owner-c",
        _deepseek_config("https://relay.example.com/v1"),
        master_key=MASTER_KEY,
    )
    set_provider_verification(
        "relay-owner-c",
        record.id,
        verification_status="verified",
        checked_at=2,
    )

    updated = update_provider_config(
        "relay-owner-c",
        record.id,
        _deepseek_config("https://relay.example.com/v2"),
        master_key=MASTER_KEY,
    )

    assert updated is not None
    assert updated.verification_status == "unverified"
    assert updated.config.enabled is True


def test_official_and_relay_deepseek_are_distinct_records():
    _owner("relay-owner-d")
    official = upsert_provider_config(
        "relay-owner-d",
        _deepseek_config("https://api.deepseek.com/v1", model="deepseek-chat"),
        master_key=MASTER_KEY,
    )
    relay = upsert_provider_config(
        "relay-owner-d",
        _deepseek_config(
            "https://api.llm.ustc.edu.cn/v1",
            model="deepseek-chat",
        ),
        master_key=MASTER_KEY,
    )

    assert official.id != relay.id
    with session_scope() as session:
        rows = list(session.scalars(select(ProviderConfigRecord)))
    assert {row.provider_type for row in rows} == {"deepseek"}
    assert {row.endpoint_identity for row in rows} == {
        "https://api.deepseek.com/v1",
        "https://api.llm.ustc.edu.cn/v1",
    }


def test_same_vendor_model_and_endpoint_can_store_distinct_wire_protocols():
    _owner("relay-owner-protocol")
    endpoint = "https://relay.example.com/v1"
    openai_wire = upsert_provider_config(
        "relay-owner-protocol",
        _deepseek_config(
            endpoint,
            model="same-model",
            wire_protocol="openai_chat_completions",
        ),
        master_key=MASTER_KEY,
    )
    anthropic_wire = upsert_provider_config(
        "relay-owner-protocol",
        _deepseek_config(
            endpoint,
            model="same-model",
            wire_protocol="anthropic_messages",
        ),
        master_key=MASTER_KEY,
    )

    assert openai_wire.id != anthropic_wire.id
    loaded = list_provider_configs("relay-owner-protocol", master_key=MASTER_KEY)
    assert {item.config.wire_protocol for item in loaded} == {
        "openai_chat_completions",
        "anthropic_messages",
    }


def test_official_repository_derives_stable_identity_for_legacy_callers():
    _owner("relay-owner-e")
    first = upsert_provider_config(
        "relay-owner-e",
        ProviderConfig(provider_type="openai", api_key="first", model="gpt-test"),
        master_key=MASTER_KEY,
    )
    second = upsert_provider_config(
        "relay-owner-e",
        ProviderConfig(provider_type="openai", api_key="second", model="gpt-test"),
        master_key=MASTER_KEY,
    )

    assert first.id == second.id
    loaded = list_provider_configs("relay-owner-e", master_key=MASTER_KEY)
    assert loaded[0].config.endpoint_identity == "https://api.openai.com/v1"


def test_registry_uses_unverified_relay_without_changing_vision_routing(
    monkeypatch,
):
    monkeypatch.setattr(settings, "runtime_environment", "development")
    registry = ExpertRegistry(seed_from_settings=False)
    official = _deepseek_config(
        "https://api.deepseek.com/v1",
        model="same-model",
    )
    relay = _deepseek_config(
        "https://relay.example.com/v1",
        model="same-model",
    )
    registry.register(official, provider_id="official")
    registry.register(relay, provider_id="relay")

    official_provider = registry.get("official")
    relay_provider = registry.get("relay")
    assert official_provider is not None and relay_provider is not None
    assert official_provider.provider_id == relay_provider.provider_id
    assert official_provider.supports_vision is False
    assert relay_provider.supports_vision is False
    assert relay_provider.can_encode_vision is True
    assert registry.pick_vision(official_provider) is None
    assert registry.select(
        ["official", "relay"], primary_provider_id="official"
    ).pick_vision(official_provider) is None
    # Unreviewed relays are not silently reused for course-material embeddings.
    assert registry.list_enabled_configs() == [official]


def test_registry_resolves_duplicate_user_labels_with_a_stable_id_suffix(
    monkeypatch,
):
    monkeypatch.setattr(settings, "runtime_environment", "development")
    registry = ExpertRegistry(seed_from_settings=False)
    for provider_type, provider_id in (
        ("openai", "provider-record-aaaa1111"),
        ("deepseek", "provider-record-bbbb2222"),
    ):
        registry.register(
            ProviderConfig(
                provider_type=provider_type,
                api_key="private-key",
                model="shared-model-name",
                base_url="https://relay.example.com/v1",
                endpoint_identity="https://relay.example.com/v1",
                wire_protocol="openai_chat_completions",
                display_name="Campus relay",
            ),
            provider_id=provider_id,
        )

    names = [item["resolved_display_name"] for item in registry.list_configs()]
    assert len(names) == len(set(names)) == 2
    assert any(str(name).endswith("aaaa1111") for name in names)
    assert any(str(name).endswith("bbbb2222") for name in names)


def test_relay_cannot_be_added_to_an_active_shared_pool_registry(monkeypatch):
    monkeypatch.setattr(settings, "runtime_environment", "development")
    registry = ExpertRegistry(seed_from_settings=False)
    registry._uses_shared_pool = True

    with pytest.raises(ValueError, match="custom_provider_shared_pool_not_allowed"):
        registry.register(_deepseek_config("https://relay.example.com/v1"))


def test_shared_pool_seed_skips_custom_environment_route(monkeypatch):
    monkeypatch.setattr(settings, "runtime_environment", "development")
    monkeypatch.setattr(settings, "shared_pool_enabled", True)
    monkeypatch.setattr(settings, "openai_api_key", "shared-key")
    monkeypatch.setattr(settings, "openai_api_base", "https://relay.example.com/v1")
    for field in (
        "gemini_api_key",
        "zhipu_api_key",
        "anthropic_api_key",
        "deepseek_api_key",
        "moonshot_api_key",
        "qwen_api_key",
    ):
        monkeypatch.setattr(settings, field, "")

    registry = ExpertRegistry(shared_owner_id="owner")

    assert registry.list_configs() == []
    assert registry.uses_shared_pool() is False


def test_shared_pool_seed_skips_invalid_environment_route(monkeypatch):
    monkeypatch.setattr(settings, "shared_pool_enabled", True)
    monkeypatch.setattr(settings, "openai_api_key", "shared-key")
    monkeypatch.setattr(settings, "openai_api_base", "http://127.0.0.1/v1")
    for field in (
        "gemini_api_key",
        "zhipu_api_key",
        "anthropic_api_key",
        "deepseek_api_key",
        "moonshot_api_key",
        "qwen_api_key",
    ):
        monkeypatch.setattr(settings, field, "")

    registry = ExpertRegistry(shared_owner_id="owner")

    assert registry.list_configs() == []
    assert registry.uses_shared_pool() is False


def test_production_kill_switch_hides_relay_but_not_official(monkeypatch):
    monkeypatch.setattr(settings, "runtime_environment", "production")
    monkeypatch.setattr(settings, "custom_provider_endpoints_enabled", False)
    registry = ExpertRegistry(seed_from_settings=False)
    registry.register(
        _deepseek_config("https://api.deepseek.com/v1"),
        provider_id="official",
    )
    registry.register(
        _deepseek_config("https://relay.example.com/v1"),
        provider_id="relay",
    )

    assert registry.get("official") is not None
    assert registry.get("relay") is None
    assert [item["provider_id"] for item in registry.list_configs() if item["enabled"]] == [
        "official"
    ]


def test_optional_verification_does_not_change_frozen_provider_fingerprint():
    _owner("relay-owner-fingerprint")
    record = upsert_provider_config(
        "relay-owner-fingerprint",
        _deepseek_config("https://relay.example.com/v1"),
        master_key=MASTER_KEY,
    )
    before = provider_configuration_fingerprint(
        owner_id="relay-owner-fingerprint",
        selected_provider_ids=[record.id],
    )
    assert set_provider_verification(
        "relay-owner-fingerprint",
        record.id,
        verification_status="verified",
        checked_at=2,
    )
    after = provider_configuration_fingerprint(
        owner_id="relay-owner-fingerprint",
        selected_provider_ids=[record.id],
    )

    assert before == after
