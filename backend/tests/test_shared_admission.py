"""Exercise persisted admission through the real text/vision request boundary."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from backend.api import experts
from backend.config import settings
from backend.db import provider_repository as repository
from backend.llm import providers
from backend.llm.concurrency import ModelScheduler, quota_key
from backend.llm.registry import ExpertRegistry, _build_scoped_registry
from backend.tests.test_adaptive_admission import profile
from backend.tests.test_automatic_model_scheduling import FakeClock, FakeHost
from backend.tests.test_image_capability import saved


def test_platform_profile_has_one_winner_for_insert_and_update():
    fingerprint = "a" * 64
    for generation in (1, 2):
        barrier = Barrier(2)
        def save(interval):
            candidate = dict(profile(revision=0, generation=generation), interval_seconds=interval)
            barrier.wait(timeout=5)
            return repository.record_shared_adaptive_limits(fingerprint, candidate)

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(save, (7, 13)))
        assert sorted(outcomes) == [False, True]
        states = repository.load_adaptive_states({"one": (None, fingerprint), "new": (None, "b" * 64)})
        winner = states["one"]["profile"]
        assert winner["interval_seconds"] == (7, 13)[outcomes.index(True)]
        assert winner["generation"] == generation and states["new"]["profile"] is None


@pytest.mark.asyncio
async def test_shared_text_and_vision_load_same_limits_before_send_and_after_restart(monkeypatch):
    clock = FakeClock()
    def restart():
        scheduler = ModelScheduler(clock=clock, host_capacity=FakeHost(), memory_reader=lambda: None)
        monkeypatch.setattr(providers, "get_scheduler", lambda: scheduler)
        return scheduler

    scheduler = restart()
    monkeypatch.setattr(settings, "shared_pool_enabled", True)
    monkeypatch.setattr(settings, "openai_api_key", "platform-fixture")
    monkeypatch.setattr(settings, "openai_model", "test-model")
    for name in ("gemini", "zhipu", "anthropic", "deepseek", "moonshot", "qwen"):
        monkeypatch.setattr(settings, name + "_api_key", "")
    # Daily quota accounting is covered separately; isolate model admission.
    monkeypatch.setattr("backend.llm.registry._shared_pool_usage.consume", lambda *args, **kwargs: None)
    sends = []
    reject = False
    async def invoke(*args, **kwargs):
        sends.append(clock())
        if reject:
            raise providers.ProviderRequestError("provider_rate_limited", status_code=429)
        return AIMessage(content="ok")
    monkeypatch.setattr(providers.OpenAIProvider, "_get_client", AsyncMock(return_value=SimpleNamespace(ainvoke=invoke)))
    alice = ExpertRegistry(shared_owner_id="alice").pick_default()
    bob = ExpertRegistry(shared_owner_id="bob").pick_default()
    text = [HumanMessage(content="read")]
    images = [providers.VisionImage(b"fixture", "image/png")]
    # Three real sends publish a provisional cold-start profile.
    for provider in (alice, bob, alice):
        await provider.ainvoke(text)
        clock.advance(1, scheduler)
    key = quota_key(alice.config, providers.endpoint_key(alice.config))
    identity, = scheduler.budgets[key].adaptive
    fingerprint = identity.removeprefix("platform:")
    learned = repository.load_adaptive_states({"p": (None, fingerprint)})["p"]["profile"]
    assert learned["generation"] == 1
    # Ordinary 429s on one owner's work freeze the group's other owner.
    # Recovered episodes must actually learn and persist a slower rate.
    for _ in range(2):
        reject = True
        with pytest.raises(providers.ProviderRequestError, match="provider_rate_limited"):
            await alice.ainvoke(text)
        reject = False
        assert scheduler.cooldown_remaining(key) == 65
        clock.advance(65, scheduler)
        await bob.ainvoke_vision("read", images)
        clock.advance(1, scheduler)
    learned = repository.load_adaptive_states({"p": (None, fingerprint)})["p"]["profile"]
    assert learned["interval_seconds"] > 1 and learned["generation"] > 1
    restart_at = clock()
    before_restart = len(sends)
    if scheduler._adaptive_refresh_task:
        await scheduler._adaptive_refresh_task
    scheduler = restart()
    budget = scheduler.budget(key, 0)
    queue_seen = asyncio.Event()
    acquire = scheduler.acquire
    async def observe(**kwargs):
        assert budget.interval == learned["interval_seconds"] and budget.limit == learned["concurrency"]
        queue_seen.set()
        return await acquire(**kwargs)
    monkeypatch.setattr(scheduler, "acquire", observe)
    await alice.ainvoke(text)  # Empty group after restart admits the first request immediately.
    queue_seen.clear()
    queued = asyncio.create_task(bob.ainvoke_vision("read", images))
    try:
        await asyncio.wait_for(queue_seen.wait(), 2)
        clock.advance(learned["interval_seconds"] - 1, scheduler)
        await asyncio.sleep(0)
        assert not queued.done() and len(sends) == before_restart + 1
        clock.advance(1, scheduler)
        await asyncio.wait_for(queued, 2)
        assert sends[before_restart:] == [restart_at, restart_at + learned["interval_seconds"]]
    finally:
        queued.cancel()
        await asyncio.gather(queued, return_exceptions=True)
        if scheduler._adaptive_refresh_task:
            await scheduler._adaptive_refresh_task
    monkeypatch.setattr(settings, "openai_api_key", "rotated-fixture")
    rotated = ExpertRegistry(shared_owner_id="alice").pick_default()
    assert rotated._provider._adaptive_source != alice._provider._adaptive_source


@pytest.mark.asyncio
async def test_byok_image_probe_observes_saved_interval_even_in_a_fresh_scheduler(monkeypatch):
    provider_id, user = saved("probe-interval")
    stored = repository.get_provider_config(user.id, provider_id, master_key=settings.provider_encryption_key)
    assert repository.record_adaptive_limits(user.id, provider_id, profile(revision=stored.updated_at),
                                             expected_updated_at=stored.updated_at)
    registry = _build_scoped_registry(user)
    provider = registry.get(provider_id)
    key = quota_key(provider.config, providers.endpoint_key(provider.config))
    clock = FakeClock()
    scheduler = ModelScheduler(clock=clock, host_capacity=FakeHost(), memory_reader=lambda: None)
    monkeypatch.setattr(providers, "get_scheduler", lambda: scheduler)
    sdk_call = AsyncMock(return_value=AIMessage(content="ok"))
    monkeypatch.setattr(type(provider), "_get_client", AsyncMock(return_value=SimpleNamespace(ainvoke=sdk_call)))
    monkeypatch.setattr(experts, "make_image_challenge", lambda: (b"image", "ok"))
    scheduler.budget(key, 0).last_start = clock() - 2
    queued = asyncio.Event()
    acquire = scheduler.acquire
    async def observe(**kwargs):
        queued.set()
        return await acquire(**kwargs)
    monkeypatch.setattr(scheduler, "acquire", observe)
    task = asyncio.create_task(experts.verify_provider_image(provider_id, current=user, registry=registry))
    try:
        await asyncio.wait_for(queued.wait(), 2)
        assert scheduler.budgets[key].interval == 10
        clock.advance(7, scheduler)
        await asyncio.sleep(0)
        sdk_call.assert_not_awaited()
        clock.advance(1, scheduler)
        assert (await asyncio.wait_for(task, 2))["image_capability_status"] == "passed"
        sdk_call.assert_awaited_once()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if scheduler._adaptive_refresh_task:
            await scheduler._adaptive_refresh_task


@pytest.mark.asyncio
@pytest.mark.parametrize("platform_url", ["", "https://api.openai.com/v1", "https://api.openai.com/v1/"])
@pytest.mark.parametrize("training_source", ["byok", "platform"])
async def test_matching_platform_and_byok_share_cooldown_but_learn_from_their_own_calls(
    monkeypatch, platform_url, training_source,
):
    from backend.tests.test_adaptive_admission_lifecycle import seed, MASTER_KEY

    config, stored = seed("my-key")
    monkeypatch.setattr(settings, "provider_encryption_key", MASTER_KEY)
    monkeypatch.setattr(settings, "shared_pool_enabled", True)
    monkeypatch.setattr(settings, "openai_api_key", config.api_key)
    monkeypatch.setattr(settings, "openai_model", config.model)
    monkeypatch.setattr(settings, "openai_api_base", platform_url)
    for name in ("gemini", "zhipu", "anthropic", "deepseek", "moonshot", "qwen"):
        monkeypatch.setattr(settings, name + "_api_key", "")
    monkeypatch.setattr("backend.llm.registry._shared_pool_usage.consume", lambda *args, **kwargs: None)
    platform = ExpertRegistry(shared_owner_id="anonymous").pick_default()
    owner = _build_scoped_registry(SimpleNamespace(id="my-key", role="teacher")).get(stored.id)
    clock = FakeClock()
    scheduler = ModelScheduler(clock=clock, host_capacity=FakeHost(), memory_reader=lambda: None)
    monkeypatch.setattr(providers, "get_scheduler", lambda: scheduler)
    sends = []
    async def invoke(*args, **kwargs):
        sends.append(clock())
        if len(sends) == 1:
            raise providers.ProviderRequestError("provider_rate_limited", status_code=429)
        return AIMessage(content="ok")
    monkeypatch.setattr(providers.OpenAIProvider, "_get_client", AsyncMock(return_value=SimpleNamespace(ainvoke=invoke)))
    trainer, other = (owner, platform) if training_source == "byok" else (platform, owner)
    messages = [HumanMessage(content="read")]
    with pytest.raises(providers.ProviderRequestError, match="provider_rate_limited"):
        await trainer.ainvoke(messages)
    # Either source must wait out a 429 from the same underlying quota.
    admitted = asyncio.Event()
    acquire = scheduler.acquire
    async def observe(**kwargs):
        admitted.set()
        return await acquire(**kwargs)
    monkeypatch.setattr(scheduler, "acquire", observe)
    queued = asyncio.create_task(other.ainvoke(messages))
    try:
        await asyncio.wait_for(admitted.wait(), 2)
        clock.advance(64, scheduler)
        for _ in range(8):
            await asyncio.sleep(0)
        assert sends == [100]
        clock.advance(1, scheduler)
        await asyncio.wait_for(queued, 2)
        assert sends == [100, 165]
        # Only the source with three successes may save a provisional estimate.
        for _ in range(3):
            clock.advance(2, scheduler)
            await trainer.ainvoke(messages)
        clock.advance(2, scheduler)
        await other.ainvoke(messages)
        _, fingerprint = platform._provider._adaptive_source
        owned = repository.get_provider_config("my-key", stored.id, master_key=MASTER_KEY).adaptive_limits
        shared = repository.load_adaptive_states({"p": (None, fingerprint)})["p"]["profile"]
        assert (owned is not None) == (training_source == "byok")
        assert (shared is not None) == (training_source == "platform")
    finally:
        queued.cancel()
        await asyncio.gather(queued, return_exceptions=True)
        if scheduler._adaptive_refresh_task:
            await scheduler._adaptive_refresh_task
