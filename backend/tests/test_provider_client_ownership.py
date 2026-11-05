"""Real provider constructors, with local fakes and no model/network calls."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from backend.llm import providers
from backend.llm.request_timeouts import response_timeout_scope
from backend.models import ProviderConfig
from backend.tests.test_provider_response_deadlines import MESSAGES, scheduler  # noqa: F401


async def wait_thread_event(event):
    async def poll():
        while not event.is_set():
            await asyncio.sleep(.001)
    await asyncio.wait_for(poll(), 5)


async def wait_cleanup():
    async def poll():
        while providers._client_owner_tasks:
            await asyncio.sleep(.001)
    await asyncio.wait_for(poll(), 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "response_only"])
@pytest.mark.parametrize("constructor_fails", [False, True])
async def test_cancelled_gemini_construction_closes_late_client_and_holds_permit(
    mode, constructor_fails, scheduler, monkeypatch,
):
    entered, release = threading.Event(), threading.Event()
    closed = asyncio.Event()
    requests = []
    async def close():
        closed.set()
    def forbidden(*args, **kwargs):
        requests.append(True)
        pytest.fail("cancelled constructor must not submit a paid request")
    client = SimpleNamespace(aclose=close, ainvoke=forbidden, invoke=forbidden)
    def build():
        entered.set()
        assert release.wait(5)
        if constructor_fails:
            raise RuntimeError("local constructor failed")
        return client
    provider = providers.GeminiProvider(ProviderConfig(provider_type="gemini", model="fixture", api_key="fake"))
    monkeypatch.setattr(providers.GeminiProvider, "_needs_proxy_mode", property(lambda self: True))
    monkeypatch.setattr(provider, "_build_client_sync", build)
    async def work():
        with response_timeout_scope(60, response_only=mode == "response_only"):
            return await provider.ainvoke(MESSAGES)
    task = asyncio.create_task(work())
    try:
        await wait_thread_event(entered)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert scheduler.proxy_slots._value == 49
        assert not closed.is_set() and not requests
    finally:
        release.set()
        await wait_cleanup()
    assert closed.is_set() is not constructor_fails
    assert scheduler.proxy_slots._value == 50
    assert not requests


@pytest.mark.asyncio
async def test_gemini_local_wait_expiry_keeps_late_constructor_owned(scheduler, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    closed = asyncio.Event()
    async def close():
        closed.set()
    def build():
        entered.set()
        assert release.wait(5)
        return SimpleNamespace(aclose=close)
    provider = providers.GeminiProvider(ProviderConfig(provider_type="gemini", model="fixture", api_key="fake"))
    monkeypatch.setattr(providers.GeminiProvider, "_needs_proxy_mode", property(lambda self: True))
    monkeypatch.setattr(provider, "_build_client_sync", build)
    scopes = []
    original = asyncio.timeout
    def capture(seconds):
        scope = original(seconds)
        if seconds is not None and 50 < seconds < 70:
            scopes.append(scope)
        return scope
    monkeypatch.setattr(asyncio, "timeout", capture)
    async def work():
        with response_timeout_scope(60, response_only=True, local_wait_seconds=60):
            return await provider.ainvoke(MESSAGES)
    task = asyncio.create_task(work())
    try:
        await wait_thread_event(entered)
        scopes[-1].reschedule(asyncio.get_running_loop().time())
        with pytest.raises(providers.ProviderRequestError, match="provider_capacity_unavailable"):
            await task
        assert scheduler.proxy_slots._value == 49
    finally:
        release.set()
        await wait_cleanup()
    assert closed.is_set() and scheduler.proxy_slots._value == 50


@pytest.mark.asyncio
@pytest.mark.parametrize("close_mode", ["error", "blocked"])
async def test_unused_gemini_close_is_bounded(close_mode, scheduler, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    closed = asyncio.Event()
    monkeypatch.setattr(providers, "_UNUSED_CLIENT_CLOSE_SECONDS", .02)
    async def close():
        try:
            if close_mode == "blocked":
                await asyncio.Event().wait()
            raise RuntimeError("local close failed")
        finally:
            closed.set()
    def build():
        entered.set()
        assert release.wait(5)
        return SimpleNamespace(aclose=close)
    provider = providers.GeminiProvider(ProviderConfig(provider_type="gemini", model="fixture", api_key="fake"))
    monkeypatch.setattr(providers.GeminiProvider, "_needs_proxy_mode", property(lambda self: True))
    monkeypatch.setattr(provider, "_build_client_sync", build)
    task = asyncio.create_task(provider.ainvoke(MESSAGES))
    try:
        await wait_thread_event(entered)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await wait_cleanup()
    assert closed.is_set() and scheduler.proxy_slots._value == 50


NATIVE_TYPES = [
    ("gemini", providers.GeminiProvider), ("openai", providers.OpenAIProvider),
    ("anthropic", providers.AnthropicProvider), ("zhipu", providers.ZhipuProvider),
    ("deepseek", providers.DeepSeekProvider), ("moonshot", providers.MoonshotProvider),
    ("qwen", providers.QwenProvider), ("tencent", providers.TencentProvider),
    ("doubao", providers.DoubaoProvider), ("minimax", providers.MiniMaxProvider),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,provider_class", NATIVE_TYPES)
async def test_custom_native_construction_is_single_owned_cache(kind, provider_class, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    client, constructions = object(), []
    def build():
        constructions.append(True)
        entered.set()
        assert release.wait(5)
        return client
    provider = provider_class(ProviderConfig(provider_type=kind, model="fixture", api_key="fake",
                                           base_url="https://relay.example/v1"))
    monkeypatch.setattr(provider, "_build_client_sync", build)
    first = asyncio.create_task(provider._get_client())
    try:
        await wait_thread_event(entered)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(provider._get_client())
        await asyncio.sleep(.01)
        assert constructions == [True] and not second.done()
    finally:
        release.set()
        await wait_cleanup()
    assert await second is client
    assert provider._client is client
    assert await provider._get_client() is client
    assert constructions == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["openai_chat_completions", "anthropic_messages", "gemini_generate_content"])
async def test_relay_cancelled_waiter_cannot_overwrite_shared_pair(protocol, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    pair, constructions = (object(), object()), []
    def build(*args, **kwargs):
        constructions.append(True)
        entered.set()
        assert release.wait(5)
        return pair
    provider = providers.SafeRelayProvider(ProviderConfig(
        provider_type="deepseek", model="fixture", api_key="fake",
        base_url="https://relay.example/v1", wire_protocol=protocol))
    monkeypatch.setattr(providers, "build_safe_provider_clients", build)
    first = asyncio.create_task(provider._relay_client())
    try:
        await wait_thread_event(entered)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(provider._relay_client())
        await asyncio.sleep(.01)
        assert constructions == [True] and not second.done()
    finally:
        release.set()
        await wait_cleanup()
    assert await second is pair[1]
    assert provider._safe_sync_client is pair[0] and provider._safe_async_client is pair[1]
    assert await provider._relay_client() is pair[1]
    assert constructions == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("relay", [False, True])
async def test_failed_owned_constructor_can_be_retried(relay, monkeypatch):
    provider = (providers.SafeRelayProvider if relay else providers.OpenAIProvider)(ProviderConfig(
        provider_type="openai", model="fixture", api_key="fake", base_url="https://relay.example/v1"))
    client = object()
    builds = []
    def build():
        builds.append(True)
        if len(builds) == 1:
            raise RuntimeError("construction failed")
        if relay:
            provider._safe_async_client = client
        return client
    monkeypatch.setattr(provider, "_build_client_sync", build)
    get_client = provider._relay_client if relay else provider._get_client
    with pytest.raises(RuntimeError, match="construction failed"):
        await get_client()
    assert await get_client() is client
    await wait_cleanup()
    assert builds == [True, True]


@pytest.mark.asyncio
@pytest.mark.parametrize("relay", [False, True])
async def test_cancelled_waiter_background_build_failure_allows_retry(relay, monkeypatch):
    provider = (providers.SafeRelayProvider if relay else providers.OpenAIProvider)(ProviderConfig(
        provider_type="openai", model="fixture", api_key="fake", base_url="https://relay.example/v1"))
    entered, release = threading.Event(), threading.Event()
    client, builds = object(), []
    def build():
        builds.append(True)
        if len(builds) == 1:
            entered.set()
            assert release.wait(5)
            raise RuntimeError("late construction failed")
        if relay:
            provider._safe_async_client = client
        return client
    monkeypatch.setattr(provider, "_build_client_sync", build)
    get_client = provider._relay_client if relay else provider._get_client
    task = asyncio.create_task(get_client())
    try:
        await wait_thread_event(entered)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await wait_cleanup()
    assert await get_client() is client
    await wait_cleanup()
    assert builds == [True, True]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,provider_class", NATIVE_TYPES)
async def test_official_constructor_stays_synchronous_and_cached(kind, provider_class, monkeypatch):
    provider = provider_class(ProviderConfig(provider_type=kind, model="fixture", api_key="fake"))
    client, builds = object(), []
    def build():
        builds.append(True)
        return client
    async def forbidden(*args, **kwargs):
        pytest.fail("official client construction unexpectedly dispatched a thread")
    monkeypatch.setattr(provider, "_build_client_sync", build)
    monkeypatch.setattr(asyncio, "to_thread", forbidden)
    assert await provider._get_client() is client
    assert await provider._get_client() is client
    assert builds == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("relay", [False, True])
@pytest.mark.parametrize("failures", [1, 3])
async def test_waiting_callers_keep_their_attempt_after_prior_constructor_failure(relay, failures, monkeypatch):
    provider = (providers.SafeRelayProvider if relay else providers.OpenAIProvider)(ProviderConfig(
        provider_type="openai", model="fixture", api_key="fake", base_url="https://relay.example/v1"))
    entered, release = threading.Event(), threading.Event()
    client, builds = object(), []
    def build():
        builds.append(True)
        if len(builds) == 1:
            entered.set()
            assert release.wait(5)
        if len(builds) <= failures:
            raise RuntimeError(f"construction {len(builds)} failed")
        if relay:
            provider._safe_async_client = client
        return client
    monkeypatch.setattr(provider, "_build_client_sync", build)
    get_client = provider._relay_client if relay else provider._get_client
    first = asyncio.create_task(get_client())
    waiters = []
    try:
        await wait_thread_event(entered)
        waiters = [asyncio.create_task(get_client()) for _ in range(3)]
        await asyncio.sleep(.01)
        assert len(builds) == 1
    finally:
        release.set()
    outcomes = await asyncio.gather(first, *waiters, return_exceptions=True)
    await wait_cleanup()
    assert isinstance(outcomes[0], RuntimeError)
    assert sum(isinstance(result, RuntimeError) for result in outcomes) == failures
    assert sum(result is client for result in outcomes) == 4 - failures
    assert len(builds) == failures + 1
