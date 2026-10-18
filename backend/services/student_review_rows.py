"""Add/remove task-local review rows while preserving source and grading history."""
import time
import uuid

from sqlalchemy import select

from backend.db import assignment_repository, workflow_repository
from backend.db.models import AssignmentQuestionRecord, SubmissionRecord, SubmissionRevisionRecord, SubmissionAnswerRecord
from backend.db.session import session_scope
from backend.domain.errors import InvalidTransition, NotFound
from backend.services import task_facade


def _review_workflow(session, *, task_id, owner_id, expected_revision):
    workflow = workflow_repository.lock_workflow_revision(session,
        assignment_id=task_id, owner_id=owner_id, expected_revision=expected_revision)
    workflow_repository._lock_live_assignment(session, assignment_id=task_id, owner_id=owner_id)
    if workflow.active_job_id or workflow.last_failed_job_id or workflow.presentation_status not in {"submissions_ready", "grading_setup"}:
        raise InvalidTransition("Student rows can only change during submission review.", code="workflow_busy")
    return workflow


def add_student(*, task_id: str, owner_id: str, expected_revision: int) -> dict:
    now = time.time()
    student_id = f"manual_{uuid.uuid4().hex[:16]}"
    display_id = f"manual-{uuid.uuid4().hex[:8]}"
    with session_scope() as session:
        workflow = _review_workflow(session, task_id=task_id, owner_id=owner_id, expected_revision=expected_revision)
        if task_facade._manual_count_required(workflow, "student"):
            raise InvalidTransition("Enter the student count before adding individual rows.")
        questions = session.scalars(select(AssignmentQuestionRecord).where(
            AssignmentQuestionRecord.assignment_id == task_id, assignment_repository.active_question_clause())).all()
        if not questions:
            raise InvalidTransition("problems_required")
        submission = SubmissionRecord(id=f"sub_{uuid.uuid4().hex[:12]}", assignment_id=task_id,
            student_id=student_id, current_revision_id=None, created_at=now, updated_at=now)
        session.add(submission)
        session.flush()
        revision = SubmissionRevisionRecord(id=f"rev_{uuid.uuid4().hex[:12]}", submission_id=submission.id,
            revision_number=1, source="teacher_import", file_name="", created_at=now)
        session.add(revision)
        session.flush()
        submission.current_revision_id = revision.id
        for question in questions:
            session.add(SubmissionAnswerRecord(id=f"ans_{uuid.uuid4().hex[:12]}", revision_id=revision.id,
                question_id=question.id, q_id=question.q_id, number=question.number, type=question.type,
                content="", flag=["recognition_manual_pending"], created_at=now))
        session.add(workflow_repository.AssignmentStudentPresentationRecord(id=f"sp_{uuid.uuid4().hex[:12]}",
            assignment_id=task_id, student_id=student_id, source_id=None, display_student_id=display_id,
            display_name="[Unknown Student]", source_filename="", identity_match_method="manual_review",
            identity_status="needs_review", is_active=True, created_at=now, updated_at=now))
        workflow.workflow_revision += 1
        workflow.presentation_status = "submissions_ready"
        workflow.updated_at = now
        updated_revision = workflow.workflow_revision
    return {"status": "ok", "student_id": display_id, "workflow_revision": updated_revision}


def delete_student(*, task_id: str, owner_id: str, student_id: str, expected_revision: int) -> dict:
    now = time.time()
    with session_scope() as session:
        workflow = _review_workflow(session, task_id=task_id, owner_id=owner_id, expected_revision=expected_revision)
        presentation = session.scalar(select(workflow_repository.AssignmentStudentPresentationRecord).where(
            workflow_repository.AssignmentStudentPresentationRecord.assignment_id == task_id,
            workflow_repository.AssignmentStudentPresentationRecord.display_student_id == student_id,
            workflow_repository.AssignmentStudentPresentationRecord.is_active.is_(True),
        ))
        if presentation is None:
            raise NotFound("student")
        # Deactivate only the current review row. Revisions, original files and
        # any historical grading/report references remain intact.
        presentation.is_active = False
        presentation.updated_at = now
        workflow.workflow_revision += 1
        workflow.presentation_status = "submissions_ready"
        workflow.updated_at = now
        updated_revision = workflow.workflow_revision
    return {"status": "ok", "workflow_revision": updated_revision}
