"""Memory lifecycle contracts, including cancellation and independent experts."""
import asyncio
from types import SimpleNamespace

import pytest

from backend.llm import memory_reclaim
from backend.llm.concurrency import HARD_LIMIT, ModelScheduler
from backend.services.grading_memory import prepared_grading
from backend.tests.test_automatic_model_scheduling import FakeClock, FakeHost


@pytest.mark.asyncio
async def test_prepared_window_is_bounded_and_other_expert_keeps_running():
    slow, fast = SimpleNamespace(), SimpleNamespace()  # Unhashable providers work too.
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
