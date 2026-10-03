"""Shared admission through real wrappers/recognition; only synthetic models."""
import asyncio
import base64
import io
import os
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile
from langchain_core.messages import HumanMessage
from PIL import Image
from starlette.datastructures import Headers

from backend.config import settings
from backend.db.provider_repository import upsert_provider_config
from backend.db.session import session_scope
from backend.domain.errors import RecognitionError
from backend.llm import registry as registry_module
from backend.llm.providers import BaseProvider, LLMResponse, OpenAIProvider, VisionImage
from backend.llm.registry import SharedPoolLimitError, _GuardedSharedProvider
from backend.models import ProviderConfig
from backend.recognition.engine import EngineLocateInputV1, EngineReadInputV1, EngineRepairInputV1, LocatorImageV1
from backend.recognition.models import NormalizedRegionV1
from backend.recognition.repair_response import RepairContextV1
from backend.services.background_errors import classify_background_error, is_retryable_background_error
from backend.services.model_quota import model_usage
from backend.skills.recognition_reader import LLMRecognitionEngine
from backend.tests.test_admin_business_config import client, save
from backend.tests.test_postgres_integration import pg_database


class FakeProvider(BaseProvider):
    provider_type = "openai"
    supports_vision = True

    def __init__(self, config=None):
        super().__init__(config or ProviderConfig(provider_type="openai", model="synthetic", api_key="fake"))
        self.calls = []
        self.sdk_attempts = 0
        self.outcome = "ok"

    def _build_client_sync(self):
        pytest.fail("Synthetic provider must never construct a real SDK client")

    async def ainvoke(self, messages, *, max_output_tokens=None):
        self.calls.append((messages, max_output_tokens))
        self.sdk_attempts += 3 if self.outcome == "internal_retry" else 1
        await asyncio.sleep(0)
        if self.outcome == "failed":
            raise RuntimeError("synthetic failure")
        if self.outcome == "cancelled":
            raise asyncio.CancelledError()
        return LLMResponse(content="1. x = -2", provider=self.provider_id, model=self.model, duration_ms=1)


def usage(owner="teacher"):
    with session_scope() as session:
        return model_usage(session, owner)["shared"]


@pytest.fixture
def shared_registry(client, monkeypatch):
    monkeypatch.setattr(settings, "shared_pool_enabled", True)
    for name in ("gemini", "zhipu", "anthropic", "deepseek", "moonshot", "qwen"):
        monkeypatch.setattr(settings, f"{name}_api_key", "")
    monkeypatch.setattr(settings, "openai_api_key", "synthetic-platform-key")
    monkeypatch.setattr(settings, "openai_api_base", "https://api.openai.com/v1")
    monkeypatch.setattr(settings, "openai_model", "synthetic")
    monkeypatch.setattr(registry_module, "build_provider", FakeProvider)
    return registry_module._build_scoped_registry(SimpleNamespace(id="teacher"))


IMAGES = [VisionImage(b"synthetic-image", "image/png", "source.png")]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["text", "vision"])
@pytest.mark.parametrize("limit,exhausted", [(0, False), (1, True)])
async def test_zero_and_exhausted_requests_never_reach_model(client, shared_registry, method, limit, exhausted):
    assert save(client, {"shared_pool_daily_request_limit": limit}).status_code == 200
    provider = shared_registry.pick_default()
    fake = provider._provider
    if exhausted:
        await provider.ainvoke([HumanMessage(content="first")])
    before = len(fake.calls)
    with pytest.raises(SharedPoolLimitError, match="shared_pool_daily_limit_reached"):
        if method == "text":
            await provider.ainvoke([HumanMessage(content="blocked")], max_output_tokens=40)
        else:
            await provider.ainvoke_vision("blocked", IMAGES, max_output_tokens=40)
    assert len(fake.calls) == before
    assert usage()["requests"] == int(exhausted)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["text", "vision"])
async def test_zero_token_limit_and_live_kill_switch_reject_before_model(client, shared_registry, monkeypatch, method):
    provider = shared_registry.pick_default()
    assert save(client, {"shared_pool_daily_estimated_token_limit": 0}).status_code == 200
    call = lambda: provider.ainvoke([]) if method == "text" else provider.ainvoke_vision("", IMAGES)
    with pytest.raises(SharedPoolLimitError, match="shared_pool_daily_limit_reached"):
        await call()
    monkeypatch.setattr(settings, "shared_pool_enabled", False)
    with pytest.raises(SharedPoolLimitError, match="shared_pool_disabled"):
        await call()
    assert provider._provider.calls == [] and usage()["requests"] == 0


@pytest.mark.parametrize("limit_kind", ["request", "token"])
def test_parallel_vision_requests_share_last_atomic_allowance(client, shared_registry, limit_kind):
    limits = ({"shared_pool_daily_request_limit": 1} if limit_kind == "request" else
              {"shared_pool_daily_estimated_token_limit": 1 + registry_module._SHARED_IMAGE_INPUT_ESTIMATE})
    assert save(client, limits).status_code == 200
    provider = shared_registry.pick_default()

    def invoke(_):
        try:
            asyncio.run(provider.ainvoke_vision("read", IMAGES))
            return True
        except SharedPoolLimitError:
            return False

    with ThreadPoolExecutor(max_workers=12) as pool:
        assert sum(pool.map(invoke, range(36))) == 1
    assert len(provider._provider.calls) == 1 and usage()["requests"] == 1


@pytest.mark.skipif(not os.environ.get("SMARTAI_TEST_POSTGRES_URL"), reason="Requires isolated PostgreSQL")
@pytest.mark.parametrize("limit_kind", ["request", "token"])
def test_postgres_parallel_vision_last_allowance(pg_database, monkeypatch, limit_kind):
    from backend.db.models import UserRecord

    monkeypatch.setattr(settings, "shared_pool_enabled", True)
    monkeypatch.setattr(settings, "shared_pool_daily_request_limit", 1 if limit_kind == "request" else 100)
    monkeypatch.setattr(settings, "shared_pool_daily_estimated_token_limit",
                        1 + registry_module._SHARED_IMAGE_INPUT_ESTIMATE if limit_kind == "token" else 100000)
    with session_scope() as session:
        session.add(UserRecord(id="vision-pg-owner", username="vision-pg-owner", password_hash="fake",
                               role="teacher", is_active=True))
    fake = FakeProvider()
    provider = _GuardedSharedProvider(fake, "vision-pg-owner")

    def invoke(_):
        try:
            asyncio.run(provider.ainvoke_vision("read", IMAGES))
            return True
        except SharedPoolLimitError:
            return False

    with ThreadPoolExecutor(max_workers=12) as pool:
        assert sum(pool.map(invoke, range(36))) == 1
    assert len(fake.calls) == 1 and usage("vision-pg-owner")["requests"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["ok", "failed", "cancelled", "internal_retry"])
async def test_vision_success_failure_cancel_and_sdk_retry_charge_once(client, shared_registry, outcome):
    assert save(client, {"shared_pool_daily_request_limit": 1}).status_code == 200
    provider = shared_registry.pick_default()
    provider._provider.outcome = outcome
    if outcome == "failed":
        with pytest.raises(RuntimeError, match="synthetic failure"):
            await provider.ainvoke_vision("read", IMAGES)
    elif outcome == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await provider.ainvoke_vision("read", IMAGES)
    else:
        await provider.ainvoke_vision("read", IMAGES)
    with pytest.raises(SharedPoolLimitError):
        await provider.ainvoke_vision("read", IMAGES)
    assert usage()["requests"] == 1 and len(provider._provider.calls) == 1
    assert provider._provider.sdk_attempts == (3 if outcome == "internal_retry" else 1)


@pytest.mark.asyncio
async def test_new_logical_retry_is_a_new_admission(client, shared_registry):
    assert save(client, {"shared_pool_daily_request_limit": 2}).status_code == 200
    provider = shared_registry.pick_default()
    provider._provider.outcome = "failed"
    with pytest.raises(RuntimeError):
        await provider.ainvoke_vision("read", IMAGES)
    provider._provider.outcome = "ok"
    await provider.ainvoke_vision("read", IMAGES)
    assert usage()["requests"] == 2 and len(provider._provider.calls) == 2


@pytest.mark.asyncio
async def test_cancel_inflight_vision_remains_charged(client, shared_registry, monkeypatch):
    provider = shared_registry.pick_default()
    started = asyncio.Event()

    async def pending(messages, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(provider._provider, "ainvoke", pending)
    task = asyncio.create_task(provider.ainvoke_vision("read", IMAGES))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert usage()["requests"] == 1


@pytest.mark.asyncio
async def test_actual_openai_sdk_internal_retries_have_one_admission(client, monkeypatch):
    import httpx
    from langchain_openai import ChatOpenAI

    monkeypatch.setattr(settings, "shared_pool_enabled", True)
    assert save(client, {"shared_pool_daily_request_limit": 1}).status_code == 200
    attempts = []

    def respond(request):
        attempts.append(request)
        if len(attempts) < 3:
            return httpx.Response(503, json={"error": {"message": "synthetic retry", "type": "server_error"}})
        return httpx.Response(200, json={
            "id": "synthetic", "object": "chat.completion", "created": 1, "model": "synthetic",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "read"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 1, "total_tokens": 13},
        })

    transport = httpx.MockTransport(respond)
    async with httpx.AsyncClient(transport=transport) as async_client:
        with httpx.Client(transport=transport) as sync_client:
            underlying = OpenAIProvider(ProviderConfig(provider_type="openai", model="synthetic", api_key="fake"))
            underlying._client = ChatOpenAI(model="synthetic", api_key="fake", max_retries=2,
                                           http_async_client=async_client, http_client=sync_client)
            result = await _GuardedSharedProvider(underlying, "teacher").ainvoke_vision("read", IMAGES)
    assert result.content == "read" and len(attempts) == 3 and usage()["requests"] == 1


@pytest.mark.asyncio
async def test_real_adapter_keeps_images_prompt_output_limit_without_double_charge(client, monkeypatch):
    monkeypatch.setattr(settings, "shared_pool_enabled", True)
    assert save(client, {"shared_pool_daily_request_limit": 2}).status_code == 200
    calls = []

    class Client:
        async def ainvoke(self, messages, **kwargs):
            calls.append((messages, kwargs))
            return SimpleNamespace(content="read", usage_metadata=None, response_metadata={})

    underlying = OpenAIProvider(ProviderConfig(provider_type="openai", model="synthetic", api_key="fake"))
    underlying._client = Client()
    provider = _GuardedSharedProvider(underlying, "teacher")
    images = [*IMAGES, VisionImage(b"second-image", "image/jpeg", "second.jpg")]
    await provider.ainvoke_vision("exact prompt", images, max_output_tokens=123)
    await provider.ainvoke([HumanMessage(content="text")], max_output_tokens=456)
    content = calls[0][0][0].content
    assert content[0] == {"type": "text", "text": "exact prompt"}
    for block, image in zip(content[1:], images, strict=True):
        header, encoded = block["image_url"]["url"].split(",", 1)
        assert header == f"data:{image.media_type};base64"
        assert base64.b64decode(encoded) == image.data
    assert [kwargs for _, kwargs in calls] == [{"max_tokens": 123}, {"max_tokens": 456}]
    assert usage()["requests"] == 2


@pytest.mark.asyncio
async def test_image_estimate_is_nonzero_and_independent_of_base64_size(client, shared_registry):
    provider = shared_registry.pick_default()
    allowance = registry_module._SHARED_IMAGE_INPUT_ESTIMATE
    estimate = 2 + 2 * allowance  # eight text characters and two image blocks
    assert save(client, {"shared_pool_daily_estimated_token_limit": estimate}).status_code == 200
    images = [VisionImage(b"x" * 100000, "image/png"), VisionImage(b"y", "image/png")]
    await provider.ainvoke_vision("12345678", images)
    assert usage()["estimated_input_tokens"] == estimate
    with pytest.raises(SharedPoolLimitError):
        await provider.ainvoke_vision("", IMAGES)
    assert usage()["requests"] == 1
    messages = [HumanMessage(content=[{"type": "text", "text": "12345678"},
                                    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "x" * 100000}},
                                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,eQ=="}}])]
    assert registry_module._shared_input_estimate(messages) == estimate


@pytest.mark.asyncio
async def test_direct_multimodal_ainvoke_uses_same_image_estimate(client, shared_registry):
    provider = shared_registry.pick_default()
    estimate = 1 + registry_module._SHARED_IMAGE_INPUT_ESTIMATE
    assert save(client, {"shared_pool_daily_estimated_token_limit": estimate}).status_code == 200
    messages = [HumanMessage(content=[{"type": "text", "text": "read"},
                                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,eA=="}}])]
    await provider.ainvoke(messages)
    assert provider._provider.calls[0][0] is messages
    assert usage()["estimated_input_tokens"] == estimate
    with pytest.raises(SharedPoolLimitError):
        await provider.ainvoke(messages)


@pytest.mark.asyncio
async def test_two_owners_and_byok_are_isolated(client, shared_registry):
    assert save(client, {"shared_pool_daily_request_limit": 1}).status_code == 200
    first = shared_registry.pick_default()
    other = registry_module._build_scoped_registry(SimpleNamespace(id="other"))
    await first.ainvoke_vision("read", IMAGES)
    with pytest.raises(SharedPoolLimitError):
        await first.ainvoke_vision("read", IMAGES)
    await other.pick_default().ainvoke_vision("read", IMAGES)
    assert usage()["requests"] == usage("other")["requests"] == 1
    assert save(client, {"shared_pool_daily_request_limit": 0}, user="teacher").status_code == 200
    upsert_provider_config("teacher", ProviderConfig(provider_type="openai", model="owned", api_key="fake-byok"),
                           master_key=settings.provider_encryption_key)
    owned = registry_module._build_scoped_registry(SimpleNamespace(id="teacher"))
    assert not owned.uses_shared_pool() and not isinstance(owned.pick_default(), _GuardedSharedProvider)
    await owned.pick_default().ainvoke_vision("read", IMAGES)
    await owned.pick_default().ainvoke([])
    assert usage()["requests"] == 1


def engine_requests():
    read = EngineReadInputV1(purpose="problems", input_mode="page_image", page_number=1,
                            content_type="image/png", payload=b"image", max_output_tokens=1024)
    locate = EngineLocateInputV1(purpose="problems", targets=["1"],
                                images=[LocatorImageV1(page_numbers=[1], payload=b"image")])
    context = RepairContextV1(purpose="problems", span_id="p0001-s0001", page_number=1,
                              region=NormalizedRegionV1(), native_text="", visual_text="", before_text="")
    repair = EngineRepairInputV1(purpose="problems", page_number=1, content_type="image/png",
                                payload=b"image", repair_context=context)
    return (("locate", locate), ("recognize", read), ("repair", repair))


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled", [False, True])
async def test_recognition_locate_read_and_repair_keep_quota_error_not_uncertain(client, shared_registry, monkeypatch, disabled):
    assert save(client, {"shared_pool_daily_request_limit": 0}).status_code == 200
    provider = shared_registry.pick_default()
    engine = LLMRecognitionEngine(provider, route_id=provider.provider_id, fingerprint="frozen")
    if disabled:
        monkeypatch.setattr(settings, "shared_pool_enabled", False)
    expected = "shared_pool_disabled" if disabled else "shared_pool_daily_limit_reached"
    for method, request in engine_requests():
        with pytest.raises(RecognitionError) as error:
            await getattr(engine, method)(request)
        assert error.value.code == expected and not error.value.submission_may_exist
        assert classify_background_error(error.value, "workflow_failed") == expected
        assert not is_retryable_background_error(expected)
    assert provider._provider.calls == [] and usage()["requests"] == 0


@pytest.mark.asyncio
async def test_independent_locator_read_and_recheck_each_admit(client, shared_registry):
    assert save(client, {"shared_pool_daily_request_limit": 3}).status_code == 200
    provider = shared_registry.pick_default()
    engine = LLMRecognitionEngine(provider, route_id=provider.provider_id, fingerprint="frozen")
    for method, request in engine_requests():
        await getattr(engine, method)(request)
    assert len(provider._provider.calls) == 3 and usage()["requests"] == 3
    with pytest.raises(RecognitionError, match="shared_pool_daily_limit_reached"):
        await engine.recognize(engine_requests()[1][1])


@pytest.mark.asyncio
async def test_source_preflight_reaches_same_guard_and_reports_quota(client, shared_registry):
    from backend.api import task_preparation
    from backend.tests.test_task_background_workflows import _seed_task

    owner, task = _seed_task()
    assert save(client, {"shared_pool_daily_request_limit": 0}).status_code == 200
    registry = registry_module._build_scoped_registry(SimpleNamespace(id=owner))
    provider = registry.pick_default()
    png = io.BytesIO()
    Image.new("RGB", (32, 32), "white").save(png, format="PNG")
    with pytest.raises(HTTPException) as error:
        await task_preparation.preflight_problem_source(
            task_id=task, file=UploadFile(io.BytesIO(png.getvalue()), filename="source.png",
                                       headers=Headers({"content-type": "image/png"})),
            library_material_id=None, stored_file_id=None, inline_text=None,
            structure_mode="organized", role="problem", extraction_hint="", save_to_library=False,
            recognition_provider_id=provider.provider_id, current=SimpleNamespace(id=owner), registry=registry,
        )
    assert error.value.detail["code"] == "shared_pool_daily_limit_reached"
    assert "recovery" not in error.value.detail
    assert registry.get(provider.provider_id)._provider.calls == [] and usage(owner)["requests"] == 0
