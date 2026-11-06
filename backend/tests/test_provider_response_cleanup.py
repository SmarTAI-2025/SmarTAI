"""Real SDK/HTTP decoding with synthetic response-stream cleanup failures."""
import asyncio
import json
from contextlib import nullcontext

import httpx
import httpx2
import pytest
from tenacity import wait_fixed

from backend.llm import providers
from backend.llm.request_timeouts import response_timeout_scope
from backend.tests.test_provider_response_deadlines import scheduler  # noqa: F401
from backend.tests.test_provider_sdk_quota_chains import MESSAGES, real_provider
from backend.tools import structured_llm

ROUTES = sorted(providers.PROVIDER_CLASSES) + ["gemini_proxy",
    "relay_openai_chat_completions", "relay_anthropic_messages", "relay_gemini_generate_content",
]


def body_for(route):
    if "gemini" in route:
        return {"candidates": [{"content": {"role": "model", "parts": [{"text": "complete"}]}, "finishReason": "STOP"}]}
    if "anthropic" in route:
        return {"id": "fixture", "type": "message", "role": "assistant", "model": "synthetic-model",
                "content": [{"type": "text", "text": "complete"}], "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1}}
    return {"id": "fixture", "object": "chat.completion", "model": "synthetic-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "complete"}, "finish_reason": "stop"}]}


@pytest.fixture
def response_transport(monkeypatch):
    import langchain_anthropic.chat_models as anthropic_model
    # The fixture owns/closes its clients. Production Anthropic clients are
    # cached across model instances, so give each test its own real transport.
    for name in ("_get_default_httpx_client", "_get_default_async_httpx_client"):
        monkeypatch.setattr(anthropic_model, name, getattr(anthropic_model, name).__wrapped__)
    from google.genai._api_client import BaseApiClient
    # Explicitly exercise Google's HTTPX fallback as well as the other SDKs.
    monkeypatch.setattr(BaseApiClient, "_use_aiohttp", lambda self: False)
    state = {"route": "openai", "fault": "error", "calls": 0, "closed": [],
             "release": asyncio.Event(), "reading": asyncio.Event()}

    async def send(self, request):
        state["calls"] += 1
        module = httpx2 if isinstance(request, httpx2.Request) else httpx
        data = json.dumps(body_for(state["route"])).encode()
        if state["route"] == "qwen":
            data = b'data: ' + json.dumps({"id": "fixture", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "complete"}, "finish_reason": "stop"}]}).encode() + b'\n\ndata: [DONE]\n\n'

        class Stream(module.AsyncByteStream):
            async def __aiter__(self):
                state["reading"].set()
                if state["fault"] == "read_error":
                    yield b'{"partial":'
                    raise module.ReadError("synthetic interrupted response")
                if state["fault"] == "read_hang":
                    await state["release"].wait()
                if state["fault"] == "invalid_json":
                    yield b'{"partial":'
                    return
                yield data

            async def aclose(self):
                state["closed"].append(True)
                if state["fault"] in {"error", "read_error", "read_hang", "invalid_json"}:
                    raise RuntimeError("synthetic response-close failure")
                if state["fault"] == "self_cancel":
                    raise asyncio.CancelledError()
                if state["fault"] == "hang":
                    await state["release"].wait()

        return module.Response(200, stream=Stream(), headers={"content-type": "text/event-stream" if state["route"] == "qwen" else "application/json"})

    for module in (httpx, httpx2):
        monkeypatch.setattr(module.AsyncHTTPTransport, "handle_async_request", send)
    monkeypatch.setattr(structured_llm._ainvoke_with_retry.retry, "wait", wait_fixed(0))
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("mode", ["ordinary", "workflow", "recognition"])
@pytest.mark.parametrize("fault", ["error", "self_cancel", "hang"])
async def test_complete_answer_is_not_lost_or_replayed(route, mode, fault, response_transport, monkeypatch, scheduler):
    state = response_transport
    state.update(route=route, fault=fault)
    protocol = route.removeprefix("relay_") if route.startswith("relay_") else None
    context = response_timeout_scope(.2, response_only=True) if mode == "recognition" else nullcontext()
    try:
        kind = "deepseek" if protocol else "gemini" if route == "gemini_proxy" else route
        async with real_provider(kind, monkeypatch, protocol=protocol, proxy=route == "gemini_proxy") as provider:
            if protocol:
                # This shared fixture bypasses the pinned/DNS transport. Reuse
                # its production response-stream boundary on the injected HTTP client.
                from backend.llm.response_cleanup import protect_client
                protect_client(provider._safe_async_client)
            with context:
                work = structured_llm.ainvoke_with_retry(provider, MESSAGES)
                if mode == "workflow":
                    from backend.tests.test_workflow_gemini_cleanup import workflow_call
                    work = workflow_call(work)
                result = await asyncio.wait_for(work, 1)
            assert result.content == "complete"
            assert state["calls"] == 1
    finally:
        state["release"].set()
        for _ in range(5):
            await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["openai", "anthropic", "gemini"])
@pytest.mark.parametrize("fault", ["read_error", "invalid_json", "read_hang"])
async def test_cleanup_does_not_hide_incomplete_response_or_user_cancellation(
    route, fault, response_transport, monkeypatch, scheduler,
):
    state = response_transport
    state.update(route=route, fault=fault)
    async with real_provider(route, monkeypatch) as provider:
        async def invoke():
            with response_timeout_scope(30, response_only=True):
                return await structured_llm.ainvoke_with_rate_retry(provider, MESSAGES)
        task = asyncio.create_task(invoke())
        try:
            await asyncio.wait_for(state["reading"].wait(), 2)
            if fault == "read_hang":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(Exception):
                    await task
            assert state["calls"] == 1
        finally:
            state["release"].set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("self_cancel", [False, True])
async def test_temporary_client_cleanup_cannot_cancel_or_hold_a_success(self_cancel, scheduler, monkeypatch):
    from types import SimpleNamespace
    from backend.tests.test_provider_response_deadlines import provider_for
    from backend.llm import response_cleanup
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(providers.GeminiProvider, "_CLIENT_CLOSE_SECONDS", .01)

    async def invoke(options):
        return SimpleNamespace(content="complete")

    async def close():
        try:
            entered.set()
            if self_cancel:
                raise asyncio.CancelledError()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()  # Misbehaving closer ignores cancellation.
        finally:
            finished.set()

    async with provider_for("gemini_proxy", invoke, monkeypatch) as provider:
        provider._build_client_sync().aclose = close
        try:
            result = await asyncio.wait_for(provider.ainvoke(MESSAGES), 2)
            await asyncio.wait_for(entered.wait(), 2)
            assert result.content == "complete" and entered.is_set()
            if not self_cancel:
                assert not finished.is_set()
                assert response_cleanup._cleanup_tasks  # Late resources remain owned.
        finally:
            release.set()
            await asyncio.wait_for(finished.wait(), 2)
