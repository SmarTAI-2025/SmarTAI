from __future__ import annotations

import pytest
from sqlalchemy import select

from backend.db.models import ProviderConfigRecord, UserRecord
from backend.db.provider_repository import (
    list_provider_configs,
    set_provider_verification,
    set_provider_vision_verification,
    update_provider_config,
    upsert_provider_config,
)
from backend.db.session import session_scope
from backend.services.grading_input_security import provider_configuration_fingerprint
from backend.config import settings
from backend.llm.endpoint_policy import (
    CUSTOM_PROVIDER_RISK_ACK_VERSION,
    normalize_provider_endpoint,
)
from backend.models import ProviderConfig
from backend.llm.registry import ExpertRegistry


MASTER_KEY = "custom-provider-test-master-key-0123456789"


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


def _custom_config(base_url: str, *, model: str = "relay-model") -> ProviderConfig:
    canonical, identity = normalize_provider_endpoint("openai_compatible", base_url)
    return ProviderConfig(
        provider_type="openai_compatible",
        api_key="sk-private-owner-key",
        model=model,
        base_url=canonical,
        endpoint_identity=identity,
        display_name="Private relay",
        enabled=False,
    )


def test_same_owner_can_store_same_model_at_two_endpoints():
    _owner("custom-owner-a")
    first = upsert_provider_config(
        "custom-owner-a",
        _custom_config("https://relay-a.example.com/v1"),
        master_key=MASTER_KEY,
        risk_ack_version=CUSTOM_PROVIDER_RISK_ACK_VERSION,
    )
    second = upsert_provider_config(
        "custom-owner-a",
        _custom_config("https://relay-b.example.com/v1"),
        master_key=MASTER_KEY,
        risk_ack_version=CUSTOM_PROVIDER_RISK_ACK_VERSION,
    )

    assert first.id != second.id
    loaded = list_provider_configs("custom-owner-a", master_key=MASTER_KEY)
    assert {item.config.endpoint_identity for item in loaded} == {
        "https://relay-a.example.com/v1",
        "https://relay-b.example.com/v1",
    }


def test_custom_provider_stays_owner_scoped_encrypted_and_acknowledged():
    _owner("custom-owner-b")
    record = upsert_provider_config(
        "custom-owner-b",
        _custom_config("https://api.llm.ustc.edu.cn/v1"),
        master_key=MASTER_KEY,
        risk_ack_version=CUSTOM_PROVIDER_RISK_ACK_VERSION,
    )

    with session_scope() as session:
        row = session.get(ProviderConfigRecord, record.id)
        assert row is not None
        assert "sk-private-owner-key" not in row.encrypted_api_key
        assert row.provider_type == "openai_compatible"
        assert row.endpoint_identity == "https://api.llm.ustc.edu.cn/v1"
        assert row.risk_ack_version == CUSTOM_PROVIDER_RISK_ACK_VERSION
        assert row.risk_ack_at is not None
        assert row.enabled is False

    assert list_provider_configs("other-owner", master_key=MASTER_KEY) == []


def test_update_invalidates_text_and_vision_verification():
    _owner("custom-owner-c")
    record = upsert_provider_config(
        "custom-owner-c",
        _custom_config("https://relay.example.com/v1"),
        master_key=MASTER_KEY,
        risk_ack_version=CUSTOM_PROVIDER_RISK_ACK_VERSION,
    )
    set_provider_verification(
        "custom-owner-c", record.id,
        verification_status="verified", checked_at=2,
    )
    set_provider_vision_verification(
        "custom-owner-c", record.id,
        verification_status="verified", checked_at=3,
    )

    updated = update_provider_config(
        "custom-owner-c",
        record.id,
        _custom_config("https://relay.example.com/v2"),
        master_key=MASTER_KEY,
        risk_ack_version=CUSTOM_PROVIDER_RISK_ACK_VERSION,
    )

    assert updated is not None
    assert updated.verification_status == "unverified"
    assert updated.vision_verification_status == "unverified"
    assert updated.config.enabled is False


def test_official_deepseek_and_custom_ustc_are_distinct_records():
    _owner("custom-owner-d")
    official_url, official_identity = normalize_provider_endpoint(
        "deepseek", "https://api.deepseek.com/v1"
    )
    official = upsert_provider_config(
        "custom-owner-d",
        ProviderConfig(
            provider_type="deepseek",
            api_key="sk-deepseek",
            model="deepseek-chat",
            base_url=official_url,
            endpoint_identity=official_identity,
        ),
        master_key=MASTER_KEY,
    )
    custom = upsert_provider_config(
        "custom-owner-d",
        _custom_config(
            "https://api.llm.ustc.edu.cn/v1", model="deepseek-chat"
        ),
        master_key=MASTER_KEY,
        risk_ack_version=CUSTOM_PROVIDER_RISK_ACK_VERSION,
    )

    assert official.id != custom.id
    with session_scope() as session:
        rows = list(session.scalars(select(ProviderConfigRecord)))
    assert {row.provider_type for row in rows} == {"deepseek", "openai_compatible"}


def test_official_repository_derives_stable_identity_for_legacy_callers():
    _owner("custom-owner-e")
    first = upsert_provider_config(
        "custom-owner-e",
        ProviderConfig(
            provider_type="openai", api_key="first", model="gpt-test"
        ),
        master_key=MASTER_KEY,
    )
    second = upsert_provider_config(
        "custom-owner-e",
        ProviderConfig(
            provider_type="openai", api_key="second", model="gpt-test"
        ),
        master_key=MASTER_KEY,
    )

    assert first.id == second.id
    loaded = list_provider_configs("custom-owner-e", master_key=MASTER_KEY)
    assert loaded[0].config.endpoint_identity == "https://api.openai.com/v1"


def test_registry_gates_custom_text_vision_and_rag_without_model_id_collision(
    monkeypatch,
):
    monkeypatch.setattr(settings, "custom_provider_endpoints_enabled", True)
    registry = ExpertRegistry(seed_from_settings=False)
    registry.register(
        _custom_config("https://relay-a.example.com/v1").model_copy(
            update={"enabled": True}
        ),
        provider_id="custom-a",
        verification_status="verified",
        vision_verification_status="unverified",
        risk_ack_version=CUSTOM_PROVIDER_RISK_ACK_VERSION,
    )
    registry.register(
        _custom_config("https://relay-b.example.com/v1").model_copy(
            update={"enabled": True}
        ),
        provider_id="custom-b",
        verification_status="verified",
        vision_verification_status="verified",
        risk_ack_version=CUSTOM_PROVIDER_RISK_ACK_VERSION,
    )

    provider_a = registry.get("custom-a")
    provider_b = registry.get("custom-b")
    assert provider_a is not None and provider_b is not None
    assert provider_a.provider_id == provider_b.provider_id
    assert registry.pick_vision(provider_a) is provider_b
    assert registry.pick_vision(provider_b) is provider_b
    assert registry.list_enabled_configs() == []
    assert registry.select(
        ["custom-a", "custom-b"], primary_provider_id="custom-a"
    ).pick_vision(provider_a) is provider_b


def test_custom_provider_cannot_be_added_to_a_shared_pool_registry(monkeypatch):
    monkeypatch.setattr(settings, "custom_provider_endpoints_enabled", True)
    registry = ExpertRegistry(seed_from_settings=False)
    registry._uses_shared_pool = True

    with pytest.raises(ValueError, match="custom_provider_shared_pool_not_allowed"):
        registry.register(_custom_config("https://relay.example.com/v1"))


def test_changed_risk_version_hides_previously_verified_custom_provider(monkeypatch):
    monkeypatch.setattr(settings, "custom_provider_endpoints_enabled", True)
    registry = ExpertRegistry(seed_from_settings=False)
    registry.register(
        _custom_config("https://relay.example.com/v1").model_copy(
            update={"enabled": True}
        ),
        provider_id="custom-old-ack",
        verification_status="verified",
        risk_ack_version="2026-01-01.old",
    )

    assert registry.get("custom-old-ack") is None
    assert registry.list_available() == []
    assert registry.list_configs()[0]["enabled"] is False


def test_unverified_custom_provider_is_projected_as_disabled(monkeypatch):
    monkeypatch.setattr(settings, "custom_provider_endpoints_enabled", True)
    registry = ExpertRegistry(seed_from_settings=False)
    registry.register(
        _custom_config("https://relay.example.com/v1").model_copy(
            update={"enabled": True}
        ),
        provider_id="custom-unverified",
        verification_status="unverified",
        risk_ack_version=CUSTOM_PROVIDER_RISK_ACK_VERSION,
    )

    assert registry.get("custom-unverified") is None
    assert registry.list_configs()[0]["enabled"] is False


def test_custom_verification_state_is_frozen_into_grading_fingerprint():
    _owner("custom-owner-fingerprint")
    record = upsert_provider_config(
        "custom-owner-fingerprint",
        _custom_config("https://relay.example.com/v1"),
        master_key=MASTER_KEY,
        risk_ack_version=CUSTOM_PROVIDER_RISK_ACK_VERSION,
    )
    before = provider_configuration_fingerprint(
        owner_id="custom-owner-fingerprint",
        selected_provider_ids=[record.id],
    )
    assert set_provider_verification(
        "custom-owner-fingerprint",
        record.id,
        verification_status="verified",
        checked_at=2,
    )
    after = provider_configuration_fingerprint(
        owner_id="custom-owner-fingerprint",
        selected_provider_ids=[record.id],
    )

    assert before != after
