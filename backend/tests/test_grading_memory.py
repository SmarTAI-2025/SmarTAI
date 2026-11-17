"""Memory lifecycle contracts, including cancellation and independent experts."""
import asyncio
from types import SimpleNamespace

import pytest

from backend.llm import memory_reclaim
from backend.llm.concurrency import HARD_LIMIT, ModelScheduler
from backend.services.grading_memory import prepared_grading
from backend.tests.test_automatic_model_scheduling import FakeClock, FakeHost
from backend.tests.test_frontier_demo_session import enabled  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize("demo", [True, False])
async def test_fresh_shared_wrappers_share_one_preparation_window(enabled, monkeypatch, demo):
    import json
    from unittest.mock import AsyncMock
    from backend.config import settings
    from backend.llm.registry import _build_scoped_registry
    from backend.services.frontier_demo import renew_token
    from backend.tests.test_frontier_demo_session import issue, FakeProvider
    from backend.skills.calculation import CalculationSkill
    from backend.skills import math_grading
    from backend.llm.providers import LLMResponse
    from backend.services.math_grading_context import math_plans_context
    from backend.tests.test_math_grading import problem, answer, plan_for, assessment
    if demo:
        owner, _ = renew_token(issue(enabled)["token"])
    else:
        from backend.models import User
        from backend.state import register_user
        monkeypatch.setattr(settings, "shared_pool_enabled", True)
        monkeypatch.setattr(settings, "shared_pool_daily_request_limit", 500)
        monkeypatch.setattr(settings, "shared_pool_daily_estimated_token_limit", 10000000)
        owner = User(id="shared-owner", username="memory-shared", role="teacher", password_hash="synthetic")
        register_user(owner)
    registry = _build_scoped_registry(owner)
    key = registry.list_available()[0].provider_id
    providers = [registry.get(key) if i % 2 else registry.list_available()[0] for i in range(217)]
    assert len({id(p) for p in providers}) == 217
    entered = []
    block = asyncio.Event()
    async def invoke(self, messages, **kwargs):
        entered.append(sum(len(m.content) for m in messages))
        await block.wait()
        return LLMResponse(json.dumps(assessment().model_dump()), self.provider_id, self.model, 1)
    monkeypatch.setattr(FakeProvider, "ainvoke", invoke)
    monkeypatch.setattr(math_grading.kb_tool, "retrieve_for_grading", AsyncMock(return_value=[]))
    with math_plans_context({"q1": plan_for()}):
        tasks = [asyncio.create_task(CalculationSkill(p).grade(problem(), answer())) for p in providers]
        try:
            for _ in range(10):
                await asyncio.sleep(0)
            assert len(entered) == HARD_LIMIT
            assert all(chars > 10000 for chars in entered)  # Actual fully prepared prompts.
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    assert asyncio.get_running_loop()._smartai_grading_windows == {}
    # Cancelled waiters cannot poison later work through another fresh wrapper.
    block.set()
    with math_plans_context({"q1": plan_for()}):
        result = await asyncio.wait_for(CalculationSkill(registry.get(key)).grade(problem(), answer()), 1)
    assert result.score == 8 and not result.error_kind


@pytest.mark.asyncio
@pytest.mark.parametrize("needs_review", [False, True])
async def test_agreed_experts_bypass_busy_preparation_but_real_review_waits(monkeypatch, needs_review):
    from contextlib import AsyncExitStack
    from unittest.mock import AsyncMock
    from backend.skills import math_grading as grading
    from backend.services.math_grading_context import math_plans_context
    from backend.tests.test_math_grading import problem, answer, plan_for, assessment, expert_for, response
    provider = SimpleNamespace(provider_id="mock:math")
    p, a, plan = problem(), answer(), plan_for()
    experts = [expert_for(assessment(needs_review=needs_review), plan, a.content) for _ in range(2)]
    call = AsyncMock(return_value=(grading.ExpertReview(revisions=[]), response()))
    monkeypatch.setattr(grading, "structured_llm_call", call)
    async with AsyncExitStack() as occupied:
        for _ in range(HARD_LIMIT):
            await occupied.enter_async_context(prepared_grading(provider))
        with math_plans_context({p.q_id: plan}), grading.math_review_scope():
            task = asyncio.create_task(grading.review_math_experts(p, a, experts, provider))
            try:
                for _ in range(5):
                    await asyncio.sleep(0)
                assert task.done() is (not needs_review)
                assert call.await_count == 0
                await occupied.aclose()
                assert await asyncio.wait_for(task, 1) == experts
                assert call.await_count == int(needs_review)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_prepared_window_is_bounded_and_other_expert_keeps_running():
    # Equal display names cannot conflate independent runtime clients.
    slow, fast = SimpleNamespace(provider_id="same"), SimpleNamespace(provider_id="same")
    blocked = asyncio.Event()
    entered = []

    async def grade(i):
        async with prepared_grading(slow):
            entered.append(i)
            await blocked.wait()

    tasks = [asyncio.create_task(grade(i)) for i in range(217)]
    try:
        for _ in range(5):
            await asyncio.sleep(0)
        assert entered == list(range(HARD_LIMIT))
        async with asyncio.timeout(1):
            async with prepared_grading(fast):
                pass
        # Cancellation while waiting must not consume or leak a permit.
        tasks[-1].cancel()
        tasks[0].cancel()
        await asyncio.gather(tasks[-1], tasks[0], return_exceptions=True)
        await asyncio.sleep(0)
        assert entered[-1] == HARD_LIMIT
        blocked.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert len(entered) == 216
        assert asyncio.get_running_loop()._smartai_grading_windows == {}
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_queued_review_does_not_wait_for_the_whole_initial_batch():
    provider = SimpleNamespace(provider_id="busy")
    entered, active = [], set()
    gates = [asyncio.Event() for _ in range(218)]

    async def grade(i, *, review=False):
        async with prepared_grading(provider, review=review):
            entered.append(i)
            active.add(i)
            assert len(active) <= HARD_LIMIT
            try:
                await gates[i].wait()
            finally:
                active.remove(i)

    tasks = [asyncio.create_task(grade(i)) for i in range(217)]
    try:
        for _ in range(5):
            await asyncio.sleep(0)
        assert entered == list(range(HARD_LIMIT))
        tasks.append(asyncio.create_task(grade(217, review=True)))
        await asyncio.sleep(0)
        gates[0].set()
        for _ in range(5):
            await asyncio.sleep(0)
        assert entered[HARD_LIMIT:] == [217]
        # An initial grade gets the next vacancy; reviews cannot monopolize it.
        gates[1].set()
        for _ in range(5):
            await asyncio.sleep(0)
        assert entered[HARD_LIMIT:] == [217, HARD_LIMIT]
        for gate in gates:
            gate.set()
        await asyncio.gather(*tasks)
        assert len(entered) == 218 and not active
        assert asyncio.get_running_loop()._smartai_grading_windows == {}
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_preparation_alternates_busy_lanes_without_starving_either(monkeypatch):
    from backend.services import grading_memory
    monkeypatch.setattr(grading_memory, "HARD_LIMIT", 1)
    provider, order = object(), []

    async def work(label, review):
        async with prepared_grading(provider, review=review):
            order.append(label)
            await asyncio.sleep(0)

    async with prepared_grading(provider):
        tasks = [asyncio.create_task(work(f"initial{i}", False)) for i in range(4)]
        tasks += [asyncio.create_task(work(f"review{i}", True)) for i in range(4)]
        await asyncio.sleep(0)
    await asyncio.gather(*tasks)
    assert order == [label for i in range(4) for label in (f"review{i}", f"initial{i}")]
    assert asyncio.get_running_loop()._smartai_grading_windows == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("after_grant", [False, True])
async def test_cancelled_review_returns_waiting_or_just_granted_permit(monkeypatch, after_grant):
    from backend.services import grading_memory
    monkeypatch.setattr(grading_memory, "HARD_LIMIT", 1)
    provider = object()
    occupied = prepared_grading(provider)
    await occupied.__aenter__()

    async def review():
        async with prepared_grading(provider, review=True):
            pytest.fail("Cancelled review must not start work.")

    task = asyncio.create_task(review())
    await asyncio.sleep(0)
    if after_grant:
        await occupied.__aexit__(None, None, None)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    if not after_grant:
        await occupied.__aexit__(None, None, None)
    async with asyncio.timeout(1):
        async with prepared_grading(provider):
            pass
    assert asyncio.get_running_loop()._smartai_grading_windows == {}


def test_idle_reclaim_rereads_memory_without_lowering_reserve():
    clock, host = FakeClock(), FakeHost()
    available, calls = [450], []

    def reclaim():
        calls.append(clock())
        available[0] = 650
        return True

    scheduler = ModelScheduler(clock=clock, host_capacity=host,
        memory_reader=lambda: (available[0] * 1024**2, 1906 * 1024**2),
        memory_reclaimer=reclaim, pressure_clear=lambda: False)
    assert scheduler._memory_slot_limit(32 * 1024**2, clock()) == HARD_LIMIT
    assert scheduler.memory_sample[0] == 650 * 1024**2
    available[0] = 450
    clock.now += 5
    assert scheduler._memory_slot_limit(32 * 1024**2, clock()) == 0
    assert len(calls) == 1
    clock.now += 30
    scheduler.active = 1
    scheduler._memory_slot_limit(32 * 1024**2, clock())
    assert len(calls) == 1
    scheduler.active = 0
    clock.now += 5
    scheduler._memory_slot_limit(32 * 1024**2, clock())
    assert len(calls) == 2


def test_reclaim_failure_keeps_low_memory_guard():
    def broken():
        raise OSError("unsupported allocator")
    scheduler = ModelScheduler(host_capacity=FakeHost(), memory_reclaimer=broken,
        memory_reader=lambda: (450 * 1024**2, 1906 * 1024**2), pressure_clear=lambda: False)
    assert scheduler._memory_slot_limit(32 * 1024**2, 100) == 0


def test_reclaim_collects_cycles_before_trimming(monkeypatch):
    calls = []
    monkeypatch.setattr(memory_reclaim, "_malloc_trim", lambda: lambda pad: calls.append(("trim", pad)))
    monkeypatch.setattr(memory_reclaim.gc, "collect", lambda: calls.append("gc"))
    assert memory_reclaim.reclaim_idle_memory() is True
    assert calls == ["gc", ("trim", 0)]
    monkeypatch.setattr(memory_reclaim, "_malloc_trim", lambda: None)
    assert memory_reclaim.reclaim_idle_memory() is False
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_periodic_cache_expiry_runs_without_recognition_requests(monkeypatch):
    from backend.services import question_sources
    from backend.tests.test_recognition_local_cache import cache, context
    instance, now = cache(ttl_seconds=10)
    instance.put(**context(), value=b"expired")
    now[0] += 5
    instance.put(**context(2), value=b"still live")
    now[0] += 5
    original_sleep = asyncio.sleep
    sleeps = []

    async def tick(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 1:
            raise asyncio.CancelledError
        await original_sleep(0)

    monkeypatch.setattr(question_sources, "_CACHE", instance)
    monkeypatch.setattr(question_sources.asyncio, "sleep", tick)
    with pytest.raises(asyncio.CancelledError):
        await question_sources.expire_recognition_cache()
    # Inspect storage directly: get/stats would themselves expire entries.
    assert instance._size_bytes == len(b"still live")
    assert len(instance._entries) == 1
    assert sleeps == [30, 30]


@pytest.mark.asyncio
async def test_application_stops_its_cache_cleanup_task(monkeypatch):
    from backend.main import create_app
    from backend.services import question_sources
    entered, stopped = asyncio.Event(), asyncio.Event()
    async def cleanup():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    monkeypatch.setattr(question_sources, "expire_recognition_cache", cleanup)
    app = create_app()
    await next(f for f in app.router.on_startup if f.__name__ == "_start_recognition_cache_cleanup")()
    await entered.wait()
    await next(f for f in app.router.on_shutdown if f.__name__ == "_stop_recognition_cache_cleanup")()
    assert stopped.is_set()
