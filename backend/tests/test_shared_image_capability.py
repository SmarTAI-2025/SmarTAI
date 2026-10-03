"""Shared probes use synthetic pixels/providers and the real owner quota guard."""
import time

import pytest
from fastapi import HTTPException
from langchain_core.messages import HumanMessage
from sqlalchemy import select

from backend.api import experts
from backend.config import settings
from backend.db.models import ProviderConfigRecord, SharedProviderImageRecord
from backend.db.session import session_scope
from backend.db.shared_image_repository import shared_image_evidence, set_shared_image_evidence
from backend.llm.image_capability import make_image_challenge
from backend.llm.providers import LLMResponse, ProviderRequestError, VisionImage
from backend.llm.registry import _build_scoped_registry
from backend.models import ProviderConfig, User
from backend.tests.test_custom_provider_api import _teacher
from backend.tests.test_shared_vision_quota import client, shared_registry, FakeProvider, save, usage


def reply(content):
    return LLMResponse(content=content, provider="openai:synthetic", model="synthetic", duration_ms=1)


@pytest.fixture(autouse=True)
def isolated_probe_cooldown(monkeypatch):
    monkeypatch.setattr(settings, "custom_provider_verification_cooldown_seconds", 0)


@pytest.mark.asyncio
async def test_shared_opt_in_probe_charges_once_without_storing_key_and_isolates_owner(client, shared_registry, monkeypatch):
    assert save(client, {"shared_pool_daily_request_limit": 3}).status_code == 200
    pixels, answer = make_image_challenge()
    monkeypatch.setattr(experts, "make_image_challenge", lambda: (pixels, answer))
    calls = []

    async def read(self, messages, **kwargs):
        calls.append((messages, kwargs))
        return reply(answer)

    monkeypatch.setattr(FakeProvider, "ainvoke", read)
    result = await experts.verify_provider_image("openai:synthetic", User(id="teacher", username="teacher"), shared_registry)
    assert result["image_capability_status"] == "passed"
    assert len(calls) == 1 and usage()["requests"] == 1
    assert calls[0][0][0].content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert answer not in calls[0][0][0].content[0]["text"]
    assert calls[0][1] == {"max_output_tokens": 64}
    current = _build_scoped_registry(User(id="teacher", username="teacher"))
    assert current.list_configs()[0]["image_capability_status"] == "passed"
    _teacher("other-image-owner")
    other = _build_scoped_registry(User(id="other-image-owner", username="other-image-owner"))
    assert other.list_configs()[0]["image_capability_status"] == "unverified"
    with session_scope() as session:
        assert session.scalar(select(ProviderConfigRecord).where(ProviderConfigRecord.owner_id == "teacher")) is None
        record = session.get(SharedProviderImageRecord, ("teacher", "openai"))
        assert record is not None and len(record.fingerprint) == 64
        assert "synthetic-platform-key" not in str(record.__dict__)


@pytest.mark.asyncio
async def test_shared_probe_exhausted_quota_never_calls_model_or_claims_unsupported(client, shared_registry, monkeypatch):
    assert save(client, {"shared_pool_daily_request_limit": 0}).status_code == 200
    async def forbidden(*args, **kwargs):
        pytest.fail("No model request may bypass the shared quota guard")
    monkeypatch.setattr(FakeProvider, "ainvoke", forbidden)
    result = await experts.verify_provider_image("openai:synthetic", User(id="teacher", username="teacher"), shared_registry)
    assert result["image_capability_status"] == "inconclusive"
    assert result["image_reason"] == "shared_pool_daily_limit_reached"
    assert usage()["requests"] == 0


@pytest.mark.asyncio
async def test_shared_inflight_credential_change_cannot_overwrite_new_state(client, shared_registry, monkeypatch):
    assert save(client, {"shared_pool_daily_request_limit": 3}).status_code == 200
    pixels, answer = make_image_challenge()
    monkeypatch.setattr(experts, "make_image_challenge", lambda: (pixels, answer))
    async def change(self, messages, **kwargs):
        monkeypatch.setattr(settings, "openai_api_key", "replacement-platform-key")
        return reply(answer)
    monkeypatch.setattr(FakeProvider, "ainvoke", change)
    with pytest.raises(HTTPException) as error:
        await experts.verify_provider_image("openai:synthetic", User(id="teacher", username="teacher"), shared_registry)
    assert error.value.status_code == 409
    assert _build_scoped_registry(User(id="teacher", username="teacher")).list_configs()[0]["image_capability_status"] == "unverified"
    assert usage()["requests"] == 1


@pytest.mark.asyncio
async def test_shared_normal_explicit_image_rejection_is_owner_bound_and_text_still_works(client, shared_registry, monkeypatch):
    assert save(client, {"shared_pool_daily_request_limit": 3}).status_code == 200
    async def reject_image(self, messages, **kwargs):
        if isinstance(messages[0].content, list):
            raise ProviderRequestError("provider_vision_not_supported", status_code=400)
        return reply("text answer")
    monkeypatch.setattr(FakeProvider, "ainvoke", reject_image)
    provider = shared_registry.pick_default()
    with pytest.raises(ProviderRequestError):
        await provider.ainvoke_vision("read", [VisionImage(b"pixels", "image/png")])
    assert _build_scoped_registry(User(id="teacher", username="teacher")).list_configs()[0]["image_capability_status"] == "unsupported"
    assert (await provider.ainvoke([HumanMessage(content="plain text")])).content == "text answer"
    assert usage()["requests"] == 2


@pytest.mark.parametrize("change", [
    {"model": "changed"}, {"api_key": "changed-key"}, {"base_url": "https://relay.example.com/v1"},
    {"wire_protocol": "anthropic_messages", "base_url": "https://relay.example.com"},
])
def test_shared_config_edit_and_revert_never_revives_previous_proof(client, change):
    config = ProviderConfig(provider_type="openai", model="original", api_key="fake")
    original = shared_image_evidence("teacher", config, create=True)
    assert set_shared_image_evidence("teacher", original, status="passed", checked_at=time.time(), reason="image_probe_answer_correct")
    changed = shared_image_evidence("teacher", config.model_copy(update=change))
    assert changed.status == "unverified" and changed.checked_at is None
    reverted = shared_image_evidence("teacher", config)
    assert reverted.status == "unverified" and reverted.updated_at != original.updated_at
    assert not set_shared_image_evidence("teacher", original, status="passed", checked_at=time.time(), reason="old")
