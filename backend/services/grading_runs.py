"""Grading-run lifecycle: creation, lease/heartbeat, result persistence,
teacher review, release, and recovery.

This service orchestrates ``grading_repository`` and ``grading_adapter``. The
adapter runs the unchanged LLM algorithm; this service owns the run state
machine and the durable lease. A worker claims a queued run, heartbeats its
lease while grading, and writes terminal state only while it still owns the
lease. Expired leases can be reclaimed; a late worker whose lease lapsed is
rejected by predicate.

AI-original result columns are immutable once written; teacher review is a
separate row. Hard failures block release; scored soft-review rows remain
publishable while retaining their review signal.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from sqlalchemy import select

from backend.config import settings
from backend.db import (
    assignment_repository,
    grading_repository,
    submission_repository,
)
from backend.db.models import GradingRunRecord, UserRecord
from backend.db.session import session_scope
from backend.domain import education
from backend.domain.errors import DomainError, NotFound, ValidationError, VersionConflict
from backend.llm.registry import _build_scoped_registry
from backend.models import TaskGradingSetup, User
from backend.progress.tracker import get_or_create_reporter
from backend.services import grading_adapter

logger = logging.getLogger(__name__)


def _questions_for_run(
    *, assignment_id: str, frozen_setup,
) -> list[education.QuestionDTO]:
    """Return immutable question inputs for a façade grading run.

    New runs persist full QuestionDTO payloads in ``input_manifest``.  Older
    manifests containing only ids/versions remain readable only while the
    current rows still match those versions; a later edit fails closed instead
    of grading silently changed questions.
    """
    current = assignment_repository.get_questions_by_assignment(
        assignment_id=assignment_id
    )
    if frozen_setup is None:
        return current
    items = list((frozen_setup.input_manifest or {}).get("questions", []))
    if not items:
        raise ValidationError("grading_question_snapshot_missing")
    if all(
        isinstance(item, dict)
        and {
            "id", "assignment_id", "q_id", "order_index", "type",
            "created_at", "updated_at", "version",
        }.issubset(item)
        for item in items
    ):
        try:
            frozen = [education.QuestionDTO.model_validate(item) for item in items]
        except Exception as exc:
            raise ValidationError("grading_question_snapshot_invalid") from exc
        if any(question.assignment_id != assignment_id for question in frozen):
            raise ValidationError("grading_question_snapshot_invalid")
        return frozen

    expected = [
        (str(item.get("id")), int(item.get("version", -1)))
        for item in items
        if isinstance(item, dict)
    ]
    actual = [(question.id, question.version) for question in current]
    if expected != actual:
        raise VersionConflict("grading_inputs_changed")
    return current


def _registry_for(teacher_id: str):
    """Build the run owner's BYOK/shared-pool registry.

    Never fall back to another process-global registry: doing so can reuse the
    first teacher's decrypted credentials for a later teacher.
    """
    with session_scope() as session:
        record = session.get(UserRecord, teacher_id)
        if record is None:
            raise NotFound("grading_run_teacher")
        user = User(id=record.id, username=record.username, email=record.email or "",
                    role=record.role, password_hash=record.password_hash,
                    created_at=record.created_at, is_active=record.is_active)
    return _build_scoped_registry(user)


def poll_queued_runs() -> list[str]:
    """Return queued runs and running runs whose lease expired.

    Including expired running rows is what lets another worker reclaim a run
    after a process crash; the claim predicate remains the concurrency gate.
    """
    with session_scope() as session:
        rows = session.scalars(
            select(GradingRunRecord.id)
            .where(
                (GradingRunRecord.status == education.GradingRunStatus.QUEUED.value)
                | (
                    (GradingRunRecord.status == education.GradingRunStatus.RUNNING.value)
                    & (GradingRunRecord.lease_expiry < time.time())
                )
            )
            .order_by(GradingRunRecord.created_at)
        ).all()
        return list(rows)


async def worker_loop(*, worker_id: str, poll_seconds: int | None = None,
                      registry=None) -> None:
    """Claim and grade queued runs until cancelled. One loop per process; DB
    lease predicates keep multiple processes safe. Shutdown cancels the loop,
    which stops claiming new work and releases no unexpired lease."""
    interval = poll_seconds if poll_seconds is not None else settings.grading_poll_seconds
    while True:
        try:
            for run_id in poll_queued_runs():
                # The injected registry is test-only. Production builds a fresh
                # owner-scoped registry inside process_run for every run.
                await process_run(run_id=run_id, worker_id=worker_id, registry=registry)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("grading worker tick failed")
        await asyncio.sleep(interval)


def start_run(
    *,
    assignment_id: str,
    teacher_id: str,
    grading_setup: dict | None = None,
    setup_fingerprint: str | None = None,
    input_manifest: dict | None = None,
) -> education.GradingRunDTO:
    """Create a run and freeze the current revisions to grade.

    Freezing at creation means a regrade of a later revision cannot retroactively
    change what a prior run graded. One active run per assignment is enforced by
    the partial unique index; a second start raises DuplicateActiveRun.
    """
    assignment = assignment_repository.get_assignment(
        assignment_id=assignment_id, actor_id=teacher_id
    )
    if assignment.status not in (education.AssignmentStatus.PUBLISHED.value,
                                 education.AssignmentStatus.CLOSED.value):
        from backend.domain.errors import InvalidTransition
        raise InvalidTransition("assignment_not_gradable")
    questions = assignment_repository.get_questions_by_assignment(assignment_id=assignment_id)
    if not questions:
        from backend.domain.errors import ValidationError
        raise ValidationError("Assignment has no questions")
    # Collect each enrolled student's current revision to freeze.
    submissions = submission_repository.list_submissions(
        assignment_id=assignment_id, actor_id=teacher_id
    )
    frozen_revision_ids: list[str] = []
    for sub in submissions:
        if sub.current_revision_id is None:
            continue
        frozen_revision_ids.append(sub.current_revision_id)
    if input_manifest is not None:
        expected_question_ids = [str(item.get("id")) for item in input_manifest.get("questions", [])]
        if expected_question_ids != [question.id for question in questions]:
            raise VersionConflict("grading_inputs_changed")
        expected_question_versions = [
            int(item.get("version", -1))
            for item in input_manifest.get("questions", [])
        ]
        if expected_question_versions != [question.version for question in questions]:
            raise VersionConflict("grading_inputs_changed")
        expected_revisions = list(input_manifest.get("submission_revision_ids", []))
        if any(revision_id not in frozen_revision_ids for revision_id in expected_revisions):
            raise VersionConflict("grading_inputs_changed")
        frozen_revision_ids = expected_revisions
    run = grading_repository.create_run_bundle(
        assignment_id=assignment_id,
        teacher_id=teacher_id,
        revision_ids=frozen_revision_ids,
        setup=grading_setup,
        setup_fingerprint=setup_fingerprint,
        input_manifest=input_manifest,
    )
    return run


def get_run(*, run_id: str, actor_id: str, role: str) -> dict:
    run = grading_repository.get_run(run_id=run_id) if role == "admin" else grading_repository.get_run(run_id=run_id, actor_id=actor_id)
    events = grading_repository.list_events(run_id=run_id, actor_id=None if role == "admin" else actor_id)
    return {"run": _serialize_run(run), "events": events}


def list_runs(*, assignment_id: str, actor_id: str, role: str) -> list[dict]:
    runs = grading_repository.list_runs_for_assignment(assignment_id=assignment_id, actor_id=actor_id)
    return [_serialize_run(r) for r in runs]


async def process_run(*, run_id: str, worker_id: str, registry=None, language: str = "en") -> None:
    """Claim and grade one queued run. Idempotent: if the lease cannot be claimed
    (already taken by another worker, or terminal), this is a no-op.

    Raises on a batch-level failure so the caller can mark the run failed; per-
    question failures land as explicit ``failed`` results via the adapter.
    """
    try:
        grading_repository.claim_lease(run_id=run_id, worker_id=worker_id, lease_seconds=settings.grading_lease_seconds)
    except DomainError:
        return  # someone else owns it or it is terminal
    run = grading_repository.get_run(run_id=run_id)
    heartbeat_task: Optional[asyncio.Task] = None

    async def _heartbeat():
        while True:
            await asyncio.sleep(settings.grading_heartbeat_seconds)
            try:
                grading_repository.heartbeat(
                    run_id=run_id, worker_id=worker_id,
                    lease_seconds=settings.grading_lease_seconds,
                )
            except DomainError:
                return  # lease lost; stop heartbeating

    try:
        heartbeat_task = asyncio.create_task(_heartbeat())
        run_registry = registry or _registry_for(run.teacher_id)
        grading_setup = None
        from backend.db.workflow_repository import get_run_setup

        # A normalized caller that does not use the Figma façade has no setup
        # row and retains the main default behavior. Database/read failures are
        # deliberately not swallowed: silently widening the provider selection
        # would violate the teacher-approved cost and privacy boundary.
        frozen_setup = get_run_setup(run_id)
        if frozen_setup is not None:
            grading_setup = TaskGradingSetup.model_validate(frozen_setup.setup)
            from backend.services.grading_input_security import (
                provider_configuration_fingerprint,
            )
            frozen_provider_fingerprint = (
                frozen_setup.input_manifest or {}
            ).get("provider_configuration_fingerprint")
            if not frozen_provider_fingerprint or (
                provider_configuration_fingerprint(
                    owner_id=run.teacher_id,
                    selected_provider_ids=grading_setup.selected_provider_ids,
                )
                != frozen_provider_fingerprint
            ):
                raise ValidationError(
                    "Provider configuration changed after run confirmation.",
                    code="grading_provider_configuration_changed",
                )
            try:
                run_registry = run_registry.select(
                    grading_setup.selected_provider_ids,
                    primary_provider_id=grading_setup.primary_provider_id,
                )
            except ValueError as exc:
                raise ValidationError("grading_provider_selection_invalid") from exc
            language = grading_setup.feedback_language
        # The explicitly enabled E2E provider is injected inside the adapter,
        # so it does not appear in a teacher-scoped production registry.
        if run_registry.count() == 0 and not settings.e2e_fake_provider:
            raise ValidationError("no_provider_configured")
        questions = _questions_for_run(
            assignment_id=run.assignment_id, frozen_setup=frozen_setup,
        )
        frozen_rows = grading_repository.list_frozen_submissions(run_id=run_id)
        frozen_revisions: list[tuple[education.SubmissionRevisionDTO, str]] = []
        for fr in frozen_rows:
            revision = submission_repository.get_revision(revision_id=fr.id, actor_id=run.teacher_id)
            if revision is not None:
                frozen_revisions.append((revision, fr.student_id))
        grading_repository.record_event(
            run_id=run_id, level="info", message="grading_started",
            payload={"students": len(frozen_revisions), "questions": len(questions)},
        )
        reporter = get_or_create_reporter(
            run_id,
            total_students=len(frozen_revisions),
            total_questions=len(questions),
        )

        def _persist_progress(event, payload):
            grading_repository.record_event(
                run_id=run_id,
                level=event.level,
                message="grading_progress",
                payload=payload,
            )

        reporter.set_event_sink(_persist_progress)
        outcomes = await grading_adapter.run_grading(
            run_id=run_id, assignment_id=run.assignment_id, teacher_id=run.teacher_id,
            questions=questions, frozen_revisions=frozen_revisions,
            registry=run_registry, language=language, reporter=reporter,
            grading_setup=grading_setup,
        )
        for outcome in outcomes:
            for res in outcome.results:
                try:
                    grading_repository.upsert_result(
                        run_id=run_id, worker_id=worker_id, grade_result=res
                    )
                except VersionConflict:
                    pass
        completed, failed = grading_repository.persisted_result_counters(run_id)
        grading_repository.mark_completed(
            run_id=run_id, worker_id=worker_id, completed=completed, failed=failed
        )
        grading_repository.record_event(
            run_id=run_id, level="info", message="run_completed",
            payload={"completed": completed, "failed": failed},
        )
    except Exception:
        logger.exception("Grading run %s failed", run_id)
        try:
            grading_repository.mark_failed(
                run_id=run_id,
                worker_id=worker_id,
                error_message="grading_failed",
            )
            grading_repository.record_event(
                run_id=run_id, level="error", message="run_failed",
                payload={"code": "grading_failed"},
            )
        except DomainError:
            pass  # lease already lost; another worker will reclaim
        raise
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except (asyncio.CancelledError, Exception):
                pass


def list_review_queue(*, assignment_id: str, teacher_id: str) -> list[education.GradeResultDTO]:
    # Ownership is implied: the teacher queries their own assignment's review queue.
    assignment_repository.get_assignment(assignment_id=assignment_id, actor_id=teacher_id)
    return grading_repository.list_results_for_review(assignment_id=assignment_id)


def add_teacher_review(
    *, grade_result_id: str, teacher_id: str, new_score: float,
    new_comment: str = "", confirm: bool = True,
) -> education.TeacherReviewDTO:
    return grading_repository.add_teacher_review(
        grade_result_id=grade_result_id, teacher_id=teacher_id,
        new_score=new_score, new_comment=new_comment, confirm=confirm,
    )


def release_run(*, run_id: str, teacher_id: str) -> education.GradingRunDTO:
    return grading_repository.release(run_id=run_id, teacher_id=teacher_id)


# Compatibility aliases used by tests/direct callers. Keep one implementation
# per use case so worker and read contracts cannot drift.
def create_run(*, teacher_id: str, assignment_id: str) -> education.GradingRunDTO:
    return start_run(assignment_id=assignment_id, teacher_id=teacher_id)


def release(*, teacher_id: str, run_id: str) -> education.GradingRunDTO:
    return grading_repository.release(run_id=run_id, teacher_id=teacher_id)


def persist_results(*, run_id: str, worker_id: str,
                    results: list[education.GradeResultDTO]) -> int:
    """Persist a batch of GradeResult DTOs for a run. Returns the count written.

    Already-written (run, revision, question) triples raise VersionConflict
    inside the repository; we skip those so a retry is idempotent.
    """
    # Direct deterministic callers may hand us a queued run; claim it before
    # writing. Running runs are never re-claimed, so another worker remains
    # protected by the repository predicate.
    current = grading_repository.get_run(run_id=run_id)
    if current.status == education.GradingRunStatus.QUEUED.value:
        grading_repository.claim_lease(
            run_id=run_id, worker_id=worker_id,
            lease_seconds=settings.grading_lease_seconds,
        )
    written = 0
    for res in results:
        try:
            grading_repository.upsert_result(
                run_id=run_id, worker_id=worker_id, grade_result=res
            )
            written += 1
        except VersionConflict:
            continue
    return written


def resolve_review(*, grade_result_id: str, teacher_id: str) -> education.GradeResultDTO:
    """Compatibility read after ``add_teacher_review`` resolved the result."""
    from backend.db.models import GradeResultRecord, GradingRunRecord
    from backend.db.session import session_scope

    with session_scope() as session:
        result = session.get(GradeResultRecord, grade_result_id)
        if result is None:
            raise NotFound("grade_result")
        run = session.get(GradingRunRecord, result.grading_run_id)
        if run is None or run.teacher_id != teacher_id:
            raise NotFound("grade_result")
        review = grading_repository._latest_reviews_by_result(session, [result.id]).get(result.id)
        return grading_repository._result_to_dto(result, review)


def _serialize_run(run: education.GradingRunDTO) -> dict:
    return {
        "id": run.id,
        "assignment_id": run.assignment_id,
        "teacher_id": run.teacher_id,
        "status": run.status,
        "lease_owner": run.lease_owner,
        "lease_expiry": run.lease_expiry,
        "last_heartbeat_at": run.last_heartbeat_at,
        "total_submissions": run.total_submissions,
        "completed_submissions": run.completed_submissions,
        "failed_submissions": run.failed_submissions,
        "error_message": run.error_message,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
        "released_at": run.released_at,
    }


# ─── Compatibility wrappers (named to match the lifecycle test contract) ──────


def student_results(*, student_id: str, assignment_id: str) -> list[education.GradeResultDTO]:
    """Released, student-visible results for one (assignment, student).

    Only results from a *released* run are returned, so draft grades and
    provider traces never reach students. Scored soft-review rows are visible
    with their effective AI default; hard failures cannot reach this point
    because they block release.

    A later submission revision must not make an already-published result
    disappear: release freezes the visible result set, while the new revision
    belongs to a future grading run.
    """
    from sqlalchemy import select as _select
    from backend.db.models import GradeResultRecord, GradingRunRecord
    from backend.db.session import session_scope

    with session_scope() as session:
        latest_run_id = session.scalar(
            _select(GradingRunRecord.id)
            .where(
                GradingRunRecord.assignment_id == assignment_id,
                GradingRunRecord.released_at.is_not(None),
            )
            .order_by(GradingRunRecord.released_at.desc())
            .limit(1)
        )
        if latest_run_id is None:
            return []
        rows = session.scalars(
            _select(GradeResultRecord)
            .where(
                GradeResultRecord.student_id == student_id,
                GradeResultRecord.grading_run_id == latest_run_id,
                GradeResultRecord.result_status.not_in(
                    education.NON_SCOREABLE_RESULT_STATUSES
                ),
            )
            .order_by(GradeResultRecord.q_id)
        ).all()
        reviews = grading_repository._latest_reviews_by_result(session, [r.id for r in rows])
        return [grading_repository._result_to_dto(r, reviews.get(r.id)) for r in rows]
