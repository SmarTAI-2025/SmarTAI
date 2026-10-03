from __future__ import annotations

import base64
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from langchain_core.messages import AIMessage
from PIL import Image

from backend.api import experts
from backend.config import settings
from backend.db.provider_repository import get_provider_config, set_image_capability, update_provider_config, upsert_provider_config
from backend.llm.image_capability import IMAGE_CHALLENGE_PROMPT, explicitly_rejects_images, make_image_challenge
from backend.llm.providers import ProviderRequestError, VisionImage, build_provider
from backend.llm.registry import _build_scoped_registry
from backend.models import ProviderConfig, User
from backend.tests.test_custom_provider_api import _teacher


@pytest.mark.parametrize("vendor", ["openai", "gemini", "anthropic", "zhipu", "qwen", "moonshot", "deepseek"])
@pytest.mark.parametrize("model", ["plain-model", "glm-4v-flash"])
@pytest.mark.asyncio
async def test_brand_and_model_names_never_establish_capability(vendor, model, monkeypatch):
    monkeypatch.setattr(settings, "http_proxy", "")
    monkeypatch.setattr(settings, "https_proxy", "")
    provider = build_provider(ProviderConfig(provider_type=vendor, model=model, api_key="fake"))
    assert provider.supports_vision is None
    assert provider.can_attempt_vision
    client = SimpleNamespace(ainvoke=AsyncMock(return_value=AIMessage(content="read")))
    provider._get_client = AsyncMock(return_value=client)
    await provider.ainvoke_vision("transcribe", [VisionImage(data=b"pixels", media_type="image/png")])
    blocks = client.ainvoke.call_args.args[0][0].content
    assert blocks[1]["image_url"]["url"] == "data:image/png;base64,cGl4ZWxz"
    assert provider.supports_vision is None  # Ordinary success is not a passed test.
    provider.config.image_capability_status = "unsupported"
    with pytest.raises(ProviderRequestError, match="provider_vision_not_supported"):
        await provider.ainvoke_vision("read", [VisionImage(data=b"pixels", media_type="image/png")])
    # A reliable text task still works on explicitly unsupported configurations.
    await provider.ainvoke([AIMessage(content="text")])
    assert client.ainvoke.await_count == 2


@pytest.mark.parametrize("protocol,payload", [
    ("openai_chat_completions", {"choices": [{"message": {"content": "ok"}}]}),
    ("anthropic_messages", {"content": [{"type": "text", "text": "ok"}]}),
    ("gemini_generate_content", {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}),
])
@pytest.mark.asyncio
async def test_relay_always_serializes_real_images_with_selected_protocol(protocol, payload, monkeypatch):
    monkeypatch.setattr(settings, "runtime_environment", "development")
    provider = build_provider(ProviderConfig(provider_type="qwen", model="arbitrary", api_key="fake", base_url="https://relay.example.com/v1", wire_protocol=protocol))
    client = SimpleNamespace(post=AsyncMock(return_value=httpx.Response(200, json=payload)))
    provider._relay_client = AsyncMock(return_value=client)
    await provider.ainvoke_vision("read", [VisionImage(data=b"pixels", media_type="image/png")])
    request = client.post.call_args.kwargs["json"]
    assert base64.b64encode(b"pixels").decode() in str(request)
    assert provider.supports_vision is None
    client.post.return_value = httpx.Response(400, json={"error": {"message": "This model does not support image input"}})
    with pytest.raises(ProviderRequestError, match="provider_vision_not_supported"):
        await provider.ainvoke_vision("read", [VisionImage(data=b"pixels", media_type="image/png")])
    assert provider.supports_vision is False


@pytest.mark.parametrize("status,message,expected", [
    (400, "This model does not support image input", True),
    (422, "image_url is not supported", True),
    (400, "Invalid image format", False),
    (400, "Unsupported image format", False),
    (400, "Image size not supported", False),
    (400, "model not found", False),
    (401, "does not support image input", False),
    (429, "does not support image input", False),
    (503, "does not support image input", False),
])
def test_only_explicit_input_rejection_is_capability_evidence(status, message, expected):
    assert explicitly_rejects_images(status, {"error": {"message": message}}) is expected


def test_fresh_challenge_has_no_answer_metadata():
    samples = [make_image_challenge() for _ in range(4)]
    assert len({answer for _, answer in samples}) == 4
    for pixels, answer in samples:
        image = Image.open(io.BytesIO(pixels))
        assert image.info == {} and image.size == (320, 88)
        assert answer not in IMAGE_CHALLENGE_PROMPT
        assert answer.encode() not in pixels
        assert len(pixels) < 20_000


def saved(owner):
    _teacher(owner)
    config = ProviderConfig(provider_type="qwen", model="no-brand-assumptions", api_key="fake")
    row = upsert_provider_config(owner, config, master_key=settings.provider_encryption_key)
    return row.id, User(id=owner, username=owner)


@pytest.mark.parametrize("outcome,state,reason", [
    ("answer", "passed", "image_probe_answer_correct"),
    ("I support images", "inconclusive", "image_probe_answer_incorrect"),
    ("wrong", "inconclusive", "image_probe_answer_incorrect"),
    (ProviderRequestError("provider_vision_not_supported", status_code=400), "unsupported", "provider_vision_not_supported"),
    (TimeoutError(), "inconclusive", "expert_verification_timeout"),
    (ProviderRequestError("provider_auth_failed", status_code=401), "inconclusive", "expert_verification_auth_failed"),
    (ProviderRequestError("provider_rate_limited", status_code=429), "inconclusive", "expert_verification_rate_limited"),
    (ProviderRequestError("provider_unreachable"), "inconclusive", "expert_verification_connection_failed"),
])
@pytest.mark.asyncio
async def test_opt_in_probe_is_single_and_checks_answer(outcome, state, reason, monkeypatch):
    provider_id, user = saved("image-probe-user")
    registry = _build_scoped_registry(user)
    provider = registry.get(provider_id)
    pixels, answer = make_image_challenge()
    monkeypatch.setattr(experts, "make_image_challenge", lambda: (pixels, answer))
    monkeypatch.setattr(settings, "custom_provider_verification_cooldown_seconds", 0)
    call = AsyncMock(side_effect=outcome) if isinstance(outcome, Exception) else AsyncMock(return_value=SimpleNamespace(content=answer if outcome == "answer" else outcome))
    provider.ainvoke_vision = call
    monkeypatch.setattr(experts, "build_provider", lambda config: provider)
    response = await experts.verify_provider_image(provider_id, current=user, registry=registry)
    assert response["image_capability_status"] == state
    assert response["image_reason"] == reason
    assert call.await_count == 1
    prompt, images = call.call_args.args
    assert images[0].data == pixels and images[0].filename is None and answer not in prompt
    stored = get_provider_config(user.id, provider_id, master_key=settings.provider_encryption_key)
    assert stored.config.image_capability_status == state
    assert stored.verification_status == "unverified"


@pytest.mark.asyncio
async def test_probe_cannot_overwrite_edited_configuration_or_cross_owner(monkeypatch):
    provider_id, user = saved("image-race-user")
    registry = _build_scoped_registry(user)
    old = get_provider_config(user.id, provider_id, master_key=settings.provider_encryption_key)
    monkeypatch.setattr(settings, "custom_provider_verification_cooldown_seconds", 0)
    async def edit_during_call(*args, **kwargs):
        update_provider_config(user.id, provider_id, old.config.model_copy(update={"api_key": "changed"}), master_key=settings.provider_encryption_key)
        return SimpleNamespace(content="wrong")
    registry.get(provider_id).ainvoke_vision = edit_during_call
    monkeypatch.setattr(experts, "build_provider", lambda config: registry.get(provider_id))
    with pytest.raises(HTTPException) as rejected:
        await experts.verify_provider_image(provider_id, current=user, registry=registry)
    assert rejected.value.status_code == 409
    current = get_provider_config(user.id, provider_id, master_key=settings.provider_encryption_key)
    assert current.config.image_capability_status == "unverified"
    assert not set_image_capability("other-user", provider_id, status="passed", checked_at=10, reason="test", expected_updated_at=current.updated_at)
    with pytest.raises(HTTPException) as rejected:
        await experts.verify_provider_image(provider_id, current=User(id="other-user", username="other-user"), registry=registry)
    assert rejected.value.status_code == 404


@pytest.mark.parametrize("changed", [{"model": "new"}, {"api_key": "new"}, {"base_url": "https://new.example/v1", "endpoint_identity": None}, {"wire_protocol": "anthropic_messages", "base_url": "https://new.example/v1", "endpoint_identity": None}])
def test_edit_invalidates_image_evidence(changed):
    provider_id, user = saved("image-edit-user")
    current = get_provider_config(user.id, provider_id, master_key=settings.provider_encryption_key)
    assert set_image_capability(user.id, provider_id, status="passed", checked_at=10, reason="correct", expected_updated_at=current.updated_at)
    updated = update_provider_config(user.id, provider_id, current.config.model_copy(update=changed), master_key=settings.provider_encryption_key)
    loaded = get_provider_config(user.id, provider_id, master_key=settings.provider_encryption_key)
    assert loaded.config.image_capability_status == "unverified"
    assert loaded.config.image_checked_at is None
    assert not set_image_capability(user.id, provider_id, status="passed", checked_at=20, reason="stale", expected_updated_at=current.updated_at)

@pytest.mark.asyncio
async def test_probe_builds_from_latest_stored_snapshot_even_if_registry_is_old(monkeypatch):
    provider_id, user = saved("image-snapshot-user")
    old_registry = _build_scoped_registry(user)
    current = get_provider_config(user.id, provider_id, master_key=settings.provider_encryption_key)
    update_provider_config(user.id, provider_id, current.config.model_copy(update={"model": "new-model", "api_key": "new-key"}), master_key=settings.provider_encryption_key)
    pixels, answer = make_image_challenge()
    monkeypatch.setattr(experts, "make_image_challenge", lambda: (pixels, answer))
    monkeypatch.setattr(settings, "custom_provider_verification_cooldown_seconds", 0)
    def build(config):
        assert config.model == "new-model" and config.api_key == "new-key"
        assert config.scheduling_owner == user.id
        return SimpleNamespace(config=config, ainvoke_vision=AsyncMock(return_value=SimpleNamespace(content=answer)))
    monkeypatch.setattr(experts, "build_provider", build)
    response = await experts.verify_provider_image(provider_id, current=user, registry=old_registry)
    assert response["image_capability_status"] == "passed"


@pytest.mark.asyncio
async def test_official_explicit_rejection_is_recorded_but_auth_and_format_are_not():
    provider_id, user = saved("image-normal-rejection")
    registry = _build_scoped_registry(user)
    provider = registry.get(provider_id)
    class SDKError(Exception):
        status_code = 400
        body = {"error": {"message": "model does not support image input"}}
    provider._get_client = AsyncMock(return_value=SimpleNamespace(ainvoke=AsyncMock(side_effect=SDKError())))
    with pytest.raises(ProviderRequestError, match="provider_vision_not_supported"):
        await provider.ainvoke_vision("read", [VisionImage(data=b"pixels", media_type="image/png")])
    stored = get_provider_config(user.id, provider_id, master_key=settings.provider_encryption_key)
    assert stored.config.image_capability_status == "unsupported"
    assert stored.config.image_checked_at is not None


def test_capability_evidence_is_not_part_of_operation_identity():
    config = ProviderConfig(provider_type="qwen", model="arbitrary", api_key="fake")
    original = config.model_dump(mode="json")
    config.image_capability_status = "passed"
    config.image_reason = "image_probe_answer_correct"
    config.image_checked_at = 100
    assert config.model_dump(mode="json") == original


def test_migration_preserves_existing_provider_and_defaults_to_unknown(tmp_path, monkeypatch):
    from alembic import command
    from sqlalchemy import create_engine, text, inspect
    from backend.tests.test_migration_roundtrip import _alembic_config
    url = f"sqlite:///{tmp_path / 'image-migration.db'}"
    cfg = _alembic_config(url, monkeypatch)
    command.upgrade(cfg, "0024_model_daily_usage")
    engine = create_engine(url)
    with engine.begin() as c:
        c.execute(text("INSERT INTO users(id,username,role,password_hash,is_active,created_at,updated_at) VALUES('old','old','teacher','hash',true,1,1)"))
        c.execute(text("INSERT INTO provider_configs(id,owner_id,provider_type,model,endpoint_identity,wire_protocol,encrypted_api_key,nonce,key_version,enabled,max_concurrent,rpm,verification_status,created_at,updated_at) VALUES('old-provider','old','qwen','whatever','https://example.com/v1','openai_chat_completions','encrypted','nonce',1,true,1,0,'verified',1,1)"))
    command.upgrade(cfg, "head")
    with engine.connect() as c:
        assert c.execute(text("SELECT model,verification_status,image_capability_status,image_checked_at,image_reason FROM provider_configs")).one() == ("whatever", "verified", "unverified", None, None)
    command.downgrade(cfg, "0024_model_daily_usage")
    assert "image_capability_status" not in {col["name"] for col in inspect(engine).get_columns("provider_configs")}
    command.upgrade(cfg, "head")
