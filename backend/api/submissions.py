"""Submissions API router — student online submission and teacher batch import.

Student and teacher ingestion both call the same submission service, which
flows through one immutable-revision pipeline. Structured answers and OCR file
uploads therefore produce the same normalized revision/answer records.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from backend.auth import get_current_user, require_student, require_teacher
from backend.api.errors import domain_error_response
from backend.domain.errors import DomainError
from backend.llm.registry import ExpertRegistry, get_scoped_expert_registry
from backend.models import User
from backend.services import submissions as submission_service
from backend.skills.ocr_ingest import LLMVisionOCRSkill

router = APIRouter(prefix="/submissions", tags=["submissions"])


class AnswerItem(BaseModel):
    q_id: str
    content: str = ""
    number: Optional[str] = None
    flag: List[str] = []


class SubmitRequest(BaseModel):
    assignment_id: str
    answers: List[AnswerItem]


class ImportItem(BaseModel):
    student_id: str
    file_name: str = ""
    answers: List[AnswerItem]


class ImportRequest(BaseModel):
    assignment_id: str
    items: List[ImportItem]


def _serialize_revision(rev) -> dict:
    return {
        "id": rev.id,
        "submission_id": rev.submission_id,
        "revision_number": rev.revision_number,
        "source": rev.source,
        "file_name": rev.file_name,
        "created_at": rev.created_at,
        "answers": [
            {
                "id": a.id, "question_id": a.question_id, "q_id": a.q_id,
                "number": a.number, "type": a.type, "content": a.content, "flag": a.flag,
            }
            for a in rev.answers
        ],
    }


@router.post("/submit")
def submit(req: SubmitRequest, current: User = Depends(require_student)):
    try:
        rev = submission_service.submit_online(
            student_id=current.id, assignment_id=req.assignment_id,
            answers=[a.model_dump() for a in req.answers],
        )
    except DomainError as exc:
        return domain_error_response(exc)
    return _serialize_revision(rev)


@router.post("/teacher-import")
def teacher_import(req: ImportRequest, current: User = Depends(require_teacher)):
    try:
        result = submission_service.teacher_import(
            teacher_id=current.id, assignment_id=req.assignment_id,
            items=[
                {"student_id": it.student_id, "file_name": it.file_name,
                 "answers": [a.model_dump() for a in it.answers]}
                for it in req.items
            ],
        )
    except DomainError as exc:
        return domain_error_response(exc)
    # 200 always, but the body carries succeeded/failed so partial failure is
    # visible; clients must inspect ``failed`` rather than treat 200 as "all in".
    return result


@router.get("/assignment/{assignment_id}")
def list_submissions(assignment_id: str, current: User = Depends(get_current_user)):
    try:
        items = submission_service.list_submissions(
            actor_id=current.id, assignment_id=assignment_id, role=current.role
        )
    except DomainError as exc:
        return domain_error_response(exc)
    return [
        {
            "id": s.id, "assignment_id": s.assignment_id, "student_id": s.student_id,
            "current_revision_number": s.current_revision_number, "created_at": s.created_at,
            "updated_at": s.updated_at,
        }
        for s in items
    ]


@router.post("/upload")
async def submit_upload(current: User = Depends(require_student),
                        assignment_id: str = Form(...),
                        file: UploadFile = File(...),
                        registry: ExpertRegistry = Depends(get_scoped_expert_registry)):
    """OCR/parse a student's file into answers and append an immutable revision."""
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
        rev = await submission_service.submit_student_file_with_ocr(
            student_id=current.id,
            assignment_id=assignment_id,
            filename=file.filename or "upload.bin",
            content=content,
            content_type=file.content_type,
            provider=provider,
            ocr_skill=ocr_skill,
        )
    except DomainError as exc:
        return domain_error_response(exc)
    return _serialize_revision(rev)


@router.post("/teacher-upload")
async def teacher_upload(
    current: User = Depends(require_teacher),
    assignment_id: str = Form(...),
    student_id: str = Form(...),
    file: UploadFile = File(...),
    registry: ExpertRegistry = Depends(get_scoped_expert_registry),
):
    """OCR/parse a teacher-uploaded file for one explicitly selected student."""
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
        rev = await submission_service.teacher_import_file_with_ocr(
            teacher_id=current.id,
            student_id=student_id,
            assignment_id=assignment_id,
            filename=file.filename or "upload.bin",
            content=content,
            content_type=file.content_type,
            provider=provider,
            ocr_skill=ocr_skill,
        )
    except DomainError as exc:
        return domain_error_response(exc)
    return _serialize_revision(rev)


@router.get("/detail/{submission_id}")
def get_submission(submission_id: str, current: User = Depends(get_current_user)):
    try:
        sub = submission_service.get_submission(
            actor_id=current.id, submission_id=submission_id, role=current.role
        )
    except DomainError as exc:
        return domain_error_response(exc)
    return {
        "id": sub.id, "assignment_id": sub.assignment_id, "student_id": sub.student_id,
        "current_revision_id": sub.current_revision_id,
        "current_revision_number": sub.current_revision_number,
        "created_at": sub.created_at, "updated_at": sub.updated_at,
    }


class CorrectRequest(BaseModel):
    answers: List[AnswerItem]


@router.post("/detail/{submission_id}/correct")
def correct_submission(submission_id: str, req: CorrectRequest,
                       current: User = Depends(require_student)):
    """Answer correction appends a new immutable revision (never mutates)."""
    try:
        rev = submission_service.correct_answer(
            student_id=current.id, submission_id=submission_id,
            answers=[a.model_dump() for a in req.answers],
        )
    except DomainError as exc:
        return domain_error_response(exc)
    return _serialize_revision(rev)
