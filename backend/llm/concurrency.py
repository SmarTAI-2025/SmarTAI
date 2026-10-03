"""Automatic, bounded model admission; no probes, profiler or extra service.

Quota/latency state is shared by credential + model within one event loop.
The host permit fence additionally bounds calls across local worker processes.
Memory is a coarse admission guard, not a promise of machine capacity.
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import sys
import time
from collections import Counter, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HARD_LIMIT = 50
INITIAL_SECONDS = 120.0
MEMORY_PER_CALL = 32 * 1024 * 1024
MEMORY_SAMPLE_SECONDS = 5.0


def initial_concurrency(rpm: int) -> int:
    return target_concurrency(rpm, INITIAL_SECONDS)


def target_concurrency(rpm: int, seconds: float) -> int:
    if rpm <= 0:
        return HARD_LIMIT
    return min(HARD_LIMIT, max(1, math.ceil(rpm * seconds / 60.0)))


def quota_key(config: Any, endpoint: str) -> str:
    # This digest stays in memory and is never logged or returned to clients.
    # The same credential/model used by different task registries shares RPM.
    value = "\0".join((endpoint, str(config.provider_type), str(config.model), str(config.api_key)))
    return hashlib.sha256(value.encode()).hexdigest()


def read_memory() -> tuple[int, int] | None:
    """Linux whole-host MemAvailable/total; a few KB once per five seconds."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        values = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            name, _, rest = line.partition(":")
            if name in {"MemAvailable", "MemTotal"}:
                values[name] = int(rest.split()[0]) * 1024
        return values["MemAvailable"], values["MemTotal"]
    except (OSError, ValueError, KeyError, IndexError):
        # Unreadable Linux metrics must not disable the memory guard.
        return (0, 2 * 1024**3)


def request_memory(messages: list[Any]) -> int:
    """Conservative rough allowance for SDK/JSON copies, without serializing."""
    size = 0
    for message in messages:
        content = getattr(message, "content", "")
        if isinstance(content, str):
            size += len(content) * 8
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, str):
                    size += len(block) * 8
                elif isinstance(block, dict):
                    text = block.get("text", "")
                    if isinstance(text, str):
                        size += len(text) * 8
                    image = block.get("image_url", "")
                    url = image.get("url", "") if isinstance(image, dict) else image
                    if isinstance(url, str):
                        size += len(url) * 4
    return MEMORY_PER_CALL + size


@dataclass
class _Budget:
    default_rpm: int
    seconds: float = INITIAL_SECONDS
    active: int = 0
    # Public RPM is capped at 10,000. Retain enough actual starts for that
    # window even if an unlimited config later shares a limited credential.
    starts: deque[float] = field(default_factory=lambda: deque(maxlen=10_000))
    next_start: float = 0.0
    requests: Counter = field(default_factory=Counter)
    ceiling: int = HARD_LIMIT
    blocked_until: float = 0.0
    last_reduction: float = -math.inf
    recovery_after: float = 0.0

    @property
    def rpm(self) -> int:
        known = [rpm for rpm, count in self.requests.items() if rpm > 0 and count > 0]
        return min(known) if known else self.default_rpm

    @property
    def limit(self) -> int:
        return min(self.ceiling, target_concurrency(self.rpm, self.seconds))


@dataclass
class _Waiter:
    key: str
    rpm: int
    owner: str
    endpoint: str
    endpoint_limit: int
    memory: int
    future: asyncio.Future
    ready: Any = None


class _Lease:
    def __init__(self, scheduler: "ModelScheduler", waiter: _Waiter, host: Any):
        self.scheduler, self.waiter, self.host = scheduler, waiter, host
        self.released = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        scheduler = self.scheduler
        scheduler.active -= 1
        scheduler.memory_active -= self.waiter.memory
        scheduler.budgets[self.waiter.key].active -= 1
        scheduler._forget_request(self.waiter.key, self.waiter.rpm)
        scheduler.endpoints[self.waiter.endpoint] -= 1
        self.host.release()
        scheduler.wake.set()


class ModelScheduler:
    def __init__(self, *, memory_reader=read_memory, host_capacity=None, clock=time.monotonic):
        if host_capacity is None:
            from backend.llm.host_capacity import HostCapacity
            host_capacity = HostCapacity()
        self.host_capacity = host_capacity
        self.memory_reader = memory_reader
        self.clock = clock
        self.budgets: dict[str, _Budget] = {}
        self.endpoints: dict[str, int] = {}
        self.queues: dict[str, deque[_Waiter]] = {}
        self.owners: deque[str] = deque()
        self.active = 0
        self.memory_active = 0
        self.memory_peak_budget: int | None = None
        self.memory_sample_active = 0
        self.memory_at = -math.inf
        self.memory_sample: tuple[int, int] | None = None
        self.wake = asyncio.Event()
        self.pump: asyncio.Task | None = None
        self.pending: set[asyncio.Future] = set()
        self._thread_limiter = None
        # Local Gemini proxy mode needs one client per sync thread. Bound
        # prepared clients as well as actual calls, including RPM waiters.
        self.proxy_slots = asyncio.Semaphore(HARD_LIMIT)

    @property
    def thread_limiter(self):
        if self._thread_limiter is None:
            import anyio
            self._thread_limiter = anyio.CapacityLimiter(HARD_LIMIT)
        return self._thread_limiter

    def budget(self, key: str, rpm: int) -> _Budget:
        if key not in self.budgets:
            self.budgets[key] = _Budget(rpm)
        else:
            self.budgets[key].default_rpm = rpm
        return self.budgets[key]

    def limit(self, key: str, rpm: int) -> int:
        return self.budget(key, rpm).limit

    def _forget_request(self, key: str, rpm: int) -> None:
        requests = self.budgets[key].requests
        requests[rpm] -= 1
        if requests[rpm] <= 0:
            del requests[rpm]

    def record_success(self, key: str, rpm: int, seconds: float, *, allow_growth=True) -> None:
        if math.isfinite(seconds) and seconds > 0:
            budget = self.budget(key, rpm)
            # Constant memory and O(1): no list of historic calls or profiling.
            now = self.clock()
            healthy = allow_growth and now >= budget.recovery_after
            if healthy or seconds <= budget.seconds:
                budget.seconds = budget.seconds * 0.8 + seconds * 0.2
            if healthy and budget.ceiling < HARD_LIMIT:
                budget.ceiling += 1
                budget.recovery_after = now + 30.0
            self.wake.set()

    def record_overload(self, key: str, rpm: int, retry_after: float | None = None) -> None:
        """Only explicit concurrency overload; generic 429 may mean TPM/billing."""
        budget = self.budget(key, rpm)
        now = self.clock()
        # A burst of in-flight failures is one reduction, not 50 halvings.
        if now - budget.last_reduction >= 5.0:
            budget.ceiling = max(1, math.ceil(budget.limit / 2))
            budget.last_reduction = now
        delay = retry_after if (
            type(retry_after) in (int, float) and math.isfinite(retry_after)
        ) else 5.0
        budget.blocked_until = max(budget.blocked_until, now + max(5.0, delay))
        budget.recovery_after = max(budget.recovery_after, now + 60.0)
        self.wake.set()

    def _memory_allows(self, cost: int, now: float) -> bool:
        if now - self.memory_at >= MEMORY_SAMPLE_SECONDS:
            self.memory_sample = self.memory_reader()
            self.memory_at = now
            self.memory_sample_active = self.memory_active
            if self.memory_sample is not None:
                available, total = self.memory_sample
                reserve = max(512 * 1024**2, total // 4)
                if self.active == 0 or self.memory_peak_budget is None:
                    self.memory_peak_budget = max(0, available - reserve)
        if self.memory_sample is None:
            # Local non-Linux validation retains the hard host cap. Production
            # Lightsail/Linux additionally uses the whole-host memory guard.
            return True
        available, total = self.memory_sample
        reserve = max(512 * 1024**2, total // 4)
        # Keep the idle-sampled peak budget until the active batch drains:
        # waiting SDK calls may not have allocated their response buffers yet.
        # The live check is separate, so existing RSS is not subtracted twice.
        unsampled = max(0, self.memory_active - self.memory_sample_active)
        return (
            self.memory_active + cost <= self.memory_peak_budget
            and available - unsampled - cost >= reserve
        )

    async def acquire(self, *, key: str, rpm: int, owner: str, endpoint: str,
                      endpoint_limit: int, memory: int, ready=None) -> _Lease:
        self.budget(key, rpm)
        self.budgets[key].requests[rpm] += 1
        future = asyncio.get_running_loop().create_future()
        self.pending.add(future)
        waiter = _Waiter(key, rpm, owner, endpoint, min(HARD_LIMIT, max(1, endpoint_limit)), memory, future, ready)
        if owner not in self.queues:
            self.queues[owner] = deque()
            self.owners.append(owner)
        self.queues[owner].append(waiter)
        self.wake.set()
        if self.pump is None or self.pump.done():
            self.pump = asyncio.create_task(self._run())
        try:
            return await asyncio.shield(future)
        except BaseException:
            if future.done() and not future.cancelled():
                try:
                    permit = future.result()
                except BaseException:
                    self._forget_request(key, rpm)
                else:
                    permit.release()
            else:
                future.cancel()
                self._forget_request(key, rpm)
            self.wake.set()
            raise
        finally:
            self.pending.discard(future)

    def _eligible(self, waiter: _Waiter, now: float) -> bool:
        if waiter.ready is not None and not waiter.ready():
            return False
        budget = self.budgets[waiter.key]
        if now < budget.blocked_until:
            return False
        if budget.active >= budget.limit or self.endpoints.get(waiter.endpoint, 0) >= waiter.endpoint_limit:
            return False
        while budget.starts and budget.starts[0] <= now - 60:
            budget.starts.popleft()
        if budget.rpm > 0 and (now < budget.next_start or len(budget.starts) >= budget.rpm):
            return False
        return self._memory_allows(waiter.memory, now)

    def _dispatch(self) -> bool:
        if self.active >= HARD_LIMIT:
            return False
        made_progress = False
        # One turn per owner. A quota-blocked model must not block that owner's
        # other models or any other user's ready requests.
        for _ in range(len(self.owners)):
            owner = self.owners.popleft()
            queue = self.queues[owner]
            candidate = None
            now = self.clock()
            for _ in range(len(queue)):
                waiter = queue.popleft()
                if waiter.future.cancelled():
                    continue
                if candidate is None and self.active < HARD_LIMIT and self._eligible(waiter, now):
                    candidate = waiter
                    break
                queue.append(waiter)
            if candidate is not None:
                try:
                    host = self.host_capacity.try_acquire(HARD_LIMIT)
                except Exception:
                    queue.appendleft(candidate)
                    self.owners.appendleft(owner)
                    raise
                if host is None:
                    queue.appendleft(candidate)
                    self.owners.append(owner)
                    # All callers use the same 50 host slots; checking every
                    # queued owner again cannot find a free slot this round.
                    return made_progress
                else:
                    budget = self.budgets[candidate.key]
                    budget.active += 1
                    budget.starts.append(now)
                    if budget.rpm > 0:
                        budget.next_start = now + 60.0 / budget.rpm
                    self.active += 1
                    self.memory_active += candidate.memory
                    self.endpoints[candidate.endpoint] = self.endpoints.get(candidate.endpoint, 0) + 1
                    candidate.future.set_result(_Lease(self, candidate, host))
                    made_progress = True
            if queue:
                self.owners.append(owner)
            else:
                del self.queues[owner]
        return made_progress

    async def _run(self) -> None:
        try:
            while self.owners:
                self.wake.clear()
                if self._dispatch():
                    # Give new users a chance to enqueue between admission rounds.
                    await asyncio.sleep(0)
                    continue
                try:
                    # One timer for the scheduler, not a poller per queued call.
                    await asyncio.wait_for(self.wake.wait(), timeout=0.25)
                except asyncio.TimeoutError:
                    pass
        except Exception as exc:
            for future in self.pending:
                if not future.done():
                    future.set_exception(exc)
            self.queues.clear()
            self.owners.clear()

    @asynccontextmanager
    async def lease(self, **kwargs):
        permit = await self.acquire(**kwargs)
        try:
            yield
        finally:
            permit.release()


def get_scheduler() -> ModelScheduler:
    loop = asyncio.get_running_loop()
    # Attaching to the loop avoids a global weak-key/value-reference cycle that
    # would retain finished validation loops and their host-directory handles.
    scheduler = getattr(loop, "_smartai_model_scheduler", None)
    if scheduler is None:
        scheduler = ModelScheduler()
        setattr(loop, "_smartai_model_scheduler", scheduler)
    return scheduler
