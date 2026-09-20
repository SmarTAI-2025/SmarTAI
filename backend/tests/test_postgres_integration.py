"""PostgreSQL integration for the normalized education invariants (Task 14).

Runs only when ``SMARTAI_TEST_POSTGRES_URL`` points at a real PostgreSQL
database; otherwise the suite is skipped. Locally we run the SQLite suite, so
this file records that real PostgreSQL execution happens in GitHub Actions
rather than pretending it ran here. When enabled, it verifies the rules that
differ between SQLite and PostgreSQL against a live database:

* the single-active-run partial unique index rejects a second active run;
* an optimistic assignment UPDATE with a stale version is a 409 conflict;
* the lease claim predicate is owner-scoped;
* role-scoped assignment/submission reads hide other owners.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

PG_URL = os.environ.get("SMARTAI_TEST_POSTGRES_URL")


pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="Set SMARTAI_TEST_POSTGRES_URL to run PostgreSQL integration (GitHub Actions).",
)


@pytest.fixture
def pg_database():
    from alembic import command
    from alembic.config import Config
    from backend.config import settings
    from backend.db.session import configure_database
    from sqlalchemy import create_engine

    old_heavy = settings.database_heavy
    settings.database_heavy = True
    configure_database(PG_URL)
    engine = create_engine(PG_URL)
    config = Config("alembic.ini")
    config.set_main_option("script_location", "backend/db/migrations")
    old_url = os.environ.get("SMARTAI_DATABASE_URL")
    old_heavy_env = os.environ.get("SMARTAI_DATABASE_HEAVY")
    os.environ["SMARTAI_DATABASE_URL"] = PG_URL
    os.environ["SMARTAI_DATABASE_HEAVY"] = "ON"
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP SCHEMA public CASCADE")
            connection.exec_driver_sql("CREATE SCHEMA public")
        command.upgrade(config, "head")
        yield
    finally:
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP SCHEMA public CASCADE")
            connection.exec_driver_sql("CREATE SCHEMA public")
        engine.dispose()
        settings.database_heavy = old_heavy
        if old_url is None:
            os.environ.pop("SMARTAI_DATABASE_URL", None)
        else:
            os.environ["SMARTAI_DATABASE_URL"] = old_url
        if old_heavy_env is None:
            os.environ.pop("SMARTAI_DATABASE_HEAVY", None)
        else:
            os.environ["SMARTAI_DATABASE_HEAVY"] = old_heavy_env


def _seed_user(role: str) -> str:
    from backend.db.models import UserRecord
    from backend.db.session import session_scope
    uid = f"pg_{role}_{uuid.uuid4().hex[:8]}"
    with session_scope() as session:
        session.add(UserRecord(id=uid, username=uid, role=role, password_hash="x", is_active=True))
    return uid


def test_postgres_registration_waits_for_cross_worker_flow_lock(pg_database, monkeypatch):
    from backend.services import email_registration
    from backend.db.session import get_engine
    from sqlalchemy import text

    monkeypatch.setattr(email_registration.settings, "allowed_email_domains", "ustc.edu.cn")
    source_ip = "203.0.113.199"
    lock_id = email_registration._database_flow_lock_id(f"ip:{source_ip}")
    sender_called = threading.Event()

    class Sender:
        def send(self, *_args):
            sender_called.set()

    with get_engine().connect() as connection:
        transaction = connection.begin()
        connection.execute(text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": lock_id})
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                email_registration.request_registration,
                username="pg-cross-worker",
                email="pg-cross-worker@ustc.edu.cn",
                password="long-enough-password",
                source_ip=source_ip,
                sender=Sender(),
            )
            time.sleep(0.2)
            assert not sender_called.is_set(), "request must block on the database advisory lock"
            transaction.commit()
            result = future.result(timeout=5)

    assert result["status"] == "verification_required"
    assert sender_called.is_set()


def test_postgres_single_active_run(pg_database):
    from backend.db import course_repository, assignment_repository, grading_repository
    from backend.domain import education
    from backend.domain.errors import DuplicateActiveRun

    teacher = _seed_user("teacher")
    student = _seed_user("student")
    course = course_repository.create_course(teacher_id=teacher, name="C")
    course_repository.enroll(course_id=course.id, student_id=student)
    asg = assignment_repository.create_assignment(teacher_id=teacher, course_id=course.id, name="A")
    assignment_repository.add_question(
        assignment_id=asg.id, teacher_id=teacher, q_id="q1", order_index=0, type="short", stem="?",
    )
    assignment_repository.publish(assignment_id=asg.id, teacher_id=teacher, expected_version=1)

    grading_repository.create_run(assignment_id=asg.id, teacher_id=teacher, total_submissions=1)
    with pytest.raises(DuplicateActiveRun):
        grading_repository.create_run(assignment_id=asg.id, teacher_id=teacher, total_submissions=1)


def test_postgres_optimistic_update_conflict(pg_database):
    from backend.db import assignment_repository, course_repository
    from backend.domain.errors import VersionConflict

    teacher = _seed_user("teacher")
    course = course_repository.create_course(teacher_id=teacher, name="C")
    asg = assignment_repository.create_assignment(teacher_id=teacher, course_id=course.id, name="A")
    # Bump version out-of-band, then a stale client update must conflict.
    assignment_repository.rename_assignment(assignment_id=asg.id, teacher_id=teacher, expected_version=1, name="v2")
    with pytest.raises(VersionConflict):
        assignment_repository.rename_assignment(assignment_id=asg.id, teacher_id=teacher, expected_version=1, name="stale")


def test_postgres_lease_claim_is_owner_scoped(pg_database):
    from backend.db import course_repository, assignment_repository, grading_repository
    from backend.domain.errors import LeaseLost

    teacher = _seed_user("teacher")
    student = _seed_user("student")
    course = course_repository.create_course(teacher_id=teacher, name="C")
    course_repository.enroll(course_id=course.id, student_id=student)
    asg = assignment_repository.create_assignment(teacher_id=teacher, course_id=course.id, name="A")
    assignment_repository.add_question(
        assignment_id=asg.id, teacher_id=teacher, q_id="q1", order_index=0, type="short", stem="?",
    )
    assignment_repository.publish(assignment_id=asg.id, teacher_id=teacher, expected_version=1)

    run = grading_repository.create_run(assignment_id=asg.id, teacher_id=teacher, total_submissions=1)
    grading_repository.claim_lease(run_id=run.id, worker_id="w1", lease_seconds=60)
    with pytest.raises(LeaseLost):
        grading_repository.claim_lease(run_id=run.id, worker_id="w2", lease_seconds=60)


def test_postgres_finalization_locks_grading_before_workflow(pg_database):
    """A finalization waiting on G must not already own W.

    This is the concrete PostgreSQL deadlock regression for the canonical
    grading lock order G -> W -> A.  The controlling transaction holds G and
    must still be able to take W while the real finalizer is blocked.
    """
    from sqlalchemy import select, text

    from backend.db import workflow_repository
    from backend.db.models import GradingRunRecord
    from backend.db.session import get_engine
    from backend.services import task_facade
    from backend.tests.test_task_finalization_contract import _prepared_task

    seeded = _prepared_task()
    workflow = workflow_repository.get_live_workflow(
        seeded["task_id"], owner_id=seeded["owner_id"]
    )

    def finalize() -> str:
        try:
            task_facade.confirm_finalization(
                task_id=seeded["task_id"],
                owner_id=seeded["owner_id"],
                expected_revision=workflow.workflow_revision,
            )
        except Exception as exc:  # lock order is the assertion, not release readiness
            return type(exc).__name__
        return "completed"

    with get_engine().connect() as connection:
        transaction = connection.begin()
        connection.execute(text("SET LOCAL lock_timeout = '750ms'"))
        connection.execute(
            select(GradingRunRecord)
            .where(GradingRunRecord.id == seeded["run_id"])
            .with_for_update()
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(finalize)
            time.sleep(0.2)
            # Old W -> G finalization times out here because it owns W while
            # waiting for this transaction's G lock.
            connection.execute(
                select(workflow_repository.AssignmentWorkflowRecord)
                .where(
                    workflow_repository.AssignmentWorkflowRecord.assignment_id
                    == seeded["task_id"]
                )
                .with_for_update()
            )
            transaction.commit()
            assert future.result(timeout=5) in {
                "ResultNotReleasable",
                "completed",
            }


def test_postgres_task_delete_finalizer_locks_children_before_workflow(pg_database):
    """Task CASCADE must wait on G before it owns W/A."""
    from sqlalchemy import select, text

    from backend.db import task_deletion_repository, workflow_repository
    from backend.db.models import GradingRunRecord
    from backend.db.session import get_engine
    from backend.services import task_facade
    from backend.tests.test_task_finalization_contract import _prepared_task

    seeded = _prepared_task()
    accepted = task_facade.delete_task(
        task_id=seeded["task_id"], owner_id=seeded["owner_id"]
    )
    operation = workflow_repository.claim_operation(
        accepted["cleanup_operation_id"],
        owner_id=seeded["owner_id"],
        worker_id="pg-delete-finalizer",
        lease_seconds=30,
    )

    def finalize() -> bool:
        return task_deletion_repository.finalize_task_deletion(
            operation_id=operation.id,
            owner_id=seeded["owner_id"],
            assignment_id=seeded["task_id"],
            attempt=operation.attempt,
            worker_id="pg-delete-finalizer",
            lease_token=str(operation.lease_token),
        )

    with get_engine().connect() as connection:
        transaction = connection.begin()
        connection.execute(text("SET LOCAL lock_timeout = '750ms'"))
        connection.execute(
            select(GradingRunRecord)
            .where(GradingRunRecord.id == seeded["run_id"])
            .with_for_update()
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(finalize)
            time.sleep(0.2)
            # An old O(delete) -> W -> A -> implicit G cascade blocks here.
            connection.execute(
                select(workflow_repository.AssignmentWorkflowRecord)
                .where(
                    workflow_repository.AssignmentWorkflowRecord.assignment_id
                    == seeded["task_id"]
                )
                .with_for_update()
            )
            transaction.commit()
            assert future.result(timeout=5) is True


def test_postgres_role_scoped_reads_hide_other_owner(pg_database):
    from backend.db import assignment_repository, course_repository
    from backend.domain.errors import NotFound

    teacher = _seed_user("teacher")
    other = _seed_user("teacher")
    course = course_repository.create_course(teacher_id=teacher, name="C")
    asg = assignment_repository.create_assignment(teacher_id=teacher, course_id=course.id, name="Secret")
    with pytest.raises(NotFound):
        assignment_repository.get_assignment(assignment_id=asg.id, actor_id=other)


def test_postgres_source_outcome_persistence_and_owner_isolation(
    pg_database,
    tmp_path,
):
    from backend.db import (
        assignment_repository,
        course_repository,
        source_outcome_repository,
        workflow_repository,
    )
    from backend.db.file_repository import save_file
    from backend.domain.errors import NotFound
    from backend.storage.local import LocalStorage

    teacher = _seed_user("teacher")
    other = _seed_user("teacher")
    course = course_repository.create_course(teacher_id=teacher, name="C")
    assignment = assignment_repository.create_assignment(
        teacher_id=teacher,
        course_id=course.id,
        name="A",
    )
    workflow_repository.ensure_workflow(
        assignment_id=assignment.id,
        owner_id=teacher,
    )
    operation, _ = workflow_repository.create_operation(
        assignment_id=assignment.id,
        owner_id=teacher,
        operation_type="submission_recognition",
        input_hash=uuid.uuid4().hex,
    )
    stored = save_file(
        storage=LocalStorage(tmp_path / "pg-source-files"),
        owner_id=teacher,
        kind="submission_source",
        original_name="answers.pdf",
        content=b"answers",
        content_type="application/pdf",
        assignment_id=assignment.id,
    )
    source, created = source_outcome_repository.register_source(
        owner_id=teacher,
        assignment_id=assignment.id,
        operation_id=operation.id,
        expected_attempt=operation.attempt,
        order_index=0,
        stored_file_id=stored.id,
    )
    source_outcome_repository.record_outcome(
        source_id=source.id,
        owner_id=teacher,
        status="parse_failed",
        student_candidate=None,
        matched_answer_count=0,
        unknown_question_ids=[],
        stable_error_code="submission_parse_failed",
        failure_phase="recognition",
        retryable=True,
    )
    summary = source_outcome_repository.summarize_sources(
        operation_id=operation.id,
        owner_id=teacher,
        attempt=operation.attempt,
    )

    assert created is True
    assert summary.uploaded_count == 1
    assert summary.failed_count == 1
    assert summary.is_complete is True
    with pytest.raises(NotFound):
        source_outcome_repository.get_source(source.id, owner_id=other)
    with pytest.raises(NotFound):
        source_outcome_repository.summarize_sources(
            operation_id=operation.id,
            owner_id=other,
            attempt=operation.attempt,
        )


def test_postgres_operation_checkpoint_cas_and_owner_isolation(pg_database):
    from backend.db import (
        assignment_repository,
        course_repository,
        workflow_repository,
    )
    from backend.domain.errors import NotFound, VersionConflict

    teacher = _seed_user("teacher")
    other = _seed_user("teacher")
    course = course_repository.create_course(teacher_id=teacher, name="C")
    assignment = assignment_repository.create_assignment(
        teacher_id=teacher,
        course_id=course.id,
        name="A",
    )
    workflow_repository.ensure_workflow(
        assignment_id=assignment.id,
        owner_id=teacher,
    )
    operation, _ = workflow_repository.create_operation(
        assignment_id=assignment.id,
        owner_id=teacher,
        operation_type="submission_recognition",
        input_hash=uuid.uuid4().hex,
    )

    def write_checkpoint(worker: str):
        try:
            saved = workflow_repository.save_operation_checkpoint(
                operation.id,
                owner_id=teacher,
                expected_attempt=operation.attempt,
                expected_checkpoint_revision=0,
                stage="parsing",
                checkpoint={"worker": worker},
            )
            return "saved", saved.checkpoint["worker"]
        except VersionConflict as exc:
            return "conflict", exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(write_checkpoint, ("first", "second")))

    assert [kind for kind, _value in results].count("saved") == 1
    assert results.count(("conflict", "stale_checkpoint_revision")) == 1
    persisted = workflow_repository.get_operation(operation.id, owner_id=teacher)
    assert persisted.checkpoint_revision == 1
    with pytest.raises(NotFound):
        workflow_repository.save_operation_checkpoint(
            operation.id,
            owner_id=other,
            expected_attempt=operation.attempt,
            expected_checkpoint_revision=persisted.checkpoint_revision,
            stage="hidden",
            checkpoint={},
        )


def test_postgres_operation_lease_one_winner_claim(pg_database):
    from backend.db import (
        assignment_repository,
        course_repository,
        workflow_repository,
    )
    from backend.domain.errors import LeaseLost

    teacher = _seed_user("teacher")
    course = course_repository.create_course(teacher_id=teacher, name="C")
    assignment = assignment_repository.create_assignment(
        teacher_id=teacher,
        course_id=course.id,
        name="A",
    )
    workflow_repository.ensure_workflow(
        assignment_id=assignment.id,
        owner_id=teacher,
    )
    operation, _ = workflow_repository.create_operation(
        assignment_id=assignment.id,
        owner_id=teacher,
        operation_type="submission_recognition",
        input_hash=uuid.uuid4().hex,
    )

    def claim(worker: str):
        try:
            workflow_repository.claim_operation(
                operation.id,
                owner_id=teacher,
                worker_id=worker,
                lease_seconds=60,
            )
            return "claimed"
        except LeaseLost:
            return "lost"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, ("worker-a", "worker-b")))

    assert results.count("claimed") == 1
    assert results.count("lost") == 1
    persisted = workflow_repository.get_operation(operation.id, owner_id=teacher)
    assert persisted.status == "running"
    assert persisted.lease_owner in {"worker-a", "worker-b"}
    assert persisted.lease_token is not None
    assert persisted.lease_expires_at is not None


def test_postgres_live_lease_rejects_same_worker_claim(pg_database):
    """A live lease fences a second claim even from the same worker_id.

    Two concurrent coroutines in one process share a worker_id; the claim
    predicate must not treat ``lease_owner == worker_id`` as claimable, or both
    could win and rotate the token independently.
    """
    from backend.db import (
        assignment_repository,
        course_repository,
        workflow_repository,
    )
    from backend.domain.errors import LeaseLost

    teacher = _seed_user("teacher")
    course = course_repository.create_course(teacher_id=teacher, name="A")
    assignment = assignment_repository.create_assignment(
        teacher_id=teacher,
        course_id=course.id,
        name="A",
    )
    workflow_repository.ensure_workflow(
        assignment_id=assignment.id,
        owner_id=teacher,
    )
    operation, _ = workflow_repository.create_operation(
        assignment_id=assignment.id,
        owner_id=teacher,
        operation_type="submission_recognition",
        input_hash=uuid.uuid4().hex,
    )

    first = workflow_repository.claim_operation(
        operation.id,
        owner_id=teacher,
        worker_id="worker-a",
        lease_seconds=60,
    )
    with pytest.raises(LeaseLost) as second_claim:
        workflow_repository.claim_operation(
            operation.id,
            owner_id=teacher,
            worker_id="worker-a",
            lease_seconds=60,
        )
    assert second_claim.value.code == "operation_not_claimable"

    persisted = workflow_repository.get_operation(operation.id, owner_id=teacher)
    assert persisted.lease_owner == "worker-a"
    assert persisted.lease_token == first.lease_token


def test_postgres_missing_source_and_replacement_use_user_then_file_lock_order(
    pg_database,
    tmp_path,
    monkeypatch,
):
    """Missing detection must not deadlock a concurrent replacement claim.

    The replacement is paused immediately after it owns the quota User row.
    The missing-object transition then reaches the same User lock. With the
    canonical User -> StoredFile order, releasing the replacement lets both
    transactions finish. The old StoredFile -> User order creates the exact
    PostgreSQL cycle: marker owns S and waits for U while replacement owns U
    and waits for S.
    """

    from backend.config import settings
    from backend.db import (
        assignment_repository,
        course_repository,
        file_repository,
        source_storage_repository,
    )
    from backend.storage.local import LocalStorage

    teacher = _seed_user("teacher")
    course = course_repository.create_course(teacher_id=teacher, name="C")
    assignment = assignment_repository.create_assignment(
        teacher_id=teacher,
        course_id=course.id,
        name="A",
    )
    monkeypatch.setattr(settings, "unfinished_source_quota_bytes", 10)
    storage = LocalStorage(tmp_path / "pg-missing-replacement-lock-order")
    old = file_repository.save_file(
        storage=storage,
        owner_id=teacher,
        kind="problem_source",
        original_name="old.pdf",
        content=b"old-source",
        content_type="application/pdf",
        assignment_id=assignment.id,
    )

    real_lock_quota_owner = source_storage_repository._lock_quota_owner
    replacement_has_user = threading.Event()
    marker_reached_user = threading.Event()
    release_replacement = threading.Event()
    pause_guard = threading.Lock()
    replacement_paused = False

    def controlled_lock_quota_owner(session, owner_id: str) -> None:
        nonlocal replacement_paused
        role = threading.current_thread().name
        if role == "pg-missing-marker":
            marker_reached_user.set()
        real_lock_quota_owner(session, owner_id)
        if role != "pg-replacement-reservation":
            return
        with pause_guard:
            should_pause = not replacement_paused
            replacement_paused = True
        if should_pause:
            replacement_has_user.set()
            if not release_replacement.wait(timeout=5):
                raise AssertionError("replacement lock-order test timed out")

    monkeypatch.setattr(
        source_storage_repository,
        "_lock_quota_owner",
        controlled_lock_quota_owner,
    )

    def reserve_replacement():
        threading.current_thread().name = "pg-replacement-reservation"
        content = b"new-source"
        return source_storage_repository.reserve_source_upload(
            file_id=f"pg-replacement-{uuid.uuid4().hex}",
            file_owner_id=teacher,
            kind="problem_source",
            original_name="new.pdf",
            storage_backend="local",
            storage_key=f"{teacher}/problem_source/new.pdf",
            content_type="application/pdf",
            requested_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            assignment_id=assignment.id,
            submission_revision_id=None,
            replacement_file_ids=(old.id,),
            replacement_group_id="pg-missing-replacement-group",
        )

    def mark_missing():
        threading.current_thread().name = "pg-missing-marker"
        return source_storage_repository.mark_available_source_missing(
            file_id=old.id,
            owner_id=teacher,
            assignment_id=assignment.id,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        replacement_future = executor.submit(reserve_replacement)
        try:
            assert replacement_has_user.wait(timeout=5)
            missing_future = executor.submit(mark_missing)
            assert marker_reached_user.wait(timeout=5)
        finally:
            release_replacement.set()
        reservation = replacement_future.result(timeout=10)
        assert missing_future.result(timeout=10) is True

    assert reservation.replacement_credit_bytes == len(b"old-source")
    retired = file_repository.get_file(file_id=old.id, owner_id=teacher)
    assert retired.availability_status == "unavailable"
    assert retired.availability_reason == "missing"
    usage = source_storage_repository.source_quota_usage(teacher)
    assert usage.used_bytes == len(b"new-source")
    assert usage.available_source_bytes == 0
    assert usage.reserved_bytes == len(b"new-source")


def test_postgres_question_preparation_atomic_publication_has_one_winner(
    pg_database,
):
    from backend.db import (
        assignment_repository,
        course_repository,
        workflow_repository,
    )
    from backend.domain.errors import DomainError
    from backend.db.session import session_scope
    from backend.services import task_facade
    from sqlalchemy import select

    teacher = _seed_user("teacher")
    course = course_repository.create_course(teacher_id=teacher, name="C")
    assignment = assignment_repository.create_assignment(
        teacher_id=teacher,
        course_id=course.id,
        name="A",
    )
    workflow_repository.ensure_workflow(
        assignment_id=assignment.id,
        owner_id=teacher,
    )
    start = threading.Barrier(2)

    def publish(input_hash: str):
        start.wait(timeout=10)
        try:
            operation, published, revision = (
                task_facade.publish_checkpointed_operation_atomic(
                    task_id=assignment.id,
                    owner_id=teacher,
                    operation_type="question_preparation",
                    input_hash=input_hash,
                    expected_workflow_revision=0,
                    operation_payload={"input_hash": input_hash},
                    initial_checkpoint_stage="sources_validated",
                    initial_checkpoint={"stage": "sources_validated"},
                    artifact_refs=[],
                    workflow_changes={
                        "active_operation": "question_preparation",
                        "presentation_status": "extracting_problems",
                    },
                    workflow_job_id_fields=(
                        "active_job_id",
                        "extract_job_id",
                    ),
                )
            )
            return "published", published, revision, operation.id
        except DomainError as exc:
            return "error", exc.code, None, None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(publish, ("a" * 64, "b" * 64)))

    assert sum(
        result[0] == "published" and result[1] is True
        for result in results
    ) == 1
    assert [result[1] for result in results if result[0] == "error"] == [
        "workflow_busy"
    ]
    with session_scope() as session:
        rows = session.scalars(
            select(workflow_repository.WorkflowOperationRecord).where(
                workflow_repository.WorkflowOperationRecord.assignment_id
                == assignment.id
            )
        ).all()
    assert len(rows) == 1
    assert rows[0].status == "pending"


@pytest.mark.asyncio
async def test_postgres_task_delete_worker_removes_full_restrict_graph_and_storage(
    pg_database,
    tmp_path,
    monkeypatch,
):
    """A tombstoned task is physically and relationally erased on PostgreSQL.

    The source item/outcome rows deliberately add PostgreSQL-enforced RESTRICT
    links to two StoredFile rows.  The rest of the fixture covers the complete
    grading graph, including questions, submissions, revisions, answers,
    results, and a teacher review.  Running the real task-delete worker proves
    its explicit source-graph teardown happens before the assignment CASCADE.
    """
    from sqlalchemy import select, update

    from backend.db import (
        file_repository,
        grading_repository,
        source_outcome_repository,
        workflow_repository,
    )
    from backend.db.models import (
        AssignmentQuestionRecord,
        AssignmentRecord,
        CourseEnrollmentRecord,
        GradeResultRecord,
        GradingRunRecord,
        StoredFileRecord,
        SubmissionAnswerRecord,
        SubmissionRecord,
        SubmissionRevisionRecord,
        TeacherReviewRecord,
        UserRecord,
    )
    from backend.db.session import session_scope
    from backend.domain.source_storage import TASK_DELETE_OPERATION
    from backend.services import task_deletion, task_facade
    from backend.services.workflow_worker import WorkflowWorker
    from backend.storage.local import LocalStorage
    from backend.tests.test_task_finalization_contract import _prepared_task

    seeded = _prepared_task()
    storage = LocalStorage(tmp_path / "postgres-task-delete")
    monkeypatch.setattr(task_deletion, "get_storage", lambda: storage)

    producer, created = workflow_repository.create_operation(
        assignment_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        operation_type="submission_recognition",
        input_hash=uuid.uuid4().hex,
    )
    assert created is True

    source_file = file_repository.save_file(
        storage=storage,
        owner_id=seeded["owner_id"],
        kind="submission_source",
        original_name="student-answer.pdf",
        content=b"student source",
        content_type="application/pdf",
        assignment_id=seeded["task_id"],
    )
    artifact_file = file_repository.save_file(
        storage=storage,
        owner_id=seeded["owner_id"],
        kind="submission_parse_artifact",
        original_name="recognized.json",
        content=b'{"answers": ["structured"]}',
        content_type="application/json",
        assignment_id=seeded["task_id"],
    )
    source, source_created = source_outcome_repository.register_source(
        owner_id=seeded["owner_id"],
        assignment_id=seeded["task_id"],
        operation_id=producer.id,
        expected_attempt=producer.attempt,
        order_index=0,
        stored_file_id=source_file.id,
    )
    assert source_created is True
    source_outcome_repository.record_outcome(
        source_id=source.id,
        owner_id=seeded["owner_id"],
        status="parsed",
        student_candidate=seeded["student_id"],
        matched_answer_count=2,
        unknown_question_ids=[],
        stable_error_code=None,
        failure_phase=None,
        retryable=False,
        artifact_file_id=artifact_file.id,
    )
    review = grading_repository.add_teacher_review(
        seeded["required_result_id"],
        teacher_id=seeded["owner_id"],
        new_score=7,
        new_comment="confirmed",
        confirm=True,
    )

    with session_scope() as session:
        target = session.get(AssignmentRecord, seeded["task_id"])
        assert target is not None
        course_id = target.course_id
        imported_user_id = "imported_pg_task_only"
        session.add(UserRecord(
            id=imported_user_id,
            username="imported-pg-task-only",
            email=None,
            password_hash="!disabled-imported-account",
            role="student",
            is_active=False,
        ))
        session.flush()
        session.add_all([
            CourseEnrollmentRecord(
                course_id=course_id,
                student_id=imported_user_id,
            ),
            SubmissionRecord(
                id="sub-imported-pg-task-only",
                assignment_id=seeded["task_id"],
                student_id=imported_user_id,
                current_revision_id=None,
            ),
        ])
        session.flush()
        question_ids = tuple(session.scalars(
            select(AssignmentQuestionRecord.id).where(
                AssignmentQuestionRecord.assignment_id == seeded["task_id"]
            )
        ))
        submission_ids = tuple(session.scalars(
            select(SubmissionRecord.id).where(
                SubmissionRecord.assignment_id == seeded["task_id"]
            )
        ))
        revision_ids = tuple(session.scalars(
            select(SubmissionRevisionRecord.id).where(
                SubmissionRevisionRecord.submission_id.in_(submission_ids)
            )
        ))
        answer_ids = tuple(session.scalars(
            select(SubmissionAnswerRecord.id).where(
                SubmissionAnswerRecord.revision_id.in_(revision_ids)
            )
        ))

    revision_file = file_repository.save_file(
        storage=storage,
        owner_id=seeded["student_id"],
        kind="submission",
        original_name="revision-source.pdf",
        content=b"revision source",
        content_type="application/pdf",
        submission_revision_id=revision_ids[0],
    )
    physical_keys = (
        source_file.storage_key,
        artifact_file.storage_key,
        revision_file.storage_key,
    )
    assert all(storage.exists(key) for key in physical_keys)

    response = task_facade.delete_task(
        task_id=seeded["task_id"], owner_id=seeded["owner_id"]
    )
    assert response["status"] == "deletion_pending"
    delete_operation_id = response["cleanup_operation_id"]
    with session_scope() as session:
        tombstoned = session.get(AssignmentRecord, seeded["task_id"])
        assert tombstoned is not None
        assert tombstoned.deletion_requested_at is not None

    worker = WorkflowWorker(
        handlers={TASK_DELETE_OPERATION: task_deletion.run_task_deletion},
        worker_id="postgres-task-delete-worker",
        lease_seconds=60,
        heartbeat_seconds=60,
        poll_seconds=60,
        claim_batch_size=10,
        max_in_flight=1,
        shutdown_seconds=1,
    )
    for _ in range(12):
        with session_scope() as session:
            if session.get(AssignmentRecord, seeded["task_id"]) is None:
                break
            session.execute(
                update(workflow_repository.WorkflowOperationRecord)
                .where(
                    workflow_repository.WorkflowOperationRecord.id
                    == delete_operation_id,
                    workflow_repository.WorkflowOperationRecord.status == "pending",
                )
                .values(expires_at=0.0)
            )
        await worker.poll_once()
        for _ in range(500):
            if worker.in_flight_count == 0:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("PostgreSQL task-delete worker did not drain")
    else:
        raise AssertionError("PostgreSQL task deletion did not finish")

    assert all(not storage.exists(key) for key in physical_keys)
    with session_scope() as session:
        assert session.get(AssignmentRecord, seeded["task_id"]) is None
        assert all(
            session.get(AssignmentQuestionRecord, row_id) is None
            for row_id in question_ids
        )
        assert all(
            session.get(SubmissionRecord, row_id) is None
            for row_id in submission_ids
        )
        assert all(
            session.get(SubmissionRevisionRecord, row_id) is None
            for row_id in revision_ids
        )
        assert all(
            session.get(SubmissionAnswerRecord, row_id) is None
            for row_id in answer_ids
        )
        assert session.get(GradingRunRecord, seeded["run_id"]) is None
        assert session.get(GradeResultRecord, seeded["required_result_id"]) is None
        assert session.get(GradeResultRecord, seeded["optional_result_id"]) is None
        assert session.get(TeacherReviewRecord, review.id) is None
        assert session.get(UserRecord, imported_user_id) is None
        assert session.get(
            CourseEnrollmentRecord,
            {"course_id": course_id, "student_id": imported_user_id},
        ) is None
        assert session.get(UserRecord, seeded["student_id"]) is not None
        assert session.get(source_outcome_repository.WorkflowSourceItemRecord, source.id) is None
        assert session.get(source_outcome_repository.WorkflowSourceOutcomeRecord, source.id) is None
        assert session.get(workflow_repository.WorkflowOperationRecord, producer.id) is None
        assert session.get(workflow_repository.WorkflowOperationRecord, delete_operation_id) is None
        assert all(
            session.get(StoredFileRecord, row_id) is None
            for row_id in (source_file.id, artifact_file.id, revision_file.id)
        )


def test_postgres_knowledge_quota_concurrent_admission_has_one_winner(
    pg_database, monkeypatch,
):
    """The User quota gate serializes distinct hashes across processes."""
    from backend.config import settings
    from backend.db.knowledge_storage_repository import reserve_upload
    from backend.domain.errors import KnowledgeStorageQuotaExceeded

    owner_id = _seed_user("teacher")
    monkeypatch.setattr(settings, "knowledge_storage_quota_bytes", 4)
    barrier = threading.Barrier(2)

    def admit(payload: bytes) -> str:
        barrier.wait(timeout=5)
        try:
            reserve_upload(
                owner_id=owner_id,
                original_name=f"{payload.decode()}.txt",
                size_bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            )
            return "reserved"
        except KnowledgeStorageQuotaExceeded:
            return "quota"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(admit, (b"aaaa", b"bbbb")))
    assert results.count("reserved") == 1
    assert results.count("quota") == 1


def test_postgres_knowledge_attach_and_reserve_share_user_first_lock_order(
    pg_database,
):
    """Attachment FK writes and task-only admission cannot deadlock A/User."""
    from sqlalchemy import select

    from backend.db import assignment_repository, course_repository
    from backend.db.knowledge_storage_repository import (
        lock_knowledge_owner_in_session,
        mark_document_attached_in_session,
        publish_upload,
        reserve_upload,
    )
    from backend.db.models import (
        AssignmentKnowledgeDocumentRecord,
        AssignmentRecord,
    )
    from backend.db.session import session_scope
    from backend.domain.knowledge_storage import KNOWLEDGE_RETENTION_TASK_ONLY

    owner_id = _seed_user("teacher")
    course = course_repository.create_course(teacher_id=owner_id, name="C")
    assignment = assignment_repository.create_assignment(
        teacher_id=owner_id, course_id=course.id, name="A"
    )
    initial = reserve_upload(
        owner_id=owner_id,
        original_name="initial.txt",
        size_bytes=1,
        sha256=hashlib.sha256(b"i").hexdigest(),
        retention_policy=KNOWLEDGE_RETENTION_TASK_ONLY,
        origin_assignment_id=assignment.id,
    )
    assert initial.entry.writer_claim_token is not None
    publish_upload(
        reservation_id=initial.entry.id,
        owner_id=owner_id,
        writer_claim_token=initial.entry.writer_claim_token,
    )
    barrier = threading.Barrier(2)

    def attach() -> str:
        barrier.wait(timeout=5)
        with session_scope() as session:
            lock_knowledge_owner_in_session(session, owner_id)
            session.scalar(
                select(AssignmentRecord)
                .where(AssignmentRecord.id == assignment.id)
                .with_for_update()
            )
            session.add(AssignmentKnowledgeDocumentRecord(
                assignment_id=assignment.id,
                document_id=initial.document_id,
                source_kind="upload",
            ))
            session.flush()
            mark_document_attached_in_session(
                session,
                assignment_id=assignment.id,
                owner_id=owner_id,
                document_id=initial.document_id,
            )
        return "attached"

    def reserve_second() -> str:
        barrier.wait(timeout=5)
        reserve_upload(
            owner_id=owner_id,
            original_name="second.txt",
            size_bytes=1,
            sha256=hashlib.sha256(b"s").hexdigest(),
            retention_policy=KNOWLEDGE_RETENTION_TASK_ONLY,
            origin_assignment_id=assignment.id,
        )
        return "reserved"

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(attach), executor.submit(reserve_second)]
        results = [future.result(timeout=10) for future in futures]
    assert sorted(results) == ["attached", "reserved"]


@pytest.mark.parametrize("same_task", [True, False], ids=["same-task", "cross-task"])
def test_postgres_source_and_task_knowledge_use_compatible_owner_assignment_order(
    pg_database,
    same_task,
):
    """Source W->User->A cannot deadlock knowledge User->A.

    The same-task case is the former concrete A/User cycle. The cross-task case
    proves a shared owner gate also serializes independent task rows without a
    hidden Workflow/Assignment inversion.
    """
    from backend.db import (
        assignment_repository,
        course_repository,
        source_storage_repository,
        workflow_repository,
    )
    from backend.db.knowledge_storage_repository import reserve_upload
    from backend.domain.knowledge_storage import KNOWLEDGE_RETENTION_TASK_ONLY

    owner_id = _seed_user("teacher")
    course = course_repository.create_course(teacher_id=owner_id, name="C")
    source_task = assignment_repository.create_assignment(
        teacher_id=owner_id, course_id=course.id, name="Source task"
    )
    workflow_repository.ensure_workflow(
        assignment_id=source_task.id, owner_id=owner_id,
    )
    if same_task:
        knowledge_task = source_task
    else:
        knowledge_task = assignment_repository.create_assignment(
            teacher_id=owner_id, course_id=course.id, name="Knowledge task"
        )
        workflow_repository.ensure_workflow(
            assignment_id=knowledge_task.id, owner_id=owner_id,
        )
    barrier = threading.Barrier(2)

    def reserve_source() -> str:
        payload = b"source"
        barrier.wait(timeout=5)
        source_storage_repository.reserve_source_upload(
            file_id=f"pg-source-{uuid.uuid4().hex}",
            file_owner_id=owner_id,
            kind="problem_source",
            original_name="problem.pdf",
            storage_backend="local",
            storage_key=(
                f"assignments/{source_task.id}/{uuid.uuid4().hex}/problem.pdf"
            ),
            content_type="application/pdf",
            requested_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            assignment_id=source_task.id,
            submission_revision_id=None,
        )
        return "source"

    def reserve_knowledge() -> str:
        barrier.wait(timeout=5)
        reserve_upload(
            owner_id=owner_id,
            original_name="task-note.txt",
            size_bytes=9,
            sha256=hashlib.sha256(
                f"knowledge-{knowledge_task.id}".encode()
            ).hexdigest(),
            retention_policy=KNOWLEDGE_RETENTION_TASK_ONLY,
            origin_assignment_id=knowledge_task.id,
        )
        return "knowledge"

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(reserve_source),
            executor.submit(reserve_knowledge),
        ]
        assert sorted(future.result(timeout=10) for future in futures) == [
            "knowledge", "source",
        ]


def test_postgres_grading_knowledge_fence_and_cleanup_are_mutually_exclusive(
    pg_database,
    tmp_path,
):
    """Run creation and cleanup serialize on User+ledger with no TOCTOU."""
    from backend.db import (
        assignment_repository,
        course_repository,
        grading_repository,
        workflow_repository,
    )
    from backend.db.knowledge_storage_repository import request_document_cleanup
    from backend.domain.errors import InvalidTransition
    from backend.services.knowledge_storage import persist_knowledge_upload
    from backend.storage.local import LocalStorage

    owner_id = _seed_user("teacher")
    course = course_repository.create_course(teacher_id=owner_id, name="C")
    assignment = assignment_repository.create_assignment(
        teacher_id=owner_id, course_id=course.id, name="A"
    )
    workflow = workflow_repository.ensure_workflow(
        assignment_id=assignment.id, owner_id=owner_id,
    )
    uploaded = persist_knowledge_upload(
        storage=LocalStorage(tmp_path / "grading-cleanup"),
        owner_id=owner_id,
        original_name="frozen.txt",
        content=b"frozen",
    )
    barrier = threading.Barrier(2)

    def start_grading() -> str:
        barrier.wait(timeout=5)
        try:
            grading_repository.create_run_bundle(
                assignment.id,
                teacher_id=owner_id,
                revision_ids=[],
                setup={},
                setup_fingerprint="f" * 64,
                input_manifest={
                    "knowledge_document_ids": [uploaded.document_id],
                },
                workflow_expected_revision=workflow.workflow_revision,
            )
            return "grading_created"
        except InvalidTransition as exc:
            return exc.code

    def cleanup() -> str:
        barrier.wait(timeout=5)
        try:
            request_document_cleanup(uploaded.document_id, owner_id)
            return "cleanup_created"
        except InvalidTransition as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(start_grading), executor.submit(cleanup)]
        results = {future.result(timeout=10) for future in futures}
    assert results in (
        {"grading_created", "knowledge_document_in_active_grading_run"},
        {"cleanup_created", "knowledge_storage_input_unavailable"},
    )


def test_postgres_grading_and_source_admission_share_workflow_first_order(
    pg_database,
    tmp_path,
):
    """Grading W->User->ledger->A and source W->User->A both complete."""
    from backend.db import (
        assignment_repository,
        course_repository,
        grading_repository,
        source_storage_repository,
        workflow_repository,
    )
    from backend.services.knowledge_storage import persist_knowledge_upload
    from backend.storage.local import LocalStorage

    owner_id = _seed_user("teacher")
    course = course_repository.create_course(teacher_id=owner_id, name="C")
    assignment = assignment_repository.create_assignment(
        teacher_id=owner_id, course_id=course.id, name="A"
    )
    workflow = workflow_repository.ensure_workflow(
        assignment_id=assignment.id, owner_id=owner_id,
    )
    uploaded = persist_knowledge_upload(
        storage=LocalStorage(tmp_path / "grading-source"),
        owner_id=owner_id,
        original_name="frozen.txt",
        content=b"frozen",
    )
    barrier = threading.Barrier(2)

    def start_grading() -> str:
        barrier.wait(timeout=5)
        grading_repository.create_run_bundle(
            assignment.id,
            teacher_id=owner_id,
            revision_ids=[],
            setup={},
            setup_fingerprint="g" * 64,
            input_manifest={"knowledge_document_ids": [uploaded.document_id]},
            workflow_expected_revision=workflow.workflow_revision,
        )
        return "grading"

    def reserve_source() -> str:
        payload = b"source"
        barrier.wait(timeout=5)
        source_storage_repository.reserve_source_upload(
            file_id=f"pg-source-{uuid.uuid4().hex}",
            file_owner_id=owner_id,
            kind="problem_source",
            original_name="problem.pdf",
            storage_backend="local",
            storage_key=(
                f"assignments/{assignment.id}/{uuid.uuid4().hex}/problem.pdf"
            ),
            content_type="application/pdf",
            requested_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            assignment_id=assignment.id,
            submission_revision_id=None,
        )
        return "source"

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(start_grading), executor.submit(reserve_source)]
        assert sorted(future.result(timeout=10) for future in futures) == [
            "grading", "source",
        ]
