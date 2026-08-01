"""Grading-run lifecycle: adapter, lease, recovery, review, release (Task 7).

Drives the grading_adapter and grading_runs service with a fake ``grade_batch``
result so the lifecycle is deterministic without an LLM. Covers:

* idempotent run creation + one active run per assignment;
* frozen submission revisions captured at run creation;
* atomic lease claim, heartbeat by current owner only, expired-lease reclaim;
* late worker rejection via the lease-owner terminal-write predicate;
* result normalization (successful Correction → graded; failure modes →
  needs_review / failed, never a real zero score);
* partial failure; teacher-only review; immutable AI fields;
* release blocking while failures are unresolved;
* student visibility only after release.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.db import (
    assignment_repository,
    course_repository,
    grading_repository,
    submission_repository,
)
from backend.domain import education
from backend.domain.errors import (
    DuplicateActiveRun,
    InvalidTransition,
    LeaseLost,
    ResultNotReleasable,
    ValidationError,
    VersionConflict,
)
from backend.services import grading_adapter, grading_runs


# ─── fixtures: teacher + course + enrolled student + published assignment ─────


@pytest.fixture
def setup_assignment():
    import uuid as _uuid
    from backend.db.session import session_scope
    from backend.db.models import UserRecord

    suffix = _uuid.uuid4().hex[:8]
    teacher_id = f"gr-teacher-{suffix}"
    student_id = f"gr-student-{suffix}"
    with session_scope() as session:
        session.add(UserRecord(id=teacher_id, username=teacher_id, password_hash="h",
                               role="teacher", is_active=True))
        session.add(UserRecord(id=student_id, username=student_id, password_hash="h",
                               role="student", is_active=True))
    course = course_repository.create_course(teacher_id=teacher_id, name="C")
    course_repository.enroll(course_id=course.id, student_id=student_id)
    asg = assignment_repository.create_assignment(
        teacher_id=teacher_id, course_id=course.id, name="A"
    )
    assignment_repository.add_question(
        assignment_id=asg.id, teacher_id=teacher_id, q_id="q1", order_index=0,
        type="short", stem="1+1?", max_score=10.0,
    )
    assignment_repository.publish(assignment_id=asg.id, teacher_id=teacher_id, expected_version=1)
    sub = submission_repository.create_submission(assignment_id=asg.id, student_id=student_id)
    questions = assignment_repository.get_questions_by_assignment(assignment_id=asg.id)
    q = questions[0]
    submission_repository.add_revision(
        submission_id=sub.id, student_id=student_id,
        source=education.SubmissionRevisionSource.ONLINE.value,
        answers=[{"question_id": q.id, "q_id": q.q_id, "type": q.type, "content": "2"}],
    )
    return {"assignment_id": asg.id, "teacher_id": teacher_id, "student_id": student_id,
            "submission_id": sub.id, "question_id": q.id, "q_id": q.q_id}


def test_create_run_is_idempotent_and_blocks_second_active(setup_assignment):
    run = grading_runs.create_run(
        teacher_id=setup_assignment["teacher_id"], assignment_id=setup_assignment["assignment_id"]
    )
    assert run.status == education.GradingRunStatus.QUEUED.value
    assert run.total_submissions == 1
    # A second active run for the same assignment is rejected.
    with pytest.raises(DuplicateActiveRun):
        grading_runs.create_run(
            teacher_id=setup_assignment["teacher_id"], assignment_id=setup_assignment["assignment_id"]
        )


def test_run_freezes_submission_revisions_at_creation(setup_assignment):
    run = grading_runs.create_run(
        teacher_id=setup_assignment["teacher_id"], assignment_id=setup_assignment["assignment_id"]
    )
    frozen = grading_repository.list_frozen_submissions(run_id=run.id)
    assert len(frozen) == 1
    first = frozen[0]
    # The frozen record carries the revision id and the student that owns it.
    assert getattr(first, "submission_revision_id", None) or getattr(first, "id", None)
    assert getattr(first, "student_id", None) == setup_assignment["student_id"]


def test_lease_claim_heartbeat_and_late_worker_rejection(setup_assignment):
    run = grading_runs.create_run(
        teacher_id=setup_assignment["teacher_id"], assignment_id=setup_assignment["assignment_id"]
    )
    claimed = grading_repository.claim_lease(run_id=run.id, worker_id="w1", lease_seconds=60)
    assert claimed.lease_owner == "w1"
    assert claimed.status == education.GradingRunStatus.RUNNING.value
    # Heartbeat by current owner extends.
    assert grading_repository.heartbeat(run_id=run.id, worker_id="w1", lease_seconds=60) is True
    # A late/different worker cannot heartbeat.
    with pytest.raises(LeaseLost):
        grading_repository.heartbeat(run_id=run.id, worker_id="w2", lease_seconds=60)


def test_terminal_write_requires_lease_owner_and_running(setup_assignment):
    run = grading_runs.create_run(
        teacher_id=setup_assignment["teacher_id"], assignment_id=setup_assignment["assignment_id"]
    )
    grading_repository.claim_lease(run_id=run.id, worker_id="w1", lease_seconds=60)
    with pytest.raises(LeaseLost):
        grading_repository.mark_completed(run_id=run.id, worker_id="w2", completed=1, failed=0)
    grading_repository.mark_completed(run_id=run.id, worker_id="w1", completed=1, failed=0)


def test_adapter_maps_successful_correction_to_graded_result(setup_assignment):
    """A successful Correction normalizes to a graded GradeResult (not zero)."""
    from backend.models import Correction, StepScore

    corrections = {
        setup_assignment["student_id"]: [
            Correction(
                q_id="q1", type="short", score=8.0, max_score=10.0, confidence=0.9,
                comment="good", steps=[StepScore(step_no=1, desc="s1", is_correct=True, score=8.0)],
            )
        ]
    }
    results = grading_adapter.normalize_results(
        assignment_id=setup_assignment["assignment_id"],
        run_id="run-x",
        student_ids=[setup_assignment["student_id"]],
        questions=assignment_repository.get_questions_by_assignment(
            assignment_id=setup_assignment["assignment_id"]
        ),
        frozen_revisions={
            setup_assignment["student_id"]: submission_repository.get_current_revision_for_run(
                submission_id=setup_assignment["submission_id"]
            )
        },
        corrections=corrections,
    )
    assert len(results) == 1
    r = results[0]
    assert r.result_status == education.GradeResultStatus.GRADED.value
    assert r.ai_score == 8.0
    assert r.requires_review is False


def test_adapter_marks_low_confidence_as_needs_review_not_zero(setup_assignment):
    """confidence == 0 (skill failure) must surface as needs_review, never a real 0."""
    from backend.models import Correction

    corrections = {
        setup_assignment["student_id"]: [
            Correction(
                q_id="q1", type="short", score=0.0, max_score=10.0, confidence=0.0,
                comment="all_failed", steps=[], synthesis_method="all_failed",
            )
        ]
    }
    results = grading_adapter.normalize_results(
        assignment_id=setup_assignment["assignment_id"], run_id="run-x",
        student_ids=[setup_assignment["student_id"]],
        questions=assignment_repository.get_questions_by_assignment(
            assignment_id=setup_assignment["assignment_id"]
        ),
        frozen_revisions={
            setup_assignment["student_id"]: submission_repository.get_current_revision_for_run(
                submission_id=setup_assignment["submission_id"]
            )
        },
        corrections=corrections,
    )
    assert results[0].result_status == education.GradeResultStatus.NEEDS_REVIEW.value
    assert results[0].requires_review is True
    assert results[0].review_reason is not None


def test_adapter_preserves_human_review_signal_and_question_max(setup_assignment):
    from backend.models import Correction

    correction = Correction(
        q_id="q1", type="short", score=8.0, max_score=100.0, confidence=0.9,
        comment="check", steps=[], requires_human_review=True,
        review_reasons=["high_indecisiveness"],
    )
    result = grading_adapter.correction_to_result(
        run_id="run-x", revision_id="rev-x",
        question=assignment_repository.get_questions_by_assignment(
            assignment_id=setup_assignment["assignment_id"]
        )[0],
        student_id=setup_assignment["student_id"], correction=correction,
    )
    assert result.result_status == education.GradeResultStatus.NEEDS_REVIEW.value
    assert result.ai_score is None
    assert result.ai_max_score == 10.0
    assert result.review_reason == "high_indecisiveness"


def test_persisted_ai_fields_are_immutable_after_review(setup_assignment):
    run = grading_runs.create_run(
        teacher_id=setup_assignment["teacher_id"], assignment_id=setup_assignment["assignment_id"]
    )
    grading_runs.persist_results(
        run_id=run.id, worker_id=setup_assignment["teacher_id"],
        results=[
            education.GradeResultDTO(
                id="", grading_run_id=run.id,
                submission_revision_id=grading_repository.list_frozen_submissions(run_id=run.id)[0].id,
                question_id=setup_assignment["question_id"], student_id=setup_assignment["student_id"],
                q_id="q1", ai_score=7.0, ai_max_score=10.0, ai_comment="ai",
                result_status=education.GradeResultStatus.NEEDS_REVIEW.value,
                requires_review=True, review_reason="low_confidence",
                created_at=0, updated_at=0,
            )
        ],
    )
    grading_repository.claim_lease(run_id=run.id, worker_id=setup_assignment["teacher_id"], lease_seconds=60)
    grading_repository.mark_completed(run_id=run.id, worker_id=setup_assignment["teacher_id"], completed=1, failed=0)

    # Teacher review adjusts the display score but the AI original stays immutable.
    results = grading_repository.list_results_for_run(run_id=run.id)
    rid = results[0].id
    review = grading_repository.add_teacher_review(
        grade_result_id=rid, teacher_id=setup_assignment["teacher_id"], new_score=9.0, new_comment="teacher override"
    )
    assert review.new_score == 9.0
    after = grading_repository.list_results_for_run(run_id=run.id)[0]
    assert after.ai_score == 7.0  # immutable AI original
    assert after.initial_requires_review is True
    assert after.initial_review_reason == "low_confidence"
    assert after.requires_review is False
    assert after.review_reason is None
    latest = grading_repository.latest_teacher_review(grade_result_id=rid)
    assert latest is not None and latest.new_score == 9.0


def test_later_serialized_review_wins_when_clock_moves_backward(
    setup_assignment, monkeypatch,
):
    """The later committed decision wins even if its wall clock is earlier.

    Each call commits separately. Production PostgreSQL serializes concurrent
    callers on the run row, so commit order receives sequences 1 then 2.
    """
    from sqlalchemy import select

    from backend.db.models import GradeResultRecord, TeacherReviewRecord
    from backend.db.session import session_scope

    run = grading_runs.create_run(
        teacher_id=setup_assignment["teacher_id"],
        assignment_id=setup_assignment["assignment_id"],
    )
    grading_runs.persist_results(
        run_id=run.id,
        worker_id=setup_assignment["teacher_id"],
        results=[
            education.GradeResultDTO(
                id="",
                grading_run_id=run.id,
                submission_revision_id=(
                    grading_repository.list_frozen_submissions(run_id=run.id)[0].id
                ),
                question_id=setup_assignment["question_id"],
                student_id=setup_assignment["student_id"],
                q_id="q1",
                ai_score=None,
                ai_max_score=10.0,
                requires_review=True,
                review_reason="low_confidence",
                result_status=education.GradeResultStatus.NEEDS_REVIEW.value,
                created_at=0,
                updated_at=0,
            )
        ],
    )
    grading_repository.mark_completed(
        run.id,
        worker_id=setup_assignment["teacher_id"],
        completed=1,
        failed=0,
    )
    result_id = grading_repository.list_results_for_run(run_id=run.id)[0].id

    timestamps = iter((200.0, 100.0))
    monkeypatch.setattr(grading_repository.time, "time", lambda: next(timestamps))
    first = grading_repository.add_teacher_review(
        grade_result_id=result_id,
        teacher_id=setup_assignment["teacher_id"],
        new_score=6.0,
        new_comment="first committed decision",
        confirm=False,
    )
    second = grading_repository.add_teacher_review(
        grade_result_id=result_id,
        teacher_id=setup_assignment["teacher_id"],
        new_score=9.0,
        new_comment="later committed decision",
        confirm=True,
    )

    assert first.created_at == 200.0
    assert second.created_at == 100.0
    latest = grading_repository.latest_teacher_review(grade_result_id=result_id)
    assert latest is not None
    assert latest.id == second.id
    assert latest.new_score == 9.0
    effective = grading_repository.list_results_for_run(run_id=run.id)[0]
    assert effective.effective_score == 9.0
    assert effective.initial_requires_review is True
    assert effective.initial_review_reason == "low_confidence"

    with session_scope() as session:
        rows = session.scalars(
            select(TeacherReviewRecord)
            .where(TeacherReviewRecord.grade_result_id == result_id)
            .order_by(TeacherReviewRecord.review_sequence)
        ).all()
        result = session.get(GradeResultRecord, result_id)
        assert result is not None
        assert [(row.id, row.review_sequence) for row in rows] == [
            (first.id, 1),
            (second.id, 2),
        ]
        assert result.initial_requires_review is True
        assert result.initial_review_reason == "low_confidence"


def test_release_blocked_while_failures_unresolved(setup_assignment):
    run = grading_runs.create_run(
        teacher_id=setup_assignment["teacher_id"], assignment_id=setup_assignment["assignment_id"]
    )
    grading_runs.persist_results(
        run_id=run.id, worker_id=setup_assignment["teacher_id"],
        results=[
            education.GradeResultDTO(
                id="", grading_run_id=run.id,
                submission_revision_id=grading_repository.list_frozen_submissions(run_id=run.id)[0].id,
                question_id=setup_assignment["question_id"], student_id=setup_assignment["student_id"],
                q_id="q1", ai_score=0.0, ai_max_score=10.0,
                result_status=education.GradeResultStatus.FAILED.value,
                requires_review=True, review_reason="exception",
                created_at=0, updated_at=0,
            )
        ],
    )
    grading_repository.claim_lease(run_id=run.id, worker_id=setup_assignment["teacher_id"], lease_seconds=60)
    grading_repository.mark_completed(run_id=run.id, worker_id=setup_assignment["teacher_id"], completed=0, failed=1)
    with pytest.raises(ResultNotReleasable):
        grading_runs.release(teacher_id=setup_assignment["teacher_id"], run_id=run.id)


def test_release_succeeds_after_review_resolves_failure(setup_assignment):
    run = grading_runs.create_run(
        teacher_id=setup_assignment["teacher_id"], assignment_id=setup_assignment["assignment_id"]
    )
    revision_id = grading_repository.list_frozen_submissions(run_id=run.id)[0].id
    grading_runs.persist_results(
        run_id=run.id, worker_id=setup_assignment["teacher_id"],
        results=[
            education.GradeResultDTO(
                id="", grading_run_id=run.id, submission_revision_id=revision_id,
                question_id=setup_assignment["question_id"], student_id=setup_assignment["student_id"],
                q_id="q1", ai_score=0.0, ai_max_score=10.0,
                result_status=education.GradeResultStatus.NEEDS_REVIEW.value,
                requires_review=True, review_reason="low_confidence",
                created_at=0, updated_at=0,
            )
        ],
    )
    grading_repository.claim_lease(run_id=run.id, worker_id=setup_assignment["teacher_id"], lease_seconds=60)
    grading_repository.mark_completed(run_id=run.id, worker_id=setup_assignment["teacher_id"], completed=1, failed=0)
    # Still blocked while the needs_review result is unresolved.
    with pytest.raises(ResultNotReleasable):
        grading_runs.release(teacher_id=setup_assignment["teacher_id"], run_id=run.id)
    # Resolve via teacher review (which sets a display score).
    rid = grading_repository.list_results_for_run(run_id=run.id)[0].id
    grading_repository.add_teacher_review(
        grade_result_id=rid, teacher_id=setup_assignment["teacher_id"], new_score=6.0, new_comment="ok"
    )
    # Mark the result resolved (graded) so it leaves the review queue.
    grading_runs.resolve_review(grade_result_id=rid, teacher_id=setup_assignment["teacher_id"])
    released = grading_runs.release(teacher_id=setup_assignment["teacher_id"], run_id=run.id)
    assert released.released_at is not None


def test_student_visibility_only_after_release(setup_assignment):
    run = grading_runs.create_run(
        teacher_id=setup_assignment["teacher_id"], assignment_id=setup_assignment["assignment_id"]
    )
    revision_id = grading_repository.list_frozen_submissions(run_id=run.id)[0].id
    grading_runs.persist_results(
        run_id=run.id, worker_id=setup_assignment["teacher_id"],
        results=[
            education.GradeResultDTO(
                id="", grading_run_id=run.id, submission_revision_id=revision_id,
                question_id=setup_assignment["question_id"], student_id=setup_assignment["student_id"],
                q_id="q1", ai_score=8.0, ai_max_score=10.0,
                result_status=education.GradeResultStatus.GRADED.value,
                created_at=0, updated_at=0,
            )
        ],
    )
    grading_repository.claim_lease(run_id=run.id, worker_id=setup_assignment["teacher_id"], lease_seconds=60)
    grading_repository.mark_completed(run_id=run.id, worker_id=setup_assignment["teacher_id"], completed=1, failed=0)
    # Before release: student sees nothing.
    assert grading_runs.student_results(student_id=setup_assignment["student_id"], assignment_id=setup_assignment["assignment_id"]) == []
    grading_runs.release(teacher_id=setup_assignment["teacher_id"], run_id=run.id)
    # After release: student sees the released result.
    seen = grading_runs.student_results(student_id=setup_assignment["student_id"], assignment_id=setup_assignment["assignment_id"])
    assert len(seen) == 1
    assert seen[0].ai_score == 8.0
    # A later submission is input to a future run. It must not hide the result
    # already published from this run.
    submission_repository.add_revision(
        submission_id=setup_assignment["submission_id"],
        student_id=setup_assignment["student_id"],
        source=education.SubmissionRevisionSource.ONLINE.value,
        answers=[{
            "question_id": setup_assignment["question_id"],
            "q_id": setup_assignment["q_id"],
            "type": "short",
            "content": "updated after release",
        }],
    )
    still_visible = grading_runs.student_results(
        student_id=setup_assignment["student_id"],
        assignment_id=setup_assignment["assignment_id"],
    )
    assert len(still_visible) == 1
    assert still_visible[0].id == seen[0].id
    with pytest.raises(InvalidTransition):
        grading_repository.add_teacher_review(
            grade_result_id=seen[0].id,
            teacher_id=setup_assignment["teacher_id"],
            new_score=9.0,
            new_comment="too late",
        )


def test_expired_lease_recovery_lets_new_worker_claim(setup_assignment):
    run = grading_runs.create_run(
        teacher_id=setup_assignment["teacher_id"], assignment_id=setup_assignment["assignment_id"]
    )
    grading_repository.claim_lease(run_id=run.id, worker_id="w-dead", lease_seconds=1)
    import time as _time
    from backend.db.session import session_scope
    from backend.db.models import GradingRunRecord
    with session_scope() as session:
        row = session.get(GradingRunRecord, run.id)
        assert row is not None
        row.lease_expiry = _time.time() - 10
    assert run.id in grading_runs.poll_queued_runs()
    reclaimed = grading_repository.claim_lease(run_id=run.id, worker_id="w-new", lease_seconds=60)
    assert reclaimed.lease_owner == "w-new"


def test_teacher_review_resolves_failure_and_validates_score(setup_assignment):
    run = grading_runs.create_run(
        teacher_id=setup_assignment["teacher_id"], assignment_id=setup_assignment["assignment_id"]
    )
    grading_repository.claim_lease(run_id=run.id, worker_id="w1", lease_seconds=60)
    revision = submission_repository.get_current_revision_for_run(
        submission_id=setup_assignment["submission_id"]
    )
    result = grading_adapter.correction_to_result(
        run_id=run.id,
        revision_id=revision.id,
        question=assignment_repository.get_questions_by_assignment(
            assignment_id=setup_assignment["assignment_id"]
        )[0],
        student_id=setup_assignment["student_id"],
        correction=None,
    )
    persisted = grading_repository.upsert_result(run.id, worker_id="w1", grade_result=result)
    # Review is only allowed on a terminal, unreleased run — close the run first
    # so the review invariant (completed/partial_failed, not released) is met.
    grading_repository.mark_completed(run_id=run.id, worker_id="w1", completed=1, failed=0)
    review = grading_runs.add_teacher_review(
        grade_result_id=persisted.id,
        teacher_id=setup_assignment["teacher_id"],
        new_score=7,
        new_comment="manual",
    )
    assert review.new_score == 7
    resolved = grading_repository.list_results_for_run(run.id)[0]
    assert resolved.result_status == education.GradeResultStatus.GRADED.value
    assert resolved.requires_review is False
    assert resolved.ai_score is None
    with pytest.raises(ValidationError):
        grading_runs.add_teacher_review(
            grade_result_id=persisted.id,
            teacher_id=setup_assignment["teacher_id"],
            new_score=11,
        )


def test_teacher_review_rejected_while_run_still_running(setup_assignment):
    """Review must not land while a run is still running: grading could still
    mutate results, so resolving would race the worker."""
    run = grading_runs.create_run(
        teacher_id=setup_assignment["teacher_id"], assignment_id=setup_assignment["assignment_id"]
    )
    grading_repository.claim_lease(run_id=run.id, worker_id="w1", lease_seconds=60)
    revision = submission_repository.get_current_revision_for_run(
        submission_id=setup_assignment["submission_id"]
    )
    result = grading_adapter.correction_to_result(
        run_id=run.id,
        revision_id=revision.id,
        question=assignment_repository.get_questions_by_assignment(
            assignment_id=setup_assignment["assignment_id"]
        )[0],
        student_id=setup_assignment["student_id"],
        correction=None,
    )
    persisted = grading_repository.upsert_result(run.id, worker_id="w1", grade_result=result)
    from backend.domain.errors import InvalidTransition
    with pytest.raises(InvalidTransition):
        grading_runs.add_teacher_review(
            grade_result_id=persisted.id,
            teacher_id=setup_assignment["teacher_id"],
            new_score=7,
        )


def test_result_write_requires_live_worker_lease_and_frozen_membership(setup_assignment):
    run = grading_runs.create_run(
        teacher_id=setup_assignment["teacher_id"], assignment_id=setup_assignment["assignment_id"]
    )
    grading_repository.claim_lease(run_id=run.id, worker_id="w1", lease_seconds=60)
    revision = submission_repository.get_current_revision_for_run(
        submission_id=setup_assignment["submission_id"]
    )
    question = assignment_repository.get_questions_by_assignment(
        assignment_id=setup_assignment["assignment_id"]
    )[0]
    result = grading_adapter.correction_to_result(
        run_id=run.id, revision_id=revision.id, question=question,
        student_id=setup_assignment["student_id"], correction=None,
    )
    with pytest.raises(LeaseLost):
        grading_repository.upsert_result(run.id, worker_id="w2", grade_result=result)
    grading_repository.upsert_result(run.id, worker_id="w1", grade_result=result)
    with pytest.raises(VersionConflict):
        grading_repository.upsert_result(run.id, worker_id="w1", grade_result=result)

    with pytest.raises(LeaseLost):
        grading_repository.upsert_result(run.id, worker_id="w2", grade_result=result)


def test_release_rechecks_unresolved_results_in_same_transaction(setup_assignment):
    run = grading_runs.create_run(
        teacher_id=setup_assignment["teacher_id"], assignment_id=setup_assignment["assignment_id"]
    )
    grading_repository.claim_lease(run_id=run.id, worker_id="w1", lease_seconds=60)
    revision = submission_repository.get_current_revision_for_run(
        submission_id=setup_assignment["submission_id"]
    )
    question = assignment_repository.get_questions_by_assignment(
        assignment_id=setup_assignment["assignment_id"]
    )[0]
    result = grading_adapter.correction_to_result(
        run_id=run.id, revision_id=revision.id, question=question,
        student_id=setup_assignment["student_id"], correction=None,
    )
    grading_repository.upsert_result(run.id, worker_id="w1", grade_result=result)
    grading_repository.mark_completed(run.id, worker_id="w1", completed=1, failed=1)
    with pytest.raises(ResultNotReleasable):
        grading_runs.release(teacher_id=setup_assignment["teacher_id"], run_id=run.id)


def test_release_blocks_when_expected_result_matrix_is_incomplete(setup_assignment):
    run = grading_runs.create_run(
        teacher_id=setup_assignment["teacher_id"],
        assignment_id=setup_assignment["assignment_id"],
    )
    grading_repository.claim_lease(
        run_id=run.id, worker_id="w-incomplete", lease_seconds=60,
    )
    grading_repository.mark_completed(
        run_id=run.id, worker_id="w-incomplete", completed=1, failed=0,
    )

    with pytest.raises(ResultNotReleasable) as exc_info:
        grading_runs.release(
            teacher_id=setup_assignment["teacher_id"], run_id=run.id,
        )
    assert exc_info.value.code == "incomplete_result_matrix"


def test_reclaimed_worker_recomputes_counters_from_existing_results(
    setup_assignment, monkeypatch,
):
    import time as _time

    from backend.db.models import GradingRunRecord
    from backend.db.session import session_scope

    run = grading_runs.create_run(
        teacher_id=setup_assignment["teacher_id"],
        assignment_id=setup_assignment["assignment_id"],
    )
    grading_repository.claim_lease(
        run_id=run.id, worker_id="w-dead", lease_seconds=60,
    )
    revision = submission_repository.get_current_revision_for_run(
        submission_id=setup_assignment["submission_id"]
    )
    question = assignment_repository.get_questions_by_assignment(
        assignment_id=setup_assignment["assignment_id"]
    )[0]
    result = grading_adapter.correction_to_result(
        run_id=run.id, revision_id=revision.id, question=question,
        student_id=setup_assignment["student_id"],
        correction=__import__("backend.models", fromlist=["Correction"]).Correction(
            q_id=question.q_id, type=question.type, score=8.0,
            max_score=question.max_score, confidence=1, comment="ok", steps=[],
        ),
    )
    grading_repository.upsert_result(
        run.id, worker_id="w-dead", grade_result=result,
    )
    with session_scope() as session:
        record = session.get(GradingRunRecord, run.id)
        assert record is not None
        record.lease_expiry = _time.time() - 1

    async def replay_existing_result(**_kwargs):
        return [grading_adapter.AdapterOutcome(
            student_id=setup_assignment["student_id"], results=[result],
        )]

    class _Registry:
        @staticmethod
        def count():
            return 1

    monkeypatch.setattr(grading_adapter, "run_grading", replay_existing_result)
    asyncio.run(grading_runs.process_run(
        run_id=run.id, worker_id="w-reclaimed", registry=_Registry(),
    ))

    recovered = grading_repository.get_run(run.id)
    assert recovered.status == education.GradingRunStatus.COMPLETED.value
    assert recovered.completed_submissions == 1
    assert recovered.failed_submissions == 0


def test_explicit_e2e_provider_does_not_require_persisted_provider_config(
    setup_assignment, monkeypatch,
):
    run = grading_runs.create_run(
        teacher_id=setup_assignment["teacher_id"],
        assignment_id=setup_assignment["assignment_id"],
    )

    class _EmptyRegistry:
        @staticmethod
        def count():
            return 0

    monkeypatch.setattr(grading_runs.settings, "e2e_fake_provider", True)
    monkeypatch.setattr(grading_runs.settings, "e2e_fail_qid", "")
    asyncio.run(grading_runs.process_run(
        run_id=run.id, worker_id="w-e2e", registry=_EmptyRegistry(),
    ))

    completed = grading_repository.get_run(run.id)
    assert completed.status == education.GradingRunStatus.COMPLETED.value
    assert completed.completed_submissions == 1
    assert completed.failed_submissions == 0


def test_adapter_marks_missing_student_batch_output_for_review(setup_assignment, monkeypatch):
    async def empty_batch(**_kwargs):
        return []

    monkeypatch.setattr(grading_adapter, "grade_batch", empty_batch)
    revision = submission_repository.get_current_revision_for_run(
        submission_id=setup_assignment["submission_id"]
    )
    questions = assignment_repository.get_questions_by_assignment(
        assignment_id=setup_assignment["assignment_id"]
    )
    outcomes = asyncio.run(grading_adapter.run_grading(
        run_id="run-missing", assignment_id=setup_assignment["assignment_id"],
        teacher_id=setup_assignment["teacher_id"], questions=questions,
        frozen_revisions=[(revision, setup_assignment["student_id"])],
        registry=None,
    ))
    assert len(outcomes) == 1
    assert outcomes[0].results[0].result_status == education.GradeResultStatus.NEEDS_REVIEW.value


def test_student_results_use_only_latest_released_run(setup_assignment):
    teacher_id = setup_assignment["teacher_id"]
    assignment_id = setup_assignment["assignment_id"]
    revision = submission_repository.get_current_revision_for_run(
        submission_id=setup_assignment["submission_id"]
    )
    question = assignment_repository.get_questions_by_assignment(assignment_id=assignment_id)[0]
    for score in (4.0, 9.0):
        run = grading_runs.create_run(teacher_id=teacher_id, assignment_id=assignment_id)
        grading_repository.claim_lease(run_id=run.id, worker_id=f"w-{score}", lease_seconds=60)
        result = grading_adapter.correction_to_result(
            run_id=run.id, revision_id=revision.id, question=question,
            student_id=setup_assignment["student_id"],
            correction=__import__("backend.models", fromlist=["Correction"]).Correction(
                q_id="q1", type="short", score=score, max_score=10, confidence=1,
                comment=str(score), steps=[],
            ),
        )
        grading_repository.upsert_result(run.id, worker_id=f"w-{score}", grade_result=result)
        grading_repository.mark_completed(run.id, worker_id=f"w-{score}", completed=1, failed=0)
        grading_runs.release(teacher_id=teacher_id, run_id=run.id)
    rows = grading_runs.student_results(
        student_id=setup_assignment["student_id"], assignment_id=assignment_id
    )
    assert len(rows) == 1
    assert rows[0].ai_score == 9.0
