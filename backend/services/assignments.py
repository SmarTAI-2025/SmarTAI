"""Assignment and question state machine + optimistic locking orchestration.

The service wraps assignment_repository so the API layer deals only with DTOs
and DomainError codes. Transition rules (publish requires questions, published
question sets are frozen) live here and in the repository; the service makes
the intent readable and keeps the API thin.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from backend.agents.ingest_agent import extract_problems
from backend.db import assignment_repository
from backend.db.file_repository import save_file
from backend.domain import education
from backend.domain.errors import Forbidden, InvalidTransition, NotFound, ValidationError
from backend.storage import get_storage
from backend.tools.file_processing import extract_text_from_upload

if TYPE_CHECKING:
    from backend.llm.providers import BaseProvider
    from backend.skills.ocr_ingest import OCRIngestSkill


def create_assignment(*, teacher_id: str, course_id: str, name: str,
                      description: str = "", due_at: float | None = None) -> education.AssignmentDTO:
    if not name.strip():
        raise ValidationError("Assignment name is required")
    return assignment_repository.create_assignment(
        teacher_id=teacher_id, course_id=course_id, name=name.strip(),
        description=description, due_at=due_at,
    )


def get_assignment(*, assignment_id: str, actor_id: str, role: str) -> education.AssignmentDTO:
    if role == "teacher":
        return assignment_repository.get_assignment(assignment_id=assignment_id, actor_id=actor_id)
    if role == "student":
        # Students see only published assignments in courses they are enrolled in.
        published = assignment_repository.list_assignments_for_student(student_id=actor_id)
        for a in published:
            if a.id == assignment_id:
                return a
        raise NotFound("assignment")
    if role == "admin":
        return assignment_repository.get_assignment_unscoped(assignment_id)
    raise Forbidden("invalid_role")


def list_assignments(*, course_id: str | None, actor_id: str, role: str) -> list[education.AssignmentDTO]:
    if role == "teacher":
        if not course_id:
            raise ValidationError("course_id is required for teacher assignment lists")
        return assignment_repository.list_assignments(course_id=course_id, actor_id=actor_id)
    if role == "student":
        return assignment_repository.list_assignments_for_student(student_id=actor_id)
    if role == "admin":
        if not course_id:
            raise ValidationError("course_id is required for admin assignment lists")
        return assignment_repository.list_assignments_unscoped(course_id=course_id)
    raise Forbidden("invalid_role")


def add_question(*, assignment_id: str, teacher_id: str, q_id: str, order_index: int,
                 type: str, stem: str = "", number: str = "", criterion: str = "",
                 max_score: float = 10.0, reference_answer: str | None = None,
                 test_cases: list | None = None, source: dict | None = None) -> education.QuestionDTO:
    if not q_id.strip():
        raise ValidationError("Question id is required")
    if not type.strip():
        raise ValidationError("Question type is required")
    return assignment_repository.add_question(
        assignment_id=assignment_id, teacher_id=teacher_id, q_id=q_id.strip(),
        order_index=order_index, type=type.strip(), stem=stem, number=number,
        criterion=criterion, max_score=max_score, reference_answer=reference_answer,
        test_cases=test_cases, source=source,
    )


async def import_questions_from_upload(
    *,
    assignment_id: str,
    teacher_id: str,
    filename: str,
    content: bytes,
    content_type: str | None,
    provider: "BaseProvider",
    ocr_skill: "OCRIngestSkill | None" = None,
) -> list[education.QuestionDTO]:
    """Extract an uploaded problem sheet and persist normalized questions.

    Authorization and editability are checked before OCR/LLM work. The original
    file is retained against the assignment for later audit.
    """
    assignment = assignment_repository.get_assignment(
        assignment_id=assignment_id, actor_id=teacher_id
    )
    if assignment.status not in education.EDITABLE_ASSIGNMENT_STATUSES:
        raise InvalidTransition("assignment_not_editable")

    text = await extract_text_from_upload(
        content,
        filename,
        ocr_skill=ocr_skill,
        purpose="problems",
    )
    extracted: dict[str, dict] = {}
    await extract_problems(text, provider, extracted)
    if not extracted:
        raise ValidationError("No questions were extracted from the upload")

    existing = assignment_repository.list_questions(
        assignment_id=assignment_id, teacher_id=teacher_id
    )
    existing_qids = {question.q_id for question in existing}
    duplicate_qids = sorted(existing_qids.intersection(extracted))
    if duplicate_qids:
        raise ValidationError(
            "Question ids already exist: " + ", ".join(duplicate_qids)
        )

    created: list[education.QuestionDTO] = []
    order_offset = len(existing)
    for index, problem in enumerate(extracted.values()):
        created.append(
            add_question(
                assignment_id=assignment_id,
                teacher_id=teacher_id,
                q_id=str(problem.get("q_id", "")).strip(),
                order_index=order_offset + index,
                number=str(problem.get("number", "")),
                type=str(problem.get("type", "其他")),
                stem=str(problem.get("stem", "")),
                criterion=str(problem.get("criterion", "")),
                max_score=float(problem.get("max_score", 10.0)),
                source={"origin": "file_upload", "filename": filename},
            )
        )

    save_file(
        storage=get_storage(),
        owner_id=teacher_id,
        kind="problem",
        original_name=filename,
        content=content,
        content_type=content_type,
        assignment_id=assignment_id,
    )
    return created


def reorder_questions(*, assignment_id: str, teacher_id: str,
                      ordered_q_ids: list[str]) -> list[education.QuestionDTO]:
    """Apply a new display order to a draft/ready assignment's questions."""
    return assignment_repository.set_question_order(
        assignment_id=assignment_id, teacher_id=teacher_id, ordered_q_ids=ordered_q_ids
    )


def list_questions(*, assignment_id: str, actor_id: str, role: str) -> list[education.QuestionDTO]:
    if role in ("teacher", "admin"):
        return assignment_repository.list_questions(assignment_id=assignment_id, teacher_id=actor_id)
    if role == "student":
        # Students see questions only for published assignments they can access.
        get_assignment(assignment_id=assignment_id, actor_id=actor_id, role="student")
        return assignment_repository.get_questions_by_assignment(assignment_id=assignment_id)
    raise Forbidden("invalid_role")


def publish(*, assignment_id: str, teacher_id: str, expected_version: int) -> education.AssignmentDTO:
    return assignment_repository.publish(
        assignment_id=assignment_id, teacher_id=teacher_id, expected_version=expected_version
    )


def close(*, assignment_id: str, teacher_id: str, expected_version: int) -> education.AssignmentDTO:
    return assignment_repository.close(
        assignment_id=assignment_id, teacher_id=teacher_id, expected_version=expected_version
    )
