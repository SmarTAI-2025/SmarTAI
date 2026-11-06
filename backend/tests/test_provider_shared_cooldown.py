"""Shared quota waits with real provider boundaries and synthetic responses only."""
import asyncio
from types import SimpleNamespace

import pytest

from backend.config import settings
from backend.llm import providers
from backend.llm.concurrency import ModelScheduler
from backend.llm.providers import ProviderRequestError
from backend.tests.test_automatic_model_scheduling import FakeClock, FakeHost, turns
from backend.tests.test_provider_response_deadlines import MESSAGES, ROUTES, provider_for


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("retry_after", [None, 120])
async def test_429_freezes_queued_calls_but_preserves_inflight_success(monkeypatch, route, retry_after):
    clock = FakeClock()
    scheduler = ModelScheduler(clock=clock, host_capacity=FakeHost(), memory_reader=lambda: None)
    monkeypatch.setattr(providers, "get_scheduler", lambda: scheduler)
    monkeypatch.setattr(providers, "_ENDPOINT_BREAKERS", {})
    monkeypatch.setattr(settings, "max_concurrent_llm_per_endpoint", 2)
    first_started, started, release, reject = (asyncio.Event() for _ in range(4))
    sends = []

    async def invoke(_):
        sends.append(clock())
        index = len(sends)
        if index == 1:
            first_started.set()
            await release.wait()
        elif index == 2:
            started.set()
            await reject.wait()
            raise ProviderRequestError("provider_rate_limited", status_code=429, retry_after=retry_after)
        return SimpleNamespace(content="complete answer")

    async with provider_for(route, invoke, monkeypatch) as provider:
        active = asyncio.create_task(provider.ainvoke(MESSAGES))
        tasks = [active]
        try:
            # Proxy clients initialize on worker threads; task creation order
            # does not guarantee HTTP arrival order. Establish both roles.
            await asyncio.wait_for(first_started.wait(), 5)
            refused = asyncio.create_task(provider.ainvoke(MESSAGES))
            tasks.append(refused)
            await asyncio.wait_for(started.wait(), 5)
            queued = asyncio.create_task(provider.ainvoke(MESSAGES))
            tasks.append(queued)
            await turns()
            reject.set()
            with pytest.raises(ProviderRequestError):
                await refused
            await turns()
            assert len(sends) == 2, "a queued call overtook the shared cooldown"
            release.set()
            assert (await active).content == "complete answer"
            await turns()
            assert not queued.done(), "an old in-flight success cleared the cooldown"
            delay = max(65, retry_after or 0)
            clock.advance(delay - 0.01, scheduler)
            await turns()
            assert not queued.done()
            clock.advance(0.01, scheduler)
            assert (await asyncio.wait_for(queued, 2)).content == "complete answer"
            assert sends == [100.0, 100.0, 100.0 + delay]
        finally:
            release.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        assert scheduler.active == 0


@pytest.mark.asyncio
async def test_late_refusal_joins_fixed_deadline_without_blocking_other_credentials(monkeypatch):
    from backend.tests.test_automatic_model_scheduling import call
    clock = FakeClock()
    scheduler = ModelScheduler(clock=clock, host_capacity=FakeHost(), memory_reader=lambda: None)
    await scheduler.record_rate_limit("same", 0, 65)
    waiting = asyncio.create_task(scheduler.acquire(**call("same", owner="other-task")))
    try:
        other = await scheduler.acquire(**call("different"))
        other.release()
        clock.advance(30, scheduler)
        await scheduler.record_rate_limit("same", 0, 65, started_at=100)
        assert scheduler.cooldown_remaining("same") == 35
        clock.advance(34, scheduler)
        await turns()
        assert not waiting.done()
        clock.advance(1, scheduler)
        permit = await asyncio.wait_for(waiting, 2)
        permit.release()
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


def test_cooldown_is_shared_with_another_process_even_without_rpm(tmp_path):
    import subprocess
    import sys
    from backend.llm.host_capacity import HostCapacity
    capacity = HostCapacity(tmp_path / "capacity")
    assert capacity.defer_start("same", 65)
    script = """
from backend.llm.host_capacity import HostCapacity
import sys
c = HostCapacity(sys.argv[1])
assert not c.try_start('same', 0)
assert not c.try_start('same', 6)
assert c.try_start('different', 0)
"""
    child = subprocess.run([sys.executable, "-c", script, str(capacity.directory)], capture_output=True, timeout=10)
    assert child.returncode == 0, child.stderr.decode()


@pytest.mark.asyncio
async def test_low_rpm_batch_improves_with_shared_cooldown(monkeypatch, record_property):
    """A/B the real admission/provider/retry path against a constructed RPM=6 API.

    Unknown configured RPM, five local slots, three attempts per logical call.
    Time and vendor responses are synthetic; no remote API is contacted.
    """
    from collections import Counter, deque
    from backend.tools import structured_llm as llm

    async def run(shared, *, adaptive=False, saved=None):
        clock = FakeClock()
        scheduler = ModelScheduler(clock=clock, host_capacity=FakeHost(), memory_reader=lambda: None)
        monkeypatch.setattr(providers, "get_scheduler", lambda: scheduler)
        monkeypatch.setattr(providers, "_ENDPOINT_BREAKERS", {})
        monkeypatch.setattr(settings, "max_concurrent_llm_per_endpoint", 5)
        sleepers, accepted, sends, learned = [], deque(), [], []

        async def sleep(seconds):
            future = asyncio.get_running_loop().create_future()
            sleepers.append((clock() + float(seconds), future))
            await future

        async def ignore_cooldown(*args, **kwargs):
            pass

        if not shared:
            monkeypatch.setattr(scheduler, "record_rate_limit", ignore_cooldown)
        monkeypatch.setattr(llm._ainvoke_with_retry.retry, "sleep", sleep)
        monkeypatch.setattr(llm.random, "uniform", lambda _low, _high: 1.0)

        async def invoke(_):
            while accepted and accepted[0] <= clock() - 60:
                accepted.popleft()
            limited = len(accepted) >= 6
            sends.append((clock(), limited, asyncio.current_task().get_name()))
            if not limited:
                accepted.append(clock())
            await sleep(0.25 if limited else 3.0)
            if limited:
                raise ProviderRequestError("provider_rate_limited", status_code=429)
            return SimpleNamespace(content="complete answer")

        async with provider_for("openai", invoke, monkeypatch) as provider:
            if adaptive:
                provider._adaptive_binding = ("owner:byok", saved, 1, lambda update: learned.append(update) or True)
            tasks = [asyncio.create_task(llm.ainvoke_with_retry(provider, MESSAGES), name=f"logical-{index}") for index in range(30)]
            try:
                for _ in range(2400):
                    await turns()
                    if all(task.done() for task in tasks):
                        break
                    clock.advance(0.25, scheduler)
                    for when, future in sleepers:
                        if when <= clock() and not future.done():
                            future.set_result(None)
                assert all(task.done() for task in tasks), "constructed batch did not finish in 600s"
                results = await asyncio.gather(*tasks, return_exceptions=True)
                assert scheduler.active == 0
                failures = Counter(name for _, limited, name in sends if limited)
                histogram = dict(sorted(Counter(failures[f"logical-{index}"] for index in range(30)).items()))
                return {"successes": sum(not isinstance(result, BaseException) for result in results),
                        "429s": sum(limited for _, limited, _ in sends), "seconds": clock() - 100,
                        "429s_per_logical_call": histogram,
                        "learned": learned}
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    before, after = await run(False), await run(True)
    record_property("without_shared_cooldown", str(before))
    record_property("with_shared_cooldown", str(after))
    assert after["successes"] == 30
    assert before["successes"] < after["successes"]
    assert after["429s"] < before["429s"]
    adaptive = await run(True, adaptive=True)
    assert adaptive["successes"] == 30 and adaptive["429s"] < after["429s"]
    assert adaptive["learned"]
    warm = await run(True, adaptive=True, saved=adaptive["learned"][-1])
    assert warm["successes"] == 30 and warm["429s"] == 0 and warm["learned"] == []
    record_property("with_adaptive_admission", str(adaptive))
    record_property("reloaded_adaptive_admission", str(warm))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["429", "timeout", "503"])
async def test_late_retry_waits_only_remaining_group_time(monkeypatch, kind):
    import httpx
    from backend.tools import structured_llm as llm
    clock = FakeClock()
    scheduler = ModelScheduler(clock=clock, host_capacity=FakeHost(), memory_reader=lambda: None)
    monkeypatch.setattr(providers, "get_scheduler", lambda: scheduler)
    sleeps, sends = [], []
    async def sleep(seconds):
        sleeps.append(seconds)
        clock.advance(seconds, scheduler)
    monkeypatch.setattr(llm._ainvoke_with_retry.retry, "sleep", sleep)
    async def invoke(_):
        sends.append(clock())
        if len(sends) == 1:
            key = next(iter(scheduler.budgets))
            await scheduler.record_rate_limit(key, 0, 65, started_at=clock())
            clock.advance(60, scheduler)
            if kind == "timeout":
                raise httpx.ReadTimeout("synthetic late timeout")
            raise ProviderRequestError("provider_rate_limited" if kind == "429" else "provider_upstream_unavailable",
                                       status_code=429 if kind == "429" else 503)
        return SimpleNamespace(content="retained success")
    async with provider_for("openai", invoke, monkeypatch) as provider:
        result = await llm.ainvoke_with_retry(provider, MESSAGES)
    assert result.content == "retained success"
    assert sends == [100, 165] and sleeps == [5]


@pytest.mark.asyncio
@pytest.mark.parametrize("response_only", [False, True])
async def test_run_stop_keeps_the_response_that_just_completed(monkeypatch, response_only):
    from contextlib import nullcontext
    from backend.llm.request_timeouts import response_timeout_scope
    from backend.services.execution_control import current_execution, run_controlled
    clock = FakeClock()
    scheduler = ModelScheduler(clock=clock, host_capacity=FakeHost(), memory_reader=lambda: None)
    monkeypatch.setattr(providers, "get_scheduler", lambda: scheduler)
    async def invoke(_):
        # All later cycles fail while this original response is still running.
        for start in (100, 166, 232):
            current_execution().rate_limited(provider, started_at=start, until=start + 65)
        return SimpleNamespace(content="keep original result")
    async with provider_for("openai", invoke, monkeypatch) as provider:
        async def work():
            with response_timeout_scope(180, response_only=True) if response_only else nullcontext():
                response = await provider.ainvoke(MESSAGES)
            await asyncio.sleep(0)
            assert response.content == "keep original result"
            assert current_execution().blocked
        await run_controlled("retain-completed", lambda: False, work())
