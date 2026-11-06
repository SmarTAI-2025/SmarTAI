"""Discrete-event policy comparison: no network, sleeps, or model requests."""
from collections import deque
import heapq
import math
from statistics import median
from types import SimpleNamespace

import pytest

from backend.llm.adaptive_admission import AdaptiveAdmission
from backend.services.execution_control import ExecutionControl


class OneWay(AdaptiveAdmission):
    """The rejected prototype, retained only as a comparison fixture."""
    def _recalibrate(self, stats):
        rows = [e for e in stats.episodes if e.recovered]
        if len(rows) < stats.threshold:
            return
        if any(b.completed < b.started for e in rows for b in e.buckets):
            return
        n = median(sum(b.succeeded for b in e.buckets) for e in rows)
        self.interval = max(self.interval * 1.5, 75 / n if n else 1)
        self.concurrency = max(1, min(math.ceil(self.concurrency / 2), math.ceil(self.response_seconds / self.interval)))
        self.adjustments += 1
        self.axis = None
        self.candidate = True
        self.probes_left = 0
        self._reset_health()
        self._publish(validated=False)
        stats.episodes.clear()


def simulate(policy, *, rpm=6, vendor_slots=50, latency=(3,), count=90, saved=None,
             server_slots=5, always_reject=False):
    state = policy(saved, 1, 0, 0) if policy else None
    provider = SimpleNamespace(provider_id="fixture")
    control = ExecutionControl("simulation", lambda: False, None)
    waiting = deque((n, 0) for n in range(count))
    accepted = deque()
    events = []
    last_start, now, blocked = -math.inf, 0.0, 0.0
    active = vendor_active = successful = limited = stopped = failed = cycles = 0
    serial = 0
    learned = []
    while waiting or events:
        if serial > count * 4:
            raise AssertionError("unbounded business retry")
        interval, concurrency = (state.interval, state.concurrency) if state else (0, 5)
        if control.blocked:
            stopped += len(waiting)
            waiting.clear()
        can_queue = waiting and active < min(concurrency, server_slots)
        next_send = max(now, blocked, last_start + interval) if can_queue else math.inf
        next_done = events[0][0] if events else math.inf
        now = min(next_send, next_done)
        if not math.isfinite(now):
            break
        if next_done <= next_send:
            when, _, item, attempt, start, ok, peak, busy = heapq.heappop(events)
            active -= 1
            if ok:
                vendor_active -= 1
                successful += 1
                before = state.adjustments if state else 0
                if state:
                    state.succeeded(now=now, started_at=start, run="simulation", busy=busy)
                control.rate_recovered(provider, started_at=start,
                                       adjusted=bool(state and state.adjustments != before))
            else:
                limited += 1
                if now >= blocked and start >= blocked:
                    blocked = now + 65
                    cycles += 1
                if state:
                    state.limited("simulation", now=now, started_at=start, until=blocked, active=peak)
                control.rate_limited(provider, started_at=start, until=blocked)
                if attempt < 3:
                    waiting.append((item, attempt))
                    waiting = deque(sorted(waiting))
                else:
                    failed += 1
            control.rate_finished(provider, started_at=start)
            if state and (update := state.take_update()):
                learned.append(update)
            continue
        item, attempt = waiting.popleft()
        while accepted and accepted[0] <= now - 60 + 1e-9:
            accepted.popleft()
        ok = not always_reject and (not rpm or len(accepted) < rpm) and vendor_active < vendor_slots
        if ok:
            accepted.append(now)
            vendor_active += 1
        active += 1
        serial += 1
        last_start = now
        if state:
            state.started(now)
        control.rate_started(provider, started_at=now)
        duration = latency[item % len(latency)] if ok else .25
        heapq.heappush(events, (now + duration, serial, item, attempt + 1, now, ok, active, bool(waiting)))
    return {"successes": successful, "429s": limited, "cycles": cycles,
            "failed_calls": failed, "stopped_unsent": stopped, "seconds": round(now, 3),
            "saved": learned[-1] if learned else saved}


SCENARIOS = [
    ("rpm6_fast", dict(rpm=6, latency=(3,))),
    ("rpm6_vision", dict(rpm=6, latency=(22,))),
    ("rpm6_slow", dict(rpm=6, latency=(90,))),
    ("rpm6_mixed", dict(rpm=6, latency=(3, 5, 22, 10, 82, 4, 166))),
    ("concurrency2", dict(rpm=0, vendor_slots=2, latency=(22,))),
    ("concurrency1_fast", dict(rpm=0, vendor_slots=1, latency=(3,))),
    ("server2", dict(rpm=60, server_slots=2, latency=(22,))),
]


@pytest.mark.parametrize("name,scenario", SCENARIOS)
def test_compare_policies(name, scenario, record_property):
    results = {label: simulate(policy, **scenario) for label, policy in (
        ("shared_only", None), ("one_way", OneWay), ("bounded", AdaptiveAdmission))}
    record_property(name, str(results))
    assert results["bounded"]["successes"] == 90
    assert results["bounded"]["stopped_unsent"] == 0
    assert results["bounded"]["429s"] <= results["shared_only"]["429s"]
    warm = simulate(AdaptiveAdmission, saved=results["bounded"]["saved"], **scenario)
    record_property(name + "_warm", str(warm))
    assert warm["successes"] == 90


def test_no_success_is_bounded_and_does_not_publish_a_fictitious_quota():
    outcome = simulate(AdaptiveAdmission, always_reject=True)
    assert outcome["cycles"] == 3
    assert outcome["stopped_unsent"] > 0 and outcome["saved"] is None
