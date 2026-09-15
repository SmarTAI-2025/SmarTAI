from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from openai import AuthenticationError, RateLimitError

from backend.llm import providers as provider_module
from backend.llm.providers import VisionImage, build_provider
from backend.models import ProviderConfig


OPENAI_COMPATIBLE_TYPES = ["openai", "zhipu", "deepseek", "moonshot", "qwen"]


def _response_body() -> dict:
    return {
        "id": "resp_mock",
        "object": "response",
        "created_at": 1,
        "status": "completed",
        "model": "gpt-6-astra",
        "error": None,
        "incomplete_details": None,
        "output": [
            {
                "id": "rs_mock",
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "Not answer text."}],
            },
            {
                "id": "msg_mock",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {"type": "output_text", "text": '{"grade":', "annotations": []},
                    {"type": "output_text", "text": "5}", "annotations": []},
                ],
            },
        ],
        "usage": {
            "input_tokens": 11,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 7,
            "output_tokens_details": {"reasoning_tokens": 2},
            "total_tokens": 18,
        },
    }


def _chat_body() -> dict:
    return {
        "id": "chatcmpl_mock",
        "object": "chat.completion",
        "created": 1,
        "model": "compatible-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Chat answer."},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
    }


@pytest.fixture
def mock_upstream(monkeypatch):
    """Exercise the installed SDK serialization with no network or real keys."""
    clients = []

    def install(body: dict, status_code: int = 200):
        requests = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append((request.url, json.loads(request.content)))
            return httpx.Response(status_code, json=body)

        sync_client = httpx.Client(transport=httpx.MockTransport(handle), trust_env=False)
        async_client = httpx.AsyncClient(transport=httpx.MockTransport(handle), trust_env=False)
        clients.append((sync_client, async_client))
        monkeypatch.setattr(
            provider_module,
            "_build_httpx_clients",
            lambda proxy_url: (sync_client, async_client),
        )
        return requests

    yield install

    for sync_client, async_client in clients:
        sync_client.close()
        asyncio.run(async_client.aclose())


def _config(provider_type="openai", **overrides) -> ProviderConfig:
    return ProviderConfig(
        provider_type=provider_type,
        api_key="test-not-a-real-key",
        **{
            "model": "gpt-6-astra",
            "base_url": "https://relay.example",
            "wire_protocol": "openai_responses",
            "reasoning_effort": "high",
            **overrides,
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_type", OPENAI_COMPATIBLE_TYPES)
async def test_shared_responses_request_and_answer_contract(mock_upstream, provider_type):
    requests = mock_upstream(_response_body())
    provider = build_provider(_config(provider_type))

    result = await provider.ainvoke(
        [SystemMessage(content="Return JSON."), HumanMessage(content="Grade synthetic work.")]
    )

    assert len(requests) == 1
    url, payload = requests[0]
    assert str(url) == "https://relay.example/responses"
    assert payload == {
        "model": "gpt-6-astra",
        "input": [
            {"role": "system", "content": "Return JSON."},
            {"role": "user", "content": "Grade synthetic work."},
        ],
        "reasoning": {"effort": "high"},
        "store": False,
        "stream": False,
    }
    assert result.content == '{"grade":5}'
    assert isinstance(result.content, str)
    assert result.input_tokens == 11
    assert result.output_tokens == 7
    assert result.provider == f"{provider_type}:gpt-6-astra"


@pytest.mark.asyncio
@pytest.mark.parametrize("base_url", ["https://relay.example/v1", "https://relay.example/v1/"])
async def test_responses_preserves_configured_base_path(mock_upstream, base_url):
    requests = mock_upstream(_response_body())

    await build_provider(_config(base_url=base_url, reasoning_effort=None)).ainvoke(
        [HumanMessage(content="Synthetic request.")]
    )

    assert str(requests[0][0]) == "https://relay.example/v1/responses"
    assert "reasoning" not in requests[0][1]
    assert "temperature" not in requests[0][1]


@pytest.mark.asyncio
async def test_responses_vision_uses_sdk_image_conversion(mock_upstream):
    requests = mock_upstream(_response_body())
    data = b"synthetic image fixture"

    result = await build_provider(_config()).ainvoke_vision(
        "Read synthetic work.", [VisionImage(data=data, media_type="image/png")]
    )

    assert requests[0][1]["input"] == [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "Read synthetic work."},
                {
                    "type": "input_image",
                    "image_url": f"data:image/png;base64,{base64.b64encode(data).decode('ascii')}",
                },
            ],
        }
    ]
    assert result.content == '{"grade":5}'


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_type", OPENAI_COMPATIBLE_TYPES)
async def test_shared_chat_explicitly_preserves_chat_protocol(mock_upstream, provider_type):
    requests = mock_upstream(_chat_body())

    result = await build_provider(
        _config(provider_type, wire_protocol="openai_chat_completions")
    ).ainvoke([HumanMessage(content="Synthetic request.")])

    url, payload = requests[0]
    assert url.path == "/chat/completions"
    assert payload["messages"] == [{"role": "user", "content": "Synthetic request."}]
    assert payload["reasoning_effort"] == "high"
    assert "temperature" not in payload
    assert "input" not in payload
    assert "store" not in payload
    assert result.content == "Chat answer."
    assert (result.input_tokens, result.output_tokens) == (8, 4)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_type", OPENAI_COMPATIBLE_TYPES)
async def test_legacy_chat_retains_temperature_and_payload(mock_upstream, provider_type):
    requests = mock_upstream(_chat_body())

    result = await build_provider(
        _config(provider_type, model="compatible-model", wire_protocol=None)
    ).ainvoke([HumanMessage(content="Legacy request.")])

    url, payload = requests[0]
    assert url.path == "/chat/completions"
    assert payload["temperature"] == 0.0
    assert payload["messages"] == [{"role": "user", "content": "Legacy request."}]
    assert "reasoning_effort" not in payload
    assert "store" not in payload
    assert result.content == "Chat answer."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "error_type"), [(401, AuthenticationError), (429, RateLimitError)]
)
async def test_responses_error_does_not_retry_or_fall_back(mock_upstream, status_code, error_type):
    requests = mock_upstream(
        {"error": {"message": "Synthetic failure.", "type": "api_error", "code": "test_error"}},
        status_code,
    )

    with pytest.raises(error_type):
        await build_provider(_config()).ainvoke([HumanMessage(content="Synthetic request.")])

    assert len(requests) == 1
    assert requests[0][0].path == "/responses"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["incomplete", "refusal"])
async def test_responses_does_not_return_partial_or_refused_answer(mock_upstream, failure):
    body = _response_body()
    if failure == "incomplete":
        body["status"] = "incomplete"
        body["incomplete_details"] = {"reason": "max_output_tokens"}
        error = "provider_response_incomplete"
    else:
        body["output"][1]["content"] = [{"type": "refusal", "refusal": "Synthetic refusal."}]
        error = "provider_response_empty"
    requests = mock_upstream(body)

    with pytest.raises(ValueError, match=error):
        await build_provider(_config()).ainvoke([HumanMessage(content="Synthetic request.")])

    assert len(requests) == 1
