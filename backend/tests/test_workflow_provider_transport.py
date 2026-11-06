"""Workflow cancellation selects transport without changing retry policy."""
import asyncio
from types import SimpleNamespace

import pytest
from tenacity import wait_fixed

from backend.llm import providers
from backend.tests.test_workflow_gemini_cleanup import workflow_call
from backend.tests.test_provider_response_deadlines import MESSAGES, provider_for, scheduler, turns  # noqa: F401
from backend.tools import structured_llm


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "cancel"])
async def test_workflow_gemini_uses_existing_async_transport(outcome, scheduler, monkeypatch):
    started, closed = asyncio.Event(), asyncio.Event()
    calls = []
    async def invoke(options):
        calls.append(options)
        started.set()
        if outcome == "cancel":
            await asyncio.Event().wait()
        return SimpleNamespace(content="complete")
    async def close():
        closed.set()
    def forbidden(*args, **kwargs):
        pytest.fail("workflow used uncancellable sync model request")
    async with provider_for("gemini_proxy", invoke, monkeypatch) as provider:
        client = provider._build_client_sync()
        client.invoke, client.aclose = forbidden, close
        async def work():
            return await workflow_call(provider.ainvoke(MESSAGES))
        task = asyncio.create_task(work())
        if outcome == "cancel":
            await asyncio.wait_for(started.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert (await task).content == "complete"
    await asyncio.wait_for(closed.wait(), 2)
    await turns()
    assert calls == [{}]  # Keep the legacy request arguments and SDK defaults.
    assert closed.is_set()
    assert scheduler.active == scheduler.host_capacity.active == 0
    assert scheduler.proxy_slots._value == 50


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["runtime", "500", "503"])
@pytest.mark.parametrize("route", ["gemini_proxy", "openai", "anthropic", "gemini",
                                  "relay_openai_chat_completions", "relay_anthropic_messages",
                                  "relay_gemini_generate_content"])
async def test_workflow_transport_preserves_ordinary_error_retries(failure, route, scheduler, monkeypatch):
    calls = []
    monkeypatch.setattr(structured_llm._ainvoke_with_retry.retry, "wait", wait_fixed(0))
    async def invoke(options):
        calls.append(options)
        if len(calls) == 1:
            if failure == "runtime":
                raise RuntimeError("temporary SDK failure")
            raise providers.ProviderRequestError("provider_upstream_unavailable", status_code=int(failure))
        return SimpleNamespace(content="recovered")
    async with provider_for(route, invoke, monkeypatch) as provider:
        result = await workflow_call(structured_llm.ainvoke_with_retry(provider, MESSAGES))
    assert result.content == "recovered"
    assert len(calls) == 2
    assert scheduler.active == scheduler.host_capacity.active == 0
