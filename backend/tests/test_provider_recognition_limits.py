import asyncio
import threading
from types import SimpleNamespace

from langchain_core.messages import HumanMessage
import pytest

from backend.llm.providers import (
    AnthropicProvider, GeminiProvider, OpenAIProvider, ProviderRequestError,
    SafeRelayProvider, VisionImage, _response_metadata, _response_text,
)
from backend.models import ProviderConfig


@pytest.mark.asyncio
@pytest.mark.parametrize("cls,expected", [
    (OpenAIProvider, {"max_tokens": 1234}),
    (AnthropicProvider, {"max_tokens": 1234}),
    (GeminiProvider, {"generation_config": {"max_output_tokens": 1234}}),
])
async def test_sdk_limit_is_per_call_and_response_metadata_is_preserved(monkeypatch, cls, expected):
    provider = cls(ProviderConfig(provider_type=cls.provider_type, model="fixture", api_key="fixture"))
    monkeypatch.setattr(GeminiProvider, "_needs_proxy_mode", property(lambda self: False))
    calls = []

    async def invoke(messages, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(content="literal -2", usage_metadata={"input_tokens": 40, "output_tokens": 7},
                               response_metadata={"finish_reason": "length", "private": "DO_NOT_KEEP"})

    async def client():
        return SimpleNamespace(ainvoke=invoke)

    monkeypatch.setattr(provider, "_get_client", client)
    response = await provider.ainvoke_vision("transcribe", [VisionImage(b"fixture", "image/png")], max_output_tokens=1234)
    assert calls == [expected]
    assert response.input_tokens == 40 and response.output_tokens == 7
    assert response.finish_reason == "length"
    assert "DO_NOT_KEEP" not in repr(response)
    await provider.ainvoke([HumanMessage(content="legacy")])
    assert calls[-1] == {}


@pytest.mark.asyncio
async def test_proxy_gemini_passes_limit_to_fresh_sync_client(monkeypatch):
    provider = GeminiProvider(ProviderConfig(provider_type="gemini", model="fixture", api_key="fixture"))
    monkeypatch.setattr(GeminiProvider, "_needs_proxy_mode", property(lambda self: True))
    calls = []

    def invoke(messages, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(content="literal", usage_metadata=None, response_metadata={"finish_reason": "STOP"})

    monkeypatch.setattr(provider, "_build_client_sync", lambda: SimpleNamespace(invoke=invoke))
    result = await provider.ainvoke_vision("read", [VisionImage(b"fixture", "image/png")], max_output_tokens=512)
    assert calls == [{"generation_config": {"max_output_tokens": 512}}]
    assert result.input_tokens is None and result.finish_reason == "stop"


@pytest.mark.parametrize("protocol,field", [
    ("openai_chat_completions", "max_tokens"),
    ("anthropic_messages", "max_tokens"),
    ("gemini_generate_content", "maxOutputTokens"),
])
def test_relay_limit_uses_selected_wire_protocol(protocol, field):
    provider = SafeRelayProvider(ProviderConfig(
        provider_type="openai", model="fixture", api_key="fixture", wire_protocol=protocol,
        base_url="https://relay.example/v1",
    ))
    _, payload = provider._request_parts([HumanMessage(content="read")], max_output_tokens=321)
    assert (payload["generationConfig"] if protocol == "gemini_generate_content" else payload)[field] == 321
    assert provider.config.model == "fixture"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, -1, True, "100", 1.5, 32769])
@pytest.mark.parametrize("cls", [OpenAIProvider, GeminiProvider, SafeRelayProvider])
async def test_invalid_limits_fail_before_any_client(value, cls, monkeypatch):
    provider = cls(ProviderConfig(provider_type="openai" if cls is SafeRelayProvider else cls.provider_type,
                                  model="fixture", api_key="fixture"))

    def forbidden():
        pytest.fail("invalid budget cannot build a client")

    monkeypatch.setattr(provider, "_build_client_sync", forbidden)
    with pytest.raises(ProviderRequestError, match="provider_output_limit_invalid"):
        await provider.ainvoke_vision("read", [VisionImage(b"fixture", "image/png")], max_output_tokens=value)


@pytest.mark.parametrize("usage", [None, [], {"input_tokens": True, "output_tokens": -1}, {"input_tokens": "20"}])
def test_unknown_usage_is_not_zero(usage):
    assert _response_metadata(SimpleNamespace(usage_metadata=usage, response_metadata={})) == {
        "input_tokens": None, "output_tokens": None, "finish_reason": None,
    }


def test_arbitrary_finish_reason_is_not_preserved():
    assert _response_metadata(SimpleNamespace(response_metadata={"finish_reason": "PRIVATE_DATA"}))["finish_reason"] == "unknown"


def test_sdk_refusal_marker_is_preserved_without_its_body():
    metadata = _response_metadata(SimpleNamespace(
        response_metadata={"finish_reason": "stop"}, additional_kwargs={"refusal": "PRIVATE_REFUSAL_BODY"},
    ))
    assert metadata["finish_reason"] == "refused"
    assert "PRIVATE_REFUSAL_BODY" not in repr(metadata)


def test_installed_gemini_sdk_accepts_generation_config_limit():
    from langchain_google_genai import ChatGoogleGenerativeAI

    client = ChatGoogleGenerativeAI(model="gemini-2.0-flash", google_api_key="fixture")
    params = client._prepare_params(None, generation_config={"max_output_tokens": 137})
    assert params.max_output_tokens == 137


def test_installed_openai_sdk_preserves_per_call_limit():
    from langchain_openai import ChatOpenAI

    client = ChatOpenAI(model="fixture", api_key="fixture")
    payload = client._get_request_payload([HumanMessage(content="read")], max_tokens=137)
    assert payload.get("max_completion_tokens", payload.get("max_tokens")) == 137


def test_sdk_visible_blocks_are_faithful_and_reasoning_is_not_source_text():
    assert _response_text([
        {"type": "thinking", "thinking": "PRIVATE_REASONING"},
        {"type": "text", "text": "x = ", "extras": {"signature": "private"}},
        {"type": "text", "text": "-2"},
    ]) == "x = -2"
    with pytest.raises(ProviderRequestError, match="provider_response_invalid"):
        _response_text([{"type": "tool_use", "text": "do something"}])


@pytest.mark.asyncio
async def test_sync_gemini_cancellation_keeps_capacity_until_worker_drains(monkeypatch):
    provider = GeminiProvider(ProviderConfig(provider_type="gemini", model="fixture", api_key="fixture", max_concurrent=1))
    monkeypatch.setattr(GeminiProvider, "_needs_proxy_mode", property(lambda self: True))
    started, release = threading.Event(), threading.Event()

    def invoke(messages, **kwargs):
        started.set()
        assert release.wait(5), "test must release its sync worker"
        return SimpleNamespace(content="literal")

    monkeypatch.setattr(provider, "_build_client_sync", lambda: SimpleNamespace(invoke=invoke))
    task = asyncio.create_task(provider.ainvoke([HumanMessage(content="read")], max_output_tokens=512))
    try:
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done() and provider._semaphore.locked()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not provider._semaphore.locked()
