"""Bounded operating estimates from business traffic, never provider quotas.

Counters retain late responses by admission time. Calibration changes one
control at a time; stable configurations never chase the provider ceiling.
"""
from __future__ import annotations

import math
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from statistics import median


def valid_profile(value: object) -> dict | None:
    if not isinstance(value, dict) or value.get("version") != 1:
        return None
    fields = ("interval_seconds", "concurrency", "response_seconds", "config_version")
    if any(type(value.get(k)) not in (int, float) or not math.isfinite(value[k]) for k in fields):
        return None
    generation, validated = value.get("generation", 0), value.get("validated", True)
    if (not 0 < value["interval_seconds"] <= 3600
            or type(value["concurrency"]) is not int or not 1 <= value["concurrency"] <= 50
            or not 0 < value["response_seconds"] <= 3600
            or type(generation) is not int or not 0 <= generation < 2**53
            or type(validated) is not bool):
        return None
    return {"version": 1, **{k: value[k] for k in fields},
            "generation": generation, "validated": validated}


@dataclass
class _Bucket:
    started: int = 0
    completed: int = 0
    succeeded: int = 0


@dataclass
class _Episode:
    until: float
    buckets: list[_Bucket]
    concurrency_hint: int | None
    recovered: bool = False


@dataclass
class _Run:
    episodes: deque[_Episode] = field(default_factory=lambda: deque(maxlen=3))
    last_until: float = 0.0
    threshold: int = 2


class AdaptiveAdmission:
    def __init__(self, profile: dict | None, revision: float, rpm: int, now: float):
        self.profile = valid_profile(profile)
        self.revision, self.rpm = revision, rpm
        self.floor = 60.0 / rpm if rpm else .001
        self.interval = max(self.floor, self.profile["interval_seconds"] if self.profile else 1.0)
        self.concurrency = self.profile["concurrency"] if self.profile else (min(50, max(1, rpm * 2)) if rpm else 5)
        self.response_seconds = self.profile["response_seconds"] if self.profile else 0.0
        self.buckets: OrderedDict[int, _Bucket] = OrderedDict()
        self.runs: OrderedDict[str, _Run] = OrderedDict()
        self.cooldown_until = 0.0
        self.healthy_since: float | None = None
        self.healthy_count = self.busy_count = 0
        self.probe_allowed = bool(self.profile and self.profile["config_version"] != revision)
        self.candidate = self.profile is None or not self.profile["validated"] or self.probe_allowed
        self.axis: str | None = None
        self.upper = 0.0
        self.safe: tuple[float, int] | None = None
        self.probing = False
        self.probes_left = 2
        self.adjustments = 0
        self.pending: dict | None = None
        self.save_after = 0.0
        self.generation = self.profile["generation"] if self.profile else 0

    def started(self, now: float) -> None:
        second = int(now)
        self.buckets.setdefault(second, _Bucket()).started += 1
        while self.buckets and next(iter(self.buckets)) < second - 300:
            self.buckets.popitem(last=False)

    def finished(self, started_at: float, *, success: bool) -> None:
        bucket = self.buckets.get(int(started_at))
        if bucket is not None:
            bucket.completed += 1
            bucket.succeeded += int(success)

    def _reset_health(self) -> None:
        self.healthy_since = None
        self.healthy_count = self.busy_count = 0

    def _publish(self, *, validated: bool) -> None:
        generation = self.pending["generation"] if self.pending else self.generation + 1
        self.pending = {
            "version": 1, "interval_seconds": self.interval,
            "concurrency": self.concurrency, "response_seconds": max(.001, self.response_seconds),
            "config_version": self.revision, "generation": generation, "validated": validated,
        }

    def saved(self, profile: dict) -> None:
        self.generation = max(self.generation, profile["generation"])
        self.profile = dict(profile)
        if self.pending == profile:
            self.pending = None
        elif self.pending is not None and self.pending["generation"] <= self.generation:
            self.pending["generation"] = self.generation + 1

    def limited(self, run: str, *, now: float, started_at: float, until: float,
                active: int = 1) -> None:
        self.finished(started_at, success=False)
        self.cooldown_until = max(self.cooldown_until, until)
        self._reset_health()
        stats = self.runs.setdefault(run, _Run(threshold=3 if self.profile and self.profile["validated"] else 2))
        self.runs.move_to_end(run)
        while len(self.runs) > 32:
            self.runs.popitem(last=False)
        if started_at < stats.last_until:
            return
        stats.last_until = until
        buckets = [b for t, b in self.buckets.items() if now - 60 < t <= now]
        hint = active if active > 1 and not any(b.succeeded for b in buckets) else None
        stats.episodes.append(_Episode(until, buckets, hint))
        if self.probing and self.safe is not None:
            self.interval, self.concurrency = self.safe
            self.probing = self.probe_allowed = False
            self.probes_left = 0
            self.axis = None
            self.candidate = True
            self._publish(validated=False)

    def _recalibrate(self, stats: _Run) -> None:
        threshold = stats.threshold
        evidence = [e for e in stats.episodes if e.recovered]
        if len(evidence) < threshold:
            return
        evidence = evidence[-threshold:]
        # Outstanding responses are unknown, not failures or zero RPM.
        if any(b.completed < b.started for e in evidence for b in e.buckets):
            return
        hints = [e.concurrency_hint for e in evidence]
        if all(h is not None for h in hints) and self.concurrency > 1:
            self.axis, self.upper = "concurrency", float(min(hints))
            self.concurrency = max(1, min(self.concurrency - 1, int(self.upper) - 1))
        else:
            count = median(sum(b.succeeded for b in e.buckets) for e in evidence)
            rate = 60.0 / self.interval
            self.axis, self.upper = "rate", min(rate, count) if count else rate
            candidate = min(.9 * rate, .9 * count) if count else .8 * rate
            self.interval = max(self.floor, min(3600.0, 60.0 / max(1 / 60, candidate)))
        self.adjustments += 1
        self.safe = None
        self.probing = self.probe_allowed = False
        self.probes_left = 2
        self.candidate = True
        stats.episodes.clear()
        self._reset_health()
        self._publish(validated=False)

    def succeeded(self, *, now: float, started_at: float, run: str = "unscoped",
                  busy: bool = False) -> None:
        self.finished(started_at, success=True)
        duration = max(.001, min(3600.0, now - started_at))
        self.response_seconds = self.response_seconds * .8 + duration * .2 if self.response_seconds else duration
        stats = self.runs.get(run)
        before = self.adjustments
        if stats is not None:
            for episode in stats.episodes:
                if started_at >= episode.until:
                    episode.recovered = True
            self._recalibrate(stats)
        if started_at < self.cooldown_until or before != self.adjustments:
            return  # This response used the previous parameters.
        if self.healthy_since is None:
            self.healthy_since = started_at
        self.healthy_count += 1
        self.busy_count += int(busy)
        if not self.candidate or self.healthy_count < 3:
            return
        if started_at - self.healthy_since < 60:
            if self.profile is None and self.pending is None and not self.probing:
                self._publish(validated=False)
            return
        self.safe = self.interval, self.concurrency
        self._publish(validated=True)
        self.candidate = False
        # Growth needs real demand; never generate requests to fill a sample.
        if self.busy_count < 3 or self.probes_left <= 0:
            self.probe_allowed = self.probing = False
            self.axis = None
            return
        edited = self.probe_allowed
        if edited:
            self.probes_left = 1
            if self.concurrency < 50 and self.concurrency < self.response_seconds / self.interval:
                self.axis, self.upper = "concurrency", float(self.concurrency + 1)
            elif self.interval > self.floor:
                self.axis, self.upper = "rate", min(60 / self.floor, 66 / self.interval)
            self.probe_allowed = False
        if self.axis == "rate":
            rate = 60 / self.interval
            if not edited and self.upper <= rate * 1.1:
                self.axis = None
                return
            target = self.upper if edited else min((rate + self.upper) / 2, rate * 1.1)
            self.interval = max(self.floor, 60 / target)
        elif self.axis == "concurrency":
            if not edited and self.upper <= self.concurrency + 1:
                self.axis = None
                return
            self.concurrency = min(50, self.concurrency + 1)
        else:
            return
        self.probes_left -= 1
        self.probing = self.candidate = True
        self.adjustments += 1
        self._reset_health()

    def take_update(self) -> dict | None:
        update = self.pending
        if update is not None:
            self.saved(update)
        return update
