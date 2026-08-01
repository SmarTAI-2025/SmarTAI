"""Personal knowledge API — document upload/list/download/delete + assignment-
scoped selection.

The legacy task-scoped selection route is gone: a teacher selects up to three
ready personal documents per *assignment* they own, and the retriever reads
that selection via the assignment scope. Document CRUD stays owner-scoped.
"""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel, Field

from backend.api.errors import domain_error_response
from backend.auth import get_current_user, require_teacher
from backend.db.knowledge_repository import (
    get_document,
    list_documents,
    list_selected_documents,
    set_task_documents,
)
from backend.db.assignment_repository import get_assignment
from backend.domain.errors import DomainError, NotFound
from backend.knowledge.service import document_file, ingest_document, remove_document
from backend.models import User
from backend.storage import get_storage

router = APIRouter(prefix="/knowledge", tags=["knowledge"])
# Assignment-scoped knowledge selection (replaces the legacy task_router).
assignment_router = APIRouter(prefix="/assignments", tags=["assignment-knowledge"])


class AssignmentKnowledgeSelection(BaseModel):
    document_ids: list[str] = Field(default_factory=list, max_length=20)


@router.post("/documents", status_code=status.HTTP_201_CREATED)
async def upload_document(file: UploadFile = File(...), current: User = Depends(get_current_user)):
    body = await file.read()
    try:
        document = await ingest_document(owner_id=current.id, original_name=file.filename or "knowledge.txt",
                                         content=body, content_type=file.content_type)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return document.public()


@router.get("/documents")
def get_documents(current: User = Depends(get_current_user)):
    return {"documents": [document.public() for document in list_documents(current.id)]}


@router.get("/documents/{document_id}")
def get_document_detail(document_id: str, current: User = Depends(get_current_user)):
    document = get_document(document_id, current.id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Knowledge document not found")
    return document.public()


@router.get("/documents/{document_id}/download")
def download_document(document_id: str, current: User = Depends(get_current_user)):
    document = get_document(document_id, current.id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Knowledge document not found")
    stored = document_file(document, current.id)
    if stored is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Knowledge file not found")
    try:
        stream = get_storage().open(stored.storage_key)
        try:
            body = stream.read()
        finally:
            stream.close()
    except (FileNotFoundError, ValueError):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Knowledge file content not found")
    return Response(content=body, media_type=stored.content_type or "application/octet-stream",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(stored.original_name, safe='')}"})


@router.delete("/documents/{document_id}")
def delete_document_endpoint(document_id: str, current: User = Depends(get_current_user)):
    document = get_document(document_id, current.id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Knowledge document not found")
    try:
        remove_document(document=document, owner_id=current.id)
    except DomainError as exc:
        return domain_error_response(exc)
    return {"status": "deleted", "id": document_id}


# ─── Assignment-scoped selection ──────────────────────────────────────────────


def _owned_assignment(assignment_id: str, current: User):
    """Resolve an assignment the teacher owns; non-owners get 404 (no leak)."""
    try:
        return get_assignment(assignment_id=assignment_id, actor_id=current.id)
    except NotFound:
        if current.role == "admin":
            from backend.db.assignment_repository import get_assignment_unscoped
            return get_assignment_unscoped(assignment_id)
        raise


@assignment_router.get("/{assignment_id}/knowledge-documents")
def assignment_knowledge_documents(assignment_id: str, current: User = Depends(require_teacher)):
    _owned_assignment(assignment_id, current)
    try:
        selected = list_selected_documents(assignment_id, current.id)
    except DomainError as exc:
        return domain_error_response(exc)
    return {"documents": [document.public() for document in selected]}


@assignment_router.put("/{assignment_id}/knowledge-documents")
def select_assignment_knowledge_documents(assignment_id: str, request: AssignmentKnowledgeSelection,
                                          current: User = Depends(require_teacher)):
    _owned_assignment(assignment_id, current)
    try:
        selected = set_task_documents(assignment_id=assignment_id, owner_id=current.id,
                                      document_ids=request.document_ids)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except DomainError as exc:
        return domain_error_response(exc)
    return {"documents": [document.public() for document in selected]}
