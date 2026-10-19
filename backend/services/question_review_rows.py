"""Teacher edits the question matrix; removed rows retain historical result links."""
import time
import uuid

from sqlalchemy import select, func
from backend.db import assignment_repository, workflow_repository
from backend.db.models import AssignmentQuestionRecord
from backend.db.session import session_scope
from backend.domain.errors import InvalidTransition, NotFound
from backend.services import task_facade


def _review_workflow(session, *, task_id, owner_id, expected_revision):
    workflow = workflow_repository.lock_workflow_revision(session,
        assignment_id=task_id, owner_id=owner_id, expected_revision=expected_revision)
    workflow_repository._lock_live_assignment(session, assignment_id=task_id, owner_id=owner_id)
    if workflow.active_job_id or workflow.last_failed_job_id or workflow.presentation_status != "problems_ready":
        raise InvalidTransition("Question rows can only change during question review.", code="workflow_busy")
    if task_facade._manual_count_required(workflow, "question"):
        raise InvalidTransition("Enter the question count before adding individual rows.")
    return workflow


def add_question(*, task_id: str, owner_id: str, expected_revision: int) -> dict:
    now = time.time()
    q_id = f"manual-{uuid.uuid4().hex[:12]}"
    with session_scope() as session:
        workflow = _review_workflow(session, task_id=task_id, owner_id=owner_id, expected_revision=expected_revision)
        last_order = session.scalar(select(func.max(AssignmentQuestionRecord.order_index)).where(
            AssignmentQuestionRecord.assignment_id == task_id))
        order = last_order + 1 if last_order is not None else 0
        number = str(order + 1)
        source = assignment_repository._normalise_question_source(
            {"origin": "teacher_review", "presentation": {"review_status": "needs_review"}},
            number=number, order_index=order, stem="", criterion="", max_score=10)
        session.add(AssignmentQuestionRecord(id=f"q_{uuid.uuid4().hex[:12]}", assignment_id=task_id,
            q_id=q_id, order_index=order, number=number, type="other", stem="", criterion="", max_score=10,
            reference_answer="", source=source, version=1, created_at=now, updated_at=now))
        workflow.workflow_revision += 1
        workflow.updated_at = now
        revision = workflow.workflow_revision
    return {"status": "ok", "q_id": q_id, "workflow_revision": revision}


def delete_question(*, task_id: str, owner_id: str, q_id: str, expected_revision: int) -> dict:
    now = time.time()
    with session_scope() as session:
        workflow = _review_workflow(session, task_id=task_id, owner_id=owner_id, expected_revision=expected_revision)
        question = session.scalar(select(AssignmentQuestionRecord).where(
            AssignmentQuestionRecord.assignment_id == task_id, AssignmentQuestionRecord.q_id == q_id,
            assignment_repository.active_question_clause()))
        if question is None:
            raise NotFound("question")
        # A physical DELETE cascades into old grades and answers. Retire only
        # the current matrix row; historical reports/results retain their FK.
        question.source = {**(question.source or {}), "removed_from_review": True}
        question.updated_at = now
        workflow.workflow_revision += 1
        workflow.updated_at = now
        revision = workflow.workflow_revision
    return {"status": "ok", "workflow_revision": revision}
