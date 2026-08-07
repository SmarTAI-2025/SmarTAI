"""Normalized implementation of the Figma task presentation contract.

``task_id`` is the normalized assignment id.  This service translates between
the presentation DTO expected by the React app and the existing
course/assignment/submission/grading repositories.  It intentionally does not
recreate the removed TaskStore or JobStore.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import delete, func, select, update

from backend.agents.ingest_agent import extract_problems, parse_student_answers
from backend.db import (
    assignment_repository,
    course_repository,
    grading_repository,
    submission_repository,
    workflow_repository,
)
from backend.db.models import (
    AssignmentQuestionRecord,
    AssignmentRecord,
    CourseEnrollmentRecord,
    CourseRecord,
    GradingRunRecord,
    SubmissionAnswerRecord,
    SubmissionRecord,
    SubmissionRevisionRecord,
    UserRecord,
)
from backend.db.session import session_scope
from backend.domain import education
from backend.domain.errors import (
    DomainError,
    InvalidTransition,
    NotFound,
    ValidationError,
    VersionConflict,
)
from backend.models import TaskGradingSetup
from backend.progress.tracker import get_or_create_reporter, get_reporter, remove_reporter
from backend.services import grading_runs
from backend.services.result_artifacts import (
    ARTIFACT_SCHEMA_VERSION,
    build_artifact_bundle,
    build_artifact_files,
    build_artifact_manifest,
)
from backend.skills.ocr_ingest import LLMVisionOCRSkill
from backend.tools.file_processing import extract_files_from_archive, extract_text_from_upload


SYSTEM_COURSE_CODE = "__SMARTAI_UNASSIGNED__"
SYSTEM_COURSE_NAME = "SmarTAI Workspace"
_SAFE_ERROR_CODES = {
    "no_provider_configured",
    "recognition_provider_not_enabled",
    "problem_extraction_failed",
    "material_import_failed",
    "ai_completion_failed",
    "replacement_confirmation_required",
    "stale_revision",
    "submission_parse_failed",
    "grading_failed",
    "unknown_ai_completion_target",
    "workflow_busy",
    "workflow_revision_conflict",
}
_AUXILIARY_QUESTION_OPERATION_TYPES = {"material_import", "ai_completion"}
logger = logging.getLogger(__name__)


def _hash_json(value: Any) -> str:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _ensure_system_course(owner_id: str) -> str:
    with session_scope() as session:
        existing = session.scalar(
            select(CourseRecord).where(
                CourseRecord.teacher_id == owner_id,
                CourseRecord.code == SYSTEM_COURSE_CODE,
            )
        )
        if existing is not None:
            return existing.id
        now = time.time()
        course_id = f"course_system_{uuid.uuid4().hex[:10]}"
        session.add(CourseRecord(
            id=course_id,
            name=SYSTEM_COURSE_NAME,
            code=SYSTEM_COURSE_CODE,
            description="Internal course for tasks without a selected course.",
            teacher_id=owner_id,
            created_at=now,
            updated_at=now,
        ))
        return course_id


def create_task(
    *, owner_id: str, name: str, semester_id: str | None, course_id: str | None,
    idempotency_key: str, tag_ids: list[str] | None = None,
) -> dict:
    normalized_name = name.strip()
    if not normalized_name:
        raise ValidationError("task_name_required")
    if not idempotency_key or len(idempotency_key) > 160:
        raise ValidationError("idempotency_key_required")
    request_hash = _hash_json({
        "name": normalized_name,
        "semester_id": semester_id,
        "course_id": course_id,
        "tag_ids": sorted(set(tag_ids or [])),
    })
    from backend.services.task_creation import create_task_bundle

    assignment_id, _created = create_task_bundle(
        owner_id=owner_id,
        name=normalized_name,
        semester_id=semester_id,
        course_id=course_id,
        tag_ids=tag_ids or [],
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        system_course_code=SYSTEM_COURSE_CODE,
        system_course_name=SYSTEM_COURSE_NAME,
    )
    return get_task(task_id=assignment_id, owner_id=owner_id, full=False)


def update_task(
    *, task_id: str, owner_id: str, name: str | None = None,
    semester_id: str | None | object = ..., course_id: str | None | object = ...,
    tag_ids: list[str] | None = None,
) -> dict:
    assignment = assignment_repository.get_assignment(task_id, actor_id=owner_id)
    changes: dict[str, Any] = {}
    if name is not None:
        normalized_name = name.strip()
        if not normalized_name:
            raise ValidationError("task_name_required")
        changes["name"] = normalized_name
    if course_id is not ...:
        resolved_course = course_id or _ensure_system_course(owner_id)
        course_repository.get_course(resolved_course, actor_id=owner_id)
        changes["course_id"] = resolved_course
    if changes:
        with session_scope() as session:
            row = session.scalar(select(AssignmentRecord).where(
                AssignmentRecord.id == task_id,
                AssignmentRecord.teacher_id == owner_id,
            ))
            if row is None:
                raise NotFound("assignment")
            for key, value in changes.items():
                setattr(row, key, value)
            row.version += 1
            row.updated_at = time.time()
    workflow_changes: dict[str, Any] = {}
    if semester_id is not ...:
        workflow_changes["semester_id"] = semester_id
    workflow_repository.update_workflow(
        task_id, owner_id=owner_id, **workflow_changes
    )
    if tag_ids is not None:
        _set_task_tags(task_id, owner_id, tag_ids)
    return get_task(task_id=task_id, owner_id=owner_id, full=False)


def delete_task(*, task_id: str, owner_id: str) -> None:
    assignment_repository.get_assignment(task_id, actor_id=owner_id)
    with session_scope() as session:
        result = session.execute(delete(AssignmentRecord).where(
            AssignmentRecord.id == task_id,
            AssignmentRecord.teacher_id == owner_id,
        ))
        if result.rowcount != 1:
            raise NotFound("assignment")


def list_tasks(*, owner_id: str) -> dict[str, dict]:
    with session_scope() as session:
        assignments = session.scalars(
            select(AssignmentRecord)
            .where(AssignmentRecord.teacher_id == owner_id)
            .order_by(AssignmentRecord.updated_at.desc())
        ).all()
        ids = [row.id for row in assignments]
    output: dict[str, dict] = {}
    for task_id in ids:
        try:
            output[task_id] = get_task(task_id=task_id, owner_id=owner_id, full=False)
        except NotFound:
            continue
    return output


def get_task(*, task_id: str, owner_id: str, full: bool = True) -> dict:
    assignment = assignment_repository.get_assignment(task_id, actor_id=owner_id)
    try:
        workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    except NotFound:
        workflow = workflow_repository.ensure_workflow(
            assignment_id=task_id, owner_id=owner_id
        )
    questions = assignment_repository.list_questions(task_id, teacher_id=owner_id)
    submissions = _active_submissions(task_id, owner_id)
    runs = grading_repository.list_runs_for_assignment(task_id, actor_id=owner_id)
    latest_run = runs[-1] if runs else None
    status = _presentation_status(workflow, questions, submissions, latest_run)
    selected_docs = _selected_knowledge(task_id, owner_id)
    tag_ids = _get_task_tags(task_id, owner_id)
    attention = bool(
        workflow.error_code
        or (latest_run and latest_run.status in {"failed", "partial_failed"})
        or (latest_run and grading_repository.has_review_queue_items(latest_run.id))
    )
    final_version = max(
        workflow.final_result_version,
        1 if latest_run and latest_run.released_at is not None else 0,
    )
    has_released_run = any(run.released_at is not None for run in runs)
    final_result_dirty = bool(
        has_released_run
        and latest_run is not None
        and latest_run.released_at is None
    )
    payload: dict[str, Any] = {
        "task_id": assignment.id,
        "name": assignment.name,
        "owner_id": assignment.teacher_id,
        "status": status,
        "workflow_revision": workflow.workflow_revision,
        "semester_id": workflow.semester_id,
        "course_id": None if _is_system_course(assignment.course_id) else assignment.course_id,
        "tag_ids": tag_ids,
        "needs_attention": attention,
        "extract_job_id": workflow.extract_job_id,
        "parse_job_id": workflow.parse_job_id,
        "grading_job_id": latest_run.id if latest_run else workflow.grading_job_id,
        "last_failed_job_id": workflow.last_failed_job_id,
        "problem_file_name": workflow.problem_file_name,
        "submission_file_name": workflow.submission_file_name,
        "pending_submission_file_name": workflow.pending_submission_file_name,
        "submission_identity_mode": workflow.submission_identity_mode,
        "submission_roster_name": workflow.submission_roster_name,
        "submission_recognition_provider_id": workflow.submission_recognition_provider_id,
        "reference_file_name": workflow.reference_file_name,
        "test_cases_file_name": workflow.test_cases_file_name,
        "reference_parse_job_id": None,
        "test_cases_parse_job_id": None,
        "ai_completion_job_id": None,
        "last_ai_completion_job_id": None,
        "ai_completion_error": None,
        "grading_setup_configured": workflow.grading_setup is not None,
        "final_result_version": final_version,
        "final_result_updated_at": workflow.final_result_updated_at or (
            latest_run.released_at if latest_run else None
        ),
        "final_result_dirty": final_result_dirty,
        "analysis_status": workflow.analysis_status,
        "analysis_result_version": workflow.analysis_result_version,
        "analysis_generated_at": workflow.analysis_generated_at,
        "analysis_error": workflow.analysis_error_code,
        "problem_count": len(questions),
        "student_count": len(submissions),
        "kb_docs": selected_docs,
        "kb_doc_count": len(selected_docs),
        "error": workflow.error_code,
        "created_at": assignment.created_at,
        "updated_at": max(assignment.updated_at, workflow.updated_at),
    }
    if full:
        payload["problem_data"] = {
            question.q_id: _serialize_problem(question) for question in questions
        }
        payload["student_data"] = _serialize_student_data(
            task_id, owner_id, submissions
        )
    return payload


def _presentation_status(workflow, questions, submissions, latest_run) -> str:
    if workflow.presentation_status in {
        "extracting_problems", "parsing_submissions", "generating_analysis", "error"
    }:
        return workflow.presentation_status
    if latest_run is not None:
        if latest_run.status in {"queued", "running"}:
            return "grading"
        if latest_run.status == "failed":
            return "error"
        if latest_run.released_at is not None:
            return "finalized"
        if latest_run.status in {"completed", "partial_failed"}:
            return "graded"
    if submissions:
        return "submissions_ready"
    if questions:
        return "problems_ready"
    return "draft"


def _serialize_problem(question) -> dict:
    presentation = dict((question.source or {}).get("presentation") or {})
    max_score = float(question.max_score)
    return {
        "q_id": question.q_id,
        "number": question.number,
        "type": question.type,
        "stem": question.stem,
        "criterion": question.criterion,
        "max_score": max_score,
        "max_score_source": presentation.get("max_score_source") or (
            "default_10" if max_score == 10 else "legacy"
        ),
        "max_score_review_status": presentation.get(
            "max_score_review_status", "needs_review"
        ),
        "review_status": presentation.get("review_status", "needs_review"),
        "reference_answer": question.reference_answer,
        "solution_code": presentation.get("solution_code"),
        "test_cases": question.test_cases,
        "material_provenance": presentation.get("material_provenance", {}),
        "ai_completion_provenance": presentation.get("ai_completion_provenance", {}),
        "preparation_issues": presentation.get("preparation_issues", []),
    }


def _serialize_student_data(task_id: str, owner_id: str, submissions) -> dict[str, dict]:
    presentations = workflow_repository.list_student_presentations(task_id)
    revisions = []
    for submission in submissions:
        if submission.current_revision_id:
            revisions.append((submission, submission_repository.get_revision(
                revision_id=submission.current_revision_id, actor_id=owner_id
            )))
    answer_ids = [answer.id for _, revision in revisions for answer in revision.answers]
    review_statuses = workflow_repository.answer_review_statuses(answer_ids)
    output: dict[str, dict] = {}
    for submission, revision in revisions:
        presentation = presentations.get(submission.student_id)
        display_id = presentation.display_student_id if presentation else submission.student_id
        output[display_id] = {
            "stu_id": display_id,
            "stu_name": presentation.display_name if presentation else submission.student_id,
            "stu_ans": [
                {
                    "q_id": answer.q_id,
                    "number": answer.number,
                    "type": answer.type,
                    "content": answer.content,
                    "flag": list(answer.flag or []),
                    "review_status": review_statuses.get(answer.id, "pending"),
                }
                for answer in revision.answers
            ],
            "source_filename": (
                presentation.source_filename if presentation else revision.file_name
            ),
            "identity_match_method": (
                presentation.identity_match_method if presentation else "filename"
            ),
            "identity_status": presentation.identity_status if presentation else "matched",
        }
    return output


def _active_submissions(task_id: str, owner_id: str):
    submissions = submission_repository.list_submissions(task_id, actor_id=owner_id)
    presentations = workflow_repository.list_student_presentations(task_id)
    inactive_ids = {
        student_id
        for student_id, presentation in presentations.items()
        if not presentation.is_active
    }
    return [
        submission for submission in submissions
        if submission.student_id not in inactive_ids
    ]


def _selected_knowledge(task_id: str, owner_id: str) -> dict[str, dict]:
    from backend.db import course_library_repository
    from backend.db.knowledge_repository import (
        list_selected_documents,
        selected_document_metadata,
    )

    metadata = selected_document_metadata(task_id, owner_id)
    output: dict[str, dict] = {}
    for document in list_selected_documents(task_id, owner_id):
        attachment = metadata.get(document.id, {})
        material_id = attachment.get("library_material_id")
        if material_id is None:
            material = course_library_repository.get_material_by_document(
                document.id, owner_id
            )
            material_id = material.material_id if material is not None else None
        output[document.id] = {
            "doc_id": document.id,
            "filename": document.original_name,
            "chunk_count": document.chunk_count,
            "uploaded_at": document.created_at,
            "source_kind": attachment.get("source_kind") or "upload",
            "library_material_id": material_id,
            "saved_to_library": material_id is not None,
        }
    return output


def _is_system_course(course_id: str) -> bool:
    with session_scope() as session:
        code = session.scalar(select(CourseRecord.code).where(CourseRecord.id == course_id))
        return code == SYSTEM_COURSE_CODE


def _operation_state(operation) -> str:
    return (
        "already_running"
        if operation.status in {"pending", "running"}
        else "already_done"
    )


def _operation_is_retryable(operation, *, now: float | None = None) -> bool:
    if operation.status == "error":
        return True
    current_time = time.time() if now is None else now
    return bool(
        operation.status in {"pending", "running"}
        and operation.expires_at is not None
        and operation.expires_at <= current_time
    )


def retryable_operation_claim_revision(
    *, workflow, replay, requested_revision: int,
) -> int:
    """Resolve the CAS base for an exact retry without hiding real edits.

    Claiming an operation is itself a workflow mutation, so a failed/expired
    attempt leaves the task exactly one revision ahead of the base stored in
    that attempt.  An identical retry may consume that internal claim bump;
    any additional revision means some other task mutation occurred and the
    original client request must remain stale.
    """
    current_revision = workflow.workflow_revision
    if current_revision == requested_revision:
        return requested_revision
    if replay is None or not _operation_is_retryable(replay):
        _raise_stale_revision()
    payload = dict(replay.payload or {})
    previous_claim_base = payload.get("base_workflow_revision")
    if isinstance(previous_claim_base, bool) or not isinstance(previous_claim_base, int):
        _raise_stale_revision()
    same_operation_owns_delta = (
        workflow.last_failed_job_id == replay.id
        or workflow.active_job_id == replay.id
    )
    if (
        not same_operation_owns_delta
        or current_revision != previous_claim_base + 1
    ):
        _raise_stale_revision()
    return current_revision


def find_task_operation(
    *, task_id: str, owner_id: str, operation_type: str, input_hash: str,
):
    with session_scope() as session:
        row = session.scalar(select(workflow_repository.WorkflowOperationRecord).where(
            workflow_repository.WorkflowOperationRecord.assignment_id == task_id,
            workflow_repository.WorkflowOperationRecord.owner_id == owner_id,
            workflow_repository.WorkflowOperationRecord.operation_type == operation_type,
            workflow_repository.WorkflowOperationRecord.input_hash == input_hash,
        ))
        if row is not None:
            session.expunge(row)
        return row


def _cas_operation_attempt_for_write(
    session, *, task_id: str, owner_id: str, operation_id: str,
    expected_operation_attempt: int, expected_statuses: tuple[str, ...],
    changes: dict[str, Any] | None = None,
):
    """Lock one operation generation through an attempt-and-status CAS.

    The first write deliberately happens before related workflow/domain writes.
    A retry that increments ``attempt`` therefore cannot race an already
    validated worker and then be overwritten by that worker's ORM flush.
    """
    now = time.time()
    claimed = session.execute(
        update(workflow_repository.WorkflowOperationRecord)
        .where(
            workflow_repository.WorkflowOperationRecord.id == operation_id,
            workflow_repository.WorkflowOperationRecord.assignment_id == task_id,
            workflow_repository.WorkflowOperationRecord.owner_id == owner_id,
            workflow_repository.WorkflowOperationRecord.attempt
            == expected_operation_attempt,
            workflow_repository.WorkflowOperationRecord.status.in_(expected_statuses),
        )
        .values(**(changes or {}), updated_at=now)
    )
    if claimed.rowcount != 1:
        current = session.scalar(select(
            workflow_repository.WorkflowOperationRecord
        ).where(
            workflow_repository.WorkflowOperationRecord.id == operation_id,
            workflow_repository.WorkflowOperationRecord.owner_id == owner_id,
        ))
        if current is None or current.assignment_id != task_id:
            raise NotFound("workflow_operation")
        if current.attempt != expected_operation_attempt:
            raise VersionConflict(
                "A newer workflow operation attempt is active.",
                code="stale_operation_attempt",
            )
        raise InvalidTransition(
            "The workflow job is not in the expected state.", code="workflow_busy"
        )
    operation = session.scalar(select(
        workflow_repository.WorkflowOperationRecord
    ).where(
        workflow_repository.WorkflowOperationRecord.id == operation_id,
        workflow_repository.WorkflowOperationRecord.owner_id == owner_id,
        workflow_repository.WorkflowOperationRecord.attempt
        == expected_operation_attempt,
    ))
    assert operation is not None
    return operation


def claim_workflow_operation_atomic(
    *, task_id: str, owner_id: str, operation_id: str,
    expected_operation_attempt: int, expected_workflow_revision: int,
    workflow_changes: dict[str, Any],
) -> int:
    """CAS the task revision and transition its durable job to running together."""
    now = time.time()
    allowed = {
        column.name
        for column in workflow_repository.AssignmentWorkflowRecord.__table__.columns
        if column.name not in {
            "assignment_id", "owner_id", "created_at", "updated_at",
            "workflow_revision",
        }
    }
    values = {
        key: value for key, value in workflow_changes.items() if key in allowed
    }
    with session_scope() as session:
        operation = _cas_operation_attempt_for_write(
            session, task_id=task_id, owner_id=owner_id,
            operation_id=operation_id,
            expected_operation_attempt=expected_operation_attempt,
            expected_statuses=("pending",),
            changes={"status": "running", "error_code": None},
        )
        if operation.operation_type in _AUXILIARY_QUESTION_OPERATION_TYPES:
            values["presentation_status"] = "problems_ready"
            values["error_code"] = None
        claimed = session.execute(
            update(workflow_repository.AssignmentWorkflowRecord)
            .where(
                workflow_repository.AssignmentWorkflowRecord.assignment_id == task_id,
                workflow_repository.AssignmentWorkflowRecord.owner_id == owner_id,
                workflow_repository.AssignmentWorkflowRecord.workflow_revision
                == expected_workflow_revision,
            )
            .values(
                **values,
                workflow_revision=(
                    workflow_repository.AssignmentWorkflowRecord.workflow_revision + 1
                ),
                updated_at=now,
            )
        )
        if claimed.rowcount != 1:
            _raise_stale_revision()
        session.flush()
        return expected_workflow_revision + 1


def _detail_error(error: DomainError, fallback: str) -> str:
    return error.code if error.code != "domain_error" else fallback


def _raise_stale_revision() -> None:
    raise VersionConflict("The task changed while this operation was running.", code="stale_revision")


def _raise_replacement_confirmation_required() -> None:
    raise InvalidTransition(
        "Existing confirmed data can only be replaced after explicit confirmation.",
        code="replacement_confirmation_required",
    )


def _ensure_no_other_active_operation(
    *, task_id: str, owner_id: str, operation_type: str, input_hash: str,
):
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    if not workflow.active_job_id:
        return workflow, None
    try:
        active = workflow_repository.get_operation(
            workflow.active_job_id, owner_id=owner_id
        )
    except NotFound:
        raise InvalidTransition("The task is busy.", code="workflow_busy")
    if active.status not in {"pending", "running"} or _operation_is_retryable(active):
        return workflow, None
    if active.operation_type == operation_type and active.input_hash == input_hash:
        return workflow, active
    raise InvalidTransition("The task is busy.", code="workflow_busy")


def _has_draft_questions(task_id: str) -> bool:
    with session_scope() as session:
        return bool(session.scalar(
            select(AssignmentQuestionRecord.id)
            .where(AssignmentQuestionRecord.assignment_id == task_id)
            .limit(1)
        ))


def queue_task_problem_extraction(
    *, task_id: str, owner_id: str, filename: str, content: bytes,
    content_type: str | None, registry, input_hash: str | None = None,
    expected_workflow_revision: int | None = None,
    replace_confirmed: bool = False,
    extraction_options: dict[str, Any] | None = None,
) -> dict:
    """Persist and claim a problem-extraction job without doing OCR/LLM work."""
    assignment = assignment_repository.get_assignment(task_id, actor_id=owner_id)
    if assignment.status not in education.EDITABLE_ASSIGNMENT_STATUSES:
        raise InvalidTransition("assignment_not_editable")
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    base_revision = (
        workflow.workflow_revision
        if expected_workflow_revision is None
        else expected_workflow_revision
    )
    digest = _hash_json({
        "source": input_hash or hashlib.sha256(content).hexdigest(),
        "base_revision": base_revision,
        "replace_confirmed": replace_confirmed,
        "extraction_options": extraction_options or {},
    })
    replay = find_task_operation(
        task_id=task_id, owner_id=owner_id,
        operation_type="problem_extraction", input_hash=digest,
    )
    if replay is not None and not _operation_is_retryable(replay):
        return {
            "status": _operation_state(replay), "task_id": task_id,
            "job_id": replay.id, "workflow_revision": workflow.workflow_revision,
        }
    claim_base_revision = retryable_operation_claim_revision(
        workflow=workflow, replay=replay, requested_revision=base_revision,
    )
    if _has_draft_questions(task_id) and not replace_confirmed:
        _raise_replacement_confirmation_required()
    if registry.pick_default() is None:
        raise ValidationError(
            "No enabled provider is available.", code="no_provider_configured"
        )
    workflow, active = _ensure_no_other_active_operation(
        task_id=task_id, owner_id=owner_id,
        operation_type="problem_extraction", input_hash=digest,
    )
    if active is not None:
        return {
            "status": "already_running", "task_id": task_id,
            "job_id": active.id, "workflow_revision": workflow.workflow_revision,
        }
    operation, created = workflow_repository.create_operation(
        assignment_id=task_id,
        owner_id=owner_id,
        operation_type="problem_extraction",
        input_hash=digest,
        payload={
            "filename": filename, "content_type": content_type,
            "base_workflow_revision": claim_base_revision,
            "replace_confirmed": replace_confirmed,
            "extraction_options": extraction_options or {},
        },
        expires_at=time.time() + 2 * 60 * 60,
    )
    if not created:
        return {
            "status": _operation_state(operation), "task_id": task_id,
            "job_id": operation.id, "workflow_revision": workflow.workflow_revision,
        }
    remove_reporter(operation.id)
    try:
        claimed_revision = claim_workflow_operation_atomic(
            task_id=task_id, owner_id=owner_id, operation_id=operation.id,
            expected_operation_attempt=operation.attempt,
            expected_workflow_revision=claim_base_revision,
            workflow_changes={
                "presentation_status": "extracting_problems",
                "active_operation": "problem_extraction",
                "active_job_id": operation.id, "extract_job_id": operation.id,
                "problem_file_name": filename, "error_code": None,
            },
        )
    except VersionConflict:
        workflow_repository.update_operation(
            operation.id, owner_id=owner_id,
            expected_attempt=operation.attempt, status="error",
            error_code="stale_revision", completed_at=time.time(),
        )
        _raise_stale_revision()
    return {
        "status": "started", "task_id": task_id, "job_id": operation.id,
        "workflow_revision": claimed_revision,
        "_job_attempt": operation.attempt,
    }


async def run_task_problem_extraction(
    *, task_id: str, owner_id: str, job_id: str, filename: str,
    content: bytes, registry, job_attempt: int,
    claimed_workflow_revision: int,
    replace_confirmed: bool, extraction_options: dict[str, Any] | None = None,
) -> None:
    """Run a previously claimed extraction job and durably record its outcome."""
    try:
        provider = registry.pick_default()
        if provider is None:
            raise ValidationError(
                "No enabled provider is available.", code="no_provider_configured"
            )
        vision = registry.pick_vision(provider)
        ocr_skill = LLMVisionOCRSkill(vision) if vision is not None else None
        reporter = get_or_create_reporter(job_id)
        await reporter.configure_workflow(
            "problem_recognition",
            ("reading_source", "recognizing_structure", "validating_questions", "completed"),
        )
        await reporter.set_phase("extracting")
        await reporter.set_stage_progress(
            "reading_source", total_steps=4, completed_steps=0,
            message="Reading problem source.",
        )
        text = await extract_text_from_upload(
            content, filename, ocr_skill=ocr_skill, purpose="problems", reporter=reporter
        )
        problem_data: dict[str, dict] = {}
        await extract_problems(
            text, provider, problem_data, reporter=reporter,
            structure_mode=str((extraction_options or {}).get("structure_mode") or "organized"),
            extraction_hint=str((extraction_options or {}).get("extraction_hint") or ""),
            confirmed_candidates=list((extraction_options or {}).get("confirmed_candidates") or []),
            manage_progress_lifecycle=False,
        )
        await reporter.set_stage_progress(
            "validating_questions", total_steps=4, completed_steps=3,
            message="Validating recognized questions.",
        )
        await reporter.set_stage_progress(
            "completed", total_steps=4, completed_steps=4,
            message="Problem recognition completed.",
        )
        await reporter.set_phase("done")
        snapshot = (await reporter.snapshot()).model_dump(mode="json")
        _replace_draft_questions(
            task_id, owner_id, problem_data, filename,
            expected_workflow_revision=claimed_workflow_revision,
            replace_confirmed=replace_confirmed,
            operation_id=job_id,
            expected_operation_attempt=job_attempt,
            operation_progress=snapshot,
        )
    except DomainError as exc:
        _fail_operation(
            task_id, owner_id, job_id, job_attempt,
            _detail_error(exc, "problem_extraction_failed"),
        )
    except Exception:
        logger.warning("Background problem extraction failed; job_id=%s", job_id)
        _fail_operation(
            task_id, owner_id, job_id, job_attempt, "problem_extraction_failed"
        )


def _replace_draft_questions(
    task_id: str, owner_id: str, problem_data: dict[str, dict], filename: str,
    *, expected_workflow_revision: int | None = None,
    replace_confirmed: bool = False, operation_id: str | None = None,
    expected_operation_attempt: int | None = None,
    operation_progress: dict | None = None,
) -> int:
    """Atomically CAS the workflow and replace the complete draft question set."""
    now = time.time()
    with session_scope() as session:
        operation = None
        if operation_id is not None:
            if expected_operation_attempt is None:
                raise ValidationError("operation_attempt_required")
            operation = _cas_operation_attempt_for_write(
                session, task_id=task_id, owner_id=owner_id,
                operation_id=operation_id,
                expected_operation_attempt=expected_operation_attempt,
                expected_statuses=("running",),
            )
        assignment = session.scalar(select(AssignmentRecord).where(
            AssignmentRecord.id == task_id,
            AssignmentRecord.teacher_id == owner_id,
            AssignmentRecord.status.in_(list(education.EDITABLE_ASSIGNMENT_STATUSES)),
        ))
        if assignment is None:
            raise InvalidTransition("assignment_not_editable")
        existing = session.scalars(select(AssignmentQuestionRecord).where(
            AssignmentQuestionRecord.assignment_id == task_id
        )).all()
        if existing and not replace_confirmed:
            _raise_replacement_confirmation_required()
        workflow = session.scalar(select(workflow_repository.AssignmentWorkflowRecord).where(
            workflow_repository.AssignmentWorkflowRecord.assignment_id == task_id,
            workflow_repository.AssignmentWorkflowRecord.owner_id == owner_id,
        ))
        if workflow is None:
            raise NotFound("workflow")
        expected = (
            workflow.workflow_revision
            if expected_workflow_revision is None
            else expected_workflow_revision
        )
        result = session.execute(
            update(workflow_repository.AssignmentWorkflowRecord)
            .where(
                workflow_repository.AssignmentWorkflowRecord.assignment_id == task_id,
                workflow_repository.AssignmentWorkflowRecord.owner_id == owner_id,
                workflow_repository.AssignmentWorkflowRecord.workflow_revision == expected,
            )
            .values(
                workflow_revision=workflow_repository.AssignmentWorkflowRecord.workflow_revision + 1,
                presentation_status="problems_ready", active_operation=None,
                active_job_id=None, error_code=None, updated_at=now,
            )
        )
        if result.rowcount != 1:
            _raise_stale_revision()
        session.execute(delete(AssignmentQuestionRecord).where(
            AssignmentQuestionRecord.assignment_id == task_id
        ))
        for index, (q_id, raw) in enumerate(problem_data.items()):
            source = {
                "origin": "figma_task_facade",
                "filename": filename,
                "presentation": {
                    "review_status": raw.get("review_status", "needs_review"),
                    "max_score_source": raw.get("max_score_source") or (
                        "default_10"
                        if float(raw.get("max_score") or 10) == 10
                        else "legacy"
                    ),
                    "max_score_review_status": raw.get(
                        "max_score_review_status", "needs_review"
                    ),
                    "solution_code": raw.get("solution_code"),
                    "material_provenance": raw.get("material_provenance", {}),
                    "ai_completion_provenance": raw.get("ai_completion_provenance", {}),
                    "preparation_issues": raw.get("preparation_issues", []),
                },
            }
            session.add(AssignmentQuestionRecord(
                id=f"q_{uuid.uuid4().hex[:12]}", assignment_id=task_id,
                q_id=str(raw.get("q_id") or q_id), order_index=index,
                number=str(raw.get("number") or index + 1),
                type=str(raw.get("type") or "其他"),
                stem=str(raw.get("stem") or ""),
                criterion=str(raw.get("criterion") or ""),
                max_score=float(raw.get("max_score") or 10),
                reference_answer=raw.get("reference_answer"),
                test_cases=raw.get("test_cases"), source=source,
                version=1, created_at=now, updated_at=now,
            ))
        assignment.status = education.AssignmentStatus.READY.value
        assignment.version += 1
        assignment.updated_at = now
        if operation is not None:
            payload = dict(operation.payload or {})
            payload.update({"filename": filename, "problem_count": len(problem_data)})
            operation.status = "done"
            operation.progress = operation_progress or {}
            operation.payload = payload
            operation.error_code = None
            operation.completed_at = now
            operation.updated_at = now
        return expected + 1


def queue_task_submission_parsing(
    *, task_id: str, owner_id: str, filename: str, content: bytes,
    content_type: str | None, registry, identity_mode: str = "filename",
    roster_entries: list[dict[str, str]] | None = None,
    roster_name: str | None = None, recognition_provider_id: str | None = None,
    replace_confirmed: bool = False,
) -> dict:
    assignment = assignment_repository.get_assignment(task_id, actor_id=owner_id)
    questions = assignment_repository.list_questions(task_id, teacher_id=owner_id)
    if not questions:
        raise InvalidTransition("problems_required")
    if _active_submissions(task_id, owner_id) and not replace_confirmed:
        _raise_replacement_confirmation_required()
    if recognition_provider_id:
        enabled_ids = {
            str(item.get("provider_id"))
            for item in registry.list_configs()
            if item.get("enabled")
        }
        if recognition_provider_id not in enabled_ids:
            raise ValidationError(
                "The selected recognition provider is not enabled.",
                code="recognition_provider_not_enabled",
            )
    provider = (
        registry.get(recognition_provider_id)
        if recognition_provider_id else registry.pick_default()
    )
    if provider is None:
        code = (
            "recognition_provider_not_enabled"
            if recognition_provider_id else "no_provider_configured"
        )
        raise ValidationError("No enabled recognition provider is available.", code=code)
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    digest = _hash_json({
        "sha256": hashlib.sha256(content).hexdigest(),
        "identity_mode": identity_mode,
        "roster": roster_entries or [],
        "provider": recognition_provider_id,
        "replace_confirmed": replace_confirmed,
        "base_revision": workflow.workflow_revision,
    })
    workflow, active = _ensure_no_other_active_operation(
        task_id=task_id, owner_id=owner_id,
        operation_type="submission_recognition", input_hash=digest,
    )
    if active is not None:
        return {
            "status": "already_running", "task_id": task_id,
            "job_id": active.id, "workflow_revision": workflow.workflow_revision,
        }
    operation, created = workflow_repository.create_operation(
        assignment_id=task_id, owner_id=owner_id,
        operation_type="submission_recognition", input_hash=digest,
        payload={
            "filename": filename,
            "base_workflow_revision": workflow.workflow_revision,
            "replace_confirmed": replace_confirmed,
        }, expires_at=time.time() + 2 * 60 * 60,
    )
    if not created:
        return {
            "status": _operation_state(operation), "task_id": task_id,
            "job_id": operation.id, "workflow_revision": workflow.workflow_revision,
        }
    remove_reporter(operation.id)
    try:
        claimed_revision = claim_workflow_operation_atomic(
            task_id=task_id, owner_id=owner_id, operation_id=operation.id,
            expected_operation_attempt=operation.attempt,
            expected_workflow_revision=workflow.workflow_revision,
            workflow_changes={
                "presentation_status": "parsing_submissions",
                "active_operation": "submission_recognition",
                "active_job_id": operation.id, "parse_job_id": operation.id,
                "pending_submission_file_name": filename,
                "submission_identity_mode": identity_mode,
                "submission_roster_name": roster_name,
                "submission_recognition_provider_id": recognition_provider_id,
                "error_code": None,
            },
        )
    except VersionConflict:
        workflow_repository.update_operation(
            operation.id, owner_id=owner_id,
            expected_attempt=operation.attempt, status="error",
            error_code="stale_revision", completed_at=time.time(),
        )
        _raise_stale_revision()
    return {
        "status": "started", "task_id": task_id, "job_id": operation.id,
        "workflow_revision": claimed_revision,
        "_job_attempt": operation.attempt,
    }


async def run_task_submission_parsing(
    *, task_id: str, owner_id: str, job_id: str, filename: str,
    content: bytes, registry, job_attempt: int, identity_mode: str,
    roster_entries: list[dict[str, str]] | None, recognition_provider_id: str | None,
    replace_confirmed: bool, claimed_workflow_revision: int,
) -> None:
    try:
        provider = (
            registry.get(recognition_provider_id)
            if recognition_provider_id else registry.pick_default()
        )
        if provider is None:
            raise ValidationError(
                "No enabled recognition provider is available.",
                code=(
                    "recognition_provider_not_enabled"
                    if recognition_provider_id else "no_provider_configured"
                ),
            )
        vision = registry.pick_vision(provider)
        ocr_skill = LLMVisionOCRSkill(vision) if vision is not None else None
        reporter = get_or_create_reporter(job_id)
        assignment = assignment_repository.get_assignment(task_id, actor_id=owner_id)
        questions = assignment_repository.list_questions(task_id, teacher_id=owner_id)
        files = await extract_files_from_archive(
            content, filename, ocr_skill=ocr_skill, purpose="submissions", reporter=reporter
        )
        parsed: dict[str, dict] = {}
        await parse_student_answers(
            files, {q.q_id: _serialize_problem(q) for q in questions}, parsed,
            provider, reporter=reporter, identity_mode=identity_mode,
            roster_entries=roster_entries,
        )
        await reporter.set_phase("done")
        snapshot = (await reporter.snapshot()).model_dump(mode="json")
        persisted = _commit_imported_submissions(
            task_id=task_id,
            owner_id=owner_id,
            course_id=assignment.course_id,
            students=list(parsed.values()),
            replace_existing=replace_confirmed,
            expected_workflow_revision=claimed_workflow_revision,
            operation_id=job_id,
            expected_operation_attempt=job_attempt,
            operation_progress=snapshot,
            submission_file_name=filename,
        )
        del persisted
    except DomainError as exc:
        _fail_operation(
            task_id, owner_id, job_id, job_attempt,
            _detail_error(exc, "submission_parse_failed"),
        )
    except Exception:
        logger.warning("Background submission parsing failed; job_id=%s", job_id)
        _fail_operation(
            task_id, owner_id, job_id, job_attempt, "submission_parse_failed"
        )


def apply_question_patches_atomic(
    *, task_id: str, owner_id: str, expected_workflow_revision: int,
    patches: list[dict[str, Any]], operation_id: str,
    expected_operation_attempt: int,
    required_operation_status: str, final_operation_status: str,
    operation_payload: dict[str, Any], operation_progress: dict | None = None,
    require_missing: bool = False,
) -> int:
    """Apply a generated/imported question batch and finish its job atomically.

    Every target and the workflow revision is validated before the first row is
    changed.  Any invalid target, stale revision, expired job, or database error
    rolls back the workflow claim, question changes, and operation transition.
    """
    now = time.time()
    allowed_fields = {
        "stem", "criterion", "max_score", "reference_answer", "test_cases"
    }
    allowed_presentation = {
        "review_status", "solution_code", "material_provenance",
        "ai_completion_provenance", "preparation_issues",
        "max_score_source", "max_score_review_status",
    }
    with session_scope() as session:
        assignment = session.scalar(select(AssignmentRecord).where(
            AssignmentRecord.id == task_id,
            AssignmentRecord.teacher_id == owner_id,
        ))
        if assignment is None:
            raise NotFound("assignment")
        if assignment.status not in education.EDITABLE_ASSIGNMENT_STATUSES:
            raise InvalidTransition("assignment_not_editable")

        operation = _cas_operation_attempt_for_write(
            session, task_id=task_id, owner_id=owner_id,
            operation_id=operation_id,
            expected_operation_attempt=expected_operation_attempt,
            expected_statuses=(required_operation_status,),
        )
        if operation.expires_at is not None and operation.expires_at <= now:
            raise InvalidTransition("The workflow job expired.", code="stale_revision")

        questions = session.scalars(select(AssignmentQuestionRecord).where(
            AssignmentQuestionRecord.assignment_id == task_id
        )).all()
        question_map = {question.q_id: question for question in questions}
        normalized: list[tuple[AssignmentQuestionRecord, dict, dict]] = []
        for patch in patches:
            q_id = str(patch.get("q_id") or "")
            question = question_map.get(q_id)
            if question is None:
                raise ValidationError(
                    "A generated target no longer matches a question.",
                    code="unknown_ai_completion_target" if require_missing else "stale_revision",
                )
            fields = dict(patch.get("fields") or {})
            presentation_updates = dict(patch.get("presentation") or {})
            if not set(fields).issubset(allowed_fields) or not set(
                presentation_updates
            ).issubset(allowed_presentation):
                raise ValidationError("Unsupported question patch.")
            if "max_score" in fields:
                max_score = float(fields["max_score"])
                if not math.isfinite(max_score) or not 0 < max_score <= 10_000:
                    raise ValidationError(
                        "Question maximum score must be between 0 and 10000.",
                        code="invalid_max_score",
                    )
                fields["max_score"] = max_score
            if require_missing:
                current_presentation = dict(
                    (question.source or {}).get("presentation") or {}
                )
                for key in fields:
                    if getattr(question, key) not in (None, "", []):
                        raise InvalidTransition(
                            "A requested completion target is no longer missing.",
                            code="unknown_ai_completion_target",
                        )
                if (
                    "solution_code" in presentation_updates
                    and current_presentation.get("solution_code")
                ):
                    raise InvalidTransition(
                        "A requested completion target is no longer missing.",
                        code="unknown_ai_completion_target",
                    )
            normalized.append((question, fields, presentation_updates))

        workflow = session.scalar(select(workflow_repository.AssignmentWorkflowRecord).where(
            workflow_repository.AssignmentWorkflowRecord.assignment_id == task_id,
            workflow_repository.AssignmentWorkflowRecord.owner_id == owner_id,
        ))
        if workflow is None:
            raise NotFound("workflow")
        workflow_values: dict[str, Any] = {
            "workflow_revision": (
                workflow_repository.AssignmentWorkflowRecord.workflow_revision + 1
            ),
            "error_code": None,
            "updated_at": now,
        }
        if operation.operation_type in _AUXILIARY_QUESTION_OPERATION_TYPES:
            workflow_values["presentation_status"] = "problems_ready"
        if workflow.active_job_id == operation_id:
            workflow_values.update(active_job_id=None, active_operation=None)
        claimed = session.execute(
            update(workflow_repository.AssignmentWorkflowRecord)
            .where(
                workflow_repository.AssignmentWorkflowRecord.assignment_id == task_id,
                workflow_repository.AssignmentWorkflowRecord.owner_id == owner_id,
                workflow_repository.AssignmentWorkflowRecord.workflow_revision
                == expected_workflow_revision,
            )
            .values(**workflow_values)
        )
        if claimed.rowcount != 1:
            _raise_stale_revision()

        for question, fields, presentation_updates in normalized:
            source = dict(question.source or {})
            presentation = dict(source.get("presentation") or {})
            for key, value in presentation_updates.items():
                if key in {"material_provenance", "ai_completion_provenance"}:
                    merged = dict(presentation.get(key) or {})
                    merged.update(dict(value or {}))
                    presentation[key] = merged
                else:
                    presentation[key] = value
            source["presentation"] = presentation
            for key, value in fields.items():
                setattr(question, key, value)
            question.source = source
            question.version += 1
            question.updated_at = now

        operation.status = final_operation_status
        operation.payload = operation_payload
        operation.progress = operation_progress or dict(operation.progress or {})
        operation.error_code = None
        operation.completed_at = now
        operation.updated_at = now
        session.flush()
        return expected_workflow_revision + 1


def complete_planning_operation_atomic(
    *, task_id: str, owner_id: str, expected_workflow_revision: int,
    operation_id: str, expected_operation_attempt: int,
    payload: dict[str, Any], progress: dict | None,
    final_status: str = "ready",
) -> int:
    """Publish a background-generated plan only if its task snapshot is current."""
    now = time.time()
    with session_scope() as session:
        operation = _cas_operation_attempt_for_write(
            session, task_id=task_id, owner_id=owner_id,
            operation_id=operation_id,
            expected_operation_attempt=expected_operation_attempt,
            expected_statuses=("running",),
        )
        if operation.expires_at is not None and operation.expires_at <= now:
            _raise_stale_revision()
        claimed = session.execute(
            update(workflow_repository.AssignmentWorkflowRecord)
            .where(
                workflow_repository.AssignmentWorkflowRecord.assignment_id == task_id,
                workflow_repository.AssignmentWorkflowRecord.owner_id == owner_id,
                workflow_repository.AssignmentWorkflowRecord.workflow_revision
                == expected_workflow_revision,
                workflow_repository.AssignmentWorkflowRecord.active_job_id == operation_id,
            )
            .values(
                active_job_id=None, active_operation=None, error_code=None,
                presentation_status=(
                    "problems_ready"
                    if operation.operation_type in _AUXILIARY_QUESTION_OPERATION_TYPES
                    else workflow_repository.AssignmentWorkflowRecord.presentation_status
                ),
                updated_at=now,
            )
        )
        if claimed.rowcount != 1:
            _raise_stale_revision()
        operation.status = final_status
        operation.payload = payload
        operation.progress = progress or {}
        operation.error_code = None
        operation.completed_at = now
        operation.updated_at = now
        session.flush()
        return expected_workflow_revision


def _safe_student_token(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip()).strip("._-")
    return cleaned[:48] or "student"


def _commit_imported_submissions(
    *, task_id: str, owner_id: str, course_id: str, students: list[dict],
    replace_existing: bool = False,
    expected_workflow_revision: int | None = None,
    operation_id: str | None = None,
    expected_operation_attempt: int | None = None,
    operation_progress: dict | None = None,
    submission_file_name: str | None = None,
) -> int:
    """Publish and persist a parsed teacher batch in one transaction.

    Parsing/OCR happens before this boundary.  If any normalized student,
    enrollment, revision, answer, or presentation row fails, the assignment
    remains in its previous state and no half-import is visible.
    """
    now = time.time()
    with session_scope() as session:
        operation = None
        if operation_id is not None:
            if expected_operation_attempt is None:
                raise ValidationError("operation_attempt_required")
            operation = _cas_operation_attempt_for_write(
                session, task_id=task_id, owner_id=owner_id,
                operation_id=operation_id,
                expected_operation_attempt=expected_operation_attempt,
                expected_statuses=("running",),
            )
        assignment = session.scalar(
            select(AssignmentRecord).where(
                AssignmentRecord.id == task_id,
                AssignmentRecord.teacher_id == owner_id,
            )
        )
        if assignment is None:
            raise NotFound("assignment")
        if assignment.status not in (
            *education.EDITABLE_ASSIGNMENT_STATUSES,
            education.AssignmentStatus.PUBLISHED.value,
        ):
            raise InvalidTransition("assignment_not_open")

        questions = session.scalars(
            select(AssignmentQuestionRecord).where(
                AssignmentQuestionRecord.assignment_id == task_id
            )
        ).all()
        question_map = {question.q_id: question for question in questions}
        if not question_map:
            raise InvalidTransition("problems_required")

        active_presentation = session.scalar(
            select(workflow_repository.AssignmentStudentPresentationRecord.id)
            .where(
                workflow_repository.AssignmentStudentPresentationRecord.assignment_id
                == task_id,
                workflow_repository.AssignmentStudentPresentationRecord.is_active.is_(True),
            )
            .limit(1)
        )
        if active_presentation is not None and not replace_existing:
            _raise_replacement_confirmation_required()

        if expected_workflow_revision is not None:
            claimed = session.execute(
                update(workflow_repository.AssignmentWorkflowRecord)
                .where(
                    workflow_repository.AssignmentWorkflowRecord.assignment_id == task_id,
                    workflow_repository.AssignmentWorkflowRecord.owner_id == owner_id,
                    workflow_repository.AssignmentWorkflowRecord.workflow_revision
                    == expected_workflow_revision,
                )
                .values(
                    workflow_revision=(
                        workflow_repository.AssignmentWorkflowRecord.workflow_revision + 1
                    ),
                    presentation_status="submissions_ready",
                    active_operation=None,
                    active_job_id=None,
                    submission_file_name=submission_file_name,
                    pending_submission_file_name=None,
                    error_code=None,
                    updated_at=now,
                )
            )
            if claimed.rowcount != 1:
                _raise_stale_revision()

        if replace_existing:
            session.execute(
                update(workflow_repository.AssignmentStudentPresentationRecord)
                .where(
                    workflow_repository.AssignmentStudentPresentationRecord.assignment_id
                    == task_id
                )
                .values(is_active=False, updated_at=now)
            )

        for student in students:
            display_id = (
                str(student.get("stu_id") or "").strip()
                or f"unknown-{uuid.uuid4().hex[:6]}"
            )
            display_name = str(student.get("stu_name") or "").strip() or display_id
            digest = hashlib.sha256(
                f"{owner_id}\0{task_id}\0{display_id}".encode()
            ).hexdigest()[:16]
            student_id = f"imported_{digest}"
            user = session.get(UserRecord, student_id)
            if user is None:
                session.add(
                    UserRecord(
                        id=student_id,
                        username=(
                            f"imported-{digest}-{_safe_student_token(display_id)[:16]}"
                        ),
                        email=None,
                        role="student",
                        password_hash="!disabled-imported-account",
                        is_active=False,
                        created_at=now,
                        updated_at=now,
                    )
                )
                session.flush()

            enrollment = session.scalar(
                select(CourseEnrollmentRecord).where(
                    CourseEnrollmentRecord.course_id == course_id,
                    CourseEnrollmentRecord.student_id == student_id,
                )
            )
            if enrollment is None:
                session.add(
                    CourseEnrollmentRecord(
                        course_id=course_id,
                        student_id=student_id,
                        enrolled_at=now,
                    )
                )

            submission = session.scalar(
                select(SubmissionRecord).where(
                    SubmissionRecord.assignment_id == task_id,
                    SubmissionRecord.student_id == student_id,
                )
            )
            if submission is None:
                submission = SubmissionRecord(
                    id=f"sub_{uuid.uuid4().hex[:12]}",
                    assignment_id=task_id,
                    student_id=student_id,
                    current_revision_id=None,
                    created_at=now,
                    updated_at=now,
                )
                session.add(submission)
                session.flush()

            next_number = (
                session.scalar(
                    select(func.max(SubmissionRevisionRecord.revision_number)).where(
                        SubmissionRevisionRecord.submission_id == submission.id
                    )
                )
                or 0
            ) + 1
            revision_id = f"rev_{uuid.uuid4().hex[:12]}"
            revision = SubmissionRevisionRecord(
                id=revision_id,
                submission_id=submission.id,
                revision_number=next_number,
                source=education.SubmissionRevisionSource.TEACHER_IMPORT.value,
                file_name=str(student.get("source_filename") or ""),
                created_at=now,
            )
            session.add(revision)
            session.flush()

            for raw in student.get("stu_ans") or []:
                question = question_map.get(str(raw.get("q_id") or ""))
                if question is None:
                    continue
                session.add(
                    SubmissionAnswerRecord(
                        id=f"ans_{uuid.uuid4().hex[:12]}",
                        revision_id=revision_id,
                        question_id=question.id,
                        q_id=question.q_id,
                        number=str(raw.get("number") or question.number),
                        type=str(raw.get("type") or question.type),
                        content=str(raw.get("content") or ""),
                        flag=list(raw.get("flag") or []),
                        created_at=now,
                    )
                )
            submission.current_revision_id = revision_id
            submission.updated_at = now

            presentation = session.scalar(
                select(workflow_repository.AssignmentStudentPresentationRecord).where(
                    workflow_repository.AssignmentStudentPresentationRecord.assignment_id
                    == task_id,
                    workflow_repository.AssignmentStudentPresentationRecord.student_id
                    == student_id,
                )
            )
            if presentation is None:
                presentation = workflow_repository.AssignmentStudentPresentationRecord(
                    id=f"sp_{uuid.uuid4().hex[:12]}",
                    assignment_id=task_id,
                    student_id=student_id,
                    display_student_id=display_id,
                    display_name=display_name,
                    is_active=True,
                    created_at=now,
                    updated_at=now,
                )
                session.add(presentation)
            presentation.display_student_id = display_id
            presentation.display_name = display_name
            presentation.source_filename = str(student.get("source_filename") or "")
            presentation.identity_match_method = str(
                student.get("identity_match_method") or "filename"
            )
            presentation.identity_status = str(
                student.get("identity_status") or "needs_review"
            )
            presentation.is_active = True
            presentation.updated_at = now

        if assignment.status in education.EDITABLE_ASSIGNMENT_STATUSES:
            assignment.status = education.AssignmentStatus.PUBLISHED.value
            assignment.published_at = now
            assignment.version += 1
        assignment.updated_at = now
        if operation is not None:
            payload = dict(operation.payload or {})
            payload.update({
                "filename": submission_file_name,
                "student_count": len(students),
                "replace_confirmed": replace_existing,
            })
            operation.status = "done"
            operation.progress = operation_progress or {}
            operation.payload = payload
            operation.error_code = None
            operation.completed_at = now
            operation.updated_at = now
        session.flush()
        return len(students)


def _fail_operation(
    task_id: str, owner_id: str, job_id: str, expected_operation_attempt: int,
    error_code: str,
) -> bool:
    safe = error_code if error_code in _SAFE_ERROR_CODES else "workflow_failed"
    now = time.time()
    # Keep the operation transition and workflow cleanup in one transaction.
    # Because retries reuse the operation id, splitting these writes would let
    # a newly claimed attempt become active between them and then be cleared by
    # the previous worker's late failure handler.
    with session_scope() as session:
        try:
            operation = _cas_operation_attempt_for_write(
                session, task_id=task_id, owner_id=owner_id,
                operation_id=job_id,
                expected_operation_attempt=expected_operation_attempt,
                expected_statuses=("pending", "running", "ready"),
                changes={
                    "status": "error", "error_code": safe,
                    "completed_at": now,
                },
            )
        except (VersionConflict, InvalidTransition) as exc:
            if exc.code in {"stale_operation_attempt", "workflow_busy"}:
                return False
            raise
        workflow = session.scalar(select(
            workflow_repository.AssignmentWorkflowRecord
        ).where(
            workflow_repository.AssignmentWorkflowRecord.assignment_id == task_id,
            workflow_repository.AssignmentWorkflowRecord.owner_id == owner_id,
        ))
        if workflow is None:
            raise NotFound("workflow")
        if operation.operation_type in _AUXILIARY_QUESTION_OPERATION_TYPES:
            workflow.last_failed_job_id = job_id
            workflow.error_code = safe
            workflow.updated_at = now
            if workflow.active_job_id in {None, job_id}:
                workflow.presentation_status = "problems_ready"
            if workflow.active_job_id == job_id:
                workflow.active_operation = None
                workflow.active_job_id = None
        elif workflow.active_job_id == job_id:
            workflow.presentation_status = "error"
            workflow.active_operation = None
            workflow.active_job_id = None
            workflow.last_failed_job_id = job_id
            workflow.error_code = safe
            workflow.updated_at = now
        session.flush()
        return True


def task_state(*, task_id: str, owner_id: str) -> dict:
    payload = get_task(task_id=task_id, owner_id=owner_id, full=False)
    try:
        workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    except NotFound:
        return payload
    progress: dict | None = None
    if workflow.active_job_id:
        reporter = get_reporter(workflow.active_job_id)
        if reporter is not None:
            # Snapshot is async; callers should use async_task_state.
            progress = None
        else:
            try:
                operation = workflow_repository.get_operation(
                    workflow.active_job_id, owner_id=owner_id
                )
                progress = dict(operation.progress or {}) or None
            except NotFound:
                progress = None
    elif workflow.extract_job_id or workflow.parse_job_id:
        job_id = workflow.parse_job_id or workflow.extract_job_id
        if job_id:
            try:
                operation = workflow_repository.get_operation(job_id, owner_id=owner_id)
                progress = dict(operation.progress or {}) or None
            except NotFound:
                pass
    payload.update({
        "progress": progress,
        "active_job_id": workflow.active_job_id,
        "active_operation": workflow.active_operation,
    })
    return payload


async def async_task_state(*, task_id: str, owner_id: str) -> dict:
    payload = task_state(task_id=task_id, owner_id=owner_id)
    active_job_id = payload.get("active_job_id")
    if active_job_id and (reporter := get_reporter(active_job_id)) is not None:
        payload["progress"] = (await reporter.snapshot()).model_dump(mode="json")
    grading_job_id = payload.get("grading_job_id")
    if payload.get("status") in {"grading", "error"} and grading_job_id:
        grading_progress = _grading_progress(grading_job_id, owner_id)
        # A reporter can reach ``done`` before result persistence finishes.
        # Prefer the durable failed-run projection so clients never render a
        # stale success phase after the database marks the run failed.
        if payload.get("status") == "grading" or grading_progress.get("phase") == "error":
            payload["progress"] = grading_progress
            payload["active_job_id"] = grading_job_id
            payload["active_operation"] = "grading"
            payload["error"] = payload.get("error") or grading_progress.get("error_detail")
    return payload


def _grading_progress(run_id: str, owner_id: str) -> dict:
    run = grading_repository.get_run(run_id, actor_id=owner_id)
    events = grading_repository.list_events(run_id, actor_id=owner_id)
    question_count = len(assignment_repository.get_questions_by_assignment(run.assignment_id))
    completed_units = run.completed_submissions * question_count
    for item in events:
        value = item.get("payload", {}).get("completed_units")
        if isinstance(value, int) and not isinstance(value, bool):
            completed_units = max(completed_units, value)
    messages = [
        {
            "ts": item["created_at"],
            "level": item["level"] if item["level"] in {"info", "warn", "error"} else "info",
            "message": item["message"],
        }
        for item in events[-100:]
    ]
    return {
        "contract_version": 1, "job_id": run_id,
        "phase": "done" if run.status in {"completed", "partial_failed"} else (
            "error" if run.status == "failed" else "grading"
        ),
        "total_students": run.total_submissions,
        "total_questions": question_count,
        "completed_units": completed_units,
        "active": [], "messages": messages,
        "error_detail": "grading_failed" if run.status == "failed" else None,
        "started_at": run.started_at or run.created_at,
        "workflow": "grading", "stage_sequence": [],
        "current_step": "completed" if run.status in {"completed", "partial_failed"} else "grading",
        "total_steps": None, "completed_steps": None, "stage_metrics": {},
    }


def start_task_grading(*, task_id: str, owner_id: str) -> dict:
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    if workflow.grading_setup is None:
        raise InvalidTransition("grading_setup_required")
    runs = grading_repository.list_runs_for_assignment(task_id, actor_id=owner_id)
    active = next((run for run in reversed(runs) if run.status in {"queued", "running"}), None)
    if active:
        return {"status": "already_running", "task_id": task_id, "job_id": active.id}
    questions = assignment_repository.get_questions_by_assignment(task_id)
    submissions = _active_submissions(task_id, owner_id)
    presentations = workflow_repository.list_student_presentations(task_id)
    revisions = [
        submission_repository.get_revision(
            revision_id=submission.current_revision_id, actor_id=owner_id
        )
        for submission in submissions
        if submission.current_revision_id is not None
    ]
    answer_statuses = workflow_repository.answer_review_statuses([
        answer.id for revision in revisions for answer in revision.answers
    ])
    setup = TaskGradingSetup.model_validate(workflow.grading_setup)
    from backend.services.grading_input_security import (
        provider_configuration_fingerprint,
    )

    input_manifest = {
        "questions": [question.model_dump(mode="json") for question in questions],
        "submission_revision_ids": sorted(
            submission.current_revision_id
            for submission in submissions
            if submission.current_revision_id is not None
        ),
        "knowledge_document_ids": sorted(_selected_knowledge(task_id, owner_id)),
        "provider_configuration_fingerprint": provider_configuration_fingerprint(
            owner_id=owner_id,
            selected_provider_ids=setup.selected_provider_ids,
        ),
        "student_presentations": [
            {
                "student_id": submission.student_id,
                "display_student_id": (
                    presentations[submission.student_id].display_student_id
                    if submission.student_id in presentations
                    else submission.student_id
                ),
                "display_name": (
                    presentations[submission.student_id].display_name
                    if submission.student_id in presentations
                    else submission.student_id
                ),
                "source_filename": (
                    presentations[submission.student_id].source_filename
                    if submission.student_id in presentations
                    else None
                ),
                "identity_match_method": (
                    presentations[submission.student_id].identity_match_method
                    if submission.student_id in presentations
                    else "filename"
                ),
                "identity_status": (
                    presentations[submission.student_id].identity_status
                    if submission.student_id in presentations
                    else "matched"
                ),
            }
            for submission in sorted(submissions, key=lambda item: item.student_id)
        ],
        "answer_review_statuses": {
            answer_id: answer_statuses[answer_id]
            for answer_id in sorted(answer_statuses)
        },
    }
    run_fingerprint = _hash_json({
        "grading_setup": workflow.grading_setup,
        "input_manifest": input_manifest,
    })
    latest = runs[-1] if runs else None
    if latest and latest.status in {"completed", "partial_failed"}:
        frozen = workflow_repository.get_run_setup(latest.id)
        if frozen is not None and frozen.fingerprint == run_fingerprint:
            return {"status": "already_done", "task_id": task_id, "job_id": latest.id}
    run = grading_runs.start_run(
        assignment_id=task_id,
        teacher_id=owner_id,
        grading_setup=dict(workflow.grading_setup),
        setup_fingerprint=run_fingerprint,
        input_manifest=input_manifest,
    )
    workflow_repository.update_workflow(
        task_id, owner_id=owner_id, bump_revision=False,
        presentation_status="grading", grading_job_id=run.id,
        active_operation="grading", active_job_id=run.id, error_code=None,
    )
    return {"status": "started", "task_id": task_id, "job_id": run.id}


def task_results(*, task_id: str, owner_id: str) -> dict:
    task = get_task(task_id=task_id, owner_id=owner_id, full=True)
    runs = grading_repository.list_runs_for_assignment(task_id, actor_id=owner_id)
    if not runs:
        return {"status": task["status"], "task_id": task_id}
    run = runs[-1]
    if run.status not in {"completed", "partial_failed"}:
        return {
            "status": "not_found" if run.status == "failed" else task["status"],
            "task_id": task_id,
            "error": "grading_failed" if run.status == "failed" else None,
        }
    results = grading_repository.list_results_for_run(run.id)
    presentations = workflow_repository.list_student_presentations(task_id)
    grouped: dict[str, list] = defaultdict(list)
    for result in results:
        grouped[result.student_id].append(result)
    rendered = []
    student_data = task["student_data"]
    for internal_id, rows in grouped.items():
        presentation = presentations.get(internal_id)
        display_id = presentation.display_student_id if presentation else internal_id
        rendered.append({
            "student_id": display_id,
            "student_name": presentation.display_name if presentation else internal_id,
            "corrections": [_serialize_correction(row) for row in rows],
            "student_answers": student_data.get(display_id, {}).get("stu_ans", []),
        })
    return {
        "status": "completed", "task_id": task_id, "results": rendered,
        "problem_data": task["problem_data"],
        "student_data": task["student_data"], "timestamp": run.completed_at,
    }


def _serialize_correction(result) -> dict:
    review = result.teacher_review or {}
    score = result.effective_score
    review_status = (
        "confirmed" if review.get("confirmed") else "edited"
    ) if review else (
        "pending" if result.requires_review else "confirmed"
    )
    return {
        "result_id": result.id,
        "q_id": result.q_id,
        "type": "",
        "score": score,
        "provisional_score": result.ai_score,
        "max_score": result.ai_max_score,
        "confidence": result.ai_confidence or 0,
        "comment": result.ai_comment,
        "steps": result.ai_steps,
        "hits": None, "logs": None,
        "expert_results": result.ai_expert_results,
        "synthesis_method": result.ai_synthesis_method,
        "is_score": None,
        "requires_human_review": result.requires_review,
        "review_reasons": list(result.review_reasons or []),
        "initial_review_reasons": list(result.initial_review_reasons or []),
        "teacher_score": review.get("new_score") if review else None,
        "teacher_comment": review.get("new_comment", "") if review else "",
        "review_status": review_status,
        "reviewed_at": review.get("created_at") if review else None,
    }


def update_problem(
    *, task_id: str, owner_id: str, q_id: str, patch: dict
) -> dict:
    questions = assignment_repository.list_questions(task_id, teacher_id=owner_id)
    question = next((item for item in questions if item.q_id == q_id), None)
    if question is None:
        raise NotFound("question")
    source = dict(question.source or {})
    presentation = dict(source.get("presentation") or {})
    for key in (
        "review_status", "solution_code", "material_provenance",
        "ai_completion_provenance", "preparation_issues",
    ):
        if key in patch:
            presentation[key] = patch[key]
    if patch.get("review_status") == "confirmed":
        presentation["max_score_review_status"] = "confirmed"
        presentation["preparation_issues"] = [
            issue
            for issue in presentation.get("preparation_issues", [])
            if issue.get("field") != "max_score"
        ]
    if "max_score" in patch:
        max_score = float(patch["max_score"])
        if not math.isfinite(max_score) or not 0 < max_score <= 10_000:
            raise ValidationError(
                "Question maximum score must be between 0 and 10000.",
                code="invalid_max_score",
            )
        presentation["max_score_source"] = "teacher_edited"
        presentation["max_score_review_status"] = "confirmed"
        presentation["preparation_issues"] = [
            issue
            for issue in presentation.get("preparation_issues", [])
            if issue.get("field") != "max_score"
        ]
    source["presentation"] = presentation
    fields = {
        key: patch[key]
        for key in (
            "stem", "criterion", "max_score", "reference_answer", "test_cases"
        )
        if key in patch
    }
    fields["source"] = source
    updated = assignment_repository.update_question(
        task_id, teacher_id=owner_id, q_id=q_id,
        expected_version=question.version, **fields,
    )
    workflow_repository.update_workflow(task_id, owner_id=owner_id)
    return {"status": "ok", "q_id": q_id, "problem": _serialize_problem(updated)}


def update_student_answer(
    *, task_id: str, owner_id: str, display_student_id: str,
    q_id: str, patch: dict, expected_revision: int | None,
) -> dict:
    claimed_workflow = (
        workflow_repository.update_workflow(
            task_id, owner_id=owner_id, expected_revision=expected_revision
        )
        if expected_revision is not None
        else None
    )
    presentations = workflow_repository.list_student_presentations(task_id)
    presentation = next(
        (item for item in presentations.values() if item.display_student_id == display_student_id),
        None,
    )
    internal_id = presentation.student_id if presentation else display_student_id
    submission = submission_repository.get_submission_for_student(task_id, student_id=internal_id)
    if submission is None or submission.current_revision_id is None:
        raise NotFound("submission")
    revision = submission_repository.get_revision(
        revision_id=submission.current_revision_id, actor_id=owner_id
    )
    questions = {q.q_id: q for q in assignment_repository.get_questions_by_assignment(task_id)}
    answers = []
    target_payload = None
    for answer in revision.answers:
        content = patch.get("content", answer.content) if answer.q_id == q_id else answer.content
        flag = patch.get("flag", answer.flag) if answer.q_id == q_id else answer.flag
        answers.append({
            "question_id": answer.question_id, "q_id": answer.q_id,
            "number": answer.number, "type": answer.type,
            "content": content, "flag": list(flag or []),
        })
        if answer.q_id == q_id:
            target_payload = {
                "q_id": answer.q_id, "number": answer.number,
                "type": answer.type, "content": content, "flag": list(flag or []),
                "review_status": patch.get("review_status", "pending"),
            }
    if target_payload is None or q_id not in questions:
        raise NotFound("answer")
    new_revision = submission_repository.add_revision(
        submission_id=submission.id, student_id=internal_id,
        source=education.SubmissionRevisionSource.TEACHER_IMPORT.value,
        file_name=revision.file_name, answers=answers,
    )
    target = next(answer for answer in new_revision.answers if answer.q_id == q_id)
    workflow_repository.set_answer_review_status(
        target.id, str(patch.get("review_status") or "pending")
    )
    updated_workflow = claimed_workflow or workflow_repository.update_workflow(
        task_id, owner_id=owner_id
    )
    return {
        "status": "ok", "stu_id": display_student_id, "q_id": q_id,
        "answer": target_payload, "workflow_revision": updated_workflow.workflow_revision,
    }


def update_student_identity(
    *, task_id: str, owner_id: str, current_display_id: str,
    new_display_id: str, new_display_name: str, expected_revision: int,
) -> dict:
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    if workflow.workflow_revision != expected_revision:
        _raise_stale_revision()
    normalized_display_id = new_display_id.strip()
    normalized_display_name = new_display_name.strip()
    presentations = workflow_repository.list_student_presentations(task_id)
    presentation = next(
        (item for item in presentations.values() if item.display_student_id == current_display_id),
        None,
    )
    if presentation is None:
        raise NotFound("student")
    duplicate = next(
        (item for item in presentations.values()
         if item.display_student_id == normalized_display_id
         and item.student_id != presentation.student_id),
        None,
    )
    if duplicate is not None:
        raise ValidationError(
            "The student ID is already used in this task.",
            code="student_identity_conflict",
        )
    # The revision CAS comes after every deterministic validation.  A rejected
    # identity edit must not silently advance the task revision.
    claimed_workflow = workflow_repository.update_workflow(
        task_id, owner_id=owner_id, expected_revision=expected_revision
    )
    updated = workflow_repository.upsert_student_presentation(
        assignment_id=task_id, student_id=presentation.student_id,
        display_student_id=normalized_display_id,
        display_name=normalized_display_name,
        source_filename=presentation.source_filename,
        identity_match_method=presentation.identity_match_method,
        identity_status="matched",
    )
    task = get_task(task_id=task_id, owner_id=owner_id, full=True)
    return {
        "status": "ok", "previous_student_id": current_display_id,
        "student": task["student_data"][updated.display_student_id],
        "workflow_revision": claimed_workflow.workflow_revision,
    }


def update_correction_review(
    *, task_id: str, owner_id: str, display_student_id: str, q_id: str,
    teacher_score: float, teacher_comment: str, confirm: bool,
    expected_revision: int,
) -> dict:
    claimed_workflow = workflow_repository.update_workflow(
        task_id, owner_id=owner_id, expected_revision=expected_revision
    )
    presentations = workflow_repository.list_student_presentations(task_id)
    presentation = next(
        (item for item in presentations.values() if item.display_student_id == display_student_id),
        None,
    )
    internal_id = presentation.student_id if presentation else display_student_id
    runs = grading_repository.list_runs_for_assignment(task_id, actor_id=owner_id)
    if not runs:
        raise NotFound("grading_run")
    target_run = runs[-1]
    if target_run.released_at is not None:
        target_run = grading_repository.clone_released_run_for_review(
            target_run.id, teacher_id=owner_id
        )
        workflow_repository.update_workflow(
            task_id, owner_id=owner_id, bump_revision=False,
            presentation_status="graded", grading_job_id=target_run.id,
            analysis_status="not_generated", analysis_result_version=None,
            analysis_generated_at=None, analysis_error_code=None,
        )
    rows = grading_repository.list_results_for_run(target_run.id)
    row = next((item for item in rows if item.student_id == internal_id and item.q_id == q_id), None)
    if row is None:
        raise NotFound("grade_result")
    previous_score = row.effective_score
    previous_comment = row.effective_comment
    unchanged = previous_score == teacher_score and previous_comment == teacher_comment
    if not unchanged or confirm:
        grading_runs.add_teacher_review(
            grade_result_id=row.id, teacher_id=owner_id,
            new_score=teacher_score, new_comment=teacher_comment,
            confirm=confirm,
        )
    refreshed = next(
        item for item in grading_repository.list_results_for_run(target_run.id)
        if item.id == row.id
    )
    return {
        "status": "ok", "unchanged": unchanged,
        "student_id": display_student_id, "q_id": q_id,
        "correction": _serialize_correction(refreshed),
        "workflow_revision": claimed_workflow.workflow_revision,
    }


def finalization(*, task_id: str, owner_id: str) -> dict:
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    runs = grading_repository.list_runs_for_assignment(task_id, actor_id=owner_id)
    run = runs[-1] if runs else None
    remaining = []
    required_review_count = 0
    confirmed_required_count = 0
    if run is not None:
        for result in grading_repository.list_results_for_run(run.id):
            if result.initial_requires_review:
                required_review_count += 1
                review = result.teacher_review or {}
                if (
                    result.result_status
                    not in education.NON_SCOREABLE_RESULT_STATUSES
                    and review.get("confirmed") is True
                ):
                    confirmed_required_count += 1
            if result.result_status in education.NON_SCOREABLE_RESULT_STATUSES:
                presentation = workflow_repository.list_student_presentations(task_id).get(result.student_id)
                remaining.append({
                    "student_id": presentation.display_student_id if presentation else result.student_id,
                    "q_id": result.q_id,
                    "reasons": list(result.review_reasons or []),
                    "confirmed": False,
                })
    released = bool(run and run.released_at is not None)
    has_released_run = any(item.released_at is not None for item in runs)
    status = "finalized" if released else _presentation_status(
        workflow,
        assignment_repository.get_questions_by_assignment(task_id),
        submission_repository.list_submissions(task_id, actor_id=owner_id),
        run,
    )
    return {
        "task_id": task_id, "task_status": status,
        "workflow_revision": workflow.workflow_revision,
        "ready_for_confirmation": bool(run and run.status in {"completed", "partial_failed"} and not remaining),
        "required_review_count": required_review_count,
        "confirmed_required_count": confirmed_required_count,
        "remaining_review_count": len(remaining), "remaining_reviews": remaining,
        "final_result_version": max(workflow.final_result_version, 1 if released else 0),
        "final_result_updated_at": workflow.final_result_updated_at or (run.released_at if run else None),
        "final_result_dirty": bool(has_released_run and run and not released),
        "analysis_status": workflow.analysis_status,
        "analysis_result_version": workflow.analysis_result_version,
        "analysis_generated_at": workflow.analysis_generated_at,
        "analysis_error": workflow.analysis_error_code,
        "available_result_versions": len(workflow_repository.list_artifact_manifests(task_id, owner_id=owner_id)),
    }


def confirm_finalization(
    *, task_id: str, owner_id: str, expected_revision: int
) -> dict:
    runs = grading_repository.list_runs_for_assignment(task_id, actor_id=owner_id)
    if not runs:
        raise NotFound("grading_run")
    _workflow, _released_at, changed = (
        workflow_repository.confirm_final_result_atomic(
            assignment_id=task_id,
            owner_id=owner_id,
            grading_run_id=runs[-1].id,
            expected_revision=expected_revision,
        )
    )
    return {
        "status": "ok" if changed else "already_done",
        "unchanged": not changed,
        **finalization(task_id=task_id, owner_id=owner_id),
    }


def _run_snapshot_payload(*, task_id: str, owner_id: str, run_id: str) -> dict:
    run = grading_repository.get_run(run_id, actor_id=owner_id)
    if run.assignment_id != task_id:
        raise NotFound("grading_run")
    rows = grading_repository.list_results_for_run(run_id)
    frozen_setup = workflow_repository.get_run_setup(run_id)
    questions = grading_runs._questions_for_run(
        assignment_id=task_id, frozen_setup=frozen_setup,
    )

    presentations = workflow_repository.list_student_presentations(task_id)
    frozen_manifest = (frozen_setup.input_manifest if frozen_setup else {}) or {}
    frozen_presentations = {
        str(item.get("student_id")): item
        for item in frozen_manifest.get("student_presentations", [])
        if isinstance(item, dict) and item.get("student_id")
    }
    student_data: dict[str, dict] = {}
    internal_to_display: dict[str, str] = {}
    frozen_rows = grading_repository.list_frozen_submissions(run_id)
    revisions = [
        (
            frozen,
            submission_repository.get_revision(
                revision_id=frozen.id, actor_id=owner_id
            ),
        )
        for frozen in frozen_rows
    ]
    answer_ids = [answer.id for _, revision in revisions for answer in revision.answers]
    frozen_answer_statuses = frozen_manifest.get("answer_review_statuses")
    answer_statuses = (
        {
            str(answer_id): str(status)
            for answer_id, status in frozen_answer_statuses.items()
        }
        if isinstance(frozen_answer_statuses, dict)
        else workflow_repository.answer_review_statuses(answer_ids)
    )
    for frozen, revision in revisions:
        presentation = presentations.get(frozen.student_id)
        frozen_presentation = frozen_presentations.get(frozen.student_id)
        display_id = (
            str(frozen_presentation.get("display_student_id"))
            if frozen_presentation is not None
            else (
                presentation.display_student_id
                if presentation else frozen.student_id
            )
        )
        internal_to_display[frozen.student_id] = display_id
        student_data[display_id] = {
            "stu_id": display_id,
            "stu_name": (
                str(frozen_presentation.get("display_name"))
                if frozen_presentation is not None
                else (
                    presentation.display_name
                    if presentation else frozen.student_id
                )
            ),
            "stu_ans": [
                {
                    "q_id": answer.q_id,
                    "number": answer.number,
                    "type": answer.type,
                    "content": answer.content,
                    "flag": list(answer.flag or []),
                    "review_status": answer_statuses.get(answer.id, "pending"),
                }
                for answer in revision.answers
            ],
            "source_filename": (
                frozen_presentation.get("source_filename")
                if frozen_presentation is not None
                else (
                    presentation.source_filename
                    if presentation else revision.file_name
                )
            ),
            "identity_match_method": (
                frozen_presentation.get("identity_match_method")
                if frozen_presentation is not None
                else (
                    presentation.identity_match_method
                    if presentation else "filename"
                )
            ),
            "identity_status": (
                frozen_presentation.get("identity_status")
                if frozen_presentation is not None
                else (
                    presentation.identity_status
                    if presentation else "matched"
                )
            ),
        }

    grouped: dict[str, list] = defaultdict(list)
    for row in rows:
        grouped[row.student_id].append(row)
    rendered_results = []
    for internal_id, result_rows in grouped.items():
        display_id = internal_to_display.get(internal_id, internal_id)
        student = student_data.get(display_id, {})
        rendered_results.append({
            "student_id": display_id,
            "student_name": student.get("stu_name", display_id),
            "corrections": [_serialize_correction(row) for row in result_rows],
            "student_answers": student.get("stu_ans", []),
        })
    return {
        "results": rendered_results,
        "problem_data": {
            question.q_id: _serialize_problem(question) for question in questions
        },
        "student_data": student_data,
    }


def result_snapshot(
    *, task_id: str, owner_id: str, result_version: int = 1,
    grading_run_id: str | None = None,
) -> dict:
    runs = grading_repository.list_runs_for_assignment(task_id, actor_id=owner_id)
    run = (
        next((item for item in runs if item.id == grading_run_id), None)
        if grading_run_id is not None
        else (runs[-1] if runs else None)
    )
    if run is None or run.status not in {"completed", "partial_failed"}:
        raise NotFound("grading_run")
    payload = _run_snapshot_payload(
        task_id=task_id, owner_id=owner_id, run_id=run.id
    )
    fingerprint = _hash_json(payload)
    return {
        "version": result_version, "fingerprint": fingerprint,
        "created_at": run.released_at or run.completed_at or run.created_at,
        "payload": payload,
    }


def generate_artifacts(
    *, task_id: str, owner_id: str, expected_revision: int
) -> dict:
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    runs = grading_repository.list_runs_for_assignment(task_id, actor_id=owner_id)
    if not runs or runs[-1].released_at is None:
        raise InvalidTransition("result_not_finalized")
    if workflow.workflow_revision != expected_revision:
        # An exact replay is accepted by the atomic persistence operation; a
        # stale request with no existing artifact still fails there.
        existing = workflow_repository.list_artifact_manifests(
            task_id, owner_id=owner_id
        )
        if not any(
            item.result_version == workflow.final_result_version
            and item.grading_run_id == runs[-1].id
            for item in existing
        ):
            raise VersionConflict("workflow_revision_conflict")
    version = workflow.final_result_version
    if version <= 0:
        raise InvalidTransition("result_not_finalized")
    snapshot = result_snapshot(
        task_id=task_id, owner_id=owner_id, result_version=version,
        grading_run_id=runs[-1].id,
    )
    task = get_task(task_id=task_id, owner_id=owner_id, full=False)
    generated_at = time.time()
    manifest = build_artifact_manifest(
        task_id=task_id, task_name=task["name"], snapshot=snapshot,
        generated_at=generated_at,
    )
    record, created, _updated_workflow = (
        workflow_repository.save_artifact_manifest_atomic(
            assignment_id=task_id,
            grading_run_id=runs[-1].id,
            owner_id=owner_id,
            result_version=version,
            result_fingerprint=snapshot["fingerprint"],
            manifest=manifest,
            expected_revision=expected_revision,
        )
    )
    return {
        "status": "ok" if created else "already_done",
        "unchanged": not created,
        **finalization(task_id=task_id, owner_id=owner_id),
        "artifacts": artifact_index(task_id=task_id, owner_id=owner_id),
    }


def artifact_index(*, task_id: str, owner_id: str) -> dict:
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    records = workflow_repository.list_artifact_manifests(task_id, owner_id=owner_id)
    runs = grading_repository.list_runs_for_assignment(task_id, actor_id=owner_id)
    final_result_dirty = bool(
        any(run.released_at is not None for run in runs)
        and runs
        and runs[-1].released_at is None
    )
    current = max(workflow.final_result_version, 1 if records else 0)
    versions = [
        {
            "version": record.result_version,
            "current": record.result_version == current,
            "status": (
                "stale"
                if record.result_version == current
                and (workflow.analysis_status == "stale" or final_result_dirty)
                else (
                    "ready"
                    if record.result_version == current
                    else "historical"
                )
            ),
            "confirmed_at": record.manifest.get("confirmed_at"),
            "generated_at": record.generated_at,
            "files": record.manifest.get("files", []),
        }
        for record in records
    ]
    if current > 0 and not any(item["version"] == current for item in versions):
        versions.insert(0, {
            "version": current,
            "current": True,
            "status": "not_generated",
            "confirmed_at": workflow.final_result_updated_at,
            "generated_at": None,
            "files": [],
        })
    return {
        "task_id": task_id, "current_result_version": current,
        "analysis_status": workflow.analysis_status,
        "analysis_result_version": workflow.analysis_result_version,
        "versions": versions,
    }


def artifact_bytes(
    *, task_id: str, owner_id: str, version: int, artifact_id: str
) -> tuple[bytes, str, str]:
    record = workflow_repository.get_artifact_manifest(
        task_id, version, owner_id=owner_id
    )
    snapshot = result_snapshot(
        task_id=task_id, owner_id=owner_id, result_version=version,
        grading_run_id=record.grading_run_id,
    )
    if snapshot["fingerprint"] != record.result_fingerprint:
        raise VersionConflict("artifact_source_changed")
    if record.manifest.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
        raise InvalidTransition(
            "Artifact renderer version is unsupported.",
            code="artifact_schema_unsupported",
        )
    task_name = str(record.manifest.get("task_name") or "")
    if not task_name:
        task_name = get_task(
            task_id=task_id, owner_id=owner_id, full=False
        )["name"]
    files = build_artifact_files(
        task_id=task_id, task_name=task_name, snapshot=snapshot,
        generated_at=record.generated_at,
    )
    expected_files = {
        str(item.get("artifact_id")): item
        for item in record.manifest.get("files", [])
        if isinstance(item, dict) and item.get("artifact_id")
    }
    for item in files:
        expected = expected_files.get(item.artifact_id)
        if (
            expected is None
            or expected.get("sha256")
            != hashlib.sha256(item.content).hexdigest()
            or expected.get("size_bytes") != len(item.content)
        ):
            raise VersionConflict("artifact_renderer_changed")
    rebuilt_manifest = build_artifact_manifest(
        task_id=task_id,
        task_name=task_name,
        snapshot=snapshot,
        generated_at=record.generated_at,
    )
    if (
        rebuilt_manifest.get("artifact_fingerprint")
        != record.manifest.get("artifact_fingerprint")
    ):
        raise VersionConflict("artifact_manifest_changed")
    if artifact_id == "bundle":
        return (
            build_artifact_bundle(files, record.manifest),
            "application/zip",
            f"smartai_{task_id}_v{version}_reports.zip",
        )
    artifact = next((item for item in files if item.artifact_id == artifact_id), None)
    if artifact is None:
        raise NotFound("artifact")
    return artifact.content, artifact.media_type, artifact.filename


def _get_task_tags(task_id: str, owner_id: str) -> list[str]:
    try:
        from backend.db import tag_repository
        return tag_repository.list_assignment_tag_ids(
            assignment_id=task_id, owner_id=owner_id
        )
    except (ImportError, AttributeError):
        return []


def _set_task_tags(task_id: str, owner_id: str, tag_ids: list[str]) -> None:
    try:
        from backend.db import tag_repository
        tag_repository.set_assignment_tags(
            assignment_id=task_id, owner_id=owner_id, tag_ids=tag_ids
        )
    except (ImportError, AttributeError):
        if tag_ids:
            raise ValidationError("tags_unavailable")
