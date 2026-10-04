"""Exercise the installed SDKs at the HTTP boundary, without real credentials."""
import json

import httpx
import httpx2
import pytest
from langchain_core.messages import HumanMessage

from backend.config import settings
from backend.llm.providers import (
    PROVIDER_CLASSES, ProviderRequestError, SafeRelayProvider, VisionImage,
    _assemble_chat_stream,
)
from backend.models import ProviderConfig


def chat_stream():
    chunks = [
        {"choices": [{"index": 0, "delta": {"reasoning_content": "PRIVATE_THOUGHT"}}]},
        {"choices": [{"index": 0, "delta": {"content": "OK"}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 2, "total_tokens": 14}},
    ]
    return "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_type,model", [
    ("openai", "gpt-6-luna"), ("openai", "gpt-6.1-sol"),
    ("openai", "gpt-6-astra"), ("openai", "gpt-5"),
    ("openai", "gpt-4o"), ("gemini", "gemini-3.5-flash-lite"),
    ("anthropic", "claude-opus-4-7"), ("anthropic", "claude-sonnet-4-20250514"),
    ("anthropic", "claude-sonnet-5-5"),
    ("deepseek", "deepseek-flash"), ("zhipu", "glm-5.2"),
    ("moonshot", "kimi-k3"), ("moonshot", "kimi-k2.5"),
    ("qwen", "qwen-plus"), ("qwen", "qwen3-32b"),
])
@pytest.mark.parametrize("vision", [False, True])
async def test_actual_sdk_wire_defaults(monkeypatch, provider_type, model, vision):
    monkeypatch.setattr(settings, "http_proxy", "")
    monkeypatch.setattr(settings, "https_proxy", "")
    sent = []

    async def send(client, request, **kwargs):
        body = json.loads(request.content)
        sent.append((str(request.url), body))
        if provider_type == "gemini":
            response = {"candidates": [{"content": {"role": "model", "parts": [{"text": "OK"}]}, "finishReason": "STOP"}], "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 2, "totalTokenCount": 14}}
        elif provider_type == "anthropic":
            response = {"id": "msg_fixture", "type": "message", "role": "assistant", "model": model, "content": [{"type": "text", "text": "OK"}], "stop_reason": "end_turn", "usage": {"input_tokens": 12, "output_tokens": 2}}
        elif provider_type == "qwen":
            return httpx.Response(200, request=request, text=chat_stream(), headers={"content-type": "text/event-stream"})
        else:
            response = {"id": "chatcmpl_fixture", "object": "chat.completion", "created": 1, "model": model, "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 12, "completion_tokens": 2, "total_tokens": 14}}
        response_class = httpx2.Response if provider_type == "anthropic" else httpx.Response
        return response_class(200, request=request, json=response)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    monkeypatch.setattr(httpx2.AsyncClient, "send", send)
    provider = PROVIDER_CLASSES[provider_type](ProviderConfig(provider_type=provider_type, model=model, api_key="fixture-key"))
    response = (await provider.ainvoke_vision("read", [VisionImage(b"fixture", "image/png")], max_output_tokens=512)
                if vision else await provider.ainvoke([HumanMessage(content="Reply OK")], max_output_tokens=512))
    assert response.content == "OK"
    assert len(sent) == 1
    url, body = sent[0]
    params = body.get("generationConfig", body)
    assert params.get("temperature") is None
    assert "reasoning_effort" not in body and "thinking" not in body
    field = "maxOutputTokens" if provider_type == "gemini" else "max_completion_tokens" if provider_type == "openai" else "max_tokens"
    assert params[field] == 512
    if provider_type not in {"gemini", "anthropic"}:
        assert url.endswith("/chat/completions")
        assert ("max_tokens" in body) != ("max_completion_tokens" in body)
    if vision:
        assert "image" in json.dumps(body)
    if provider_type == "qwen":
        assert body["stream"] is True
        assert response.input_tokens == 12


@pytest.mark.parametrize("provider_type", list(PROVIDER_CLASSES))
def test_relays_use_same_sampling_defaults_and_vendor_limits(provider_type):
    provider = SafeRelayProvider(ProviderConfig(provider_type=provider_type, model="fixture", api_key="fixture", base_url="https://relay.example/v1"))
    _, body = provider._request_parts([HumanMessage(content="OK")], max_output_tokens=512)
    params = body.get("generationConfig", body)
    assert "temperature" not in params
    field = "maxOutputTokens" if provider_type == "gemini" else "max_completion_tokens" if provider_type == "openai" else "max_tokens"
    assert params[field] == 512
    assert body.get("stream", False) == (provider_type == "qwen")


def test_qwen_stream_keeps_answer_usage_and_ignores_reasoning():
    result = _assemble_chat_stream(chat_stream())
    assert result["choices"][0]["message"]["content"] == "OK"
    assert result["usage"]["completion_tokens"] == 2
    assert "PRIVATE_THOUGHT" not in json.dumps(result)


@pytest.mark.parametrize("body", ["data: [DONE]\n\n", "data: invalid\n\n", 'data: {"error":{"message":"PRIVATE"}}\n\n', chat_stream().replace("data: [DONE]\n\n", "")])
def test_bad_or_interrupted_stream_is_not_accepted(body):
    with pytest.raises(ProviderRequestError, match="provider_response_invalid"):
        _assemble_chat_stream(body)


def test_gemini_relay_does_not_include_thoughts_in_recognized_text():
    provider = SafeRelayProvider(ProviderConfig(provider_type="gemini", model="gemini-3.5-flash-lite", api_key="fixture", base_url="https://relay.example"))
    response = provider._parse_response({"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "PRIVATE", "thought": True}, {"text": "OK"}]}}]}, 1)
    assert response.content == "OK"
