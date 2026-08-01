"""Normalized education repository behavior (Task 2).

These tests pin the transactional, predicate-driven contract of the four focused
repositories before any service/API code is written:

* course ownership and student-only enrollment, with authorization predicates
  evaluated in SQL (not in Python after a full row load);
* draft assignment creation, ordered questions, publish validation, and the
  immutability of a published question set;
* optimistic locking: a version-conflicting update is a 409, never a silent
  last-writer-wins overwrite, and the conditional UPDATE touches exactly one row;
* immutable submission revisions and per-revision/per-question answer uniqueness;
* role-scoped reads (owner / enrolled) and 404-without-leakage for other actors.

The tests seed raw ``UserRecord`` rows rather than going through auth code, so
they stay valid while Task 3 removes the legacy ``course_ids`` compatibility
mirror from the identity layer.
"""
from __future__ import annotations

import time
import uuid

import pytest
from sqlalchemy import inspect, select

from backend.db import (
    assignment_repository,
    course_repository,
    grading_repository,
    submission_repository,
)
from backend.db.base import Base
from backend.db.models import (
    AssignmentRecord,
    CourseEnrollmentRecord,
    CourseRecord,
    GradingRunRecord,
    SubmissionRecord,
    UserRecord,
)
from backend.db.session import session_scope
from backend.domain import education
from backend.domain.errors import (
    AssignmentClosed,
    Forbidden,
    InvalidTransition,
    NotFound,
    VersionConflict,
)


# ─── fixtures ─────────────────────────────────────────────────────────────────


def _seed_user(*, role: str, user_id: str | None = None) -> str:
    uid = user_id or f"u_{role}_{uuid.uuid4().hex[:10]}"
    with session_scope() as session:
        session.add(
            UserRecord(
                id=uid,
                username=uid,
                email=None,
                role=role,
                password_hash="x",
                is_active=True,
            )
        )
        session.flush()
    return uid


@pytest.fixture
def teacher() -> str:
    return _seed_user(role="teacher")


@pytest.fixture
def other_teacher() -> str:
    return _seed_user(role="teacher")


@pytest.fixture
def student() -> str:
    return _seed_user(role="student")


@pytest.fixture
def course(teacher: str) -> education.CourseDTO:
    return course_repository.create_course(teacher_id=teacher, name="Algebra", code="MATH101")


# ─── course ownership ──────────────────────────────────────────────────────────


def test_create_and_read_course_returns_dto(teacher):
    dto = course_repository.create_course(teacher_id=teacher, name="Physics", code="PHY")
    assert dto.teacher_id == teacher
    assert dto.name == "Physics"

    fetched = course_repository.get_course(dto.id, actor_id=teacher)
    assert fetched.id == dto.id
    assert fetched.student_ids == []


def test_get_course_by_other_teacher_is_not_found(other_teacher, course):
    # Authorization predicate lives in SQL: a non-owner reads no row, surfaced as
    # NotFound rather than a payload leak with a 403 body.
    with pytest.raises(NotFound):
        course_repository.get_course(course.id, actor_id=other_teacher)


def test_list_courses_returns_only_owned(teacher, other_teacher, course):
    course_repository.create_course(teacher_id=other_teacher, name="Other", code="O")
    owned = course_repository.list_courses(actor_id=teacher)
    assert [c.id for c in owned] == [course.id]


def test_delete_course_is_owner_scoped(teacher, other_teacher, course):
    with pytest.raises(NotFound):
        course_repository.delete_course(course.id, actor_id=other_teacher)
    course_repository.delete_course(course.id, actor_id=teacher)
    with pytest.raises(NotFound):
        course_repository.get_course(course.id, actor_id=teacher)


def test_delete_course_cascades_enrollments(teacher, course, student):
    course_repository.enroll(course_id=course.id, student_id=student)
    course_repository.delete_course(course.id, actor_id=teacher)
    with session_scope() as session:
        rows = session.scalars(
            select(CourseEnrollmentRecord).where(CourseEnrollmentRecord.course_id == course.id)
        ).all()
        assert rows == []


# ─── enrollment: student role only ─────────────────────────────────────────────


def test_enroll_persists_membership_and_lists_students(teacher, course, student):
    course_repository.enroll(course_id=course.id, student_id=student)
    fetched = course_repository.get_course(course.id, actor_id=teacher)
    assert fetched.student_ids == [student]


def test_enroll_rejects_teacher_role(teacher, course, other_teacher):
    # Only students may be enrolled; a teacher id is a validation error, not a
    # silently-accepted membership that would later confuse scoping.
    with pytest.raises(Exception):
        course_repository.enroll(course_id=course.id, student_id=other_teacher)


def test_enroll_is_owner_scoped(course, student, other_teacher):
    with pytest.raises(NotFound):
        course_repository.enroll(course_id=course.id, student_id=student, actor_id=other_teacher)


def test_enroll_is_idempotent(teacher, course, student):
    course_repository.enroll(course_id=course.id, student_id=student)
    # Re-enrolling the same student is a no-op rather than a uniqueness violation.
    course_repository.enroll(course_id=course.id, student_id=student)
    fetched = course_repository.get_course(course.id, actor_id=teacher)
    assert fetched.student_ids == [student]


def test_is_enrolled_is_the_authorization_source(course, student, other_teacher):
    assert not course_repository.is_enrolled(course_id=course.id, student_id=student)
    course_repository.enroll(course_id=course.id, student_id=student)
    assert course_repository.is_enrolled(course_id=course.id, student_id=student)
    assert not course_repository.is_enrolled(course_id=course.id, student_id=other_teacher)


# ─── assignment + questions ────────────────────────────────────────────────────


@pytest.fixture
def assignment(teacher, course) -> education.AssignmentDTO:
    return assignment_repository.create_assignment(
        teacher_id=teacher, course_id=course.id, name="Homework 1"
    )


def test_create_assignment_is_draft(assignment):
    assert assignment.status == education.AssignmentStatus.DRAFT.value
    assert assignment.version == 1
    assert assignment.question_count == 0


def test_get_assignment_is_owner_scoped(assignment, other_teacher):
    with pytest.raises(NotFound):
        assignment_repository.get_assignment(assignment.id, actor_id=other_teacher)


def test_list_assignments_for_course_is_owner_scoped(assignment, course, other_teacher):
    rows = assignment_repository.list_assignments(course_id=course.id, actor_id=other_teacher)
    assert rows == []


def test_add_questions_preserves_order(teacher, assignment):
    q1 = assignment_repository.add_question(
        assignment_id=assignment.id,
        teacher_id=teacher,
        q_id="q1",
        order_index=0,
        type="short",
        stem="What is 1+1?",
    )
    q2 = assignment_repository.add_question(
        assignment_id=assignment.id,
        teacher_id=teacher,
        q_id="q2",
        order_index=1,
        type="short",
        stem="What is 2+2?",
    )
    assert [q.q_id for q in assignment_repository.list_questions(assignment.id, teacher_id=teacher)] == ["q1", "q2"]
    assert q1.version == 1 and q2.version == 1


def test_add_question_is_owner_scoped(assignment, other_teacher):
    with pytest.raises(NotFound):
        assignment_repository.add_question(
            assignment_id=assignment.id,
            teacher_id=other_teacher,
            q_id="x",
            order_index=0,
            type="short",
            stem="?",
        )


def test_publish_requires_at_least_one_question(teacher, assignment):
    # Publishing an empty assignment is an invalid transition, not a silent 200.
    with pytest.raises(InvalidTransition):
        assignment_repository.publish(assignment.id, teacher_id=teacher, expected_version=1)


def test_publish_after_questions_succeeds(teacher, assignment):
    assignment_repository.add_question(
        assignment_id=assignment.id, teacher_id=teacher, q_id="q1", order_index=0,
        type="short", stem="?",
    )
    published = assignment_repository.publish(assignment.id, teacher_id=teacher, expected_version=1)
    assert published.status == education.AssignmentStatus.PUBLISHED.value
    assert published.version == 2
    assert published.published_at is not None


def test_publish_with_stale_version_is_conflict(teacher, assignment):
    assignment_repository.add_question(
        assignment_id=assignment.id, teacher_id=teacher, q_id="q1", order_index=0,
        type="short", stem="?",
    )
    # Someone else (or a stale client) published first, bumping version to 2.
    assignment_repository.publish(assignment.id, teacher_id=teacher, expected_version=1)
    with pytest.raises(VersionConflict):
        assignment_repository.publish(assignment.id, teacher_id=teacher, expected_version=1)


def test_published_questions_are_immutable(teacher, assignment):
    assignment_repository.add_question(
        assignment_id=assignment.id, teacher_id=teacher, q_id="q1", order_index=0,
        type="short", stem="?",
    )
    assignment_repository.publish(assignment.id, teacher_id=teacher, expected_version=1)
    # After publish the question set is frozen: editing is an invalid transition.
    with pytest.raises(InvalidTransition):
        assignment_repository.update_question(
            assignment_id=assignment.id, teacher_id=teacher, q_id="q1", expected_version=1, stem="changed",
        )


def test_optimistic_update_touches_exactly_one_row(teacher, assignment, monkeypatch):
    # The conditional UPDATE must match the expected version AND teacher in its
    # WHERE clause; rowcount != 1 surfaces as VersionConflict. Patching the row's
    # version to a different value in a separate session simulates a concurrent
    # writer between read and update.
    dto = assignment_repository.get_assignment(assignment.id, actor_id=teacher)
    with session_scope() as session:
        row = session.get(AssignmentRecord, assignment.id)
        assert row is not None
        row.version = dto.version + 5
    with pytest.raises(VersionConflict):
        assignment_repository.rename_assignment(
            assignment.id, teacher_id=teacher, expected_version=dto.version, name="renamed"
        )


def test_optimistic_update_by_wrong_owner_is_not_found(assignment, other_teacher):
    with pytest.raises(NotFound):
        assignment_repository.rename_assignment(
            assignment.id, teacher_id=other_teacher, expected_version=1, name="hijack"
        )


def test_optimistic_update_owner_scoped_predicate_in_sql(teacher, other_teacher, assignment):
    # A wrong-owner update with a "correct" version must NOT match the row even if
    # the version number were right; the teacher_id predicate is in the UPDATE
    # WHERE, so it cannot mutate another teacher's assignment.
    captured = _capture_statements()
    with captured:
        try:
            assignment_repository.rename_assignment(
                assignment.id, teacher_id=other_teacher, expected_version=1, name="hijack"
            )
        except NotFound:
            pass
    assert any(
        "UPDATE" in stmt.upper() and "assignments" in stmt.lower()
        and "teacher_id" in stmt.lower() and "version" in stmt.lower()
        for stmt in captured.statements
    ), "assignment updates must carry teacher_id + version predicates in SQL"
    # The owner's row is untouched.
    still = assignment_repository.get_assignment(assignment.id, actor_id=teacher)
    assert still.name == "Homework 1"


# ─── submissions: immutable revisions + answer uniqueness ──────────────────────


@pytest.fixture
def published_assignment(teacher, course, student):
    assignment = assignment_repository.create_assignment(
        teacher_id=teacher, course_id=course.id, name="Quiz"
    )
    course_repository.enroll(course_id=course.id, student_id=student)
    assignment_repository.add_question(
        assignment_id=assignment.id, teacher_id=teacher, q_id="q1", order_index=0,
        type="short", stem="?", max_score=10.0,
    )
    assignment_repository.publish(assignment.id, teacher_id=teacher, expected_version=1)
    return assignment


def test_create_submission_for_enrolled_student(published_assignment, student):
    sub = submission_repository.create_submission(
        assignment_id=published_assignment.id, student_id=student
    )
    assert sub.assignment_id == published_assignment.id
    assert sub.student_id == student
    assert sub.current_revision_number is None


@pytest.mark.parametrize(
    "status",
    [
        education.AssignmentStatus.DRAFT.value,
        education.AssignmentStatus.READY.value,
        education.AssignmentStatus.CLOSED.value,
        education.AssignmentStatus.ARCHIVED.value,
    ],
)
def test_create_submission_requires_published_assignment(
    assignment, course, student, status
):
    course_repository.enroll(course_id=course.id, student_id=student)
    with session_scope() as session:
        row = session.get(AssignmentRecord, assignment.id)
        assert row is not None
        row.status = status

    with pytest.raises(AssignmentClosed):
        submission_repository.create_submission(
            assignment_id=assignment.id, student_id=student
        )


def test_create_submission_rejects_unenrolled_student(published_assignment, other_teacher):
    # other_teacher is a teacher and not enrolled; the assignment is published so
    # the only remaining gate is enrollment, which must reject the writer.
    with pytest.raises(Forbidden):
        submission_repository.create_submission(
            assignment_id=published_assignment.id, student_id=other_teacher
        )


def test_revision_is_immutable_and_numbered(published_assignment, student, teacher):
    questions = assignment_repository.list_questions(published_assignment.id, teacher_id=teacher)
    q = questions[0]
    sub = submission_repository.create_submission(
        assignment_id=published_assignment.id, student_id=student
    )
    rev1 = submission_repository.add_revision(
        submission_id=sub.id,
        student_id=student,
        source=education.SubmissionRevisionSource.ONLINE.value,
        answers=[{"question_id": q.id, "q_id": q.q_id, "type": q.type, "content": "first"}],
    )
    assert rev1.revision_number == 1
    rev2 = submission_repository.add_revision(
        submission_id=sub.id,
        student_id=student,
        source=education.SubmissionRevisionSource.ONLINE.value,
        answers=[{"question_id": q.id, "q_id": q.q_id, "type": q.type, "content": "second"}],
    )
    assert rev2.revision_number == 2

    refreshed = submission_repository.get_submission(sub.id, actor_id=student)
    assert refreshed.current_revision_number == 2
    assert refreshed.current_revision_id == rev2.id

    # The first revision's answers are frozen: re-reading it still shows "first".
    rev1_again = submission_repository.get_revision(rev1.id, actor_id=student)
    assert rev1_again.answers[0].content == "first"


@pytest.mark.parametrize(
    "status",
    [
        education.AssignmentStatus.DRAFT.value,
        education.AssignmentStatus.READY.value,
        education.AssignmentStatus.CLOSED.value,
        education.AssignmentStatus.ARCHIVED.value,
    ],
)
def test_add_revision_requires_published_assignment(published_assignment, student, teacher, status):
    q = assignment_repository.list_questions(published_assignment.id, teacher_id=teacher)[0]
    sub = submission_repository.create_submission(
        assignment_id=published_assignment.id, student_id=student
    )
    with session_scope() as session:
        row = session.get(AssignmentRecord, published_assignment.id)
        assert row is not None
        row.status = status

    with pytest.raises(AssignmentClosed):
        submission_repository.add_revision(
            submission_id=sub.id,
            student_id=student,
            source=education.SubmissionRevisionSource.ONLINE.value,
            answers=[{"question_id": q.id, "q_id": q.q_id, "type": q.type, "content": "blocked"}],
        )


def test_answer_uniqueness_per_revision_question(published_assignment, student, teacher):
    questions = assignment_repository.list_questions(published_assignment.id, teacher_id=teacher)
    q = questions[0]
    sub = submission_repository.create_submission(
        assignment_id=published_assignment.id, student_id=student
    )
    submission_repository.add_revision(
        submission_id=sub.id,
        student_id=student,
        source=education.SubmissionRevisionSource.ONLINE.value,
        answers=[{"question_id": q.id, "q_id": q.q_id, "type": q.type, "content": "a"}],
    )
    # Adding the same question twice in one revision must violate the
    # (revision_id, question_id) uniqueness enforced in SQL.
    with pytest.raises(Exception):
        submission_repository.add_revision(
            submission_id=sub.id,
            student_id=student,
            source=education.SubmissionRevisionSource.ONLINE.value,
            answers=[
                {"question_id": q.id, "q_id": q.q_id, "type": q.type, "content": "a1"},
                {"question_id": q.id, "q_id": q.q_id, "type": q.type, "content": "a2"},
            ],
        )


def test_get_submission_is_student_scoped(published_assignment, student, other_teacher):
    sub = submission_repository.create_submission(
        assignment_id=published_assignment.id, student_id=student
    )
    with pytest.raises(NotFound):
        submission_repository.get_submission(sub.id, actor_id=other_teacher)


def test_list_submissions_for_assignment_is_owner_scoped(
    published_assignment, student, other_teacher
):
    submission_repository.create_submission(
        assignment_id=published_assignment.id, student_id=student
    )
    # The assignment's teacher may list; another teacher reads nothing.
    assert submission_repository.list_submissions(
        assignment_id=published_assignment.id, actor_id=published_assignment.teacher_id
    )
    assert submission_repository.list_submissions(
        assignment_id=published_assignment.id, actor_id=other_teacher
    ) == []


# ─── grading run: one active run + lease predicate ─────────────────────────────


def test_only_one_active_run_per_assignment(teacher, published_assignment):
    run1 = grading_repository.create_run(
        assignment_id=published_assignment.id, teacher_id=teacher, total_submissions=1
    )
    assert run1.status == education.GradingRunStatus.QUEUED.value
    from backend.domain.errors import DuplicateActiveRun
    with pytest.raises(DuplicateActiveRun):
        grading_repository.create_run(
            assignment_id=published_assignment.id, teacher_id=teacher, total_submissions=1
        )


def test_claim_lease_is_atomic_and_owner_scoped(teacher, published_assignment):
    run = grading_repository.create_run(
        assignment_id=published_assignment.id, teacher_id=teacher, total_submissions=1
    )
    claimed = grading_repository.claim_lease(
        run_id=run.id, worker_id="worker-A", lease_seconds=60
    )
    assert claimed.lease_owner == "worker-A"
    assert claimed.status == education.GradingRunStatus.RUNNING.value
    # A second worker cannot claim the already-leased run.
    from backend.domain.errors import LeaseLost
    with pytest.raises(LeaseLost):
        grading_repository.claim_lease(run_id=run.id, worker_id="worker-B", lease_seconds=60)


def test_heartbeat_only_by_current_owner(teacher, published_assignment):
    run = grading_repository.create_run(
        assignment_id=published_assignment.id, teacher_id=teacher, total_submissions=1
    )
    grading_repository.claim_lease(run_id=run.id, worker_id="worker-A", lease_seconds=60)
    from backend.domain.errors import LeaseLost
    with pytest.raises(LeaseLost):
        grading_repository.heartbeat(run_id=run.id, worker_id="worker-B", lease_seconds=60)
    # The legitimate owner may extend.
    ok = grading_repository.heartbeat(run_id=run.id, worker_id="worker-A", lease_seconds=60)
    assert ok is True


def test_expired_lease_can_be_reclaimed(teacher, published_assignment):
    run = grading_repository.create_run(
        assignment_id=published_assignment.id, teacher_id=teacher, total_submissions=1
    )
    claimed = grading_repository.claim_lease(run_id=run.id, worker_id="worker-A", lease_seconds=1)
    # Force the lease into the past so recovery/reclaim treats it as expired.
    with session_scope() as session:
        row = session.get(GradingRunRecord, run.id)
        assert row is not None
        row.lease_expiry = time.time() - 10
    reclaimed = grading_repository.claim_lease(run_id=run.id, worker_id="worker-B", lease_seconds=60)
    assert reclaimed.lease_owner == "worker-B"


def test_terminal_write_requires_lease_owner_and_running(teacher, published_assignment):
    run = grading_repository.create_run(
        assignment_id=published_assignment.id, teacher_id=teacher, total_submissions=1
    )
    grading_repository.claim_lease(run_id=run.id, worker_id="worker-A", lease_seconds=60)
    from backend.domain.errors import LeaseLost
    # A late worker that lost the lease cannot write terminal state.
    with pytest.raises(LeaseLost):
        grading_repository.mark_completed(
            run_id=run.id, worker_id="worker-B", completed=1, failed=0
        )
    # The legitimate owner may complete.
    grading_repository.mark_completed(
        run_id=run.id, worker_id="worker-A", completed=1, failed=0
    )


# ─── helpers ───────────────────────────────────────────────────────────────────


class _StatementCapture:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self._engine = None

    def _on(self, conn, cursor, statement, parameters, context, executemany) -> None:
        self.statements.append(statement)

    def __enter__(self) -> "_StatementCapture":
        from sqlalchemy import event
        from backend.db.session import get_engine

        self._engine = get_engine()
        event.listen(self._engine, "before_cursor_execute", self._on)
        return self

    def __exit__(self, *exc) -> None:
        from sqlalchemy import event

        if self._engine is not None:
            event.remove(self._engine, "before_cursor_execute", self._on)


def _capture_statements() -> _StatementCapture:
    return _StatementCapture()
