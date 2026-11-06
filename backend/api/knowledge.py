"""Knowledge activity, retrieval, ingestion recovery and citation API.

Library CRUD lives under /course-materials; task attachments use /tasks/*/kb.
This router supplies their shared retrieval and ingestion controls.
"""
from __future__ import annotations

from urllib.parse import quote
from typing import Literal

from fastapi import APIRouter, Depends, Form, Query, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from backend.api.errors import domain_error_response
from backend.auth import get_current_user, get_task_user, require_teacher
from backend.db.knowledge_repository import (
    get_document,
)
from backend.domain.errors import DomainError, NotFound
from backend.knowledge.service import document_file
from backend.models import User
from backend.storage import get_storage
from backend.llm.registry import get_scoped_expert_registry

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    document_ids: list[str] = Field(min_length=1, max_length=20)
    limit: int = Field(default=5, ge=1, le=10)
    query_provider_id: str | None = Field(default=None, max_length=200)


@router.get("/activity")
def knowledge_activity(q: str = Query(default="", max_length=128),
                       state: Literal["all", "active", "attention", "completed"] = "all",
                       page: int = Query(default=1, ge=1, le=10000),
                       page_size: int = Query(default=20, ge=1, le=100),
                       prioritize_active: bool = False,
                       current: User = Depends(require_teacher)):
    from backend.knowledge.activity import list_activity
    return list_activity(current.id, query=q, state=state, page=page,
                         page_size=page_size, prioritize_active=prioritize_active)


@router.post("/search")
async def search_knowledge(request: KnowledgeSearchRequest, current: User = Depends(get_task_user)):
    from dataclasses import asdict
    from backend.knowledge.retriever import PersistentKnowledgeRetriever, _reference
    from backend.db.knowledge_ingestion_repository import live_document
    from backend.db.knowledge_repository import _document
    from backend.db.session import session_scope
    from backend.knowledge.query_plan import QUERY_PLANNER, QueryPlan, fuse_results
    try:
        with session_scope() as session:
            documents = [_document(live_document(session, key, current.id)) for key in dict.fromkeys(request.document_ids)]
        refs = [_reference(doc) for doc in documents if doc.chunk_count and doc.status in {"ready", "partial"}]
        retriever = PersistentKnowledgeRetriever()
        chunks = await retriever.retrieve_documents(request.query, request.limit,
            owner_id=current.id, documents=documents, refs=refs)
        plan = QueryPlan()
        if request.query_provider_id:
            registry = await run_in_threadpool(get_scoped_expert_registry, current)
            provider = registry.get(request.query_provider_id)
            if registry.uses_shared_pool() or provider is None or not provider.config.enabled:
                raise HTTPException(status.HTTP_404_NOT_FOUND, detail={"code":"knowledge_query_provider_unavailable"})
            if refs:
                plan = await QUERY_PLANNER.plan(request.query, provider=provider, scope="owner:" + current.id)
                groups = [chunks]
                for query in plan.queries:
                    groups.append(await retriever.retrieve_documents(query, request.limit,
                        owner_id=current.id, documents=documents, refs=refs))
                chunks = fuse_results(groups, request.limit)
                # Recheck after model work, including all cached original results.
                from backend.knowledge.retriever import live_references
                visible = await run_in_threadpool(live_references, current.id, refs)
                chunks = [c for c in chunks if c.citation.get("document_id") in visible]
        return dict(matches=[asdict(chunk) for chunk in chunks], documents=[doc.public() for doc in documents],
                    matched=bool(chunks), retrieval="query_expansion_rrf" if plan.queries else "local_bm25",
                    provider_calls=plan.provider_calls, query_plan=asdict(plan))
    except DomainError as exc:
        return domain_error_response(exc)


@router.get("/documents/{document_id}/citations/{chunk_id}")
def get_citation(document_id: str, chunk_id: str, current: User = Depends(get_task_user)):
    from backend.db.knowledge_ingestion_repository import live_document
    from backend.db.models import KnowledgeChunkRecord
    from backend.db.session import session_scope
    try:
        with session_scope() as session:
            doc = live_document(session, document_id, current.id)
            chunk = session.get(KnowledgeChunkRecord, chunk_id)
            if chunk is None or chunk.document_id != doc.id:
                raise NotFound("knowledge_citation")
            return dict(chunk_id=chunk.id, content=chunk.content, document_id=doc.id,
                        content_version=chunk.content_version, source_sha256=doc.sha256,
                        metadata=chunk.chunk_metadata, original_name=doc.original_name)
    except DomainError as exc:
        return domain_error_response(exc)


@router.get("/storage/usage")
def get_knowledge_storage_usage(current: User = Depends(get_current_user)):
    from backend.db.knowledge_storage_repository import knowledge_storage_usage

    try:
        from sqlalchemy import select, func
        from backend.db.models import KnowledgeEvidenceRecord, KnowledgeChunkRecord, KnowledgeDocumentRecord
        from backend.db.session import session_scope
        with session_scope() as session:
            evidence_bytes = session.scalar(select(func.coalesce(func.sum(KnowledgeEvidenceRecord.size_bytes), 0))
                .join(KnowledgeDocumentRecord, KnowledgeDocumentRecord.id == KnowledgeEvidenceRecord.document_id)
                .where(KnowledgeDocumentRecord.owner_id == current.id))
            text_chars = session.scalar(select(func.coalesce(func.sum(func.length(KnowledgeChunkRecord.content)), 0))
                .join(KnowledgeDocumentRecord, KnowledgeDocumentRecord.id == KnowledgeChunkRecord.document_id)
                .where(KnowledgeDocumentRecord.owner_id == current.id))
        return dict(knowledge_storage_usage(current.id).as_dict(), recognition_evidence_bytes=evidence_bytes, indexed_text_characters=text_chars)
    except DomainError as exc:
        return domain_error_response(exc)


@router.get("/documents/{document_id}/coverage")
def document_coverage(document_id: str, offset: int = Query(default=0, ge=0),
                      limit: int = Query(default=100, ge=1, le=100), issues_only: bool = False,
                      current: User = Depends(get_current_user)):
    from backend.db.knowledge_ingestion_repository import manifest
    try:
        return manifest(document_id, current.id, offset=offset, limit=limit, issues_only=issues_only)
    except DomainError as exc:
        return domain_error_response(exc)


@router.post("/documents/{document_id}/resume")
def resume_document(document_id: str, current: User = Depends(get_current_user)):
    from backend.db.knowledge_ingestion_repository import resume
    try:
        resume(document_id, current.id)
        return {"status": "queued"}
    except DomainError as exc:
        return domain_error_response(exc)


@router.post("/documents/{document_id}/cancel")
def cancel_document(document_id: str, current: User = Depends(get_current_user)):
    from backend.db.knowledge_ingestion_repository import cancel
    try:
        cancel(document_id, current.id)
        return {"status": "cancelled"}
    except DomainError as exc:
        return domain_error_response(exc)


@router.post("/documents/{document_id}/retry-failed")
async def retry_document(document_id: str, recognition_route_id: str | None = Form(default=None),
                         accept_uncertain_resubmission: bool = Form(default=False),
                         current: User = Depends(get_current_user)):
    from backend.db.knowledge_ingestion_repository import queue_document, manifest
    from backend.knowledge.ingestion import frozen_configuration
    from backend.services.task_facade import _registry_for_owner
    from starlette.concurrency import run_in_threadpool
    try:
        # Authorize before loading provider credentials or creating a job.
        await run_in_threadpool(manifest, document_id, current.id, limit=1)
        registry = await run_in_threadpool(_registry_for_owner, current.id)
        document = await run_in_threadpool(get_document, document_id, current.id)
        configuration = frozen_configuration(current.id, registry, recognition_route_id,
            require_visual=bool(document and (document.content_type or "").startswith("image/")))
        job_id = await run_in_threadpool(queue_document, document_id, current.id, configuration,
                                        new_version=True, resubmit_uncertain=accept_uncertain_resubmission,
                                        retry_failed_only=True)
        return {"status": "queued", "id": job_id}
    except DomainError as exc:
        return domain_error_response(exc)


@router.get("/documents/{document_id}/citations/{chunk_id}/download")
def download_citation_original(document_id: str, chunk_id: str, current: User = Depends(get_task_user)):
    return _download_original(document_id, current, chunk_id=chunk_id)


def _download_original(document_id, current, *, chunk_id):
    def current_document():
        from backend.db.knowledge_ingestion_repository import live_document
        from backend.db.knowledge_repository import _document
        from backend.db.models import KnowledgeChunkRecord
        from backend.db.session import session_scope
        with session_scope() as session:
            try:
                doc = live_document(session, document_id, current.id)
            except DomainError:
                return None
            chunk = session.get(KnowledgeChunkRecord, chunk_id)
            return _document(doc) if chunk is not None and chunk.document_id == doc.id else None
    document = current_document()
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Knowledge document not found")
    stored = document_file(document, current.id)
    if stored is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Knowledge file not found")
    try:
        stream = get_storage().open(stored.storage_key)
        try:
            from backend.rag.chunker import MAX_FILE_BYTES
            body = stream.read(MAX_FILE_BYTES + 1)
        finally:
            stream.close()
    except (FileNotFoundError, ValueError):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Knowledge file content not found")
    import hashlib
    if len(body) != stored.size_bytes or hashlib.sha256(body).hexdigest() != stored.sha256 or current_document() is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Knowledge file unavailable")
    return Response(content=body, media_type=stored.content_type or "application/octet-stream",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(stored.original_name, safe='')}", "Cache-Control": "private, no-store"})
