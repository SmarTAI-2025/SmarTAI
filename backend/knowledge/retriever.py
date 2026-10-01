from __future__ import annotations

from typing import Optional
from starlette.concurrency import run_in_threadpool
from sqlalchemy import select

from backend.db.knowledge_repository import get_document, list_selected_documents
from backend.db.knowledge_storage_repository import visible_document_ids
from backend.db.models import AssignmentRecord
from backend.db.session import session_scope
from backend.domain.errors import DomainError
from backend.knowledge.index import INDEX_CACHE
from backend.knowledge.snapshots import INDEX_VERSION
from backend.db.knowledge_ingestion_repository import live_document
from backend.rag.store import InMemoryTaskRetriever
from backend.tools.knowledge import KnowledgeChunk, KnowledgeRetriever


def _reference(doc):
    return dict(document_id=doc.id, content_version=doc.active_version or "legacy", chunk_count=doc.chunk_count,
        source_sha256=doc.sha256, index_version=INDEX_VERSION,
        coverage_complete=(doc.ingestion_summary or {}).get("coverage_complete", doc.status == "ready"),
        warning_pages=(doc.ingestion_summary or {}).get("warning_pages", 0))


def resolve_scope(scope):
    """Read-only scope resolution. Never queue, resume, parse or repair here."""
    if not scope:
        return None, [], []
    if scope.startswith("grading-run:"):
        from backend.db import grading_repository
        from backend.db.workflow_repository import get_run_setup
        try:
            run = grading_repository.get_run(scope.removeprefix("grading-run:"))
        except DomainError:
            return None, [], []
        setup = get_run_setup(run.id)
        if setup is None or setup.owner_id != run.teacher_id or setup.setup.get("knowledge_scope") == "none":
            return None, [], []
        owner_id, assignment_id = run.teacher_id, run.assignment_id
        manifest = setup.input_manifest or {}
        ids = manifest.get("knowledge_document_ids", [])
        visible = set(visible_document_ids(owner_id, include_task_only=True, document_ids=ids))
        documents = [doc for key in ids if isinstance(key, str) and key in visible
                     and (doc := get_document(key, owner_id)) is not None]
        if "knowledge_content_versions" in manifest:
            allowed = {doc.id: doc for doc in documents}
            refs = [dict(ref) for ref in manifest["knowledge_content_versions"] if isinstance(ref, dict)
                    and ref.get("document_id") in allowed and ref.get("index_version") == INDEX_VERSION
                    and ref.get("source_sha256") == allowed[ref["document_id"]].sha256
                    and isinstance(ref.get("content_version"), str) and type(ref.get("chunk_count")) is int
                    and ref["chunk_count"] > 0]
        else:
            # Pre-version manifests cannot authorize a subsequently replaced
            # book. Legacy content remains readable only while still legacy.
            refs = [_reference(doc) for doc in documents if doc.active_version is None and doc.status == "ready"]
    else:
        assignment_id = scope
        with session_scope() as session:
            assignment = session.get(AssignmentRecord, assignment_id)
            if assignment is None:
                return None, [], []
            owner_id = assignment.teacher_id
        documents = list_selected_documents(assignment_id, owner_id)
        refs = [_reference(doc) for doc in documents if doc.chunk_count > 0]
    with session_scope() as session:
        active = session.scalar(select(AssignmentRecord.id).where(AssignmentRecord.id == assignment_id,
            AssignmentRecord.teacher_id == owner_id, AssignmentRecord.deletion_requested_at.is_(None)))
    return (owner_id, documents, refs) if active else (None, [], [])


def list_selected_documents_for_scope(scope):
    return resolve_scope(scope)[1]


def live_references(owner_id, refs):
    available = set()
    with session_scope() as session:
        for ref in refs:
            try:
                doc = live_document(session, ref["document_id"], owner_id)
            except DomainError:
                continue
            if doc.sha256 == ref["source_sha256"]:
                available.add(doc.id)
    return available


class PersistentKnowledgeRetriever(KnowledgeRetriever):
    """Owner/version-scoped local retrieval with bounded reusable indexes."""
    def __init__(self, *, cache=None):
        self.cache = cache or INDEX_CACHE

    async def retrieve(self, query: str, k: int = 5, *, scope: Optional[str] = None) -> list[KnowledgeChunk]:
        if not query or not scope:
            return []
        owner_id, documents, refs = await run_in_threadpool(resolve_scope, scope)
        result = await self.retrieve_documents(query, k, owner_id=owner_id, documents=documents, refs=refs)
        current_owner, _, _ = await run_in_threadpool(resolve_scope, scope)
        return result if current_owner == owner_id else []

    async def retrieve_documents(self, query, k=5, *, owner_id, documents, refs):
        if not owner_id or not query.strip() or not refs:
            return []
        by_id = {doc.id: doc for doc in documents if doc.owner_id == owner_id}
        visible_before = await run_in_threadpool(live_references, owner_id, refs)
        if any(ref["document_id"] not in visible_before or ref["document_id"] not in by_id
               or ref["source_sha256"] != by_id[ref["document_id"]].sha256 for ref in refs):
            return []
        index = await run_in_threadpool(self.cache.get, owner_id, refs)
        if len(index.chunks) != sum(ref["chunk_count"] for ref in refs):
            return []
        limit = min(10, max(0, int(k)))
        found = await run_in_threadpool(index.search, query, limit)
        if 0 < len(found) < limit:
            found = index.adjacent(found, limit)
        # A cache entry is never an authorization token. Recheck after CPU/DB
        # work so deletion and cleanup cannot expose retained in-memory text.
        visible = await run_in_threadpool(live_references, owner_id, refs)
        references = {ref["document_id"]: ref for ref in refs}
        output = []
        for chunk, score in found:
            if chunk.document_id not in visible or chunk.document_id not in by_id:
                continue
            document, ref = by_id[chunk.document_id], references[chunk.document_id]
            metadata = chunk.metadata
            citation = dict(citation_id="kb:" + chunk.id, chunk_id=chunk.id, document_id=chunk.document_id,
                content_version=chunk.content_version, readable_chunk_count=ref["chunk_count"], source_sha256=ref["source_sha256"],
                original_name=document.original_name, page_number=metadata.get("page_number"),
                unit=metadata.get("unit", "page"), start=metadata.get("start"), end=metadata.get("end"),
                artifact_ids=metadata.get("artifact_ids", []), confidence=metadata.get("confidence", "unverified"),
                warning_codes=metadata.get("warning_codes", []), coverage_complete=ref.get("coverage_complete", False),
                page_state=metadata.get("page_state"), index_version=INDEX_VERSION,
                provenance="retrieved_reference_not_verified_claim")
            label = document.original_name
            if citation["page_number"] is not None:
                label += f" ({citation['unit']} {citation['page_number']})"
            output.append(KnowledgeChunk(content=chunk.content, source=label, score=score, citation=citation))
        return output


class CombinedKnowledgeRetriever(InMemoryTaskRetriever):
    def __init__(self) -> None:
        super().__init__()
        self._persistent = PersistentKnowledgeRetriever()

    async def retrieve(self, query: str, k: int = 5, *, scope: Optional[str] = None) -> list[KnowledgeChunk]:
        transient = [] if scope and scope.startswith("grading-run:") else await super().retrieve(query, k, scope=scope)
        persistent = await self._persistent.retrieve(query, k, scope=scope)
        return sorted(transient + persistent, key=lambda item: item.score, reverse=True)[:max(0, int(k))]
