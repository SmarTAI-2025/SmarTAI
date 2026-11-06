"""Configuration lifecycle and competing workers with the real metadata store."""
import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

from backend.db import provider_repository as repository
from backend.llm.concurrency import ModelScheduler, quota_key
from backend.llm.providers import endpoint_key
from backend.llm.registry import _build_scoped_registry
from backend.models import ProviderConfig
from backend.tests.test_adaptive_admission import profile
from backend.tests.test_automatic_model_scheduling import FakeClock, FakeHost, call, turns
from backend.tests.test_custom_provider_persistence import _owner, MASTER_KEY


@pytest.mark.asyncio
async def test_moving_identity_removes_old_group_control_and_fences_old_snapshot():
    clock = FakeClock()
    scheduler = ModelScheduler(clock=clock, host_capacity=FakeHost(), memory_reader=lambda: None)
    scheduler.bind_adaptive("shared", 1, "alice", None, 1)
    scheduler.bind_adaptive("shared", 0, "bob", None, 1)
    first = await scheduler.acquire(**call("shared", rpm=1))
    first.release()
    waiting = asyncio.create_task(scheduler.acquire(**call("shared", rpm=0, owner="bob")))
    try:
        await turns()
        scheduler.bind_adaptive("new-key", 1, "alice", None, 2)
        scheduler.bind_adaptive("shared", 1, "alice", None, 1)  # Old running registry.
        assert "alice" not in scheduler.budgets["shared"].adaptive
        clock.advance(1, scheduler)
        await turns()
        assert waiting.done(), "the old group's sixty-second interval survived"
        waiting.result().release()
        assert scheduler.budgets["shared"].interval == 1
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


def seed(owner, *, rpm=0):
    _owner(owner)
    config = ProviderConfig(provider_type="openai", model="fixture", api_key="shared-synthetic", rpm=rpm)
    return config, repository.upsert_provider_config(owner, config, master_key=MASTER_KEY)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["edit", "delete", "disable", "upsert"])
async def test_database_changes_release_other_owner_without_another_departed_owner_call(change):
    config, a = seed("alice", rpm=1)
    _, b = seed("bob")
    clock = FakeClock()
    scheduler = ModelScheduler(clock=clock, host_capacity=FakeHost(), memory_reader=lambda: None)
    for owner, row, rpm in (("alice", a, 1), ("bob", b, 0)):
        scheduler.bind_adaptive("shared", rpm, owner, None, row.updated_at, source=(owner, row.id))
    await scheduler.refresh_adaptive(force=True)
    first = await scheduler.acquire(**call("shared", rpm=1))
    first.release()
    waiting = asyncio.create_task(scheduler.acquire(**call("shared", owner="bob")))
    try:
        await turns()
        if change == "edit":
            repository.update_provider_config("alice", a.id, config.model_copy(update={"api_key": "new-key"}), master_key=MASTER_KEY)
        elif change == "upsert":
            repository.upsert_provider_config("alice", config.model_copy(update={"api_key": "new-key"}), master_key=MASTER_KEY)
        elif change == "delete":
            assert repository.delete_provider_config("alice", a.id)
        else:
            assert repository.set_provider_enabled("alice", a.id, False)
        clock.advance(5, scheduler)
        # The existing scheduler pump refreshes from committed metadata, even
        # when the departed user never calls the old provider again.
        lease = await asyncio.wait_for(waiting, 2)
        lease.release()
        assert "alice" not in scheduler.budgets["shared"].adaptive
        assert "bob" in scheduler.budgets["shared"].adaptive
        scheduler.bind_adaptive("shared", 1, "alice", None, a.updated_at)
        assert "alice" not in scheduler.budgets["shared"].adaptive
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        if scheduler._adaptive_refresh_task:
            await scheduler._adaptive_refresh_task


@pytest.mark.asyncio
async def test_removing_estimate_preserves_group_cooldown_and_inflight_lease():
    _, a = seed("alice")
    clock = FakeClock()
    scheduler = ModelScheduler(clock=clock, host_capacity=FakeHost(), memory_reader=lambda: None)
    scheduler.bind_adaptive("shared", 0, "alice", None, a.updated_at, source=("alice", a.id))
    lease = await scheduler.acquire(**call("shared"))
    await scheduler.record_rate_limit("shared", 0, 65, started_at=clock())
    assert repository.delete_provider_config("alice", a.id)
    await scheduler.refresh_adaptive(force=True)
    assert scheduler.cooldown_remaining("shared") == 65
    assert scheduler.active == 1 and scheduler.host_capacity.active == 1
    lease.release()
    if scheduler._adaptive_refresh_task:
        await scheduler._adaptive_refresh_task


@pytest.mark.asyncio
async def test_failed_generation_save_refreshes_and_allows_later_verified_save(monkeypatch):
    from backend.config import settings
    from backend.llm import providers
    config, row = seed("learner")
    monkeypatch.setattr(settings, "provider_encryption_key", MASTER_KEY)
    provider = _build_scoped_registry(SimpleNamespace(id="learner", role="teacher")).get(row.id)
    clock = FakeClock()
    scheduler = ModelScheduler(clock=clock, host_capacity=FakeHost(), memory_reader=lambda: None)
    monkeypatch.setattr(providers, "get_scheduler", lambda: scheduler)
    monkeypatch.setattr(providers, "_ENDPOINT_BREAKERS", {})
    identity, saved, revision, recorder = provider._adaptive_binding
    key = quota_key(provider.config, endpoint_key(provider.config))
    scheduler.bind_adaptive(key, 0, identity, saved, revision)
    control = scheduler.budgets[key].adaptive[identity]
    control.interval = 2
    control._publish(validated=True)
    other_worker = ModelScheduler(clock=clock, host_capacity=FakeHost(), memory_reader=lambda: None)
    other_worker.bind_adaptive(key, 0, identity, saved, revision)
    other_control = other_worker.budgets[key].adaptive[identity]
    other_control.interval = 10
    other_control._publish(validated=True)
    winner = other_control.pending
    outcomes = []
    def record(update):
        # A competing worker wins immediately before this worker's CAS.
        if not outcomes:
            assert repository.record_adaptive_limits("learner", row.id, winner, expected_updated_at=revision)
        result = recorder(update)
        outcomes.append(result)
        return result
    provider._adaptive_binding = (identity, saved, revision, record)
    async def invoke(*args, **kwargs):
        return SimpleNamespace(content="complete")
    provider._client = SimpleNamespace(ainvoke=invoke)
    result = await provider.ainvoke([HumanMessage(content="synthetic")])
    assert result.content == "complete" and outcomes == [False]
    current = scheduler.budgets[key].adaptive[identity]
    assert current.generation == 1 and current.interval == winner["interval_seconds"]
    assert current.pending is None  # Do not blindly renumber the stale candidate.
    # Fresh validated observations can publish against the refreshed version.
    current.interval = 9
    current._publish(validated=True)
    clock.advance(31, scheduler)
    assert (await provider.ainvoke([HumanMessage(content="synthetic")])).content == "complete"
    stored = repository.get_provider_config("learner", row.id, master_key=MASTER_KEY)
    assert outcomes == [False, True]
    assert stored.adaptive_limits["generation"] == 2
    assert stored.adaptive_limits["interval_seconds"] == 9
    if scheduler._adaptive_refresh_task:
        await scheduler._adaptive_refresh_task


@pytest.mark.asyncio
async def test_old_save_acknowledgement_does_not_modify_new_configuration():
    scheduler = ModelScheduler(clock=FakeClock(), host_capacity=FakeHost(), memory_reader=lambda: None)
    scheduler.bind_adaptive("same", 0, "byok", profile(), 1)
    scheduler.bind_adaptive("same", 0, "byok", profile(), 2)
    current = scheduler.budgets["same"].adaptive["byok"]
    scheduler.adaptive_saved("same", "byok", profile(revision=1, generation=4))
    assert current.revision == 2 and current.generation == 1


def test_lifecycle_metadata_lookup_is_owner_scoped_and_never_reads_keys(monkeypatch):
    _, row = seed("owner")
    def forbid_decryption(*args, **kwargs):
        raise AssertionError("metadata refresh must not decrypt a key")
    monkeypatch.setattr(repository, "decrypt_secret", forbid_decryption)
    states = repository.load_adaptive_states({"valid": ("owner", row.id), "wrong": ("other", row.id)})
    assert set(states) == {"valid"}
    assert set(states["valid"]) == {"revision", "enabled", "profile"}


@pytest.mark.asyncio
async def test_late_metadata_read_cannot_detach_a_newer_binding(monkeypatch):
    import threading
    entered, release = threading.Event(), threading.Event()
    def read(sources):
        entered.set()
        assert release.wait(2)
        return {"byok": {"revision": 1, "enabled": True, "profile": None}}
    monkeypatch.setattr(repository, "load_adaptive_states", read)
    scheduler = ModelScheduler(clock=FakeClock(), host_capacity=FakeHost(), memory_reader=lambda: None)
    scheduler.bind_adaptive("old", 1, "byok", None, 1, source=("owner", "id"))
    refreshing = asyncio.create_task(scheduler.refresh_adaptive(force=True))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        scheduler.bind_adaptive("new", 0, "byok", None, 2, source=("owner", "id"))
        release.set()
        await refreshing
        assert "byok" not in scheduler.budgets["old"].adaptive
        assert scheduler.budgets["new"].adaptive["byok"].revision == 2
    finally:
        release.set()
        await refreshing
