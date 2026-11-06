"""Fresh Gemini clients cannot turn completed workflow calls into retries."""
import asyncio
from types import SimpleNamespace

import pytest
from tenacity import wait_fixed

from backend.config import settings
from backend.llm import providers
from backend.llm.request_timeouts import current_workflow_cancellation_scope
from backend.services.execution_control import run_controlled
from backend.services.workflow_deadlines import processing_budget, run_owned_work
from backend.tests.test_provider_response_deadlines import MESSAGES, provider_for, scheduler  # noqa: F401
from backend.tools import structured_llm


async def workflow_call(awaitable, *, kind="grading"):
    async with processing_budget("cleanup-regression", kind=kind):
        return await run_owned_work(run_controlled("cleanup-regression", lambda: False, awaitable))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["question_preparation", "submission_recognition", "grading"])
@pytest.mark.parametrize("cleanup_outcome", ["error", "hang"])
async def test_completed_workflow_response_survives_client_cleanup(
    kind, cleanup_outcome, scheduler, monkeypatch,
):
    monkeypatch.setattr(providers.GeminiProvider, "_CLIENT_CLOSE_SECONDS", .02)
    monkeypatch.setattr(structured_llm._ainvoke_with_retry.retry, "wait", wait_fixed(0))
    calls, closes = [], []
    closed = asyncio.Event()

    async def invoke(options):
        calls.append(options)
        return SimpleNamespace(content="complete")

    async def close():
        closes.append(True)
        try:
            if cleanup_outcome == "hang":
                await asyncio.Event().wait()
            raise RuntimeError("synthetic cleanup failure")
        finally:
            closed.set()

    async with provider_for("gemini_proxy", invoke, monkeypatch) as provider:
        provider._build_client_sync().aclose = close
        result = await asyncio.wait_for(workflow_call(
            structured_llm.ainvoke_with_retry(provider, MESSAGES), kind=kind,
        ), 2)

    assert result.content == "complete"
    assert calls == [{}] and closes == [True] and closed.is_set()
    assert scheduler.active == scheduler.host_capacity.active == 0
    assert scheduler.proxy_slots._value == 50
    assert not current_workflow_cancellation_scope()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["failure", "timeout", "cancel"])
@pytest.mark.parametrize("cleanup_outcome", ["error", "hang"])
async def test_workflow_cleanup_preserves_original_failure(
    outcome, cleanup_outcome, scheduler, monkeypatch,
):
    monkeypatch.setattr(providers.GeminiProvider, "_CLIENT_CLOSE_SECONDS", .02)
    if outcome == "timeout":
        monkeypatch.setattr(settings, "llm_timeout", .04)
    calls = []
    started, closed = asyncio.Event(), asyncio.Event()
    original_error = providers.ProviderRequestError("provider_authentication_failed", status_code=403)

    async def invoke(options):
        calls.append(options)
        started.set()
        if outcome == "failure":
            raise original_error
        await asyncio.Event().wait()

    async def close():
        try:
            if cleanup_outcome == "hang":
                await asyncio.Event().wait()
            raise RuntimeError("synthetic cleanup failure")
        finally:
            closed.set()

    async with provider_for("gemini_proxy", invoke, monkeypatch) as provider:
        provider._build_client_sync().aclose = close
        task = asyncio.create_task(workflow_call(provider.ainvoke(MESSAGES)))
        try:
            await asyncio.wait_for(started.wait(), 2)
            if outcome == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
            else:
                with pytest.raises(providers.ProviderRequestError) as error:
                    await asyncio.wait_for(task, 2)
                if outcome == "failure":
                    assert error.value is original_error
                else:
                    assert error.value.code == "provider_timeout"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert calls == [{}] and closed.is_set()
    assert scheduler.active == scheduler.host_capacity.active == 0
    assert scheduler.proxy_slots._value == 50
    assert not current_workflow_cancellation_scope()


@pytest.mark.asyncio
async def test_cancellation_during_workflow_cleanup_keeps_closer_running(scheduler, monkeypatch):
    started, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def invoke(options):
        return SimpleNamespace(content="complete")

    async def close():
        started.set()
        try:
            await release.wait()
        finally:
            closed.set()

    async with provider_for("gemini_proxy", invoke, monkeypatch) as provider:
        provider._build_client_sync().aclose = close
        task = asyncio.create_task(workflow_call(provider.ainvoke(MESSAGES)))
        try:
            await asyncio.wait_for(started.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            assert not closed.is_set()
        finally:
            release.set()
            await asyncio.wait_for(closed.wait(), 2)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert scheduler.active == scheduler.host_capacity.active == 0
    assert scheduler.proxy_slots._value == 50
    assert not current_workflow_cancellation_scope()


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["openai", "anthropic", "gemini",
                                  "relay_openai_chat_completions", "relay_anthropic_messages",
                                  "relay_gemini_generate_content"])
@pytest.mark.parametrize("outcome", ["success", "failure", "timeout", "cancel"])
async def test_other_workflow_transports_keep_their_outcomes(route, outcome, scheduler, monkeypatch):
    if outcome == "timeout":
        monkeypatch.setattr(settings, "llm_timeout", .04)
    calls = []
    started = asyncio.Event()
    original_error = providers.ProviderRequestError("provider_authentication_failed", status_code=403)

    async def invoke(options):
        calls.append(options)
        started.set()
        if outcome == "failure":
            raise original_error
        if outcome in {"timeout", "cancel"}:
            await asyncio.Event().wait()
        return SimpleNamespace(content="complete")

    async with provider_for(route, invoke, monkeypatch) as provider:
        if not route.startswith("relay_"):
            async def unexpected_close():
                pytest.fail("workflow closed a shared client")
            provider._client.aclose = unexpected_close
        task = asyncio.create_task(workflow_call(provider.ainvoke(MESSAGES)))
        try:
            await asyncio.wait_for(started.wait(), 2)
            if outcome == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
            elif outcome == "success":
                assert (await asyncio.wait_for(task, 2)).content == "complete"
            else:
                with pytest.raises(providers.ProviderRequestError) as error:
                    await asyncio.wait_for(task, 2)
                if outcome == "failure":
                    assert error.value is original_error
                else:
                    assert error.value.code == "provider_timeout"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert len(calls) == 1
    assert scheduler.active == scheduler.host_capacity.active == 0
    assert scheduler.proxy_slots._value == 50
    assert not current_workflow_cancellation_scope()
