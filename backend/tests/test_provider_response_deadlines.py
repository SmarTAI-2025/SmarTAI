"""Provider-bound response deadlines; every SDK/HTTP call is a local fake."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import httpx2
import pytest
from anthropic import APIConnectionError, APITimeoutError, AsyncAnthropic
from langchain_core.messages import HumanMessage
from tenacity import wait_fixed

from backend.llm import providers, request_timeouts
from backend.llm.concurrency import ModelScheduler
from backend.llm.providers import AnthropicProvider, GeminiProvider, OpenAIProvider, ProviderRequestError, SafeRelayProvider
from backend.llm.request_timeouts import current_response_scope, current_response_timeout_seconds, response_timeout_scope
from backend.models import ProviderConfig
from backend.tests.test_automatic_model_scheduling import FakeHost
from backend.tools import structured_llm


ROUTES = ["openai", "anthropic", "gemini", "gemini_proxy",
          "relay_openai_chat_completions", "relay_anthropic_messages", "relay_gemini_generate_content"]
MESSAGES = [HumanMessage(content="inspected source")]


@pytest.fixture
def scheduler(monkeypatch):
    value = ModelScheduler(host_capacity=FakeHost(), memory_reader=lambda: None)
    monkeypatch.setattr(providers, "get_scheduler", lambda: value)
    monkeypatch.setattr(providers, "_ENDPOINT_BREAKERS", {})
    return value


@asynccontextmanager
async def provider_for(route, invoke, monkeypatch):
    """Exercise real provider dispatch and protocol code with fake transports."""
    async def sdk(messages, **kwargs):
        return await invoke(kwargs)

    async def close():
        pass

    if route.startswith("relay_"):
        protocol = route.removeprefix("relay_")

        async def handle(request):
            result = await invoke(request.extensions["timeout"])
            if isinstance(result, httpx.Response):
                return result
            if protocol == "openai_chat_completions":
                payload = {"choices": [{"message": {"content": result.content}, "finish_reason": "stop"}]}
            elif protocol == "anthropic_messages":
                payload = {"content": [{"type": "text", "text": result.content}], "stop_reason": "end_turn"}
            else:
                payload = {"candidates": [{"content": {"parts": [{"text": result.content}]}, "finishReason": "STOP"}]}
            return httpx.Response(200, json=payload)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle), timeout=120) as client:
            provider = SafeRelayProvider(ProviderConfig(
                provider_type="deepseek", model="fixture", api_key="fake",
                base_url="https://relay.example/v1", wire_protocol=protocol,
            ))
            provider._safe_async_client = client
            yield provider
    else:
        kind = "gemini" if route == "gemini_proxy" else route
        cls = {"openai": OpenAIProvider, "anthropic": AnthropicProvider, "gemini": GeminiProvider}[kind]
        provider = cls(ProviderConfig(provider_type=kind, model="fixture", api_key="fake"))
        client = SimpleNamespace(ainvoke=sdk, aclose=close)
        if kind == "gemini":
            monkeypatch.setattr(GeminiProvider, "_needs_proxy_mode", property(lambda self: route == "gemini_proxy"))
        if route == "gemini_proxy":
            # Use the real AnyIO thread boundary only for local client creation.
            monkeypatch.setattr(provider, "_build_client_sync", lambda: client)
        else:
            provider._client = client
        yield provider


async def turns():
    for _ in range(30):
        await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
async def test_queue_and_guard_do_not_spend_complete_response_time(route, scheduler, monkeypatch):
    scheduler.host_capacity.capacity = 0
    events, states = [], []

    async def guard():
        events.append("guard")
        await asyncio.sleep(0.06)  # Longer than the response limit.

    async def invoke(options):
        events.append("request")
        assert options["read" if route.startswith("relay_") else "timeout"] == 0.04
        await asyncio.sleep(0.01)
        return SimpleNamespace(content="read")

    async with provider_for(route, invoke, monkeypatch) as provider:
        async def work():
            with response_timeout_scope(0.04, response_only=True, local_wait_seconds=0.5,
                                        before_send=guard, on_send=lambda: events.append("reserve")) as state:
                states.append(state)
                return await provider.ainvoke(MESSAGES)

        task = asyncio.create_task(work())
        try:
            await asyncio.sleep(0.06)  # Queue wait also exceeds response time.
            assert events == [] and not task.done()
            scheduler.host_capacity.capacity = 50
            scheduler.wake.set()
            result = await asyncio.wait_for(task, 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert result.content == "read"
    assert events == ["guard", "reserve", "request"]
    assert states[0].attempt_count == 1 and not states[0].submission_may_exist
    assert states[0].local_wait_remaining > 0.3
    assert scheduler.active == scheduler.host_capacity.active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
async def test_initial_admission_timeout_never_authorizes_or_submits(route, scheduler, monkeypatch):
    scheduler.host_capacity.capacity = 0
    events = []

    async def guard():
        events.append("guard")

    async def invoke(options):
        events.append("request")
        return SimpleNamespace(content="unexpected")

    async with provider_for(route, invoke, monkeypatch) as provider:
        with response_timeout_scope(300, response_only=True, local_wait_seconds=0.03,
                                    before_send=guard, on_send=lambda: events.append("reserve")) as state:
            with pytest.raises(ProviderRequestError, match="provider_capacity_unavailable") as raised:
                await provider.ainvoke(MESSAGES)
    assert raised.value.retryable is False and events == []
    assert not state.ever_started and not state.submission_may_exist
    assert scheduler.active == scheduler.host_capacity.active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
async def test_full_response_deadline_stops_active_chunks_without_retry(route, scheduler, monkeypatch):
    calls, chunks = [], []
    closed = asyncio.Event()

    async def invoke(options):
        calls.append(options)
        try:
            while True:
                chunks.append("keepalive")
                await asyncio.sleep(0.002)
        finally:
            closed.set()

    async with provider_for(route, invoke, monkeypatch) as provider:
        with response_timeout_scope(0.04, response_only=True) as state:
            with pytest.raises(ProviderRequestError, match="provider_timeout"):
                await asyncio.wait_for(structured_llm.ainvoke_with_rate_retry(provider, MESSAGES), 1)
    assert len(calls) == 1 and len(chunks) >= 2 and closed.is_set()
    assert state.ever_started and state.submission_may_exist and not state.last_attempt_rejected
    assert current_response_scope() is None and current_response_timeout_seconds() is None
    assert scheduler.active == scheduler.host_capacity.active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["openai", "anthropic", "gemini", "relay_openai_chat_completions"])
async def test_429_attempts_get_new_response_limits_with_one_authorization(route, scheduler, monkeypatch):
    monkeypatch.setattr(structured_llm._invoke_with_rate_retry.retry, "wait", wait_fixed(0.05))
    events = []

    async def guard():
        events.append("guard")

    async def invoke(options):
        events.append("request")
        await asyncio.sleep(0.01)
        if events.count("request") < 3:
            raise ProviderRequestError("provider_rate_limited", status_code=429)
        return SimpleNamespace(content="done")

    async with provider_for(route, invoke, monkeypatch) as provider:
        with response_timeout_scope(0.04, response_only=True, local_wait_seconds=0.2,
                                    before_send=guard, on_send=lambda: events.append("reserve")) as state:
            result = await structured_llm.ainvoke_with_rate_retry(provider, MESSAGES)
    assert result.content == "done" and state.attempt_count == 3
    assert events.count("guard") == events.count("reserve") == 1
    assert state.local_wait_remaining > 0.15  # Neither response nor retry sleep is local admission.
    assert not state.submission_may_exist and not state.last_attempt_rejected


@pytest.mark.asyncio
async def test_queue_timeout_after_429_is_known_rejection_not_unknown(scheduler, monkeypatch):
    monkeypatch.setattr(structured_llm._invoke_with_rate_retry.retry, "wait", wait_fixed(0))
    calls, reservations = [], []

    async def invoke(options):
        calls.append(options)
        scheduler.host_capacity.capacity = 0
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    async with provider_for("relay_openai_chat_completions", invoke, monkeypatch) as provider:
        with response_timeout_scope(300, response_only=True, local_wait_seconds=0.04,
                                    on_send=lambda: reservations.append(1)) as state:
            with pytest.raises(ProviderRequestError, match="provider_capacity_unavailable"):
                await structured_llm.ainvoke_with_rate_retry(provider, MESSAGES)
    assert len(calls) == len(reservations) == state.attempt_count == 1
    assert state.ever_started and state.last_attempt_rejected and not state.submission_may_exist


@pytest.mark.asyncio
@pytest.mark.parametrize(("failure", "uncertain"), [
    (httpx.ConnectTimeout, False), (httpx.ConnectError, False), (httpx.PoolTimeout, False),
    (httpx.ReadTimeout, True), (httpx.WriteTimeout, True), (httpx.ReadError, True),
])
async def test_transport_evidence_preserves_submission_certainty(failure, uncertain, scheduler, monkeypatch):
    async def invoke(options):
        error = failure("synthetic failure")
        # A previous attempt's exception context cannot prove this request was rejected.
        error.__context__ = ProviderRequestError("provider_rate_limited", status_code=429)
        raise error

    async with provider_for("relay_openai_chat_completions", invoke, monkeypatch) as provider:
        with response_timeout_scope(300, response_only=True) as state:
            with pytest.raises(ProviderRequestError):
                await provider.ainvoke(MESSAGES)
    assert state.submission_may_exist is uncertain
    assert state.last_attempt_rejected is (not uncertain)


@pytest.mark.asyncio
@pytest.mark.parametrize(("failure", "sdk_error", "uncertain"), [
    (httpx2.ConnectTimeout, APITimeoutError, False),
    (httpx2.ConnectError, APIConnectionError, False),
    (httpx2.PoolTimeout, APITimeoutError, False),
    (httpx2.ReadTimeout, APITimeoutError, True),
    (httpx2.WriteTimeout, APITimeoutError, True),
    (httpx2.ReadError, APIConnectionError, True),
])
async def test_anthropic_sdk_transport_causes_preserve_submission_certainty(
    failure, sdk_error, uncertain, scheduler, monkeypatch,
):
    calls = []

    async def handle(request):
        calls.append(request)
        assert request.extensions["timeout"] == {"connect": 300, "read": 300, "write": 300, "pool": 300}
        raise failure("synthetic transport failure", request=request)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as transport:
        async with AsyncAnthropic(api_key="fake", max_retries=0, http_client=transport) as sdk:
            async def invoke(options):
                return await sdk.messages.create(
                    model="fixture", max_tokens=8, messages=[{"role": "user", "content": "source"}],
                    timeout=options["timeout"],
                )

            async with provider_for("anthropic", invoke, monkeypatch) as provider:
                with response_timeout_scope(300, response_only=True) as state:
                    with pytest.raises(sdk_error) as caught:
                        await provider.ainvoke(MESSAGES)

    assert isinstance(caught.value.__cause__, failure)
    assert len(calls) == state.attempt_count == 1
    assert state.submission_may_exist is uncertain
    assert state.last_attempt_rejected is (not uncertain)


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "uncertain"), [(400, False), (429, False), (408, True),
                                                 (500, True), (502, True), (503, True), (504, True)])
async def test_http_error_is_checked_before_clearing_unknown(status, uncertain, scheduler, monkeypatch):
    async def invoke(options):
        return httpx.Response(status, json={"error": {"message": "synthetic response"}})

    async with provider_for("relay_openai_chat_completions", invoke, monkeypatch) as provider:
        with response_timeout_scope(300, response_only=True) as state:
            with pytest.raises(ProviderRequestError):
                await provider.ainvoke(MESSAGES)
    assert state.submission_may_exist is uncertain
    assert state.last_attempt_rejected is (not uncertain)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["openai", "gemini_proxy", "relay_openai_chat_completions"])
async def test_invalid_complete_response_keeps_original_pending_protection(route, scheduler, monkeypatch):
    async def invoke(options):
        return httpx.Response(200, json={"invalid": True}) if route.startswith("relay_") else SimpleNamespace(content=object())

    async with provider_for(route, invoke, monkeypatch) as provider:
        with response_timeout_scope(300, response_only=True) as state:
            with pytest.raises(ProviderRequestError, match="provider_response_invalid"):
                await provider.ainvoke(MESSAGES)
    assert state.submission_may_exist and not state.last_attempt_rejected


@pytest.mark.asyncio
async def test_unknown_attempt_cannot_be_reauthorized(scheduler, monkeypatch):
    events = []

    async def invoke(options):
        events.append("request")
        raise httpx.ReadTimeout("uncertain")

    async with provider_for("openai", invoke, monkeypatch) as provider:
        with response_timeout_scope(300, response_only=True, on_send=lambda: events.append("reserve")) as state:
            with pytest.raises(httpx.ReadTimeout):
                await provider.ainvoke(MESSAGES)
            with pytest.raises(ProviderRequestError, match="provider_submit_uncertain"):
                await provider.ainvoke(MESSAGES)
    assert events == ["reserve", "request"] and state.attempt_count == 1
    assert state.submission_may_exist


@pytest.mark.asyncio
async def test_guard_failure_never_reserves_or_starts_request(scheduler, monkeypatch):
    events = []

    async def guard():
        events.append("guard")
        raise RuntimeError("checkpoint write failed")

    async def invoke(options):
        events.append("request")

    async with provider_for("openai", invoke, monkeypatch) as provider:
        with response_timeout_scope(300, response_only=True, before_send=guard,
                                    on_send=lambda: events.append("reserve")) as state:
            with pytest.raises(RuntimeError, match="checkpoint write failed"):
                await provider.ainvoke(MESSAGES)
    assert events == ["guard"] and not state.ever_started
    assert not state.submission_may_exist


@pytest.mark.asyncio
@pytest.mark.parametrize("point", ["prepare", "breaker", "proxy_slots"])
async def test_all_local_provider_waits_are_bounded_before_pending(point, scheduler, monkeypatch):
    events = []

    async def blocked(*args, **kwargs):
        await asyncio.Event().wait()

    async def invoke(options):
        events.append("request")

    route = "gemini_proxy" if point == "proxy_slots" else "openai"
    async with provider_for(route, invoke, monkeypatch) as provider:
        if point == "prepare":
            monkeypatch.setattr(provider, "_get_client", blocked)
        elif point == "breaker":
            monkeypatch.setattr(provider._endpoint_breaker(), "before_call", blocked)
        else:
            scheduler.proxy_slots = asyncio.Semaphore(0)
        with response_timeout_scope(300, response_only=True, local_wait_seconds=0.03,
                                    on_send=lambda: events.append("reserve")) as state:
            with pytest.raises(ProviderRequestError, match="provider_capacity_unavailable"):
                await provider.ainvoke(MESSAGES)
    assert events == [] and not state.ever_started


@pytest.mark.asyncio
async def test_local_gates_share_one_wait_allowance():
    with response_timeout_scope(300, response_only=True, local_wait_seconds=0.06) as state:
        await state.wait_local(asyncio.sleep(0.04))
        assert state.local_wait_remaining < 0.03
        with pytest.raises(ProviderRequestError, match="provider_capacity_unavailable"):
            await state.wait_local(asyncio.Event().wait())
        assert state.local_wait_remaining == 0
        assert not state.ever_started


@pytest.mark.asyncio
async def test_progress_runs_before_guard_and_completion_is_preserved(scheduler, monkeypatch):
    events = []
    monkeypatch.setattr(request_timeouts, "_LOCAL_STEP_SECONDS", 0.02)

    async def progress(provider, state, **details):
        events.append(state)
        if state in {"responding", "processing"}:
            await asyncio.Event().wait()

    monkeypatch.setattr("backend.progress.recognition_activity.report_provider_state", progress)

    async def guard():
        events.append("guard")

    async def invoke(options):
        events.append("request")
        return SimpleNamespace(content="complete")

    async with provider_for("openai", invoke, monkeypatch) as provider:
        with response_timeout_scope(0.01, response_only=True, before_send=guard,
                                    on_send=lambda: events.append("reserve")) as state:
            result = await provider.ainvoke(MESSAGES)
    assert result.content == "complete" and not state.submission_may_exist
    assert events == ["queued", "responding", "guard", "reserve", "request", "processing"]


@pytest.mark.asyncio
async def test_external_cancel_after_complete_response_does_not_become_unknown(scheduler, monkeypatch):
    processing = asyncio.Event()
    states = []

    async def progress(provider, state, **details):
        if state == "processing":
            processing.set()
            await asyncio.Event().wait()

    monkeypatch.setattr("backend.progress.recognition_activity.report_provider_state", progress)

    async def invoke(options):
        return SimpleNamespace(content="complete")

    async with provider_for("openai", invoke, monkeypatch) as provider:
        async def work():
            with response_timeout_scope(300, response_only=True) as state:
                states.append(state)
                return await provider.ainvoke(MESSAGES)

        task = asyncio.create_task(work())
        await asyncio.wait_for(processing.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert states[0].ever_started and not states[0].submission_may_exist
    assert scheduler.active == scheduler.host_capacity.active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("response_only", [False, True])
async def test_concurrent_scopes_never_mutate_shared_relay_client(response_only, scheduler, monkeypatch):
    seen, release, started = {}, asyncio.Event(), asyncio.Event()

    async def handle(request):
        import json
        label = json.loads(request.content)["messages"][0]["content"]
        seen[label] = request.extensions["timeout"].copy()
        if label == "ocr":
            started.set()
            await release.wait()
        return httpx.Response(200, json={"choices": [{"message": {"content": "complete"}, "finish_reason": "stop"}]})

    original = httpx.Timeout(60, connect=15)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), timeout=original) as client:
        provider = SafeRelayProvider(ProviderConfig(provider_type="deepseek", model="fixture", api_key="fake",
                                                    base_url="https://relay.example/v1"))
        provider._safe_async_client = client

        async def work():
            with response_timeout_scope(300, response_only=response_only):
                return await provider.ainvoke([HumanMessage(content="ocr")])

        task = asyncio.create_task(work())
        try:
            await asyncio.wait_for(started.wait(), 1)
            await provider.ainvoke([HumanMessage(content="old")])
        finally:
            release.set()
            await task
        assert client.timeout == original
    assert seen["ocr"] == {"connect": 15, "read": 300, "write": 300, "pool": 300}
    assert seen["old"] == original.as_dict()


@pytest.mark.asyncio
async def test_legacy_native_scope_keeps_existing_sdk_behavior(scheduler, monkeypatch):
    seen = []

    async def invoke(options):
        seen.append(options)
        await asyncio.sleep(0.03)
        return SimpleNamespace(content="old")

    async with provider_for("openai", invoke, monkeypatch) as provider:
        with response_timeout_scope(0.01):
            result = await provider.ainvoke(MESSAGES)
    assert result.content == "old" and "timeout" not in seen[0]
    assert current_response_scope() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("point", ["check", "waiting"])
async def test_retry_control_io_is_local_and_bounded(point, scheduler, monkeypatch):
    monkeypatch.setattr(request_timeouts, "_LOCAL_STEP_SECONDS", 0.02)
    monkeypatch.setattr(structured_llm._invoke_with_rate_retry.retry, "wait", wait_fixed(0))
    calls = []

    async def check(provider):
        if point == "check":
            await asyncio.Event().wait()

    async def waiting(*args):
        await asyncio.Event().wait()

    control = SimpleNamespace(check=check, waiting=waiting, rate_failures={}, block=lambda *args: None)
    monkeypatch.setattr("backend.services.execution_control.current_execution", lambda: control)

    async def invoke(options):
        calls.append(options)
        raise ProviderRequestError("provider_rate_limited", status_code=429)

    async with provider_for("openai", invoke, monkeypatch) as provider:
        with response_timeout_scope(300, response_only=True) as state:
            with pytest.raises(ProviderRequestError, match="provider_capacity_unavailable"):
                await structured_llm.ainvoke_with_rate_retry(provider, MESSAGES)
    assert not state.submission_may_exist
    assert len(calls) == state.attempt_count == (1 if point == "waiting" else 0)
    assert state.last_attempt_rejected is (point == "waiting")


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "timeout", "cancel"])
async def test_new_gemini_proxy_closes_fresh_client_on_all_outcomes(outcome, scheduler, monkeypatch):
    started, closed = asyncio.Event(), asyncio.Event()
    states = []

    async def invoke(options):
        started.set()
        if outcome != "success":
            await asyncio.Event().wait()
        return SimpleNamespace(content="complete")

    async def close():
        closed.set()

    async with provider_for("gemini_proxy", invoke, monkeypatch) as provider:
        provider._build_client_sync().aclose = close

        async def work():
            with response_timeout_scope(0.03 if outcome == "timeout" else 300, response_only=True) as state:
                states.append(state)
                return await provider.ainvoke(MESSAGES)

        task = asyncio.create_task(work())
        if outcome == "cancel":
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif outcome == "timeout":
            with pytest.raises(ProviderRequestError, match="provider_timeout"):
                await task
        else:
            assert (await task).content == "complete"
    assert closed.is_set()
    assert states[0].submission_may_exist is (outcome != "success")
    assert scheduler.active == scheduler.host_capacity.active == 0
    assert scheduler.proxy_slots._value == 50


@pytest.mark.asyncio
async def test_cancel_during_new_gemini_close_preserves_cancel_and_finishes_cleanup(scheduler, monkeypatch):
    close_started, release_close, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    states = []

    async def invoke(options):
        return SimpleNamespace(content="complete")

    async def close():
        close_started.set()
        await release_close.wait()
        closed.set()

    async with provider_for("gemini_proxy", invoke, monkeypatch) as provider:
        provider._build_client_sync().aclose = close

        async def work():
            with response_timeout_scope(300, response_only=True) as state:
                states.append(state)
                return await provider.ainvoke(MESSAGES)

        task = asyncio.create_task(work())
        try:
            await asyncio.wait_for(close_started.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not states[0].submission_may_exist
        finally:
            release_close.set()
            await asyncio.wait_for(closed.wait(), 1)
            await turns()
    assert scheduler.active == scheduler.host_capacity.active == 0
    assert scheduler.proxy_slots._value == 50


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["hang", "error"])
async def test_new_gemini_close_failure_does_not_discard_response(failure, scheduler, monkeypatch):
    monkeypatch.setattr(request_timeouts, "_LOCAL_STEP_SECONDS", 0.02)
    closed = asyncio.Event()

    async def invoke(options):
        return SimpleNamespace(content="complete")

    async def close():
        try:
            if failure == "hang":
                await asyncio.Event().wait()
            raise RuntimeError("synthetic cleanup failure")
        finally:
            closed.set()

    async with provider_for("gemini_proxy", invoke, monkeypatch) as provider:
        provider._build_client_sync().aclose = close
        with response_timeout_scope(300, response_only=True) as state:
            result = await provider.ainvoke(MESSAGES)
    assert result.content == "complete" and not state.submission_may_exist
    assert closed.is_set()


@pytest.mark.asyncio
async def test_legacy_gemini_proxy_keeps_sync_thread_and_cleanup_policy(scheduler, monkeypatch):
    calls = []

    async def forbidden(options):
        pytest.fail("legacy proxy unexpectedly used async SDK")

    async def close():
        pytest.fail("legacy proxy unexpectedly used new cleanup")

    def invoke(messages, **options):
        calls.append(options)
        return SimpleNamespace(content="legacy")

    async with provider_for("gemini_proxy", forbidden, monkeypatch) as provider:
        client = provider._build_client_sync()
        client.invoke, client.aclose = invoke, close
        with response_timeout_scope(300):
            result = await provider.ainvoke(MESSAGES)
    assert result.content == "legacy" and calls == [{}]
    assert scheduler.active == scheduler.host_capacity.active == 0


@pytest.mark.asyncio
async def test_unknown_sdk_failure_with_old_429_context_never_enters_retry_backoff(scheduler, monkeypatch):
    calls = []

    def forbidden_wait(retry_state):
        pytest.fail("unknown request entered rate-limit backoff")

    monkeypatch.setattr(structured_llm._invoke_with_rate_retry.retry, "wait", forbidden_wait)

    async def invoke(options):
        calls.append(options)
        error = httpx.ReadTimeout("new request has unknown result")
        error.__context__ = structured_llm.RateLimitError("provider_rate_limited")
        raise error

    async with provider_for("openai", invoke, monkeypatch) as provider:
        with response_timeout_scope(300, response_only=True) as state:
            with pytest.raises(httpx.ReadTimeout):
                await structured_llm.ainvoke_with_rate_retry(provider, MESSAGES)
    assert len(calls) == state.attempt_count == 1
    assert state.submission_may_exist and not state.last_attempt_rejected
