"""One workflow's in-memory, provider-neutral call and token reservations.

Reservation precedes dispatch; cancellation or uncertain submission never refunds
it. Unbounded routes have call/time limits, not invented output-token reserves.
This ledger is not billing, durable submit-once storage, or an I/O executor.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import threading
import time
from typing import Callable, Literal

from pydantic import ValidationError

from backend.domain.errors import RecognitionError
from backend.recognition.models import EvidenceModel, RecognitionCandidateV1, RecognitionPolicyV1, RecognitionSourceRefV1
from backend.recognition.planner import EngineCapabilitiesV1

Phase = Literal["locator", "read"]
CallKind = Literal["locator", "initial", "empty_recovery", "patch"]
Outcome = Literal["ok", "empty", "failed"]


class BudgetTicket:
    """Identity-only reservation handle; valid only in its issuing budget."""

    __slots__ = ()

    def __repr__(self):
        return "<RecognitionBudgetTicket>"


class BudgetSnapshot(EvidenceModel):
    locator_calls: int
    initial_calls: int
    empty_recovery_calls: int
    patch_calls: int
    read_calls: int
    total_calls: int
    settled_calls: int
    pending_calls: int
    known_input_tokens: int
    known_output_tokens: int
    input_tokens: int | None
    output_tokens: int | None
    unknown_input_calls: int
    unknown_output_calls: int
    reserved_output_tokens: int
    charged_output_tokens: int | None
    usage_complete: bool
    bounded_output_tokens: bool
    output_limit_exceeded: bool
    duration_ms: float
    global_remaining_seconds: float
    locator_duration_ms: float | None
    read_duration_ms: float | None
    locator_remaining_seconds: float | None
    read_remaining_seconds: float | None


def validate_budget_accounting(
    snapshot: BudgetSnapshot, capabilities: EngineCapabilitiesV1 | None,
    records: list[tuple[RecognitionCandidateV1, bool, int]],
) -> None:
    """Reconcile a persisted summary with raw call evidence, never recreate credit."""
    pending = sum(uncertain for _, uncertain, _ in records)
    known_input = sum(candidate.input_tokens or 0 for candidate, uncertain, _ in records if not uncertain)
    known_output = sum(candidate.output_tokens or 0 for candidate, uncertain, _ in records if not uncertain)
    unknown_input = sum(uncertain or candidate.input_tokens is None for candidate, uncertain, _ in records)
    unknown_output = sum(uncertain or candidate.output_tokens is None for candidate, uncertain, _ in records)
    bounded = bool(capabilities and capabilities.bounded_output_tokens)
    reserved = sum(limit for candidate, uncertain, limit in records if uncertain or candidate.output_tokens is None) if bounded else 0
    overrun = bounded and any(not uncertain and candidate.output_tokens is not None and candidate.output_tokens > limit
                              for candidate, uncertain, limit in records)
    expected = {
        "pending_calls": pending, "settled_calls": len(records) - pending,
        "known_input_tokens": known_input, "known_output_tokens": known_output,
        "unknown_input_calls": unknown_input, "unknown_output_calls": unknown_output,
        "input_tokens": None if unknown_input else known_input, "output_tokens": None if unknown_output else known_output,
        "usage_complete": not (pending or unknown_input or unknown_output), "bounded_output_tokens": bounded,
        "reserved_output_tokens": reserved, "charged_output_tokens": known_output + reserved if bounded else None,
        "output_limit_exceeded": overrun,
    }
    if any(getattr(snapshot, name) != value for name, value in expected.items()):
        raise ValueError("workflow budget must match every retained call outcome")


@dataclass
class _Reservation:
    kind: CallKind
    requested_output: int
    region_keys: tuple[str, ...]
    outcome: Outcome | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


def _copy_context(source, policy, capabilities):
    try:
        if not isinstance(source, RecognitionSourceRefV1) or not isinstance(policy, RecognitionPolicyV1):
            raise ValueError
        if capabilities is not None and not isinstance(capabilities, EngineCapabilitiesV1):
            raise ValueError
        return (
            RecognitionSourceRefV1.model_validate(source.model_dump(warnings=False)),
            RecognitionPolicyV1.model_validate(policy.model_dump(warnings=False)),
            None if capabilities is None else EngineCapabilitiesV1.model_validate(capabilities.model_dump(warnings=False)),
        )
    except (ValidationError, ValueError, TypeError):
        raise RecognitionError("recognition_request_invalid") from None


class RecognitionBudget:
    def __init__(
        self,
        source: RecognitionSourceRefV1,
        policy: RecognitionPolicyV1,
        capabilities: EngineCapabilitiesV1 | None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._source, self._policy, self._capabilities = _copy_context(source, policy, capabilities)
        self._bounded_output = bool(self._capabilities and self._capabilities.bounded_output_tokens)
        self._clock = clock
        self._lock = threading.Lock()
        self._last_time: float | None = None
        self._started = self._now()
        self._phase_starts: dict[Phase, float] = {}
        self._reservations: dict[BudgetTicket, _Reservation] = {}
        self._initial_regions: dict[str, BudgetTicket] = {}
        self._extra_regions: set[str] = set()
        self._output_overrun = False

    def _now(self) -> float:
        try:
            value = self._clock()
            if type(value) not in {int, float} or not math.isfinite(value):
                raise ValueError
        except Exception:
            raise RecognitionError("recognition_request_invalid") from None
        if self._last_time is not None and value < self._last_time:
            raise RecognitionError("recognition_request_invalid")
        self._last_time = float(value)
        return self._last_time

    def assert_context(self, source, policy, capabilities) -> None:
        source, policy, capabilities = _copy_context(source, policy, capabilities)
        with self._lock:
            if source != self._source:
                raise RecognitionError("recognition_source_mismatch")
            if policy != self._policy:
                raise RecognitionError("recognition_plan_changed")
            if capabilities != self._capabilities:
                raise RecognitionError("recognition_route_changed")

    @staticmethod
    def _valid_phase(phase):
        if type(phase) is not str or phase not in {"locator", "read"}:
            raise RecognitionError("recognition_request_invalid")

    def _remaining(self, now: float, phase: Phase | None, *, start: bool) -> float:
        remaining = self._policy.total_seconds - (now - self._started)
        if phase is not None:
            self._valid_phase(phase)
            if start:
                self._phase_starts.setdefault(phase, now)
            if phase in self._phase_starts:
                limit = self._policy.locator_seconds if phase == "locator" else self._policy.read_seconds
                remaining = min(remaining, limit - (now - self._phase_starts[phase]))
        return max(0.0, remaining)

    def start_phase(self, phase: Phase) -> None:
        self.remaining(phase)

    def remaining(self, phase: Phase | None = None) -> float:
        with self._lock:
            result = self._remaining(self._now(), phase, start=True)
            if result <= 0:
                raise RecognitionError("recognition_timeout")
            return result

    def _charged_output(self) -> int:
        return sum(record.output_tokens if record.output_tokens is not None else record.requested_output
                   for record in self._reservations.values())

    @staticmethod
    def _valid_output_request(value):
        if type(value) is not int or not 1 <= value <= 32768:
            raise RecognitionError("recognition_request_invalid")

    def output_limit(self, requested: int = 4096) -> int:
        self._valid_output_request(requested)
        with self._lock:
            if not self._bounded_output:
                return requested
            remaining = self._policy.max_output_tokens - self._charged_output()
            if self._output_overrun or remaining <= 0:
                raise RecognitionError("recognition_budget_exhausted")
            return min(requested, remaining)

    def reserve(
        self,
        kind: CallKind,
        max_output_tokens: int,
        *,
        region_keys: tuple[str, ...] = (),
    ) -> BudgetTicket:
        if type(kind) is not str or kind not in {"locator", "initial", "empty_recovery", "patch"}:
            raise RecognitionError("recognition_request_invalid")
        self._valid_output_request(max_output_tokens)
        if (type(region_keys) is not tuple or len(region_keys) > 24
                or any(type(key) is not str or not key.strip() or len(key) > 512 for key in region_keys)
                or len(region_keys) != len(set(region_keys))):
            raise RecognitionError("recognition_request_invalid")
        if (kind == "locator" and region_keys) or (kind == "initial" and not region_keys):
            raise RecognitionError("recognition_request_invalid")
        if kind in {"empty_recovery", "patch"} and len(region_keys) != 1:
            raise RecognitionError("recognition_request_invalid")
        with self._lock:
            if self._remaining(self._now(), "locator" if kind == "locator" else "read", start=True) <= 0:
                raise RecognitionError("recognition_timeout")
            caps = self._capabilities
            if caps is None or not caps.visual_inputs:
                raise RecognitionError("recognition_input_unsupported")
            counts = {name: sum(record.kind == name for record in self._reservations.values())
                      for name in ("locator", "initial", "empty_recovery", "patch")}
            if self._bounded_output and (
                self._output_overrun or self._charged_output() + max_output_tokens > self._policy.max_output_tokens
            ):
                raise RecognitionError("recognition_budget_exhausted")
            if kind == "locator":
                if counts[kind] >= self._policy.max_locator_calls:
                    raise RecognitionError("recognition_budget_exhausted")
            else:
                if sum(counts[name] for name in ("initial", "empty_recovery", "patch")) >= self._policy.max_calls:
                    raise RecognitionError("recognition_budget_exhausted")
                if kind == "initial":
                    if (counts[kind] >= self._policy.max_initial_calls
                            or len(self._initial_regions) + len(region_keys) > self._policy.max_regions
                            or any(key in self._initial_regions for key in region_keys)):
                        raise RecognitionError("recognition_budget_exhausted")
                else:
                    if (not caps.response_recheck
                            or (kind == "patch" and (not caps.semantic_repair or not self._policy.enable_repair))):
                        raise RecognitionError("recognition_input_unsupported")
                    limit = self._policy.max_empty_recoveries if kind == "empty_recovery" else self._policy.max_patches
                    key = region_keys[0]
                    if (counts[kind] >= limit or counts["empty_recovery"] + counts["patch"] >= 6
                            or key in self._extra_regions):
                        raise RecognitionError("recognition_budget_exhausted")
                    initial = self._reservations.get(self._initial_regions.get(key))
                    expected = "empty" if kind == "empty_recovery" else "ok"
                    if initial is None or initial.outcome != expected:
                        raise RecognitionError("recognition_request_invalid")
            ticket = BudgetTicket()
            self._reservations[ticket] = _Reservation(kind, max_output_tokens, region_keys)
            if kind == "initial":
                self._initial_regions.update({key: ticket for key in region_keys})
            elif kind in {"empty_recovery", "patch"}:
                self._extra_regions.add(region_keys[0])
            return ticket

    def settle(
        self,
        ticket: BudgetTicket,
        input_tokens: int | None,
        output_tokens: int | None,
        *,
        outcome: Outcome,
    ) -> BudgetSnapshot:
        if type(outcome) is not str or outcome not in {"ok", "empty", "failed"} or any(
            value is not None and (type(value) is not int or value < 0) for value in (input_tokens, output_tokens)
        ):
            raise RecognitionError("recognition_response_invalid")
        with self._lock:
            if type(ticket) is not BudgetTicket or ticket not in self._reservations:
                raise RecognitionError("recognition_request_invalid")
            record = self._reservations[ticket]
            if record.outcome is not None:
                raise RecognitionError("recognition_request_invalid")
            # Settlement remains possible after deadlines: known spent usage must
            # not disappear simply because its response arrived late.
            record.outcome, record.input_tokens, record.output_tokens = outcome, input_tokens, output_tokens
            if self._bounded_output and output_tokens is not None and output_tokens > record.requested_output:
                self._output_overrun = True
            return self._snapshot(self._now())

    def _snapshot(self, now: float) -> BudgetSnapshot:
        records = list(self._reservations.values())
        counts = {name: sum(record.kind == name for record in records)
                  for name in ("locator", "initial", "empty_recovery", "patch")}
        known_input = sum(record.input_tokens or 0 for record in records)
        known_output = sum(record.output_tokens or 0 for record in records)
        unknown_input = sum(record.input_tokens is None for record in records)
        unknown_output = sum(record.output_tokens is None for record in records)
        pending = sum(record.outcome is None for record in records)
        phases = {}
        for phase in ("locator", "read"):
            phases[phase + "_duration_ms"] = ((now - self._phase_starts[phase]) * 1000
                                              if phase in self._phase_starts else None)
            phases[phase + "_remaining_seconds"] = (self._remaining(now, phase, start=False)
                                                    if phase in self._phase_starts else None)
        return BudgetSnapshot(
            locator_calls=counts["locator"], initial_calls=counts["initial"],
            empty_recovery_calls=counts["empty_recovery"], patch_calls=counts["patch"],
            read_calls=len(records) - counts["locator"], total_calls=len(records),
            settled_calls=len(records) - pending, pending_calls=pending,
            known_input_tokens=known_input, known_output_tokens=known_output,
            input_tokens=None if unknown_input else known_input,
            output_tokens=None if unknown_output else known_output,
            unknown_input_calls=unknown_input, unknown_output_calls=unknown_output,
            # Zero means no enforceable reservation on an unbounded route, not
            # free usage. Unknown totals remain None and known spend is separate.
            reserved_output_tokens=(sum(record.requested_output for record in records if record.output_tokens is None)
                                    if self._bounded_output else 0),
            charged_output_tokens=self._charged_output() if self._bounded_output else None,
            usage_complete=not (pending or unknown_input or unknown_output),
            bounded_output_tokens=self._bounded_output,
            output_limit_exceeded=self._output_overrun, duration_ms=(now - self._started) * 1000,
            global_remaining_seconds=self._remaining(now, None, start=False), **phases,
        )

    def snapshot(self) -> BudgetSnapshot:
        with self._lock:
            return self._snapshot(self._now())
