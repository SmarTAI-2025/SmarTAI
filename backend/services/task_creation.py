"""Atomic creation of the normalized assignment behind a Figma task.

The idempotency key, assignment, presentation workflow, and initial tag links
must commit together.  Keeping them in one transaction prevents a process
crash or two concurrent requests from leaving duplicate orphan assignments.
"""
from __future__ import annotations

import hashlib
import time
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.db.models import (
    AssignmentRecord,
    AssignmentTagRecord,
    CourseRecord,
    TagRecord,
)
from backend.db.session import session_scope
from backend.db.workflow_repository import (
    AssignmentWorkflowRecord,
    TaskCreateIdempotencyRecord,
)
from backend.domain.errors import NotFound, ValidationError, VersionConflict


def create_task_bundle(
    *,
    owner_id: str,
    name: str,
    semester_id: str | None,
    course_id: str | None,
    tag_ids: list[str],
    idempotency_key: str,
    request_hash: str,
    system_course_code: str,
    system_course_name: str,
) -> tuple[str, bool]:
    """Return ``(assignment_id, created)`` for one idempotent request."""
    normalized_tag_ids = sorted(set(tag_ids))
    # One retry resolves either an idempotency-key race or two first tasks that
    # simultaneously try to create the same deterministic internal course.
    for retry in range(2):
        try:
            return _create_task_bundle_once(
                owner_id=owner_id,
                name=name,
                semester_id=semester_id,
                course_id=course_id,
                tag_ids=normalized_tag_ids,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                system_course_code=system_course_code,
                system_course_name=system_course_name,
            )
        except IntegrityError:
            if retry:
                raise
    raise AssertionError("unreachable")


def _create_task_bundle_once(
    *,
    owner_id: str,
    name: str,
    semester_id: str | None,
    course_id: str | None,
    tag_ids: list[str],
    idempotency_key: str,
    request_hash: str,
    system_course_code: str,
    system_course_name: str,
) -> tuple[str, bool]:
    now = time.time()
    with session_scope() as session:
        replay = session.scalar(
            select(TaskCreateIdempotencyRecord).where(
                TaskCreateIdempotencyRecord.owner_id == owner_id,
                TaskCreateIdempotencyRecord.idempotency_key == idempotency_key,
            )
        )
        if replay is not None:
            if replay.request_hash != request_hash:
                raise VersionConflict("idempotency_key_reused")
            return replay.assignment_id, False

        if course_id is not None:
            course = session.scalar(
                select(CourseRecord).where(
                    CourseRecord.id == course_id,
                    CourseRecord.teacher_id == owner_id,
                )
            )
            if course is None:
                raise NotFound("course")
            resolved_course_id = course.id
        else:
            course = session.scalar(
                select(CourseRecord)
                .where(
                    CourseRecord.teacher_id == owner_id,
                    CourseRecord.code == system_course_code,
                )
                .order_by(CourseRecord.created_at)
                .limit(1)
            )
            if course is None:
                owner_digest = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:20]
                course = CourseRecord(
                    id=f"course_system_{owner_digest}",
                    name=system_course_name,
                    code=system_course_code,
                    description="Internal course for tasks without a selected course.",
                    teacher_id=owner_id,
                    created_at=now,
                    updated_at=now,
                )
                session.add(course)
                session.flush()
            resolved_course_id = course.id

        if tag_ids:
            owned_tag_ids = set(session.scalars(
                select(TagRecord.id).where(
                    TagRecord.owner_id == owner_id,
                    TagRecord.id.in_(tag_ids),
                )
            ).all())
            if owned_tag_ids != set(tag_ids):
                raise ValidationError("invalid_tag_ids")

        assignment_id = f"asg_{uuid.uuid4().hex[:12]}"
        session.add(AssignmentRecord(
            id=assignment_id,
            course_id=resolved_course_id,
            teacher_id=owner_id,
            name=name,
            description="",
            status="draft",
            due_at=None,
            created_at=now,
            updated_at=now,
            published_at=None,
            version=1,
        ))
        session.flush()
        session.add(AssignmentWorkflowRecord(
            assignment_id=assignment_id,
            owner_id=owner_id,
            semester_id=semester_id,
            presentation_status="draft",
            workflow_revision=0,
            submission_identity_mode="filename",
            final_result_version=0,
            analysis_status="not_generated",
            created_at=now,
            updated_at=now,
        ))
        session.add(TaskCreateIdempotencyRecord(
            id=f"idem_{uuid.uuid4().hex[:12]}",
            owner_id=owner_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            assignment_id=assignment_id,
            created_at=now,
        ))
        for tag_id in tag_ids:
            session.add(AssignmentTagRecord(
                assignment_id=assignment_id,
                tag_id=tag_id,
                assigned_at=now,
            ))
        session.flush()
        return assignment_id, True
