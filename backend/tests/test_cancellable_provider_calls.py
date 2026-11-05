"""Durable/HTTP deadlines never dispatch paid Gemini calls into sync threads."""
import asyncio
from contextlib import contextmanager
import threading
from types import SimpleNamespace

from fastapi import HTTPException
import httpx
import pytest
from tenacity import wait_fixed

from backend.api.request_deadlines import request_deadline
from backend.domain.errors import RecognitionError
from backend.llm import providers, request_timeouts
from backend.llm.request_timeouts import (
    cancellable_provider_calls, cancellable_provider_scope, response_timeout_scope,
)
from backend.services import submission_work_budget as clocks
from backend.tests.test_provider_response_deadlines import MESSAGES, provider_for, scheduler  # noqa: F401
from backend.tools import structured_llm


@contextmanager
def work_budget():
    budget = clocks.SubmissionWorkBudget("durable", limit_seconds=1)
    token = clocks._current.set(budget)
    try:
        yield budget
    finally:
        budget.close()
        clocks._current.reset(token)


def forbidden_sync(*args, **kwargs):
    pytest.fail("a bounded caller must never start a synchronous paid SDK call")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["budget", "http"])
@pytest.mark.parametrize("outcome", ["timeout", "cancel", "success"])
async def test_bounded_gemini_uses_async_closes_client_and_releases_capacity(mode, outcome, scheduler, monkeypatch):
    monkeypatch.setattr(providers.settings, "llm_timeout", 0.02 if mode == "budget" and outcome == "timeout" else 1)
    started, closed, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []

    async def invoke(options):
        calls.append(options)
        started.set()
        try:
            if outcome != "success":
                await asyncio.Event().wait()
            return SimpleNamespace(content="complete")
        finally:
            cancelled.set()

    async def close():
        closed.set()

    async with provider_for("gemini_proxy", invoke, monkeypatch) as provider:
        client = provider._build_client_sync()
        client.invoke, client.aclose = forbidden_sync, close

        async def work():
            if mode == "http":
                @request_deadline(0.02 if outcome == "timeout" else 1)
                async def endpoint():
                    return await provider.ainvoke(MESSAGES)
                return await endpoint()
            with work_budget() as budget:
                result = await provider.ainvoke(MESSAGES)
                assert budget.active == 0
                return result

        task = asyncio.create_task(work())
        if outcome == "cancel":
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
        elif outcome == "timeout":
            expected = HTTPException if mode == "http" else providers.ProviderRequestError
            with pytest.raises(expected) as raised:
                await asyncio.wait_for(task, 1)
            if mode == "http":
                assert raised.value.status_code == 504
                assert raised.value.detail == {"code": "request_timeout"}
            else:
                assert raised.value.code == "provider_timeout"
                assert raised.value.retryable is False and raised.value.submission_may_exist
        else:
            assert (await task).content == "complete"
    assert len(calls) == 1 and calls[0]["timeout"] == providers.settings.llm_timeout
    assert cancelled.is_set() and closed.is_set()
    assert scheduler.active == scheduler.host_capacity.active == 0
    assert scheduler.proxy_slots._value == 50
    assert not cancellable_provider_calls()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["deadline", "unknown", "generic"])
async def test_budget_unknown_paid_request_is_not_automatically_replayed(failure, scheduler, monkeypatch):
    monkeypatch.setattr(providers.settings, "llm_timeout", 0.02)
    monkeypatch.setattr(structured_llm._ainvoke_with_retry.retry, "wait", wait_fixed(0))
    calls = []

    async def invoke(options):
        calls.append(options)
        if failure == "unknown":
            raise httpx.ReadTimeout("unknown upstream outcome")
        if failure == "generic":
            raise RuntimeError("PRIVATE_SDK_ERROR")
        await asyncio.Event().wait()

    async with provider_for("gemini_proxy", invoke, monkeypatch) as provider:
        provider._build_client_sync().invoke = forbidden_sync
        with work_budget():
            with pytest.raises(structured_llm.PermanentLLMError) as raised:
                await structured_llm.ainvoke_with_retry(provider, MESSAGES)
    assert len(calls) == 1
    assert raised.value.__cause__.submission_may_exist
    assert raised.value.__cause__.retryable is False
    assert str(raised.value) == ("provider_submit_uncertain" if failure == "generic" else "provider_timeout")
    if failure != "generic":
        from backend.skills.base import classify_skill_error
        assert "超时" in classify_skill_error(raised.value)[1]
    assert scheduler.active == scheduler.host_capacity.active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("point", ["prepare", "breaker", "capacity", "proxy_slots"])
async def test_budget_exhaustion_wakes_every_provider_admission_without_charging_queue(point, scheduler, monkeypatch):
    calls = []

    async def blocked(*args, **kwargs):
        await asyncio.Event().wait()

    async def invoke(options):
        calls.append(options)
        return SimpleNamespace(content="unexpected")

    route = "gemini_proxy" if point == "proxy_slots" else "openai"
    async with provider_for(route, invoke, monkeypatch) as provider:
        if point == "prepare":
            monkeypatch.setattr(provider, "_get_client", blocked)
        elif point == "breaker":
            monkeypatch.setattr(provider._endpoint_breaker(), "before_call", blocked)
        elif point == "capacity":
            scheduler.host_capacity.capacity = 0
        else:
            scheduler.proxy_slots = asyncio.Semaphore(0)
        with work_budget() as budget:
            task = asyncio.create_task(provider.ainvoke(MESSAGES))
            try:
                await asyncio.sleep(0.03)
                assert budget.waiters and budget.active == 0 and budget.elapsed == 0
                budget.expire()
                with pytest.raises(RecognitionError, match="submission_time_limit_reached"):
                    await asyncio.wait_for(task, 1)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    assert calls == []
    assert scheduler.active == scheduler.host_capacity.active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["openai", "gemini_proxy"])
async def test_admission_is_independent_of_paid_response_limit(route, scheduler, monkeypatch):
    monkeypatch.setattr(providers.settings, "llm_timeout", 0.2)
    monkeypatch.setattr(providers, "_LOCAL_ADMISSION_SECONDS", 0.2)
    scheduler.host_capacity.capacity = 0
    calls = []

    async def invoke(options):
        calls.append(options)
        await asyncio.sleep(0.002)
        return SimpleNamespace(content="complete")

    async with provider_for(route, invoke, monkeypatch) as provider:
        with work_budget() as budget, response_timeout_scope(0.015):
            task = asyncio.create_task(provider.ainvoke(MESSAGES))
            try:
                await asyncio.sleep(0.04)
                assert not task.done() and not calls
                assert budget.elapsed == 0 and budget.active == 0
                scheduler.host_capacity.capacity = 50
                scheduler.wake.set()
                assert (await asyncio.wait_for(task, 1)).content == "complete"
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            assert 0 < budget.elapsed < 0.03
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("point", ["prepare", "breaker", "capacity", "proxy_slots"])
async def test_fallback_admission_has_its_own_local_cap(point, scheduler, monkeypatch):
    monkeypatch.setattr(providers, "_LOCAL_ADMISSION_SECONDS", 0.02)
    monkeypatch.setattr(providers.settings, "llm_timeout", 5)

    async def blocked(*args, **kwargs):
        await asyncio.Event().wait()

    async with provider_for("gemini_proxy" if point == "proxy_slots" else "openai", blocked, monkeypatch) as provider:
        if point == "prepare":
            monkeypatch.setattr(provider, "_get_client", blocked)
        elif point == "breaker":
            monkeypatch.setattr(provider._endpoint_breaker(), "before_call", blocked)
        elif point == "capacity":
            scheduler.host_capacity.capacity = 0
        else:
            scheduler.proxy_slots = asyncio.Semaphore(0)
        with work_budget() as budget:
            with pytest.raises(providers.ProviderRequestError, match="provider_capacity_unavailable") as raised:
                await asyncio.wait_for(provider.ainvoke(MESSAGES), 1)
            assert raised.value.retryable is False
            assert budget.elapsed == 0 and not budget.exhausted.is_set()
    assert scheduler.active == scheduler.host_capacity.active == 0


@pytest.mark.asyncio
async def test_fresh_client_close_is_bounded_without_response_scope(scheduler, monkeypatch):
    monkeypatch.setattr(request_timeouts, "_LOCAL_STEP_SECONDS", 0.02)
    closed = asyncio.Event()

    async def invoke(options):
        return SimpleNamespace(content="complete")

    async def close():
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    async with provider_for("gemini_proxy", invoke, monkeypatch) as provider:
        client = provider._build_client_sync()
        client.invoke, client.aclose = forbidden_sync, close
        with cancellable_provider_scope():
            assert (await asyncio.wait_for(provider.ainvoke(MESSAGES), 1)).content == "complete"
    assert closed.is_set()
    assert scheduler.active == scheduler.host_capacity.active == 0
    assert scheduler.proxy_slots._value == 50


@pytest.mark.asyncio
async def test_unpaid_client_build_can_cancel_without_starting_sync_request(scheduler, monkeypatch):
    started, release = threading.Event(), threading.Event()

    def build():
        started.set()
        assert release.wait(2), "test must release client construction"
        return SimpleNamespace(invoke=forbidden_sync)

    async with provider_for("gemini_proxy", forbidden_sync, monkeypatch) as provider:
        monkeypatch.setattr(provider, "_build_client_sync", build)
        with cancellable_provider_scope():
            task = asyncio.create_task(provider.ainvoke(MESSAGES))
        try:
            async with asyncio.timeout(1):
                while not started.is_set():
                    await asyncio.sleep(0.001)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            assert scheduler.active == scheduler.host_capacity.active == 0
            assert scheduler.proxy_slots._value == 50
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_budget_per_call_override_does_not_modify_other_calls(scheduler, monkeypatch):
    monkeypatch.setattr(providers.settings, "llm_timeout", 1)
    seen = []

    async def invoke(options):
        seen.append(options["timeout"])
        return SimpleNamespace(content="complete")

    async with provider_for("gemini_proxy", invoke, monkeypatch) as provider:
        with work_budget():
            with response_timeout_scope(0.03):
                await provider.ainvoke(MESSAGES)
            await provider.ainvoke(MESSAGES)
    assert seen == [0.03, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("scoped", [False, True])
async def test_swallowed_response_cancellation_does_not_report_success(scoped, scheduler, monkeypatch):
    monkeypatch.setattr(providers.settings, "llm_timeout", 0.02)

    async def invoke(options):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return SimpleNamespace(content="not a successful response")

    async with provider_for("openai", invoke, monkeypatch) as provider:
        with work_budget(), response_timeout_scope(0.02, response_only=scoped) as state:
            with pytest.raises(providers.ProviderRequestError, match="provider_timeout"):
                await provider.ainvoke(MESSAGES)
            if state:
                assert state.submission_may_exist


@pytest.mark.asyncio
async def test_budget_known_rejection_can_retry_without_replaying_unknown_work(scheduler, monkeypatch):
    monkeypatch.setattr(structured_llm._ainvoke_with_retry.retry, "wait", wait_fixed(0))
    calls = []

    async def invoke(options):
        calls.append(options)
        if len(calls) == 1:
            raise providers.ProviderRequestError("provider_rate_limited", status_code=429)
        return SimpleNamespace(content="complete")

    async with provider_for("gemini_proxy", invoke, monkeypatch) as provider:
        provider._build_client_sync().invoke = forbidden_sync
        with work_budget():
            assert (await structured_llm.ainvoke_with_retry(provider, MESSAGES)).content == "complete"
    assert len(calls) == 2
    assert scheduler.active == scheduler.host_capacity.active == 0


@pytest.mark.asyncio
async def test_active_budget_expiry_drains_started_response_then_blocks_new_call(scheduler, monkeypatch):
    calls = []
    started, release = asyncio.Event(), asyncio.Event()

    async def invoke(options):
        calls.append(options)
        started.set()
        await release.wait()
        return SimpleNamespace(content="complete")

    async with provider_for("gemini_proxy", invoke, monkeypatch) as provider:
        provider._build_client_sync().invoke = forbidden_sync
        with work_budget() as budget:
            task = asyncio.create_task(provider.ainvoke(MESSAGES))
            try:
                await asyncio.wait_for(started.wait(), 1)
                budget.expire()
                await asyncio.sleep(0)
                assert not task.done()
                release.set()
                assert (await asyncio.wait_for(task, 1)).content == "complete"
                with pytest.raises(RecognitionError, match="submission_time_limit_reached"):
                    await provider.ainvoke(MESSAGES)
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
    assert len(calls) == 1
    assert scheduler.active == scheduler.host_capacity.active == 0
    assert scheduler.proxy_slots._value == 50


@pytest.mark.asyncio
async def test_http_context_does_not_leak_to_concurrent_or_later_calls():
    entered, release = asyncio.Event(), asyncio.Event()

    @request_deadline(1)
    async def endpoint():
        assert cancellable_provider_calls()
        entered.set()
        await release.wait()
        with cancellable_provider_scope():
            assert cancellable_provider_calls()
        assert cancellable_provider_calls()

    task = asyncio.create_task(endpoint())
    try:
        await entered.wait()
        assert not cancellable_provider_calls()
    finally:
        release.set()
        await task
    assert not cancellable_provider_calls()
