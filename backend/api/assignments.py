"""Assignments API router — assignment + question resource endpoints.

Authorization and the question/publish state machine live in the service and
repository; this router maps request shapes to service calls and translates
DomainError into the stable error envelope. Optimistic-lock version is carried
in the request body (``expected_version``) and a mismatch surfaces as 409
``version_conflict``. Student submission endpoints are added in Task 5.
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from backend.auth import get_current_user, require_teacher
from backend.api.errors import domain_error_response
from backend.domain.errors import DomainError
from backend.llm.registry import ExpertRegistry, get_scoped_expert_registry
from backend.models import User
from backend.services import assignments as assignment_service
from backend.skills.ocr_ingest import LLMVisionOCRSkill

router = APIRouter(prefix="/assignments", tags=["assignments"])


class CreateAssignmentRequest(BaseModel):
    course_id: str
    name: str
    description: str = ""
    due_at: Optional[float] = None


class AddQuestionRequest(BaseModel):
    q_id: str
    order_index: int = 0
    number: str = ""
    type: str
    stem: str = ""
    criterion: str = ""
    max_score: float = 10.0
    reference_answer: Optional[str] = None
    test_cases: Optional[List[dict]] = None
    source: Optional[dict] = None


class ReorderRequest(BaseModel):
    ordered_q_ids: List[str]


class PublishRequest(BaseModel):
    expected_version: int


@router.post("")
def create_assignment(req: CreateAssignmentRequest, current: User = Depends(require_teacher)):
    try:
        a = assignment_service.create_assignment(
            teacher_id=current.id, course_id=req.course_id, name=req.name,
            description=req.description, due_at=req.due_at,
        )
    except DomainError as exc:
        return domain_error_response(exc)
    return _serialize_assignment(a)


@router.get("")
def list_assignments(course_id: Optional[str] = None, current: User = Depends(get_current_user)):
    try:
        items = assignment_service.list_assignments(
            course_id=course_id, actor_id=current.id, role=current.role
        )
    except DomainError as exc:
        return domain_error_response(exc)
    return [_serialize_assignment(a) for a in items]


@router.get("/{assignment_id}")
def get_assignment(assignment_id: str, current: User = Depends(get_current_user)):
    try:
        a = assignment_service.get_assignment(
            assignment_id=assignment_id, actor_id=current.id, role=current.role
        )
    except DomainError as exc:
        return domain_error_response(exc)
    return _serialize_assignment(a)


@router.get("/{assignment_id}/questions")
def list_questions(assignment_id: str, current: User = Depends(get_current_user)):
    try:
        questions = assignment_service.list_questions(
            assignment_id=assignment_id, actor_id=current.id, role=current.role
        )
    except DomainError as exc:
        return domain_error_response(exc)
    serializer = (
        _serialize_student_question if current.role == "student" else _serialize_question
    )
    return [serializer(q) for q in questions]


@router.post("/{assignment_id}/questions")
def add_question(assignment_id: str, req: AddQuestionRequest, current: User = Depends(require_teacher)):
    try:
        q = assignment_service.add_question(
            assignment_id=assignment_id, teacher_id=current.id, q_id=req.q_id,
            order_index=req.order_index, number=req.number, type=req.type, stem=req.stem,
            criterion=req.criterion, max_score=req.max_score, reference_answer=req.reference_answer,
            test_cases=req.test_cases, source=req.source,
        )
    except DomainError as exc:
        return domain_error_response(exc)
    return _serialize_question(q)


@router.post("/{assignment_id}/questions/import-file")
async def import_questions_file(
    assignment_id: str,
    file: UploadFile = File(...),
    current: User = Depends(require_teacher),
    registry: ExpertRegistry = Depends(get_scoped_expert_registry),
):
    provider = registry.pick_default()
    if provider is None:
        raise HTTPException(
            503, detail="No LLM provider configured. Add an API key first."
        )
    vision_provider = registry.pick_vision(provider)
    ocr_skill = (
        LLMVisionOCRSkill(vision_provider) if vision_provider is not None else None
    )
    content = await file.read()
    try:
        questions = await assignment_service.import_questions_from_upload(
            assignment_id=assignment_id,
            teacher_id=current.id,
            filename=file.filename or "problems",
            content=content,
            content_type=file.content_type,
            provider=provider,
            ocr_skill=ocr_skill,
        )
    except DomainError as exc:
        return domain_error_response(exc)
    return [_serialize_question(question) for question in questions]


@router.post("/{assignment_id}/questions/reorder")
def reorder_questions(assignment_id: str, req: ReorderRequest, current: User = Depends(require_teacher)):
    try:
        questions = assignment_service.reorder_questions(
            assignment_id=assignment_id, teacher_id=current.id, ordered_q_ids=req.ordered_q_ids
        )
    except DomainError as exc:
        return domain_error_response(exc)
    return [_serialize_question(q) for q in questions]


@router.post("/{assignment_id}/publish")
def publish_assignment(assignment_id: str, req: PublishRequest, current: User = Depends(require_teacher)):
    try:
        a = assignment_service.publish(
            assignment_id=assignment_id, teacher_id=current.id, expected_version=req.expected_version
        )
    except DomainError as exc:
        return domain_error_response(exc)
    return _serialize_assignment(a)


@router.post("/{assignment_id}/close")
def close_assignment(assignment_id: str, req: PublishRequest, current: User = Depends(require_teacher)):
    try:
        a = assignment_service.close(
            assignment_id=assignment_id, teacher_id=current.id, expected_version=req.expected_version
        )
    except DomainError as exc:
        return domain_error_response(exc)
    return _serialize_assignment(a)


def _serialize_assignment(a) -> dict:
    return {
        "id": a.id,
        "course_id": a.course_id,
        "teacher_id": a.teacher_id,
        "name": a.name,
        "description": a.description,
        "status": a.status,
        "due_at": a.due_at,
        "version": a.version,
        "question_count": a.question_count,
        "created_at": a.created_at,
        "updated_at": a.updated_at,
        "published_at": a.published_at,
    }


def _serialize_question(q) -> dict:
    return {
        "id": q.id,
        "assignment_id": q.assignment_id,
        "q_id": q.q_id,
        "order_index": q.order_index,
        "number": q.number,
        "type": q.type,
        "stem": q.stem,
        "criterion": q.criterion,
        "max_score": q.max_score,
        "reference_answer": q.reference_answer,
        "test_cases": q.test_cases,
        "source": q.source,
        "version": q.version,
    }


def _serialize_student_question(q) -> dict:
    return {
        "id": q.id,
        "assignment_id": q.assignment_id,
        "q_id": q.q_id,
        "order_index": q.order_index,
        "number": q.number,
        "type": q.type,
        "stem": q.stem,
        "max_score": q.max_score,
        "version": q.version,
    }
