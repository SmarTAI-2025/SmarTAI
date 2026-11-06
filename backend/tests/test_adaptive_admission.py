"""Deterministic admission learning, persistence and run-stop contracts."""
import asyncio
from types import SimpleNamespace

import pytest

from backend.llm.adaptive_admission import AdaptiveAdmission, valid_profile
from backend.llm.concurrency import ModelScheduler
from backend.tests.test_automatic_model_scheduling import FakeClock, FakeHost, call, turns


def profile(revision=1, generation=1):
    return dict(version=1, interval_seconds=10.0, concurrency=5,
                response_seconds=30.0, config_version=revision,
                generation=generation, validated=True)


def success(state, start, duration=3, run="same-run", busy=True):
    state.started(start)
    state.succeeded(now=start + duration, started_at=start, run=run, busy=busy)


def episode(state, at, run="same-run", *, recover=True):
    for start in range(int(at) - 55, int(at), 10):
        success(state, start, run=run)
    state.started(at - .1)
    state.limited(run, now=at, started_at=at - .1, until=at + 65, active=2)
    if recover:
        success(state, at + 66, run=run)


@pytest.mark.parametrize("historical,threshold", [(False, 2), (True, 3)])
def test_only_recovered_episodes_trigger_learning_and_rate_does_not_halve_concurrency(historical, threshold):
    state = AdaptiveAdmission(profile() if historical else None, 1, 0, 0)
    initial = state.interval, state.concurrency
    for n in range(1, threshold):
        episode(state, n * 200)
        assert (state.interval, state.concurrency) == initial
    episode(state, threshold * 200, recover=False)
    assert (state.interval, state.concurrency) == initial
    success(state, threshold * 200 + 66)
    assert state.interval > initial[0]
    assert state.concurrency == initial[1]
    assert state.take_update()["validated"] is False


def test_rate_calibration_can_speed_back_up_and_then_stops():
    state = AdaptiveAdmission(None, 1, 0, 0)
    episode(state, 200)
    episode(state, 400)
    slow = state.interval
    assert slow == pytest.approx(60 / 5.4)
    assert state.take_update()["validated"] is False
    for start in (480, 510, 540):
        success(state, start)
    assert state.interval == pytest.approx(60 / 5.7)
    # The upward candidate is not saved until it has actually been exercised.
    assert state.take_update()["interval_seconds"] == slow
    for start in (555, 585, 615):
        success(state, start)
    stable = state.take_update()
    assert stable["validated"] and stable["interval_seconds"] < slow
    for start in range(630, 1200, 11):
        success(state, start)
    assert state.take_update() is None and state.concurrency == 5


def test_concurrency_evidence_changes_slots_without_inventing_two_rpm():
    state = AdaptiveAdmission(None, 1, 0, 0)
    for at in (200, 400):
        for start in (at - 3, at - 2, at - 1):
            state.started(start)
        state.limited("same-run", now=at, started_at=at - 1, until=at + 65, active=3)
        for start in (at - 3, at - 2):
            state.succeeded(now=at + 20, started_at=start, run="same-run")
        success(state, at + 66)
    assert state.concurrency == 2 and state.interval == 1
    for start in (480, 510, 540):
        success(state, start, duration=22)
    assert state.concurrency == 2 and not state.probing


def test_unfinished_requests_are_not_counted_as_failures_or_precise_rpm():
    state = AdaptiveAdmission(None, 1, 0, 0)
    episode(state, 200)
    state.started(350)
    episode(state, 400)
    assert state.adjustments == 0
    state.succeeded(now=500, started_at=350, run="same-run")
    assert state.adjustments == 1  # Recovery was already observed; late evidence is now complete.
    success(state, 510)
    assert state.adjustments == 1


def test_late_wave_does_not_add_episode_and_separate_runs_do_not_combine():
    state = AdaptiveAdmission(profile(), 1, 0, 0)
    for n in range(8):
        episode(state, 200 * (n + 1), run=f"run-{n}")
    assert state.adjustments == 0
    state = AdaptiveAdmission(None, 1, 0, 0)
    state.started(99)
    state.started(100)
    state.limited("same", now=101, started_at=99, until=166)
    state.limited("same", now=161, started_at=100, until=166)
    assert len(state.runs["same"].episodes) == 1


def test_stable_profile_does_not_chase_ceiling_and_sparse_batches_save_provisional_result():
    state = AdaptiveAdmission(profile(), 1, 0, 0)
    for start in range(0, 3600, 10):
        success(state, start)
    assert state.take_update() is None and state.interval == 10 and state.concurrency == 5
    cold = AdaptiveAdmission(None, 1, 0, 0)
    for start in (0, 1, 2):
        success(cold, start)
    saved = cold.take_update()
    assert saved and not saved["validated"]
    reloaded = AdaptiveAdmission(saved, 1, 0, 100)
    assert (reloaded.interval, reloaded.concurrency) == (cold.interval, cold.concurrency)


@pytest.mark.parametrize("reject", [False, True])
def test_configuration_edit_has_one_bounded_trial_and_can_roll_back(reject):
    state = AdaptiveAdmission(profile(), 2, 0, 0)
    for start in (0, 30, 60):
        success(state, start)
    assert state.interval == pytest.approx(10 / 1.1) and state.concurrency == 5
    state.take_update()  # Stable old rate is saved under the new config revision.
    if reject:
        state.started(69)
        state.limited("same-run", now=70, started_at=69, until=135)
        assert state.interval == 10
        starts = (136, 166, 196)
    else:
        starts = (75, 105, 135)
    for start in starts:
        success(state, start)
    learned = state.take_update()
    assert learned["validated"] and learned["config_version"] == 2
    assert not state.probing and not state.probe_allowed


def test_user_rpm_is_a_floor_on_interval_and_invalid_profiles_are_rejected():
    state = AdaptiveAdmission(profile(), 2, 3, 0)
    for start in (0, 30, 60, 100, 140, 180):
        success(state, start)
    assert state.interval >= 20
    for value in ({}, {**profile(), "interval_seconds": float("nan")},
                  {**profile(), "concurrency": True}, {**profile(), "generation": -1}):
        assert valid_profile(value) is None


@pytest.mark.asyncio
async def test_loaded_parameters_gate_queue_by_interval_and_slots():
    clock = FakeClock()
    scheduler = ModelScheduler(clock=clock, host_capacity=FakeHost(), memory_reader=lambda: None)
    scheduler.bind_adaptive("same", 0, "byok", {**profile(), "concurrency": 2}, 1)
    tasks = [asyncio.create_task(scheduler.acquire(**call("same"))) for _ in range(3)]
    try:
        await turns()
        assert sum(t.done() for t in tasks) == 1
        clock.advance(10, scheduler)
        await turns()
        assert sum(t.done() for t in tasks) == 2
        clock.advance(10, scheduler)
        await turns()
        assert not tasks[2].done()
        tasks[0].result().release()
        await turns()
        assert tasks[2].done()
    finally:
        for task in tasks:
            if task.done() and not task.cancelled():
                task.result().release()
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert scheduler.active == 0


def test_persistence_allows_bidirectional_updates_but_not_stale_writers(monkeypatch):
    from backend.config import settings
    from backend.db.provider_repository import (get_provider_config, record_adaptive_limits,
                                               update_provider_config, upsert_provider_config)
    from backend.llm.registry import _build_scoped_registry
    from backend.models import ProviderConfig
    from backend.tests.test_custom_provider_persistence import _owner, MASTER_KEY
    _owner("learner")
    config = ProviderConfig(provider_type="openai", model="fixture", api_key="synthetic-key", rpm=6)
    row = upsert_provider_config("learner", config, master_key=MASTER_KEY)
    learned = profile(row.updated_at)
    assert not record_adaptive_limits("different-owner", row.id, learned, expected_updated_at=row.updated_at)
    assert record_adaptive_limits("learner", row.id, learned, expected_updated_at=row.updated_at)
    faster = {**learned, "interval_seconds": 9, "generation": 2}
    assert record_adaptive_limits("learner", row.id, faster, expected_updated_at=row.updated_at)
    assert not record_adaptive_limits("learner", row.id, learned, expected_updated_at=row.updated_at)
    stored = get_provider_config("learner", row.id, master_key=MASTER_KEY)
    assert stored.config.rpm == 6 and stored.updated_at == row.updated_at
    monkeypatch.setattr(settings, "provider_encryption_key", MASTER_KEY)
    registry = _build_scoped_registry(SimpleNamespace(id="learner", role="teacher"))
    assert registry.get(row.id)._adaptive_binding[1] == faster
    edited = update_provider_config("learner", row.id, config, master_key=MASTER_KEY)
    assert edited.adaptive_limits == faster
    revised = {**faster, "config_version": edited.updated_at, "generation": 3}
    assert record_adaptive_limits("learner", row.id, revised, expected_updated_at=edited.updated_at)
    assert not record_adaptive_limits("learner", row.id, {**faster, "generation": 4}, expected_updated_at=row.updated_at)


@pytest.mark.asyncio
@pytest.mark.parametrize("adjusted,limit", [(False, 3), (True, 5)])
async def test_run_stop_counts_cycles_not_parallel_refusals_and_grants_extra_only_once(adjusted, limit):
    from backend.services.execution_control import ExecutionControl
    provider = SimpleNamespace(provider_id="fixture")
    control = ExecutionControl("run", lambda: False, asyncio.current_task())
    if adjusted:
        control.rate_recovered(provider, started_at=0, adjusted=True)
    for cycle in range(limit):
        start = cycle * 66.0
        for _ in range(20):
            control.rate_limited(provider, started_at=start, until=start + 65)
        assert bool(control.blocked) == (cycle == limit - 1)
        # Repeated parameter changes do not reset the failed-cycle count or budget.
        if adjusted:
            control.rate_recovered(provider, started_at=-1, adjusted=True)
    assert control.rate_cycles[next(iter(control.rate_cycles))].failures == limit


@pytest.mark.asyncio
async def test_only_post_cooldown_success_resets_failure_streak():
    from backend.services.execution_control import ExecutionControl
    p = SimpleNamespace(provider_id="fixture")
    control = ExecutionControl("run", lambda: False, asyncio.current_task())
    control.rate_limited(p, started_at=0, until=65)
    control.rate_limited(p, started_at=66, until=131)
    control.rate_recovered(p, started_at=60)
    state = next(iter(control.rate_cycles.values()))
    assert state.failures == 2
    control.rate_recovered(p, started_at=132)
    assert state.failures == 0


@pytest.mark.asyncio
async def test_late_post_cooldown_success_prevents_false_global_stop():
    from backend.services.execution_control import ExecutionControl
    p = SimpleNamespace(provider_id="fixture")
    control = ExecutionControl("run", lambda: False, asyncio.current_task())
    control.rate_limited(p, started_at=0, until=65)
    control.rate_started(p, started_at=66)
    control.rate_limited(p, started_at=67, until=132)
    control.rate_started(p, started_at=133)
    control.rate_limited(p, started_at=134, until=199)
    assert not control.blocked  # Both admitted siblings still have unknown outcomes.
    control.rate_recovered(p, started_at=66)
    control.rate_finished(p, started_at=66)
    assert not control.blocked
    control.rate_recovered(p, started_at=133, adjusted=True)
    control.rate_finished(p, started_at=133)
    state = next(iter(control.rate_cycles.values()))
    assert state.failures == 0 and state.extra_remaining == 2
    assert not control.blocked


def test_newer_saved_generation_refreshes_stale_worker_without_accepting_older_registry():
    scheduler = ModelScheduler(clock=FakeClock(), host_capacity=FakeHost(), memory_reader=lambda: None)
    scheduler.bind_adaptive("key", 0, "byok", profile(), 1)
    stale = scheduler.budgets["key"].adaptive["byok"]
    stale._publish(validated=False)
    updated = {**profile(generation=2), "interval_seconds": 12.0}
    scheduler.bind_adaptive("key", 0, "byok", updated, 1)
    current = scheduler.budgets["key"].adaptive["byok"]
    assert current is not stale and current.interval == 12 and current.pending is None
    scheduler.bind_adaptive("key", 0, "byok", profile(), 1)
    assert scheduler.budgets["key"].adaptive["byok"] is current
