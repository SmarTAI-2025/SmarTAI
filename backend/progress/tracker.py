"""
Progress tracking infrastructure for long-running grading pipelines.

Provides ProgressReporter as an async context manager that agents/skills/tools
use to emit fine-grained status updates. The frontend can poll or subscribe.

Usage in a skill:
    async with reporter.step(student_id, q_id, skill="ConceptSkill", expert="gemini:..."):
        async with reporter.substep("retrieve_knowledge"):
            chunks = await knowledge.retrieve(...)
        async with reporter.substep("llm_grade"):
            result = await structured_llm(...)
"""
from __future__ import annotations

import time
import logging
import asyncio
import json
from collections import OrderedDict, deque
from contextlib import asynccontextmanager
from threading import RLock
from typing import Optional, Deque, Sequence, Callable, Any, Mapping

from backend.config import settings
from backend.models import JobProgress, ActiveUnit, ProgressEvent

logger = logging.getLogger(__name__)

_QUESTION_LABELS_UTF8_BUDGET_BYTES = 16 * 1024
_QUESTION_LABEL_MAX_CHARACTERS = 120


class ProgressReporter:
    """
    Accumulates progress for a single grading job.
    Thread-safe for concurrent skill/expert execution within one job.
    """

    def __init__(self, job_id: str, total_students: int = 0, total_questions: int = 0):
        self.job_id = job_id
        self._progress = JobProgress(
            job_id=job_id,
            total_students=total_students,
            total_questions=total_questions,
        )
        self._lock = asyncio.Lock()
        self._events: Deque[ProgressEvent] = deque(maxlen=settings.progress_ring_buffer_size)
        # SSE subscribers (asyncio.Queue for each)
        self._subscribers: list[asyncio.Queue[ProgressEvent]] = []
        self._event_sink: Optional[Callable[[ProgressEvent, dict[str, Any]], None]] = None
        self._question_generation_ids: tuple[str, ...] = ()

    def set_event_sink(
        self,
        sink: Optional[Callable[[ProgressEvent, dict[str, Any]], None]],
    ) -> None:
        """Attach a best-effort durable sink (for normalized run events).

        The in-memory reporter remains a rebuildable SSE/polling cache; the
        sink receives credential-free progress counters and the public event.
        """
        self._event_sink = sink

    async def set_phase(self, phase: str) -> None:
        async with self._lock:
            if self._progress.started_at is None:
                self._progress.started_at = time.time()
            self._progress.phase = phase
        await self._emit(ProgressEvent(message=f"Phase: {phase}"))

    async def set_totals(self, students: int, questions: int) -> None:
        async with self._lock:
            self._progress.total_students = students
            self._progress.total_questions = questions

    async def configure_workflow(
        self,
        workflow: str,
        stage_sequence: Sequence[str],
    ) -> None:
        """Publish the stable workflow contract before reporting stage progress.

        The sequence is owned by the outer orchestration job. Nested skills may
        emit messages and factual counters, but must not replace this contract.
        This keeps polling clients forward-compatible when the backend inserts
        real stages such as OCR or layout analysis later.
        """

        normalized_workflow = workflow.strip()
        normalized_stages = [stage.strip() for stage in stage_sequence]
        if not normalized_workflow:
            raise ValueError("workflow must not be empty")
        if not normalized_stages or any(not stage for stage in normalized_stages):
            raise ValueError("stage_sequence requires named stages")
        if len(set(normalized_stages)) != len(normalized_stages):
            raise ValueError("stage_sequence must not contain duplicates")

        async with self._lock:
            if self._progress.started_at is None:
                self._progress.started_at = time.time()
            if (
                self._progress.workflow is not None
                and self._progress.workflow != normalized_workflow
            ):
                raise ValueError("workflow cannot change after progress starts")
            if (
                self._progress.stage_sequence
                and self._progress.stage_sequence != normalized_stages
            ):
                raise ValueError("stage_sequence cannot change after progress starts")
            self._progress.workflow = normalized_workflow
            self._progress.stage_sequence = normalized_stages

    async def increment_completed(self) -> None:
        async with self._lock:
            self._progress.completed_units += 1
        await self._emit(ProgressEvent(message="Completed one grading unit."))

    async def set_stage_progress(
        self,
        current_step: str,
        *,
        total_steps: int,
        completed_steps: int,
        message: Optional[str] = None,
    ) -> None:
        """Atomically publish a workflow's factual stage progress.

        ``completed_units`` remains dedicated to grading's student/question
        pairs. Workflows such as problem recognition use these stage fields for
        real milestones; callers must not substitute timers, page estimates, or
        other synthetic progress.
        """
        if total_steps < 0:
            raise ValueError("total_steps must be non-negative")
        if completed_steps < 0 or completed_steps > total_steps:
            raise ValueError("completed_steps must be between 0 and total_steps")
        if not current_step:
            raise ValueError("current_step must not be empty")

        async with self._lock:
            if self._progress.stage_sequence:
                if current_step not in self._progress.stage_sequence:
                    raise ValueError("current_step is not part of the configured workflow")
                if total_steps != len(self._progress.stage_sequence):
                    raise ValueError("total_steps must match the configured workflow")
                previous_completed = self._progress.completed_steps
                if previous_completed is not None and completed_steps < previous_completed:
                    raise ValueError("completed_steps must not move backwards")
            if self._progress.started_at is None:
                self._progress.started_at = time.time()
            self._progress.current_step = current_step
            self._progress.total_steps = total_steps
            self._progress.completed_steps = completed_steps
        if message:
            await self._emit(ProgressEvent(message=message))

    async def set_current_step(
        self,
        current_step: str,
        *,
        message: Optional[str] = None,
    ) -> None:
        """Publish a factual step without inventing a stage percentage."""

        if not current_step:
            raise ValueError("current_step must not be empty")
        async with self._lock:
            if self._progress.started_at is None:
                self._progress.started_at = time.time()
            self._progress.current_step = current_step
        if message:
            await self._emit(ProgressEvent(message=message))

    async def set_stage_metrics(self, **metrics: int) -> None:
        """Replace factual workflow counters after validating non-negative ints."""

        normalized = _validated_stage_metrics(metrics)
        async with self._lock:
            self._progress.stage_metrics = normalized

    async def increment_stage_metrics(self, **deltas: int) -> None:
        """Atomically increment factual workflow counters from concurrent work."""

        normalized = _validated_stage_metrics(deltas)
        async with self._lock:
            for key, delta in normalized.items():
                self._progress.stage_metrics[key] = (
                    self._progress.stage_metrics.get(key, 0) + delta
                )

    async def configure_question_generation(
        self,
        question_ids: Sequence[str],
        *,
        completed_question_ids: Sequence[str] = (),
        question_labels: Mapping[str, str] | None = None,
    ) -> None:
        """Initialize factual progress for major-question generation.

        A question id is one scored major question. Labels such as ``(a)`` and
        ``(b)`` never appear in this counter as separate units.
        """

        normalized = tuple(str(q_id).strip() for q_id in question_ids)
        if not normalized or any(not q_id for q_id in normalized):
            raise ValueError("question generation requires named major questions")
        if len(normalized) != len(set(normalized)):
            raise ValueError("question generation ids must be unique")
        completed = [
            str(q_id).strip()
            for q_id in completed_question_ids
            if str(q_id).strip()
        ]
        if len(completed) != len(set(completed)) or not set(completed) <= set(normalized):
            raise ValueError("completed question ids must be unique configured ids")
        labels = _bounded_question_labels(normalized, question_labels)
        now = time.time()
        async with self._lock:
            self._question_generation_ids = normalized
            self._progress.total_questions = len(normalized)
            self._progress.question_labels = labels
            self._progress.question_error_codes = {}
            self._progress.completed_question_ids = completed
            self._progress.active_question_ids = []
            self._progress.failed_question_ids = []
            self._progress.last_activity_at = now
            self._progress.stage_metrics.update({
                "solution_total_questions": len(normalized),
                "solution_completed_questions": len(completed),
                "solution_failed_questions": 0,
            })
        await self._emit(ProgressEvent(
            message=f"Preparing {len(normalized)} major-question generation units"
        ))

    async def mark_question_generation_started(self, q_id: str) -> None:
        normalized = str(q_id).strip()
        now = time.time()
        async with self._lock:
            if normalized not in self._question_generation_ids:
                raise ValueError("question id is not part of this generation run")
            if normalized in self._progress.completed_question_ids:
                raise ValueError("completed question cannot start again")
            if normalized not in self._progress.active_question_ids:
                self._progress.active_question_ids.append(normalized)
                self._progress.active_question_ids.sort(
                    key=self._question_generation_ids.index
                )
            self._progress.last_activity_at = now
        await self._emit(ProgressEvent(
            message=f"Generating materials for major question {normalized}"
        ))

    async def mark_question_generation_finished(
        self,
        q_id: str,
        *,
        succeeded: bool,
        error_code: str | None = None,
    ) -> None:
        normalized = str(q_id).strip()
        now = time.time()
        async with self._lock:
            if normalized not in self._question_generation_ids:
                raise ValueError("question id is not part of this generation run")
            self._progress.active_question_ids = [
                item for item in self._progress.active_question_ids
                if item != normalized
            ]
            if succeeded:
                self._progress.question_error_codes.pop(normalized, None)
                if normalized not in self._progress.completed_question_ids:
                    self._progress.completed_question_ids.append(normalized)
                    self._progress.completed_question_ids.sort(
                        key=self._question_generation_ids.index
                    )
                self._progress.stage_metrics["solution_completed_questions"] = len(
                    self._progress.completed_question_ids
                )
            else:
                if normalized not in self._progress.failed_question_ids:
                    self._progress.failed_question_ids.append(normalized)
                    self._progress.failed_question_ids.sort(
                        key=self._question_generation_ids.index
                    )
                self._progress.stage_metrics["solution_failed_questions"] = len(
                    self._progress.failed_question_ids
                )
                if error_code:
                    self._progress.question_error_codes[normalized] = error_code
            self._progress.last_activity_at = now
        outcome = "completed" if succeeded else "failed"
        await self._emit(ProgressEvent(
            level="info" if succeeded else "error",
            message=f"Major question {normalized} generation {outcome}",
        ))

    async def mark_question_generation_cancelled(self, q_id: str) -> None:
        """Remove a cancelled unit without misreporting success or failure."""

        normalized = str(q_id).strip()
        async with self._lock:
            if normalized not in self._question_generation_ids:
                raise ValueError("question id is not part of this generation run")
            self._progress.active_question_ids = [
                item for item in self._progress.active_question_ids
                if item != normalized
            ]
            self._progress.last_activity_at = time.time()
        await self._emit(ProgressEvent(
            level="warn",
            message=f"Major question {normalized} generation cancelled",
        ))

    async def set_error(self, detail: str) -> None:
        async with self._lock:
            self._progress.phase = "error"
            self._progress.error_detail = detail
        await self._emit(ProgressEvent(level="error", message=detail))

    async def snapshot(self) -> JobProgress:
        """Return a copy of current progress (for polling endpoint)."""
        async with self._lock:
            # Shallow copy is sufficient since the lists are replaced, not mutated
            snap = self._progress.model_copy(deep=True)
            snap.messages = list(self._events)
            return snap

    @asynccontextmanager
    async def step(
        self,
        student_id: str,
        q_id: str,
        skill: str,
        expert: Optional[str] = None,
    ):
        """
        Context manager for a grading unit (student, question).
        Adds to active list on enter, removes on exit.
        """
        unit = ActiveUnit(
            student_id=student_id,
            q_id=q_id,
            skill=skill,
            expert=expert,
            step="starting",
        )
        async with self._lock:
            self._progress.active.append(unit)
        await self._emit(ProgressEvent(
            message=f"Start grading {student_id}/{q_id} with {skill}" + (f" ({expert})" if expert else ""),
            unit=unit,
        ))
        try:
            yield unit
        finally:
            async with self._lock:
                self._progress.active = [
                    a for a in self._progress.active
                    if not (a.student_id == student_id and a.q_id == q_id
                            and a.skill == skill and a.expert == expert)
                ]
            await self._emit(ProgressEvent(
                message=f"Done grading {student_id}/{q_id} with {skill}" + (f" ({expert})" if expert else ""),
                unit=unit,
            ))

    async def substep(self, unit: ActiveUnit, substep_name: str):
        """Mark a substep transition (simple status update, not a context manager)."""
        unit.step = substep_name
        await self._emit(ProgressEvent(
            message=f"{unit.student_id}/{unit.q_id}: {substep_name}",
            unit=unit,
        ))

    async def _emit(self, event: ProgressEvent) -> None:
        """Add event to ring buffer and push to SSE subscribers."""
        _mark_reporter_active(self.job_id)
        self._events.append(event)
        logger.info(f"[progress:{self.job_id}] {event.message}")
        if self._event_sink is not None:
            async with self._lock:
                durable_payload = {
                    "phase": self._progress.phase,
                    "total_students": self._progress.total_students,
                    "total_questions": self._progress.total_questions,
                    "completed_units": self._progress.completed_units,
                    "active_units": len(self._progress.active),
                    "workflow": self._progress.workflow,
                    "current_step": self._progress.current_step,
                    "total_steps": self._progress.total_steps,
                    "completed_steps": self._progress.completed_steps,
                    "stage_metrics": dict(self._progress.stage_metrics),
                    "question_labels": dict(self._progress.question_labels),
                    "question_error_codes": dict(
                        self._progress.question_error_codes
                    ),
                    "completed_question_ids": list(
                        self._progress.completed_question_ids
                    ),
                    "active_question_ids": list(self._progress.active_question_ids),
                    "failed_question_ids": list(self._progress.failed_question_ids),
                    "last_activity_at": self._progress.last_activity_at,
                }
            try:
                self._event_sink(event, durable_payload)
            except Exception:
                logger.exception("Progress event sink failed for job %s", self.job_id)
        for q in self._subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # drop if subscriber is slow

    async def _emit_message(self, message: str, level: str = "info") -> None:
        """Convenience: emit a free-form text event without a unit."""
        await self._emit(ProgressEvent(level=level, message=message))

    def subscribe(self) -> asyncio.Queue[ProgressEvent]:
        """Create an SSE subscriber queue. Caller should unsubscribe() on disconnect."""
        q: asyncio.Queue[ProgressEvent] = asyncio.Queue(maxsize=100)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[ProgressEvent]) -> None:
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass


def _validated_stage_metrics(metrics: dict[str, int]) -> dict[str, int]:
    normalized: dict[str, int] = {}
    for key, value in metrics.items():
        if not key or not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("stage metrics require named non-negative integers")
        normalized[key] = value
    return normalized


def _bounded_question_labels(
    question_ids: Sequence[str],
    question_labels: Mapping[str, str] | None,
) -> dict[str, str]:
    """Keep optional display labels within a bounded UTF-8 JSON budget.

    Durable operation progress has a 64 KiB limit and must always retain the
    complete question-id and counter contract. Labels are optional UI hints,
    so later labels are omitted when their encoded JSON entry would exceed a
    smaller reserved budget. Iterating in question order keeps the result
    deterministic, and whole strings are retained or omitted without cutting
    a multi-byte UTF-8 sequence.
    """

    candidates = {
        str(q_id): str(label).strip()[:_QUESTION_LABEL_MAX_CHARACTERS]
        for q_id, label in (question_labels or {}).items()
        if str(label).strip()
    }
    bounded: dict[str, str] = {}
    encoded_bytes = len(b"{}")
    for q_id in question_ids:
        label = candidates.get(q_id)
        if not label:
            continue
        encoded_entry = json.dumps(
            {q_id: label},
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        additional_bytes = len(encoded_entry) - len(b"{}")
        if bounded:
            additional_bytes += len(b",")
        if encoded_bytes + additional_bytes > _QUESTION_LABELS_UTF8_BUDGET_BYTES:
            continue
        bounded[q_id] = label
        encoded_bytes += additional_bytes
    return bounded


# ─── Job-level progress store (in-memory, maps job_id → reporter) ──────────

_REPORTER_TTL_SECONDS = 2 * 60 * 60
_REPORTER_MAX_ENTRIES = 500
_reporters: "OrderedDict[str, ProgressReporter]" = OrderedDict()
_reporter_last_seen: dict[str, float] = {}
_reporters_lock = RLock()


def _prune_reporters(now: Optional[float] = None) -> None:
    current = time.monotonic() if now is None else now
    for reporter_id, touched_at in list(_reporter_last_seen.items()):
        if current - touched_at > _REPORTER_TTL_SECONDS:
            _reporters.pop(reporter_id, None)
            _reporter_last_seen.pop(reporter_id, None)
    while len(_reporters) > _REPORTER_MAX_ENTRIES:
        reporter_id, _ = _reporters.popitem(last=False)
        _reporter_last_seen.pop(reporter_id, None)


def _mark_reporter_active(job_id: str) -> None:
    with _reporters_lock:
        if job_id not in _reporters:
            return
        _reporters.move_to_end(job_id)
        _reporter_last_seen[job_id] = time.monotonic()
        _prune_reporters()


def get_or_create_reporter(
    job_id: str, total_students: int = 0, total_questions: int = 0
) -> ProgressReporter:
    with _reporters_lock:
        _prune_reporters()
        reporter = _reporters.get(job_id)
        if reporter is None:
            reporter = ProgressReporter(job_id, total_students, total_questions)
            _reporters[job_id] = reporter
        _reporters.move_to_end(job_id)
        _reporter_last_seen[job_id] = time.monotonic()
        _prune_reporters()
        return reporter


def get_reporter(job_id: str) -> Optional[ProgressReporter]:
    with _reporters_lock:
        _prune_reporters()
        reporter = _reporters.get(job_id)
        if reporter is not None:
            _reporters.move_to_end(job_id)
            _reporter_last_seen[job_id] = time.monotonic()
        return reporter


def remove_reporter(job_id: str) -> None:
    with _reporters_lock:
        _reporters.pop(job_id, None)
        _reporter_last_seen.pop(job_id, None)
