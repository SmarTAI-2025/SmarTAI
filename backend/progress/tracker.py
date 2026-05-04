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
from collections import deque
from contextlib import asynccontextmanager
from typing import Optional, Deque

from backend.config import settings
from backend.models import JobProgress, ActiveUnit, ProgressEvent

logger = logging.getLogger(__name__)


class ProgressReporter:
    """
    Accumulates progress for a single grading job.
    Thread-safe for concurrent skill/expert execution within one job.
    """

    def __init__(self, job_id: str, total_students: int = 0, total_questions: int = 0):
        self.job_id = job_id
        self._progress = JobProgress(
            total_students=total_students,
            total_questions=total_questions,
        )
        self._lock = asyncio.Lock()
        self._events: Deque[ProgressEvent] = deque(maxlen=settings.progress_ring_buffer_size)
        # SSE subscribers (asyncio.Queue for each)
        self._subscribers: list[asyncio.Queue[ProgressEvent]] = []

    async def set_phase(self, phase: str) -> None:
        async with self._lock:
            self._progress.phase = phase
        await self._emit(ProgressEvent(message=f"Phase: {phase}"))

    async def set_totals(self, students: int, questions: int) -> None:
        async with self._lock:
            self._progress.total_students = students
            self._progress.total_questions = questions

    async def increment_completed(self) -> None:
        async with self._lock:
            self._progress.completed_units += 1

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
        self._events.append(event)
        logger.info(f"[progress:{self.job_id}] {event.message}")
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


# ─── Job-level progress store (in-memory, maps job_id → reporter) ──────────

_reporters: dict[str, ProgressReporter] = {}


def get_or_create_reporter(
    job_id: str, total_students: int = 0, total_questions: int = 0
) -> ProgressReporter:
    if job_id not in _reporters:
        _reporters[job_id] = ProgressReporter(job_id, total_students, total_questions)
    return _reporters[job_id]


def get_reporter(job_id: str) -> Optional[ProgressReporter]:
    return _reporters.get(job_id)


def remove_reporter(job_id: str) -> None:
    _reporters.pop(job_id, None)
