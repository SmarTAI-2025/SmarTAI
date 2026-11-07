"""Automatic, bounded model admission; no probes, profiler or extra service.

Quota/latency state is shared by credential + model within one event loop.
The host permit fence additionally bounds calls across local worker processes.
Memory is a coarse admission guard, not a promise of machine capacity.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import sys
import time
from bisect import insort_right
from collections import Counter, deque
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from itertools import count
from pathlib import Path
from typing import Any

HARD_LIMIT = 50
INITIAL_SECONDS = 120.0
MEMORY_PER_CALL = 32 * 1024 * 1024
MEMORY_SAMPLE_SECONDS = 5.0
_MIB = 1024**2
logger = logging.getLogger(__name__)


@dataclass
class _AdmissionOrder:
    task: Any
    order: int | None = None


_order: ContextVar[_AdmissionOrder | None] = ContextVar("model_admission_order", default=None)
_sequence = count()


@contextmanager
def admission_order():
    """Retain a logical call's place across retries, never across child tasks."""
    current = _order.get()
    if current is not None and current.task is asyncio.current_task():
        yield
        return
    token = _order.set(_AdmissionOrder(asyncio.current_task()))
    try:
        yield
    finally:
        _order.reset(token)


def ordered_call(invoke):
    @wraps(invoke)
    async def wrapped(*args, **kwargs):
        with admission_order():
            return await invoke(*args, **kwargs)
    return wrapped


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


def memory_pressure_clear(proc: Path = Path("/proc"), root: Path = Path("/sys/fs/cgroup")) -> bool:
    """Only spend host headroom when PSI is quiet and no v2 group has its own ceiling."""
    try:
        group = next(line.removeprefix("0::") for line in (proc / "self/cgroup").read_text().splitlines()
                     if line.startswith("0::/"))
        if ".." in Path(group).parts or "memory" not in (root / "cgroup.controllers").read_text().split():
            return False
        directory = root / group.lstrip("/")
        while directory != root:
            if any((directory / name).read_text().strip() != "max" for name in ("memory.high", "memory.max")):
                return False
            directory = directory.parent
        # A cgroup namespace can expose a limited group as its apparent root.
        for name in ("memory.high", "memory.max"):
            path = root / name
            if path.exists() and path.read_text().strip() != "max":
                return False
        for line in (proc / "pressure/memory").read_text().splitlines():
            name, *fields = line.split()
            if name == "some":
                values = dict(field.split("=", 1) for field in fields)
                return float(values["avg10"]) == 0 and float(values["avg60"]) == 0
    except (OSError, KeyError, ValueError, StopIteration):
        pass
    return False


def _small_request_fits(sample, cost: int) -> bool:
    if sample is None:
        return False
    available, total = sample
    return (1536 * _MIB <= total <= 2048 * _MIB and 0 < cost <= 2 * MEMORY_PER_CALL
            and available - cost >= max(384 * _MIB, total // 5))


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
    last_start: float | None = None
    requests: Counter = field(default_factory=Counter)
    ceiling: int = HARD_LIMIT
    blocked_until: float = 0.0
    rate_limited_until: float | None = None
    last_reduction: float = -math.inf
    recovery_after: float = 0.0
    adaptive: dict = field(default_factory=dict)

    @property
    def rpm(self) -> int:
        known = [rpm for rpm, count in self.requests.items() if rpm > 0 and count > 0]
        return min(known) if known else (0 if self.requests else self.default_rpm)

    @property
    def next_start(self) -> float:
        return self.last_start + self.interval if self.last_start is not None else 0.0

    @property
    def limit(self) -> int:
        return min(self.ceiling, target_concurrency(self.rpm, self.seconds),
                   *(control.concurrency for control in self.adaptive.values()))

    @property
    def interval(self) -> float:
        return max(60.0 / self.rpm if self.rpm else 0.0,
                   *(control.interval for control in self.adaptive.values()), 0.0)


@dataclass
class _Waiter:
    key: str
    rpm: int
    owner: str
    endpoint: str
    endpoint_limit: int
    memory: int
    future: asyncio.Future
    order: int
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
    def __init__(self, *, memory_reader=read_memory, pressure_clear=memory_pressure_clear,
                 host_capacity=None, clock=time.monotonic):
        if host_capacity is None:
            from backend.config import settings
            from backend.llm.host_capacity import HostCapacity
            host_capacity = HostCapacity(recovery_seconds=float(settings.llm_rate_limit_retry_seconds))
        self.host_capacity = host_capacity
        self.memory_reader = memory_reader
        self.pressure_clear = pressure_clear
        self.quiet_small_host = False
        self.clock = clock
        self.budgets: dict[str, _Budget] = {}
        self.endpoints: dict[str, int] = {}
        self.queue: list[_Waiter] = []
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
        self._adaptive_keys: dict[str, str] = {}
        self._adaptive_revisions: dict[str, float] = {}
        self._adaptive_sources: dict[str, tuple[str, str]] = {}
        self._adaptive_refresh_after = 0.0
        self._adaptive_refresh_lock = asyncio.Lock()
        self._adaptive_refresh_task: asyncio.Task | None = None
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

    def bind_adaptive(self, key: str, rpm: int, identity: str, profile: dict | None,
                      revision: float, *, source: tuple[str, str] | None = None) -> None:
        from backend.llm.adaptive_admission import AdaptiveAdmission, valid_profile
        if revision < self._adaptive_revisions.get(identity, -math.inf):
            return  # A running task may still hold an edited/deleted snapshot.
        previous_key = self._adaptive_keys.get(identity)
        if previous_key is not None and (previous_key != key or revision > self._adaptive_revisions[identity]):
            self._detach_adaptive(identity)
        self._adaptive_keys[identity] = key
        self._adaptive_revisions[identity] = revision
        if source is not None and identity not in self._adaptive_sources:
            self._adaptive_sources[identity] = source
            self._adaptive_refresh_after = 0.0
        budget = self.budget(key, rpm)
        previous = budget.adaptive.get(identity)
        saved = valid_profile(profile)
        if (previous is None or previous.revision < revision
                or (previous.revision == revision and saved is not None
                    and saved["generation"] > previous.generation)):
            budget.adaptive[identity] = AdaptiveAdmission(profile, revision, rpm, self.clock())

    def _detach_adaptive(self, identity: str) -> None:
        key = self._adaptive_keys.pop(identity, None)
        if key is not None:
            budget = self.budgets[key]
            budget.adaptive.pop(identity, None)
            # Drop the departed configuration's pacing, not shared cooldowns
            # or already-counted starts/leases belonging to this quota group.
            self.wake.set()

    async def refresh_adaptive(self, *, force: bool = False) -> None:
        """One batched metadata read per 5s of demand, also across processes.

        No keys are decrypted. Deletion/config changes fence old snapshots;
        a newer saved generation wins over a conflicting stale candidate.
        """
        async with self._adaptive_refresh_lock:
            if not force and self.clock() < self._adaptive_refresh_after:
                return
            self._adaptive_refresh_after = self.clock() + 5.0
            observed = {identity: (key, self.budgets[key].adaptive[identity])
                        for identity, key in self._adaptive_keys.items() if identity in self._adaptive_sources}
            if not observed:
                return
            from backend.db.provider_repository import load_adaptive_states
            from backend.llm.adaptive_admission import AdaptiveAdmission, valid_profile
            try:
                async with asyncio.timeout(10):
                    states = await asyncio.to_thread(load_adaptive_states, {
                        identity: self._adaptive_sources[identity] for identity in observed})
            except Exception:
                self._adaptive_refresh_after = self.clock() + 30.0
                logger.warning("Unable to refresh BYOK admission metadata")
                return
            for identity, (key, control) in observed.items():
                if self._adaptive_keys.get(identity) != key or self.budgets[key].adaptive.get(identity) is not control:
                    continue  # A newer bind arrived while the database read ran.
                state = states.get(identity)
                revision = state["revision"] if state else math.inf
                self._adaptive_revisions[identity] = max(control.revision, revision)
                if state is None or not state["enabled"] or revision != control.revision:
                    self._detach_adaptive(identity)
                    continue
                saved = valid_profile(state["profile"])
                if saved is not None and saved["generation"] > control.generation:
                    if saved == control.pending:
                        control.saved(saved)  # Our own write is awaiting its acknowledgement.
                    else:
                        # Adopt the winner; do not simply renumber and overwrite
                        # it with stale observations. New traffic can learn again.
                        self.budgets[key].adaptive[identity] = AdaptiveAdmission(
                            saved, revision, control.rpm, self.clock())
                    self.wake.set()

    def record_adaptive_start(self, key: str, started_at: float) -> None:
        for control in self.budgets[key].adaptive.values():
            control.started(started_at)

    def record_adaptive_failure(self, key: str, started_at: float) -> None:
        for control in self.budgets[key].adaptive.values():
            control.finished(started_at, success=False)

    def adjustment_count(self, key: str) -> int:
        return sum(c.adjustments for c in self.budgets[key].adaptive.values())

    def record_adaptive_success(self, key: str, identity: str, revision: float, started_at: float,
                                *, run: str = "unscoped", busy: bool = False) -> dict | None:
        budget = self.budgets[key]
        for control in budget.adaptive.values():
            control.succeeded(now=self.clock(), started_at=started_at, run=run, busy=busy)
        # next_start derives from the current controls, including removals.
        control = budget.adaptive.get(identity)
        if (control is not None and control.revision == revision and control.pending is not None
                and self.clock() >= control.save_after):
            control.save_after = self.clock() + 30
            return dict(control.pending)
        return None

    def adaptive_saved(self, key: str, identity: str, profile: dict) -> None:
        control = self.budgets[key].adaptive.get(identity)
        if (control is not None and control.revision == profile["config_version"]
                and control.generation <= profile["generation"]):
            control.saved(profile)

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

    async def record_rate_limit(self, key: str, rpm: int, seconds: float, *,
                               run: str = "unscoped", started_at: float | None = None,
                               active: int = 1, extend: bool = False) -> float:
        """Pause all new calls sharing this quota, without cancelling responses.

        A generic 429 does not identify RPM, TPM or a concurrency ceiling.
        BYOK learning estimates operating parameters, never user quota fields.
        Publish before releasing the rejected call's slot, so a queued sibling
        cannot take that slot while only the rejected coroutine is sleeping.
        """
        budget = self.budget(key, rpm)
        now = self.clock()
        started_at = now if started_at is None else started_at
        previous_block = budget.blocked_until
        previous = budget.rate_limited_until or 0.0
        until = max(previous, now + seconds) if extend or (now >= previous and started_at >= previous) else previous
        budget.blocked_until = max(budget.blocked_until, until)
        self.wake.set()
        defer = getattr(self.host_capacity, "defer_start", None)
        if defer is not None:
            # OS quota locks are nonblocking. Contention holds this call, not
            # the event loop, and never lets this worker ignore the cooldown.
            while (remaining := defer(key, seconds, started_at=started_at, extend=extend)) is None:
                await asyncio.sleep(0.01)
            until = self.clock() + remaining if remaining > 0 else min(until, self.clock())
        budget.rate_limited_until = until
        budget.blocked_until = max(previous_block, until)
        for control in budget.adaptive.values():
            control.limited(run, now=self.clock(), started_at=started_at, until=until, active=active)
        return until

    def cooldown_remaining(self, key: str) -> float | None:
        budget = self.budgets.get(key)
        if budget is None:
            return None
        until = budget.rate_limited_until
        read = getattr(self.host_capacity, "cooldown_remaining", None)
        if read is not None:
            remaining = read(key)
            if remaining is not None and remaining > 0:
                until = max(until or 0, self.clock() + remaining)
                budget.rate_limited_until = until
                budget.blocked_until = max(budget.blocked_until, until)
        return None if until is None else max(0.0, until - self.clock())

    def _memory_slot_limit(self, cost: int, now: float) -> int:
        if now - self.memory_at >= MEMORY_SAMPLE_SECONDS:
            self.memory_sample = self.memory_reader()
            self.memory_at = now
            self.memory_sample_active = self.memory_active
            self.quiet_small_host = False
            if self.memory_sample is not None:
                available, total = self.memory_sample
                reserve = max(512 * 1024**2, total // 4)
                if available < reserve + 2 * MEMORY_PER_CALL and _small_request_fits(self.memory_sample, MEMORY_PER_CALL):
                    self.quiet_small_host = self.pressure_clear()
                if self.active == 0 or self.memory_peak_budget is None:
                    self.memory_peak_budget = max(0, available - reserve)
        if self.memory_sample is None:
            # Local non-Linux validation retains the hard host cap. Production
            # Lightsail/Linux additionally uses the whole-host memory guard.
            return HARD_LIMIT
        available, total = self.memory_sample
        reserve = max(512 * 1024**2, total // 4)
        # Keep the idle-sampled peak budget until the active batch drains:
        # waiting SDK calls may not have allocated their response buffers yet.
        # The live check is separate, so existing RSS is not subtracted twice.
        unsampled = max(0, self.memory_active - self.memory_sample_active)
        if self.memory_active + cost <= self.memory_peak_budget and available - unsampled - cost >= reserve:
            return HARD_LIMIT
        # Slot zero is the same OS lock for every worker. At most one small
        # request across the host can use the lower reserve at any time.
        if self.active == 0 and self.quiet_small_host and _small_request_fits(self.memory_sample, cost):
            return 1
        return 0

    async def acquire(self, *, key: str, rpm: int, owner: str, endpoint: str,
                      endpoint_limit: int, memory: int, ready=None) -> _Lease:
        self.budget(key, rpm)
        self.budgets[key].requests[rpm] += 1
        future = asyncio.get_running_loop().create_future()
        self.pending.add(future)
        ticket = _order.get()
        if ticket is None or ticket.task is not asyncio.current_task():
            ticket = _AdmissionOrder(asyncio.current_task())
        if ticket.order is None:
            ticket.order = next(_sequence)
        waiter = _Waiter(key, rpm, owner, endpoint, min(HARD_LIMIT, max(1, endpoint_limit)),
                         memory, future, ticket.order, ready)
        insort_right(self.queue, waiter, key=lambda item: item.order)
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
        if now < budget.next_start or (budget.rpm > 0 and len(budget.starts) >= budget.rpm):
            return False
        return True

    def _dispatch(self) -> bool:
        if self.active >= HARD_LIMIT:
            return False
        self.queue[:] = [waiter for waiter in self.queue if not waiter.future.cancelled()]
        # Across all owners, take the earliest eligible logical call. Skipping
        # cooling/rate/capacity-blocked calls never changes their queue order.
        now = self.clock()
        for candidate in self.queue:
            if not self._eligible(candidate, now):
                continue
            limit = self._memory_slot_limit(candidate.memory, now)
            if not limit:
                continue
            host = self.host_capacity.try_acquire(limit)
            if host is None:
                if limit == 1:
                    continue  # A smaller request may fit with the normal reserve.
                return False
            budget = self.budgets[candidate.key]
            try:
                if limit == 1 and not (_small_request_fits(self.memory_reader(), candidate.memory)
                                       and self.pressure_clear()):
                    self.quiet_small_host = False
                    host.release()
                    continue
                try_start = getattr(self.host_capacity, "try_start", None)
                allowed = try_start is None or try_start(candidate.key, budget.rpm, min_interval=budget.interval)
            except Exception:
                host.release()
                raise
            if not allowed:
                host.release()
                continue
            self.queue.remove(candidate)
            budget.active += 1
            budget.starts.append(now)
            budget.last_start = now
            self.active += 1
            self.memory_active += candidate.memory
            self.endpoints[candidate.endpoint] = self.endpoints.get(candidate.endpoint, 0) + 1
            candidate.future.set_result(_Lease(self, candidate, host))
            return True
        return False

    async def _run(self) -> None:
        try:
            while self.queue:
                self.wake.clear()
                if (self._adaptive_sources and self.clock() >= self._adaptive_refresh_after
                        and (self._adaptive_refresh_task is None or self._adaptive_refresh_task.done())):
                    self._adaptive_refresh_task = asyncio.create_task(self.refresh_adaptive())
                if self._dispatch():
                    # Yield between grants so cancellations and cooldowns are visible.
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
            self.queue.clear()

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
