"""Admission behavior with deterministic clocks and no network/model spend."""

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

from backend.llm.concurrency import (
    HARD_LIMIT, MEMORY_PER_CALL, ModelScheduler, initial_concurrency,
    quota_key, target_concurrency,
)
from backend.llm.providers import (
    BaseProvider, GeminiProvider, ProviderRequestError, SafeRelayProvider, ZhipuProvider,
)
from backend.models import ProviderConfig


class FakeClock:
    now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds, scheduler):
        self.now += seconds
        scheduler.wake.set()


class FakeHostLease:
    def __init__(self, host):
        self.host, self.released = host, False

    def release(self):
        if not self.released:
            self.released = True
            self.host.active -= 1


class FakeHost:
    def __init__(self, capacity=1000):
        self.capacity = capacity
        self.active = self.peak = 0
        self.failure = False

    def try_acquire(self, limit):
        if self.failure:
            raise RuntimeError("synthetic host failure")
        if self.active >= min(self.capacity, limit):
            return None
        self.active += 1
        self.peak = max(self.peak, self.active)
        return FakeHostLease(self)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def host():
    return FakeHost()


@pytest.fixture
def scheduler(clock, host):
    return ModelScheduler(clock=clock, host_capacity=host, memory_reader=lambda: None)


@pytest.fixture(autouse=True)
def isolated_endpoint_breakers():
    from backend.llm import providers
    providers._ENDPOINT_BREAKERS.clear()
    yield
    providers._ENDPOINT_BREAKERS.clear()


def call(key="model", *, rpm=0, owner="teacher-a", memory=MEMORY_PER_CALL, ready=None):
    return dict(key=key, rpm=rpm, owner=owner, endpoint=key,
                endpoint_limit=50, memory=memory, ready=ready)


async def turns(count=30):
    for _ in range(count):
        await asyncio.sleep(0)


async def cleanup(tasks):
    for task in tasks:
        if task.done() and not task.cancelled() and task.exception() is None:
            task.result().release()
        elif not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await turns()


@pytest.mark.parametrize(("rpm", "expected"), [(0, 50), (1, 2), (5, 10), (10, 20), (30, 50), (10000, 50)])
def test_initial_formula_and_hard_ceiling(rpm, expected):
    assert initial_concurrency(rpm) == expected
    assert target_concurrency(1, 1) == 1
    assert target_concurrency(10, 61) == 11


class RecordingProvider(BaseProvider):
    provider_type = "openai"

    def __init__(self, config, clock, starts):
        super().__init__(config)
        self.clock, self.starts = clock, starts

    def _build_client_sync(self):
        async def invoke(messages, **_kwargs):
            self.starts.append(self.clock())
            return SimpleNamespace(content="ok")
        return SimpleNamespace(ainvoke=invoke)


@pytest.mark.asyncio
async def test_separate_task_provider_instances_share_actual_rpm(scheduler, clock, monkeypatch):
    monkeypatch.setattr("backend.llm.providers.get_scheduler", lambda: scheduler)
    starts = []
    first = RecordingProvider(ProviderConfig(
        provider_type="openai", model="shared", api_key="fake-secret", rpm=1,
        scheduling_owner="teacher-a",
    ), clock, starts)
    second = RecordingProvider(first.config.model_copy(update={
        "scheduling_owner": "teacher-b",
    }), clock, starts)
    messages = [HumanMessage(content="test")]
    await first.ainvoke(messages)
    pending = asyncio.create_task(second.ainvoke(messages))
    try:
        await turns()
        assert starts == [100.0]
        assert not pending.done()
        clock.advance(59, scheduler)
        await turns()
        assert not pending.done()
        clock.advance(1, scheduler)
        await asyncio.wait_for(pending, timeout=1)
        assert starts == [100.0, 160.0]
        assert scheduler.active == 0
        # Changing owner does not bypass a credential/model quota; changing
        # credential or model does get an independent supplier budget.
        key = quota_key(first.config, "endpoint")
        assert key == quota_key(second.config, "endpoint")
        assert key != quota_key(first.config.model_copy(update={"api_key": "other"}), "endpoint")
        assert key != quota_key(first.config.model_copy(update={"model": "other"}), "endpoint")
        assert "fake-secret" not in key
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("mode", ["native", "relay", "proxy"])
@pytest.mark.asyncio
async def test_slow_client_preparation_cannot_age_quota_and_create_send_burst(
    scheduler, clock, monkeypatch, mode,
):
    import httpx
    from anyio import to_thread

    monkeypatch.setattr("backend.llm.providers.get_scheduler", lambda: scheduler)
    prepared = asyncio.Event()
    starts = []
    config = ProviderConfig(
        provider_type="gemini" if mode == "proxy" else "openai",
        model=f"cold-{mode}", api_key="fake-cold", rpm=2,
        base_url="https://relay.example.com/v1" if mode == "relay" else None,
    )

    async def invoke(messages, **kwargs):
        starts.append(clock())
        return SimpleNamespace(content="ok")

    async def post(*args, **kwargs):
        starts.append(clock())
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        })

    async def prepare():
        await prepared.wait()
        return SimpleNamespace(ainvoke=invoke, post=post)

    if mode == "native":
        provider = RecordingProvider(config, clock, starts)
        monkeypatch.setattr(provider, "_get_client", prepare)
    elif mode == "relay":
        provider = SafeRelayProvider(config)
        monkeypatch.setattr(provider, "_relay_client", prepare)
    else:
        provider = GeminiProvider(config)
        monkeypatch.setattr(GeminiProvider, "_needs_proxy_mode", property(lambda self: True))

        def sync_invoke(messages, **kwargs):
            starts.append(clock())
            return SimpleNamespace(content="ok")

        async def run_sync(function, *args, **kwargs):
            if function.__name__ == "_build_client_sync":
                await prepared.wait()
                return SimpleNamespace(invoke=sync_invoke)
            return function(*args)

        monkeypatch.setattr(to_thread, "run_sync", run_sync)

    tasks = [asyncio.create_task(provider.ainvoke([HumanMessage(content="cold start")])) for _ in range(2)]
    try:
        await turns()
        clock.advance(90, scheduler)
        await turns()
        assert starts == []
        assert scheduler.active == 0
        assert not any(budget.starts for budget in scheduler.budgets.values())
        prepared.set()
        await turns(100)
        assert starts == [190.0]
        clock.advance(29, scheduler)
        await turns()
        assert starts == [190.0]
        clock.advance(1, scheduler)
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)
        assert starts == [190.0, 220.0]
        assert scheduler.active == 0
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_resource_wait_does_not_spend_or_age_rpm_reservations(scheduler, host, clock):
    host.capacity = 0
    tasks = [asyncio.create_task(scheduler.acquire(**call(rpm=2))) for _ in range(2)]
    try:
        await turns()
        clock.advance(90, scheduler)
        await turns()
        assert not any(task.done() for task in tasks)
        assert not scheduler.budgets["model"].starts
        host.capacity = 50
        scheduler.wake.set()
        await turns()
        assert sum(task.done() for task in tasks) == 1
        assert list(scheduler.budgets["model"].starts) == [190.0]
        clock.advance(29, scheduler)
        await turns()
        assert sum(task.done() for task in tasks) == 1
        clock.advance(1, scheduler)
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)
        assert list(scheduler.budgets["model"].starts) == [190.0, 220.0]
    finally:
        await cleanup(tasks)


@pytest.mark.asyncio
async def test_all_models_and_users_share_fifty_call_ceiling(scheduler, host):
    tasks = [asyncio.create_task(scheduler.acquire(**call(
        f"model-{index}", owner=f"teacher-{index % 3}",
    ))) for index in range(60)]
    try:
        await turns(200)
        assert sum(task.done() for task in tasks) == HARD_LIMIT
        assert scheduler.active == host.active == host.peak == 50
        admitted = [task for task in tasks if task.done()]
        for task in admitted[:10]:
            task.result().release()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)
        assert scheduler.active == host.active == 50
        assert host.peak == 50
    finally:
        await cleanup(tasks)
    assert scheduler.active == scheduler.memory_active == host.active == 0


@pytest.mark.asyncio
async def test_fairness_is_per_teacher_and_not_per_model_count(scheduler, host):
    host.capacity = 0
    first = [asyncio.create_task(scheduler.acquire(**call(
        f"model-{index}", owner="teacher-many-models",
    ))) for index in range(20)]
    second = asyncio.create_task(scheduler.acquire(**call("other", owner="teacher-one-model")))
    tasks = [*first, second]
    try:
        await turns()
        host.capacity = 2
        scheduler.wake.set()
        await turns(100)
        assert second.done()
        assert sum(task.done() for task in first) == 1
    finally:
        await cleanup(tasks)


@pytest.mark.parametrize(("occupied", "capacity"), [(0, 1), (49, 50)])
@pytest.mark.asyncio
async def test_backlogged_teachers_alternate_when_slots_free_one_at_a_time(
    scheduler, host, occupied, capacity,
):
    held = [await scheduler.acquire(**call("held", owner="holder")) for _ in range(occupied)]
    host.capacity = occupied
    tasks = []
    owners = {}
    for owner in ("teacher-a", "teacher-b"):
        for index in range(4):
            task = asyncio.create_task(scheduler.acquire(**call(f"{owner}-{index}", owner=owner)))
            tasks.append(task)
            owners[task] = owner
    try:
        await turns()
        host.capacity = capacity
        scheduler.wake.set()
        seen, served = set(), []
        for _ in range(4):
            await turns(50)
            fresh = [task for task in tasks if task.done() and task not in seen]
            assert len(fresh) == 1
            seen.add(fresh[0])
            served.append(owners[fresh[0]])
            fresh[0].result().release()
        assert all(left != right for left, right in zip(served, served[1:])), served
    finally:
        await cleanup(tasks)
        for lease in held:
            lease.release()


@pytest.mark.asyncio
async def test_quota_blocked_model_does_not_block_other_ready_work(scheduler):
    lease = await scheduler.acquire(**call("blocked", rpm=1))
    lease.release()
    blocked = asyncio.create_task(scheduler.acquire(**call("blocked", rpm=1)))
    ready = asyncio.create_task(scheduler.acquire(**call("healthy")))
    other = asyncio.create_task(scheduler.acquire(**call("other", owner="teacher-b")))
    try:
        await asyncio.wait_for(asyncio.gather(ready, other), timeout=1)
        assert not blocked.done()
    finally:
        await cleanup([blocked, ready, other])


@pytest.mark.asyncio
async def test_mixed_saved_rpm_is_conservative_until_old_requests_drain(scheduler, clock):
    old_lease = await scheduler.acquire(**call(rpm=2))
    pending = asyncio.create_task(scheduler.acquire(**call(rpm=10, owner="teacher-b")))
    try:
        await turns()
        clock.advance(6, scheduler)
        await turns()
        assert not pending.done()
        assert scheduler.budgets["model"].rpm == 2
        clock.advance(24, scheduler)
        new_lease = await asyncio.wait_for(pending, timeout=1)
        assert scheduler.budgets["model"].rpm == 2
        old_lease.release()
        assert scheduler.budgets["model"].rpm == 10
        assert scheduler.limit("model", 10) == 20
        new_lease.release()
    finally:
        old_lease.release()
        await cleanup([pending])


@pytest.mark.asyncio
async def test_waiting_cancellation_and_lease_body_failure_refund_resources(scheduler, host):
    host.capacity = 1
    running = await scheduler.acquire(**call("running"))
    pending = asyncio.create_task(scheduler.acquire(**call("cancelled", rpm=10)))
    await turns()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert scheduler.budgets["cancelled"].requests[10] == 0
    assert not scheduler.pending
    running.release()
    with pytest.raises(RuntimeError, match="body failure"):
        async with scheduler.lease(**call("error", rpm=10)):
            raise RuntimeError("body failure")
    assert scheduler.active == scheduler.memory_active == host.active == 0
    assert scheduler.budgets["error"].requests[10] == 0
    assert scheduler.endpoints["error"] == 0


@pytest.mark.parametrize("failure", ["eligibility", "host"])
@pytest.mark.asyncio
async def test_dispatch_failure_rejects_every_waiter_and_can_recover(scheduler, host, failure):
    def broken_ready():
        raise RuntimeError("synthetic eligibility failure")
    host.failure = failure == "host"
    tasks = [asyncio.create_task(scheduler.acquire(**call(
        f"error-{index}", rpm=10,
        ready=broken_ready if failure == "eligibility" else None,
    ))) for index in range(3)]
    responses = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=1)
    assert all(isinstance(response, RuntimeError) for response in responses)
    assert not scheduler.pending
    assert scheduler.active == scheduler.memory_active == host.active == 0
    assert all(budget.requests[10] == 0 for budget in scheduler.budgets.values())
    host.failure = False
    recovered = await asyncio.wait_for(scheduler.acquire(**call("recovered")), timeout=1)
    recovered.release()


@pytest.mark.asyncio
async def test_memory_sampling_is_cached_and_keeps_peak_reservations(clock, host):
    samples = []
    def memory_reader():
        samples.append(clock())
        return 768 * 1024**2, 2048 * 1024**2
    scheduler = ModelScheduler(clock=clock, host_capacity=host, memory_reader=memory_reader)
    tasks = [asyncio.create_task(scheduler.acquire(**call(f"model-{i}"))) for i in range(9)]
    try:
        await turns(100)
        assert sum(task.done() for task in tasks) == 8
        assert len(samples) == 1
        clock.advance(4, scheduler)
        await turns()
        assert len(samples) == 1
        clock.advance(1, scheduler)
        await turns()
        assert len(samples) == 2
        assert sum(task.done() for task in tasks) == 8
        next(task for task in tasks if task.done()).result().release()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)
        assert host.active == 8
    finally:
        await cleanup(tasks)


@pytest.mark.asyncio
async def test_materialized_memory_is_not_subtracted_twice(clock, host):
    available = [1024 * 1024**2]
    scheduler = ModelScheduler(clock=clock, host_capacity=host,
                               memory_reader=lambda: (available[0], 2048 * 1024**2))
    leases = [await scheduler.acquire(**call(f"model-{i}")) for i in range(8)]
    try:
        # The OS now reports the eight 32MiB allocations. Nine calls still fit
        # the original 512MiB peak budget; subtracting all active cost again
        # would incorrectly reject the ninth one.
        available[0] = 768 * 1024**2
        clock.advance(5, scheduler)
        ninth = await asyncio.wait_for(scheduler.acquire(**call("ninth")), timeout=1)
        leases.append(ninth)
        assert scheduler.active == 9
    finally:
        for lease in leases:
            lease.release()


@pytest.mark.asyncio
async def test_refreshed_low_memory_reserves_each_new_start_within_cached_sample(clock, host):
    available = [1024 * 1024**2]
    scheduler = ModelScheduler(clock=clock, host_capacity=host,
                               memory_reader=lambda: (available[0], 2048 * 1024**2))
    old = [await scheduler.acquire(**call(f"old-{i}")) for i in range(8)]
    tasks = []
    try:
        # Other application work consumes memory while model calls run. The
        # fresh sample has just 96MiB above the reserve: three new calls fit,
        # even though the original idle peak budget has room for eight.
        available[0] = 608 * 1024**2
        clock.advance(5, scheduler)
        tasks = [asyncio.create_task(scheduler.acquire(**call(f"new-{i}"))) for i in range(4)]
        await turns(100)
        assert sum(task.done() for task in tasks) == 3
        assert host.active == 11
        old[0].release()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)
        assert host.active == 11
    finally:
        for lease in old:
            lease.release()
        await cleanup(tasks)


def test_success_duration_updates_target_but_overload_cannot_raise_it(scheduler):
    initial = scheduler.limit("model", 10)
    scheduler.record_success("model", 10, 600, allow_growth=False)
    assert scheduler.limit("model", 10) == initial
    scheduler.record_success("model", 10, 60, allow_growth=False)
    assert scheduler.limit("model", 10) < initial
    scheduler.record_success("model", 10, 300)
    assert initial < scheduler.limit("model", 10) <= HARD_LIMIT


@pytest.mark.asyncio
async def test_recent_endpoint_overload_suppresses_success_duration_growth(scheduler, clock, monkeypatch):
    monkeypatch.setattr("backend.llm.providers.get_scheduler", lambda: scheduler)
    monkeypatch.setattr("backend.llm.providers.time.monotonic", clock)
    provider = RecordingProvider(ProviderConfig(
        provider_type="openai", model="recent-overload", api_key="fake-overload", rpm=10,
    ), clock, [])
    breaker = provider._endpoint_breaker()
    initial = provider.effective_concurrency
    breaker.record_failure()
    breaker.record_success()
    provider._record_duration(600_000)
    assert provider.effective_concurrency == initial
    clock.advance(61, scheduler)
    provider._record_duration(600_000)
    assert initial < provider.effective_concurrency <= HARD_LIMIT


def test_concurrency_overload_burst_reduces_once_and_recovers_gradually(scheduler, clock):
    assert scheduler.limit("busy", 10) == 20
    for _ in range(30):
        scheduler.record_overload("busy", 10)
    assert scheduler.limit("busy", 10) == 10
    assert scheduler.budgets["busy"].blocked_until == 105.0
    assert scheduler.limit("unrelated", 10) == 20

    clock.advance(5, scheduler)
    scheduler.record_overload("busy", 10)
    assert scheduler.limit("busy", 10) == 5
    scheduler.record_success("busy", 10, 600)
    assert scheduler.limit("busy", 10) == 5
    clock.advance(59, scheduler)
    scheduler.record_success("busy", 10, 120)
    assert scheduler.limit("busy", 10) == 5
    clock.advance(1, scheduler)
    scheduler.record_success("busy", 10, 120)
    assert scheduler.limit("busy", 10) == 6
    scheduler.record_success("busy", 10, 120)
    assert scheduler.limit("busy", 10) == 6
    clock.advance(30, scheduler)
    scheduler.record_success("busy", 10, 120)
    assert scheduler.limit("busy", 10) == 7


@pytest.mark.asyncio
async def test_concurrency_cooldown_is_shared_by_quota_but_not_unrelated_users(scheduler, clock):
    scheduler.record_overload("same-credential", 0, retry_after=12)
    tasks = [asyncio.create_task(scheduler.acquire(**call(
        "same-credential", owner=owner,
    ))) for owner in ("teacher-a", "teacher-b")]
    unrelated = await asyncio.wait_for(scheduler.acquire(**call(
        "different-credential", owner="teacher-c",
    )), timeout=1)
    try:
        await turns()
        assert not any(task.done() for task in tasks)
        assert scheduler.limit("same-credential", 0) == 25
        assert scheduler.limit("different-credential", 0) == 50
        clock.advance(11, scheduler)
        await turns()
        assert not any(task.done() for task in tasks)
        clock.advance(1, scheduler)
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)
    finally:
        unrelated.release()
        await cleanup(tasks)


@pytest.mark.parametrize("mode", ["native", "relay"])
@pytest.mark.parametrize("code", ["1305", "1302"])
@pytest.mark.asyncio
async def test_only_explicit_zhipu_concurrency_response_lowers_automatic_ceiling(
    scheduler, clock, monkeypatch, mode, code,
):
    import httpx
    from openai import RateLimitError
    from backend.llm.providers import endpoint_key

    monkeypatch.setattr("backend.llm.providers.get_scheduler", lambda: scheduler)
    config = ProviderConfig(
        provider_type="zhipu", model=f"test-{mode}-{code}", api_key="fake-zhipu", rpm=10,
        base_url="https://relay.example.com/v1" if mode == "relay" else None,
    )
    body = {"error": {"code": code, "message": "private upstream body"}}

    if mode == "native":
        provider = ZhipuProvider(config)

        async def invoke(*args, **kwargs):
            raise RateLimitError("rate limited", response=httpx.Response(
                429, headers={"retry-after": "12"},
                request=httpx.Request("POST", "https://open.bigmodel.cn"),
            ), body=body)

        async def client():
            return SimpleNamespace(ainvoke=invoke)
        monkeypatch.setattr(provider, "_get_client", client)
    else:
        provider = SafeRelayProvider(config)

        async def post(*args, **kwargs):
            return httpx.Response(429, headers={"retry-after": "12"}, json=body)

        async def client():
            return SimpleNamespace(post=post)
        monkeypatch.setattr(provider, "_relay_client", client)

    with pytest.raises((RateLimitError, ProviderRequestError)) as caught:
        await provider.ainvoke([HumanMessage(content="test")])
    budget = scheduler.budgets[quota_key(config, endpoint_key(config))]
    if code == "1305":
        assert isinstance(caught.value, ProviderRequestError)
        assert caught.value.code == "provider_overloaded"
        assert caught.value.retry_after == 12.0
        assert budget.limit == 10
        assert budget.blocked_until == 112.0
    else:
        assert budget.limit == 20
        assert budget.blocked_until == 0.0
    assert not provider._endpoint_breaker()._failures
    assert scheduler.active == scheduler.memory_active == 0
