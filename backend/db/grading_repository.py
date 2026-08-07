"""Grading run, lease, result, review, release, and progress access.

A run is the unit of batch grading for one assignment. The design enforces two
invariants in SQL plus conditional UPDATEs:

* at most one *active* (queued/running) run per assignment — a partial unique
  index whose active-status set mirrors ``ACTIVE_GRADING_RUN_STATUSES``;
* a durable lease — only the worker that claimed the run may write terminal
  state, and only while its lease has not expired. ``claim_lease`` is a single
  conditional UPDATE that either flips a free/expired run to running (rowcount 1)
  or loses (rowcount 0 → ``LeaseLost``). Heartbeats and terminal writes carry
  the ``lease_owner`` predicate so a late worker cannot overwrite another.

AI-original ``grade_results`` columns are immutable once written; a teacher
review is a separate ``teacher_reviews`` row, never an overwrite. Only hard
failures block release; scored soft-review rows keep their warning and use the
AI score by default.
"""
from __future__ import annotations

import time
import uuid
import math

from sqlalchemy import exists, func, select, update

from backend.db.models import (
    AssignmentRecord,
    AssignmentQuestionRecord,
    GradeResultRecord,
    GradingRunEventRecord,
    GradingRunRecord,
    GradingRunSubmissionRecord,
    SubmissionRecord,
    SubmissionRevisionRecord,
    TeacherReviewRecord,
)
from backend.db.session import session_scope
from backend.domain import education
from backend.domain.errors import (
    DuplicateActiveRun,
    InvalidTransition,
    LeaseLost,
    NotFound,
    ResultNotReleasable,
    ValidationError,
    VersionConflict,
)


def _new_run_id() -> str:
    return f"run_{uuid.uuid4().hex[:12]}"


def _new_result_id() -> str:
    return f"gr_{uuid.uuid4().hex[:12]}"


def _new_review_id() -> str:
    return f"rev_{uuid.uuid4().hex[:12]}"


def _new_event_id() -> str:
    return f"evt_{uuid.uuid4().hex[:12]}"


def _run_to_dto(record: GradingRunRecord) -> education.GradingRunDTO:
    return education.GradingRunDTO(
        id=record.id,
        assignment_id=record.assignment_id,
        teacher_id=record.teacher_id,
        status=record.status,
        lease_owner=record.lease_owner,
        lease_expiry=record.lease_expiry,
        last_heartbeat_at=record.last_heartbeat_at,
        total_submissions=record.total_submissions,
        completed_submissions=record.completed_submissions,
        failed_submissions=record.failed_submissions,
        error_message=record.error_message,
        created_at=record.created_at,
        started_at=record.started_at,
        completed_at=record.completed_at,
        released_at=record.released_at,
    )


def create_run(assignment_id: str, *, teacher_id: str, total_submissions: int) -> education.GradingRunDTO:
    run_id = _new_run_id()
    now = time.time()
    with session_scope() as session:
        assignment = session.scalar(
            select(AssignmentRecord).where(
                AssignmentRecord.id == assignment_id, AssignmentRecord.teacher_id == teacher_id
            )
        )
        if assignment is None:
            raise NotFound("assignment")
        # At most one active run per assignment is enforced by the partial unique
        # index; an IntegrityError here means a concurrent active run exists.
        from sqlalchemy.exc import IntegrityError

        record = GradingRunRecord(
            id=run_id,
            assignment_id=assignment_id,
            teacher_id=teacher_id,
            status=education.GradingRunStatus.QUEUED.value,
            lease_owner=None,
            lease_expiry=None,
            last_heartbeat_at=None,
            total_submissions=total_submissions,
            completed_submissions=0,
            failed_submissions=0,
            error_message=None,
            created_at=now,
            started_at=None,
            completed_at=None,
        )
        session.add(record)
        try:
            session.flush()
        except IntegrityError as exc:  # pragma: no cover - depends on dialect
            raise DuplicateActiveRun("active_run_exists") from exc
        return _run_to_dto(record)


def create_run_bundle(
    assignment_id: str,
    *,
    teacher_id: str,
    revision_ids: list[str],
    setup: dict | None = None,
    setup_fingerprint: str | None = None,
    input_manifest: dict | None = None,
) -> education.GradingRunDTO:
    """Atomically create a queued run, its frozen revisions, and setup.

    A worker polls committed queued rows.  Keeping every prerequisite in the
    same transaction prevents it from observing a run before the frozen input
    set or the teacher-approved provider selection exists.
    """
    if (setup is None) != (setup_fingerprint is None):
        raise ValidationError("grading_setup_bundle_incomplete")

    # Imported lazily to keep the normalized repository usable independently
    # while still sharing this transaction with façade-only presentation data.
    from backend.db.workflow_repository import GradingRunSetupRecord
    from sqlalchemy.exc import IntegrityError

    run_id = _new_run_id()
    now = time.time()
    with session_scope() as session:
        assignment = session.scalar(
            select(AssignmentRecord).where(
                AssignmentRecord.id == assignment_id,
                AssignmentRecord.teacher_id == teacher_id,
            )
        )
        if assignment is None:
            raise NotFound("assignment")

        record = GradingRunRecord(
            id=run_id,
            assignment_id=assignment_id,
            teacher_id=teacher_id,
            status=education.GradingRunStatus.QUEUED.value,
            lease_owner=None,
            lease_expiry=None,
            last_heartbeat_at=None,
            total_submissions=len(revision_ids),
            completed_submissions=0,
            failed_submissions=0,
            error_message=None,
            created_at=now,
            started_at=None,
            completed_at=None,
        )
        session.add(record)
        try:
            session.flush()
        except IntegrityError as exc:  # pragma: no cover - dialect-specific
            raise DuplicateActiveRun("active_run_exists") from exc

        for revision_id in revision_ids:
            revision = session.get(SubmissionRevisionRecord, revision_id)
            if revision is None:
                raise ValidationError("frozen_submission_revision_missing")
            submission = session.get(SubmissionRecord, revision.submission_id)
            if submission is None or submission.assignment_id != assignment_id:
                raise ValidationError("frozen_submission_assignment_mismatch")
            session.add(
                GradingRunSubmissionRecord(
                    id=f"rs_{uuid.uuid4().hex[:12]}",
                    grading_run_id=run_id,
                    submission_revision_id=revision_id,
                    student_id=submission.student_id,
                    created_at=now,
                )
            )

        if setup is not None:
            session.add(
                GradingRunSetupRecord(
                    grading_run_id=run_id,
                    assignment_id=assignment_id,
                    owner_id=teacher_id,
                    setup=dict(setup),
                    input_manifest=dict(input_manifest or {}),
                    fingerprint=setup_fingerprint,
                    created_at=now,
                )
            )

        session.add(
            GradingRunEventRecord(
                id=_new_event_id(),
                grading_run_id=run_id,
                sequence=1,
                level="info",
                message="run_created",
                payload={
                    "frozen_revisions": len(revision_ids),
                    "setup_frozen": setup is not None,
                },
                created_at=now,
            )
        )
        session.flush()
        return _run_to_dto(record)


def clone_released_run_for_review(
    run_id: str, *, teacher_id: str
) -> education.GradingRunDTO:
    """Create an unreleased review revision without mutating published results.

    The cloned run keeps immutable AI facts and frozen submission/question FKs.
    Its latest effective teacher values are copied as confirmed review rows.
    Subsequent draft edits affect only the clone; students continue reading the
    previous released run until the teacher explicitly releases this revision.
    """
    from backend.db.workflow_repository import GradingRunSetupRecord

    now = time.time()
    new_run_id = _new_run_id()
    with session_scope() as session:
        source = session.scalar(
            select(GradingRunRecord).where(
                GradingRunRecord.id == run_id,
                GradingRunRecord.teacher_id == teacher_id,
                GradingRunRecord.released_at.is_not(None),
            )
        )
        if source is None:
            raise InvalidTransition("released_run_required")

        clone = GradingRunRecord(
            id=new_run_id,
            assignment_id=source.assignment_id,
            teacher_id=teacher_id,
            status=education.GradingRunStatus.COMPLETED.value,
            lease_owner=None,
            lease_expiry=None,
            last_heartbeat_at=None,
            total_submissions=source.total_submissions,
            completed_submissions=source.completed_submissions,
            failed_submissions=0,
            error_message=None,
            created_at=now,
            started_at=source.started_at,
            completed_at=now,
            released_at=None,
        )
        session.add(clone)
        session.flush()

        frozen_rows = session.scalars(
            select(GradingRunSubmissionRecord).where(
                GradingRunSubmissionRecord.grading_run_id == run_id
            )
        ).all()
        for frozen in frozen_rows:
            session.add(
                GradingRunSubmissionRecord(
                    id=f"rs_{uuid.uuid4().hex[:12]}",
                    grading_run_id=new_run_id,
                    submission_revision_id=frozen.submission_revision_id,
                    student_id=frozen.student_id,
                    created_at=now,
                )
            )

        source_results = session.scalars(
            select(GradeResultRecord).where(
                GradeResultRecord.grading_run_id == run_id
            )
        ).all()
        source_reviews = _latest_reviews_by_result(
            session, [result.id for result in source_results]
        )
        for source_result in source_results:
            result_id = _new_result_id()
            cloned_result = GradeResultRecord(
                id=result_id,
                grading_run_id=new_run_id,
                submission_revision_id=source_result.submission_revision_id,
                question_id=source_result.question_id,
                student_id=source_result.student_id,
                q_id=source_result.q_id,
                ai_score=source_result.ai_score,
                ai_max_score=source_result.ai_max_score,
                ai_comment=source_result.ai_comment,
                ai_steps=list(source_result.ai_steps or []),
                ai_confidence=source_result.ai_confidence,
                ai_expert_results=list(source_result.ai_expert_results or []),
                ai_synthesis_method=source_result.ai_synthesis_method,
                requires_review=False,
                review_reasons=[],
                initial_requires_review=source_result.initial_requires_review,
                initial_review_reasons=list(
                    source_result.initial_review_reasons or []
                ),
                result_status=education.GradeResultStatus.GRADED.value,
                created_at=now,
                updated_at=now,
            )
            session.add(cloned_result)
            review = source_reviews.get(source_result.id)
            if review is not None:
                session.add(
                    TeacherReviewRecord(
                        id=_new_review_id(),
                        grade_result_id=result_id,
                        teacher_id=teacher_id,
                        previous_score=review.new_score,
                        previous_comment=review.new_comment,
                        new_score=review.new_score,
                        new_comment=review.new_comment,
                        comment=review.new_comment,
                        confirmed=True,
                        review_sequence=1,
                        created_at=now,
                    )
                )

        source_setup = session.get(GradingRunSetupRecord, run_id)
        if source_setup is not None:
            session.add(
                GradingRunSetupRecord(
                    grading_run_id=new_run_id,
                    assignment_id=source_setup.assignment_id,
                    owner_id=source_setup.owner_id,
                    setup=dict(source_setup.setup or {}),
                    input_manifest=dict(source_setup.input_manifest or {}),
                    fingerprint=source_setup.fingerprint,
                    created_at=now,
                )
            )
        session.add(
            GradingRunEventRecord(
                id=_new_event_id(),
                grading_run_id=new_run_id,
                sequence=1,
                level="info",
                message="review_revision_created",
                payload={"source_run_id": run_id},
                created_at=now,
            )
        )
        session.flush()
        return _run_to_dto(clone)


def get_run(run_id: str, *, actor_id: str | None = None) -> education.GradingRunDTO:
    with session_scope() as session:
        record = session.get(GradingRunRecord, run_id)
        if record is None:
            raise NotFound("grading_run")
        if actor_id is not None and record.teacher_id != actor_id:
            # Scope by teacher; non-owner reads no run (no payload leak).
            raise NotFound("grading_run")
        return _run_to_dto(record)


def list_runs_for_assignment(assignment_id: str, *, actor_id: str) -> list[education.GradingRunDTO]:
    with session_scope() as session:
        records = session.scalars(
            select(GradingRunRecord)
            .where(GradingRunRecord.assignment_id == assignment_id, GradingRunRecord.teacher_id == actor_id)
            .order_by(GradingRunRecord.created_at)
        ).all()
        return [_run_to_dto(r) for r in records]


def claim_lease(run_id: str, *, worker_id: str, lease_seconds: int) -> education.GradingRunDTO:
    """Atomically claim or reclaim a run's lease.

    The conditional UPDATE matches a run that is either never-leased, or whose
    lease has expired, or already owned by this worker. rowcount 0 means the run
    is held by another live worker → ``LeaseLost``.
    """
    now = time.time()
    new_expiry = now + lease_seconds
    with session_scope() as session:
        result = session.execute(
            update(GradingRunRecord)
            .where(
                GradingRunRecord.id == run_id,
                GradingRunRecord.status.in_([
                    education.GradingRunStatus.QUEUED.value,
                    education.GradingRunStatus.RUNNING.value,
                ]),
                # Free, expired, or already mine.
                (
                    (GradingRunRecord.lease_owner.is_(None))
                    | (GradingRunRecord.lease_owner == worker_id)
                    | (GradingRunRecord.lease_expiry < now)
                ),
            )
            .values(
                lease_owner=worker_id,
                lease_expiry=new_expiry,
                last_heartbeat_at=now,
                status=education.GradingRunStatus.RUNNING.value,
                started_at=now,
            )
        )
        if result.rowcount != 1:
            raise LeaseLost("run_not_claimable")
        record = session.get(GradingRunRecord, run_id)
        assert record is not None
        return _run_to_dto(record)


def heartbeat(run_id: str, *, worker_id: str, lease_seconds: int) -> bool:
    """Extend the lease. Only the current owner may heartbeat; any other worker
    (or a worker whose lease already lapsed) raises ``LeaseLost`` so the worker
    loop stops rather than silently extending a lease it no longer holds."""
    now = time.time()
    with session_scope() as session:
        result = session.execute(
            update(GradingRunRecord)
            .where(
                GradingRunRecord.id == run_id,
                GradingRunRecord.lease_owner == worker_id,
                GradingRunRecord.lease_expiry >= now,
                GradingRunRecord.status == education.GradingRunStatus.RUNNING.value,
            )
            .values(lease_expiry=now + lease_seconds, last_heartbeat_at=now)
        )
        if result.rowcount != 1:
            raise LeaseLost("lease_lost")
        return True


def _terminal_update(run_id: str, *, worker_id: str, status: str,
                     completed: int, failed: int, error_message: str | None = None) -> education.GradingRunDTO:
    now = time.time()
    with session_scope() as session:
        result = session.execute(
            update(GradingRunRecord)
            .where(
                GradingRunRecord.id == run_id,
                GradingRunRecord.lease_owner == worker_id,
                GradingRunRecord.lease_expiry >= now,
                GradingRunRecord.status == education.GradingRunStatus.RUNNING.value,
            )
            .values(
                status=status,
                completed_submissions=completed,
                failed_submissions=failed,
                error_message=error_message,
                completed_at=now,
                lease_owner=None,
                lease_expiry=None,
            )
        )
        if result.rowcount != 1:
            raise LeaseLost("lease_lost")
        record = session.get(GradingRunRecord, run_id)
        assert record is not None
        return _run_to_dto(record)


def mark_completed(run_id: str, *, worker_id: str, completed: int, failed: int) -> education.GradingRunDTO:
    status = (
        education.GradingRunStatus.PARTIAL_FAILED.value
        if failed > 0
        else education.GradingRunStatus.COMPLETED.value
    )
    return _terminal_update(run_id=run_id, worker_id=worker_id, status=status, completed=completed, failed=failed)


def mark_failed(run_id: str, *, worker_id: str, error_message: str) -> education.GradingRunDTO:
    return _terminal_update(
        run_id=run_id, worker_id=worker_id,
        status=education.GradingRunStatus.FAILED.value, completed=0, failed=0,
        error_message=error_message,
    )


def cancel(run_id: str, *, teacher_id: str) -> education.GradingRunDTO:
    """Teacher-initiated cancel of an active run (drops the lease)."""
    now = time.time()
    with session_scope() as session:
        result = session.execute(
            update(GradingRunRecord)
            .where(
                GradingRunRecord.id == run_id,
                GradingRunRecord.teacher_id == teacher_id,
                GradingRunRecord.status.in_([
                    education.GradingRunStatus.QUEUED.value,
                    education.GradingRunStatus.RUNNING.value,
                ]),
            )
            .values(
                status=education.GradingRunStatus.CANCELLED.value,
                completed_at=now,
                lease_owner=None,
                lease_expiry=None,
            )
        )
        if result.rowcount != 1:
            existing = session.get(GradingRunRecord, run_id)
            if existing is None or existing.teacher_id != teacher_id:
                raise NotFound("grading_run")
            raise InvalidTransition("run_not_active")
        record = session.get(GradingRunRecord, run_id)
        assert record is not None
        return _run_to_dto(record)


def add_frozen_submissions(run_id: str, *, revision_ids: list[str]) -> None:
    """Record the (run, revision) pairs graded by this run, frozen at creation."""
    now = time.time()
    with session_scope() as session:
        for revision_id in revision_ids:
            revision = session.get(SubmissionRevisionRecord, revision_id)
            assert revision is not None
            submission = session.get(SubmissionRecord, revision.submission_id)
            assert submission is not None
            session.add(
                GradingRunSubmissionRecord(
                    id=f"rs_{uuid.uuid4().hex[:12]}",
                    grading_run_id=run_id,
                    submission_revision_id=revision_id,
                    student_id=submission.student_id,
                    created_at=now,
                )
            )


def list_frozen_submissions(run_id: str) -> list["FrozenSubmission"]:
    """Frozen (revision, student) pairs for a run, in creation order.

    student_id is denormalized onto the frozen row at run creation so the grader
    need not re-join submissions to know who owns each revision. ``id`` is the
    revision id (the value results point at).
    """
    with session_scope() as session:
        rows = session.scalars(
            select(GradingRunSubmissionRecord)
            .where(GradingRunSubmissionRecord.grading_run_id == run_id)
            .order_by(GradingRunSubmissionRecord.created_at)
        ).all()
        out: list[FrozenSubmission] = []
        for r in rows:
            revision = session.get(SubmissionRevisionRecord, r.submission_revision_id)
            out.append(FrozenSubmission(
                id=r.submission_revision_id, submission_id=revision.submission_id if revision else "",
                student_id=r.student_id, revision_number=revision.revision_number if revision else 0,
            ))
        return out


from dataclasses import dataclass


@dataclass(frozen=True)
class FrozenSubmission:
    id: str  # submission_revision_id
    submission_id: str
    student_id: str
    revision_number: int


def record_event(run_id: str, *, level: str, message: str, payload: dict | None = None) -> None:
    now = time.time()
    with session_scope() as session:
        next_seq = (session.scalar(
            select(func.max(GradingRunEventRecord.sequence)).where(
                GradingRunEventRecord.grading_run_id == run_id
            )
        ) or 0) + 1
        session.add(
            GradingRunEventRecord(
                id=_new_event_id(),
                grading_run_id=run_id,
                sequence=next_seq,
                level=level,
                message=message,
                payload=payload or {},
                created_at=now,
            )
        )


def list_events(run_id: str, *, actor_id: str | None = None) -> list[dict]:
    with session_scope() as session:
        if actor_id is not None:
            run = session.get(GradingRunRecord, run_id)
            if run is None or run.teacher_id != actor_id:
                raise NotFound("grading_run")
        rows = session.scalars(
            select(GradingRunEventRecord)
            .where(GradingRunEventRecord.grading_run_id == run_id)
            .order_by(GradingRunEventRecord.sequence)
        ).all()
        return [
            {"sequence": r.sequence, "level": r.level, "message": r.message,
             "payload": dict(r.payload or {}), "created_at": r.created_at}
            for r in rows
        ]


# ─── grade results (immutable AI outputs) ─────────────────────────────────────


def _result_matrix_axes(
    session, run: GradingRunRecord,
) -> tuple[set[str], set[str]]:
    """Return the immutable revision/question axes expected for ``run``.

    Facade runs freeze question ids in their setup manifest. Normalized callers
    without a setup row use the assignment's questions, which are immutable
    after publication. Worker recovery and release therefore share one matrix
    definition instead of deriving counters from only the latest attempt.
    """
    from backend.db.workflow_repository import GradingRunSetupRecord

    revision_ids = set(session.scalars(
        select(GradingRunSubmissionRecord.submission_revision_id).where(
            GradingRunSubmissionRecord.grading_run_id == run.id
        )
    ).all())
    setup = session.get(GradingRunSetupRecord, run.id)
    if setup is not None:
        raw_questions = (setup.input_manifest or {}).get("questions")
        if not isinstance(raw_questions, list):
            return revision_ids, set()
        question_ids = {
            str(item.get("id"))
            for item in raw_questions
            if isinstance(item, dict) and item.get("id")
        }
    else:
        question_ids = set(session.scalars(
            select(AssignmentQuestionRecord.id).where(
                AssignmentQuestionRecord.assignment_id == run.assignment_id
            )
        ).all())
    return revision_ids, question_ids


def persisted_result_counters(run_id: str) -> tuple[int, int]:
    """Recompute terminal counters from durable results, including retries.

    ``completed`` counts frozen revisions with every expected question result.
    ``failed`` counts incomplete revisions or complete revisions containing a
    non-scoreable hard failure. A complete revision containing only scored soft
    reviews is completed, not failed. Thus an expired-lease retry that encounters
    only duplicate rows still reports work committed by the previous worker.
    """
    with session_scope() as session:
        run = session.get(GradingRunRecord, run_id)
        if run is None:
            raise NotFound("grading_run")
        revision_ids, question_ids = _result_matrix_axes(session, run)
        rows = session.execute(
            select(
                GradeResultRecord.submission_revision_id,
                GradeResultRecord.question_id,
                GradeResultRecord.result_status,
            ).where(GradeResultRecord.grading_run_id == run_id)
        ).all()
        statuses_by_revision: dict[str, dict[str, str]] = {
            revision_id: {} for revision_id in revision_ids
        }
        for revision_id, question_id, result_status in rows:
            if revision_id in revision_ids and question_id in question_ids:
                statuses_by_revision[revision_id][question_id] = result_status

        completed = 0
        failed = 0
        for statuses in statuses_by_revision.values():
            complete = bool(question_ids) and question_ids.issubset(statuses)
            if complete:
                completed += 1
            if not complete or any(
                status in education.NON_SCOREABLE_RESULT_STATUSES
                for status in statuses.values()
            ):
                failed += 1
        return completed, failed


def upsert_result(run_id: str, *, worker_id: str,
                  grade_result: education.GradeResultDTO) -> education.GradeResultDTO:
    """Persist one graded (run, revision, question) result. AI columns are immutable
    once written; re-grading the same triple is rejected rather than overwritten.

    The live lease and frozen membership checks share this transaction with the
    insert. A stale worker therefore cannot add a row after cancellation,
    completion, release, or lease takeover.
    """
    now = time.time()
    with session_scope() as session:
        run = session.scalar(
            select(GradingRunRecord).where(
                GradingRunRecord.id == run_id,
                GradingRunRecord.status == education.GradingRunStatus.RUNNING.value,
                GradingRunRecord.lease_owner == worker_id,
                GradingRunRecord.lease_expiry > now,
                GradingRunRecord.released_at.is_(None),
            )
        )
        if run is None:
            raise LeaseLost("result_write_lease_lost")
        if grade_result.grading_run_id != run_id:
            raise ValidationError("result_run_mismatch")
        frozen = session.scalar(
            select(GradingRunSubmissionRecord).where(
                GradingRunSubmissionRecord.grading_run_id == run_id,
                GradingRunSubmissionRecord.submission_revision_id
                == grade_result.submission_revision_id,
                GradingRunSubmissionRecord.student_id == grade_result.student_id,
            )
        )
        if frozen is None:
            raise ValidationError("result_not_in_frozen_submissions")
        question = session.scalar(
            select(AssignmentQuestionRecord).where(
                AssignmentQuestionRecord.id == grade_result.question_id,
                AssignmentQuestionRecord.assignment_id == run.assignment_id,
                AssignmentQuestionRecord.q_id == grade_result.q_id,
            )
        )
        if question is None:
            raise ValidationError("result_question_not_in_assignment")
        if (
            not math.isfinite(grade_result.ai_max_score)
            or grade_result.ai_max_score <= 0
            or not math.isclose(
                grade_result.ai_max_score,
                question.max_score,
                rel_tol=1e-9,
                abs_tol=1e-9,
            )
        ):
            raise ValidationError("result_max_score_mismatch")
        if grade_result.ai_score is not None and (
            not math.isfinite(grade_result.ai_score)
            or grade_result.ai_score < 0
            or grade_result.ai_score > question.max_score
        ):
            raise ValidationError("result_score_out_of_range")
        if (
            grade_result.result_status == education.GradeResultStatus.FAILED.value
            and grade_result.ai_score is not None
        ):
            raise ValidationError("failed_result_must_not_have_score")
        if (
            grade_result.result_status == education.GradeResultStatus.GRADED.value
            and grade_result.ai_score is None
        ):
            raise ValidationError("graded_result_requires_score")
        if (
            grade_result.result_status == education.GradeResultStatus.NEEDS_REVIEW.value
            and grade_result.ai_score is None
        ):
            raise ValidationError("soft_review_result_requires_score")
        existing = session.scalar(
            select(GradeResultRecord).where(
                GradeResultRecord.grading_run_id == run_id,
                GradeResultRecord.submission_revision_id == grade_result.submission_revision_id,
                GradeResultRecord.question_id == grade_result.question_id,
            )
        )
        if existing is not None:
            raise VersionConflict("result_already_written")
        record = GradeResultRecord(
            id=_new_result_id(),
            grading_run_id=run_id,
            submission_revision_id=grade_result.submission_revision_id,
            question_id=grade_result.question_id,
            student_id=grade_result.student_id,
            q_id=grade_result.q_id,
            ai_score=grade_result.ai_score,
            ai_max_score=grade_result.ai_max_score,
            ai_comment=grade_result.ai_comment,
            ai_steps=list(grade_result.ai_steps or []),
            ai_confidence=grade_result.ai_confidence,
            ai_expert_results=list(grade_result.ai_expert_results or []),
            ai_synthesis_method=grade_result.ai_synthesis_method,
            requires_review=grade_result.requires_review,
            review_reasons=list(grade_result.review_reasons or []),
            initial_requires_review=grade_result.initial_requires_review,
            initial_review_reasons=list(
                grade_result.initial_review_reasons or []
            ),
            result_status=grade_result.result_status,
            created_at=now,
            updated_at=now,
        )
        session.add(record)
        session.flush()
        return _result_to_dto(record)


def list_results_for_run(run_id: str) -> list[education.GradeResultDTO]:
    with session_scope() as session:
        records = session.scalars(
            select(GradeResultRecord)
            .where(GradeResultRecord.grading_run_id == run_id)
            .order_by(GradeResultRecord.student_id, GradeResultRecord.q_id)
        ).all()
        reviews = _latest_reviews_by_result(session, [r.id for r in records])
        return [_result_to_dto(r, reviews.get(r.id)) for r in records]


def list_results_for_review(assignment_id: str) -> list[education.GradeResultDTO]:
    """Failed/needs_review results across the latest runs of an assignment."""
    with session_scope() as session:
        records = session.scalars(
            select(GradeResultRecord)
            .join(GradingRunRecord, GradingRunRecord.id == GradeResultRecord.grading_run_id)
            .where(
                GradingRunRecord.assignment_id == assignment_id,
                GradeResultRecord.result_status.in_(
                    education.REVIEW_QUEUE_RESULT_STATUSES
                ),
            )
            .order_by(GradeResultRecord.created_at)
        ).all()
        reviews = _latest_reviews_by_result(session, [r.id for r in records])
        return [_result_to_dto(r, reviews.get(r.id)) for r in records]


def _review_to_dto(record: TeacherReviewRecord) -> education.TeacherReviewDTO:
    return education.TeacherReviewDTO(
        id=record.id,
        grade_result_id=record.grade_result_id,
        teacher_id=record.teacher_id,
        previous_score=record.previous_score,
        previous_comment=record.previous_comment,
        new_score=record.new_score,
        new_comment=record.new_comment,
        comment=record.comment,
        confirmed=record.confirmed,
        created_at=record.created_at,
    )


def _latest_reviews_by_result(session, result_ids: list[str]) -> dict[str, TeacherReviewRecord]:
    if not result_ids:
        return {}
    records = session.scalars(
        select(TeacherReviewRecord)
        .where(TeacherReviewRecord.grade_result_id.in_(result_ids))
        .order_by(
            TeacherReviewRecord.review_sequence,
            TeacherReviewRecord.created_at,
            TeacherReviewRecord.id,
        )
    ).all()
    return {record.grade_result_id: record for record in records}


def _result_to_dto(record: GradeResultRecord,
                   review: TeacherReviewRecord | None = None) -> education.GradeResultDTO:
    review_dto = _review_to_dto(review) if review is not None else None
    return education.GradeResultDTO(
        id=record.id,
        grading_run_id=record.grading_run_id,
        submission_revision_id=record.submission_revision_id,
        question_id=record.question_id,
        student_id=record.student_id,
        q_id=record.q_id,
        ai_score=record.ai_score,
        ai_max_score=record.ai_max_score,
        ai_comment=record.ai_comment or "",
        ai_steps=list(record.ai_steps or []),
        ai_confidence=record.ai_confidence,
        ai_expert_results=list(record.ai_expert_results or []),
        ai_synthesis_method=record.ai_synthesis_method,
        requires_review=record.requires_review,
        review_reasons=list(record.review_reasons or []),
        initial_requires_review=record.initial_requires_review,
        initial_review_reasons=list(record.initial_review_reasons or []),
        result_status=record.result_status,
        effective_score=review.new_score if review is not None else record.ai_score,
        effective_comment=review.new_comment if review is not None else (record.ai_comment or ""),
        teacher_review=review_dto.model_dump() if review_dto is not None else None,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


# ─── teacher review + release ─────────────────────────────────────────────────


def add_teacher_review(
    grade_result_id: str,
    *,
    teacher_id: str,
    new_score: float,
    new_comment: str = "",
    confirm: bool = True,
) -> education.TeacherReviewDTO:
    """Record a teacher adjustment. The latest review for a result supplies the
    display score/comment; the AI original is preserved for audit."""
    with session_scope() as session:
        result = session.get(GradeResultRecord, grade_result_id)
        if result is None:
            raise NotFound("grade_result")
        run = session.scalar(
            select(GradingRunRecord)
            .where(GradingRunRecord.id == result.grading_run_id)
            .with_for_update()
        )
        if run is None or run.teacher_id != teacher_id:
            raise NotFound("grade_result")
        if run.status not in (
            education.GradingRunStatus.COMPLETED.value,
            education.GradingRunStatus.PARTIAL_FAILED.value,
        ):
            raise InvalidTransition("run_not_reviewable")
        if run.released_at is not None:
            raise InvalidTransition("released_run_is_immutable")
        if not math.isfinite(new_score) or new_score < 0 or new_score > result.ai_max_score:
            raise ValidationError("review_score_out_of_range")
        # The run-row lock serializes reviews for every result in this run and
        # release. Capture wall-clock metadata only after acquiring that lock;
        # the per-result sequence below, not the clock, determines recency.
        now = time.time()
        previous_review = session.scalar(
            select(TeacherReviewRecord)
            .where(TeacherReviewRecord.grade_result_id == grade_result_id)
            .order_by(
                TeacherReviewRecord.review_sequence.desc(),
                TeacherReviewRecord.created_at.desc(),
                TeacherReviewRecord.id.desc(),
            )
            .limit(1)
        )
        review_sequence = session.scalar(
            select(func.coalesce(func.max(TeacherReviewRecord.review_sequence), 0) + 1)
            .where(TeacherReviewRecord.grade_result_id == grade_result_id)
        )
        assert review_sequence is not None
        previous_score = previous_review.new_score if previous_review else result.ai_score
        previous_comment = previous_review.new_comment if previous_review else result.ai_comment
        review = TeacherReviewRecord(
            id=_new_review_id(),
            grade_result_id=grade_result_id,
            teacher_id=teacher_id,
            previous_score=previous_score,
            previous_comment=previous_comment,
            new_score=new_score,
            new_comment=new_comment,
            comment=new_comment,
            confirmed=confirm,
            review_sequence=review_sequence,
            created_at=now,
        )
        session.add(review)
        if confirm:
            result.result_status = education.GradeResultStatus.GRADED.value
            result.requires_review = False
            result.review_reasons = []
        else:
            result.result_status = education.GradeResultStatus.NEEDS_REVIEW.value
            result.requires_review = True
            result.review_reasons = ["teacher_edit_pending_confirmation"]
        result.updated_at = now
        session.flush()
        return _review_to_dto(review)


def latest_teacher_review(grade_result_id: str) -> education.TeacherReviewDTO | None:
    with session_scope() as session:
        record = session.scalar(
            select(TeacherReviewRecord)
            .where(TeacherReviewRecord.grade_result_id == grade_result_id)
            .order_by(
                TeacherReviewRecord.review_sequence.desc(),
                TeacherReviewRecord.created_at.desc(),
                TeacherReviewRecord.id.desc(),
            )
            .limit(1)
        )
        if record is None:
            return None
        return _review_to_dto(record)


def has_review_queue_items(run_id: str) -> bool:
    """Return whether this run still has hard failures or soft review signals."""
    with session_scope() as session:
        return session.scalar(
            select(func.count()).select_from(GradeResultRecord).where(
                GradeResultRecord.grading_run_id == run_id,
                GradeResultRecord.result_status.in_(
                    education.REVIEW_QUEUE_RESULT_STATUSES
                ),
            )
        ) > 0


def has_unresolved_failures(run_id: str) -> bool:
    """Return whether this run contains a release-blocking hard failure."""
    with session_scope() as session:
        return session.scalar(
            select(func.count()).select_from(GradeResultRecord).where(
                GradeResultRecord.grading_run_id == run_id,
                GradeResultRecord.result_status.in_(
                    education.NON_SCOREABLE_RESULT_STATUSES
                ),
            )
        ) > 0


def release(run_id: str, *, teacher_id: str) -> education.GradingRunDTO:
    """Mark a completed run released so students see results.

    Blocking: a run with a hard failure cannot be released until the teacher
    supplies a valid score. A scored ``needs_review`` row remains visible in the
    review queue but uses its AI score by default. The release is a single
    conditional UPDATE that requires a terminal status AND no prior release; it
    stamps ``released_at`` so the student read model can gate visibility on it.
    """
    now = time.time()
    with session_scope() as session:
        # Serialize release against teacher-review inserts.  Without this row
        # lock, PostgreSQL could let a review transaction pass the
        # ``released_at`` check and commit immediately after release.
        run = session.scalar(
            select(GradingRunRecord)
            .where(GradingRunRecord.id == run_id)
            .with_for_update()
        )
        if run is None or run.teacher_id != teacher_id:
            raise NotFound("grading_run")
        if run.status not in (education.GradingRunStatus.COMPLETED.value,
                              education.GradingRunStatus.PARTIAL_FAILED.value):
            raise InvalidTransition("run_not_complete")
        unresolved = exists(
            select(GradeResultRecord.id).where(
                GradeResultRecord.grading_run_id == run_id,
                GradeResultRecord.result_status.in_(
                    education.NON_SCOREABLE_RESULT_STATUSES
                ),
            )
        )
        if session.scalar(select(unresolved)):
            raise ResultNotReleasable("unresolved_failures")
        revision_ids, question_ids = _result_matrix_axes(session, run)
        actual_pairs = {
            (revision_id, question_id)
            for revision_id, question_id in session.execute(
                select(
                    GradeResultRecord.submission_revision_id,
                    GradeResultRecord.question_id,
                ).where(GradeResultRecord.grading_run_id == run_id)
            ).all()
        }
        expected_pairs = {
            (revision_id, question_id)
            for revision_id in revision_ids
            for question_id in question_ids
        }
        if not question_ids or actual_pairs != expected_pairs:
            raise ResultNotReleasable(
                "The grading result matrix is incomplete.",
                code="incomplete_result_matrix",
            )
        result = session.execute(
            update(GradingRunRecord)
            .where(
                GradingRunRecord.id == run_id,
                GradingRunRecord.teacher_id == teacher_id,
                GradingRunRecord.status.in_([
                    education.GradingRunStatus.COMPLETED.value,
                    education.GradingRunStatus.PARTIAL_FAILED.value,
                ]),
                GradingRunRecord.released_at.is_(None),
                ~unresolved,
            )
            .values(released_at=now)
        )
        if result.rowcount != 1:
            existing = session.get(GradingRunRecord, run_id)
            assert existing is not None
            if existing.released_at is not None:
                # Idempotent re-release: no error, just return current state.
                return _run_to_dto(existing)
            if session.scalar(select(unresolved)):
                raise ResultNotReleasable("unresolved_failures")
            raise InvalidTransition("run_not_releasable")
        record = session.get(GradingRunRecord, run_id)
        assert record is not None
        return _run_to_dto(record)


def is_released(run_id: str) -> bool:
    with session_scope() as session:
        record = session.get(GradingRunRecord, run_id)
        return record is not None and record.released_at is not None
