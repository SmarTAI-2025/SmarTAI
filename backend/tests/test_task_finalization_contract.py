from __future__ import annotations

import csv
from io import StringIO


def _prepared_task():
    from backend.db import (
        assignment_repository,
        course_repository,
        grading_repository,
        submission_repository,
    )
    from backend.db.models import UserRecord
    from backend.db.session import session_scope
    from backend.domain import education
    from backend.services import grading_runs, task_facade

    owner_id = "finalization-owner"
    student_id = "finalization-student"
    with session_scope() as session:
        session.add_all([
            UserRecord(
                id=owner_id, username=owner_id, password_hash="hash",
                role="teacher", is_active=True,
            ),
            UserRecord(
                id=student_id, username=student_id, password_hash="hash",
                role="student", is_active=True,
            ),
        ])
    task = task_facade.create_task(
        owner_id=owner_id,
        name="Formal result",
        semester_id=None,
        course_id=None,
        idempotency_key="formal-result-task",
    )
    task_id = task["task_id"]
    assignment = assignment_repository.get_assignment(task_id, actor_id=owner_id)
    course_repository.enroll(course_id=assignment.course_id, student_id=student_id)
    questions = [
        assignment_repository.add_question(
            assignment_id=task_id, teacher_id=owner_id,
            q_id=q_id, order_index=index, type="short", stem=q_id,
            max_score=10,
        )
        for index, q_id in enumerate(("q-required", "q-optional"))
    ]
    assignment_repository.publish(
        assignment_id=task_id,
        teacher_id=owner_id,
        expected_version=assignment.version,
    )
    submission = submission_repository.create_submission(
        assignment_id=task_id, student_id=student_id,
    )
    revision = submission_repository.add_revision(
        submission_id=submission.id,
        student_id=student_id,
        source=education.SubmissionRevisionSource.ONLINE.value,
        answers=[{
            "question_id": question.id,
            "q_id": question.q_id,
            "type": question.type,
            "content": "answer",
        } for question in questions],
    )
    run = grading_runs.create_run(teacher_id=owner_id, assignment_id=task_id)
    grading_repository.claim_lease(
        run_id=run.id, worker_id="finalization-worker", lease_seconds=60,
    )
    required = education.GradeResultDTO(
        id="",
        grading_run_id=run.id,
        submission_revision_id=revision.id,
        question_id=questions[0].id,
        student_id=student_id,
        q_id=questions[0].q_id,
        ai_score=6,
        ai_max_score=10,
        requires_review=True,
        review_reasons=["low_confidence"],
        result_status=education.GradeResultStatus.NEEDS_REVIEW.value,
        created_at=0,
        updated_at=0,
    )
    optional = education.GradeResultDTO(
        id="",
        grading_run_id=run.id,
        submission_revision_id=revision.id,
        question_id=questions[1].id,
        student_id=student_id,
        q_id=questions[1].q_id,
        ai_score=9,
        ai_max_score=10,
        requires_review=False,
        result_status=education.GradeResultStatus.GRADED.value,
        created_at=0,
        updated_at=0,
    )
    required_row = grading_repository.upsert_result(
        run.id, worker_id="finalization-worker", grade_result=required,
    )
    optional_row = grading_repository.upsert_result(
        run.id, worker_id="finalization-worker", grade_result=optional,
    )
    grading_repository.mark_completed(
        run.id, worker_id="finalization-worker", completed=1, failed=0,
    )
    return {
        "owner_id": owner_id,
        "student_id": student_id,
        "task_id": task_id,
        "run_id": run.id,
        "required_result_id": required_row.id,
        "optional_result_id": optional_row.id,
    }


def test_required_review_audit_counts_only_originally_required_results():
    from backend.db import grading_repository
    from backend.services import task_facade

    seeded = _prepared_task()
    before = task_facade.finalization(
        task_id=seeded["task_id"], owner_id=seeded["owner_id"],
    )
    assert (
        before["required_review_count"],
        before["confirmed_required_count"],
        before["remaining_review_count"],
    ) == (1, 0, 0)

    grading_repository.add_teacher_review(
        seeded["required_result_id"],
        teacher_id=seeded["owner_id"],
        new_score=7,
        new_comment="required confirmed",
        confirm=True,
    )
    grading_repository.add_teacher_review(
        seeded["optional_result_id"],
        teacher_id=seeded["owner_id"],
        new_score=9.5,
        new_comment="optional override",
        confirm=True,
    )
    after = task_facade.finalization(
        task_id=seeded["task_id"], owner_id=seeded["owner_id"],
    )
    assert (
        after["required_review_count"],
        after["confirmed_required_count"],
        after["remaining_review_count"],
    ) == (1, 1, 0)


def test_soft_review_ai_score_is_publishable_and_exported_by_default():
    from backend.services import task_facade

    seeded = _prepared_task()
    final = task_facade.confirm_finalization(
        task_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        expected_revision=0,
    )
    assert final["status"] == "ok"
    assert final["required_review_count"] == 1
    assert final["confirmed_required_count"] == 0
    assert final["remaining_review_count"] == 0

    snapshot = task_facade.result_snapshot(
        task_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        result_version=1,
    )
    corrections = snapshot["payload"]["results"][0]["corrections"]
    by_question = {row["q_id"]: row for row in corrections}
    assert by_question["q-required"]["score"] == 6
    assert by_question["q-required"]["teacher_score"] is None
    assert by_question["q-required"]["review_status"] == "pending"

    generated = task_facade.generate_artifacts(
        task_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        expected_revision=1,
    )
    assert generated["status"] == "ok"
    body, media_type, _filename = task_facade.artifact_bytes(
        task_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        version=1,
        artifact_id="grades_csv",
    )
    assert media_type == "text/csv; charset=utf-8"
    rows = list(csv.DictReader(StringIO(body.decode("utf-8-sig"))))
    assert len(rows) == 1
    assert rows[0]["total_score"] == "15"
    assert rows[0]["q-required_score"] == "6"
    assert rows[0]["q-optional_score"] == "9"


def test_finalization_artifacts_and_dirty_state_are_idempotent():
    from backend.db import grading_repository
    from backend.services import task_facade

    seeded = _prepared_task()
    grading_repository.add_teacher_review(
        seeded["required_result_id"],
        teacher_id=seeded["owner_id"],
        new_score=7,
        new_comment="confirmed",
        confirm=True,
    )
    first = task_facade.confirm_finalization(
        task_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        expected_revision=0,
    )
    replay = task_facade.confirm_finalization(
        task_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        expected_revision=0,
    )
    assert first["status"] == "ok"
    assert first["final_result_version"] == 1
    assert replay["status"] == "already_done"
    assert replay["workflow_revision"] == first["workflow_revision"] == 1

    generated = task_facade.generate_artifacts(
        task_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        expected_revision=1,
    )
    generated_replay = task_facade.generate_artifacts(
        task_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        expected_revision=1,
    )
    assert generated["status"] == "ok"
    assert generated["artifacts"]["versions"][0]["status"] == "ready"
    assert generated_replay["status"] == "already_done"
    assert generated_replay["workflow_revision"] == generated["workflow_revision"] == 2

    bundle, media_type, filename = task_facade.artifact_bytes(
        task_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        version=1,
        artifact_id="bundle",
    )
    assert bundle.startswith(b"PK")
    assert media_type == "application/zip"
    assert filename.endswith(".zip")

    edited = task_facade.update_correction_review(
        task_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        display_student_id=seeded["student_id"],
        q_id="q-optional",
        teacher_score=8.5,
        teacher_comment="new draft",
        confirm=False,
        expected_revision=2,
    )
    assert edited["workflow_revision"] == 3
    stale_index = task_facade.artifact_index(
        task_id=seeded["task_id"], owner_id=seeded["owner_id"],
    )
    assert stale_index["versions"][0]["status"] == "stale"
