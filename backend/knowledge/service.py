from __future__ import annotations

import logging
from pathlib import Path

from backend.db.file_repository import StoredFile, get_file
from backend.db.knowledge_repository import (
    KnowledgeDocument,
    get_document,
    request_document_deletion,
    replace_document_chunks,
    update_document,
)
from backend.rag.chunker import MAX_FILE_BYTES, chunk_text, extract_text
from backend.services.knowledge_storage import persist_knowledge_upload
from backend.storage import get_storage


logger = logging.getLogger(__name__)


async def ingest_document(*, owner_id: str, original_name: str, content: bytes,
                          content_type: str | None = None, title: str | None = None,
                          retention_policy: str = "retained",
                          origin_assignment_id: str | None = None) -> KnowledgeDocument:
    if len(content) > MAX_FILE_BYTES:
        raise ValueError(f"Knowledge file too large ({len(content)} bytes > {MAX_FILE_BYTES}).")
    safe_name = Path(original_name).name or "knowledge.txt"
    upload = persist_knowledge_upload(
        storage=get_storage(),
        owner_id=owner_id,
        original_name=safe_name,
        content=content,
        content_type=content_type,
        title=(title or Path(safe_name).stem or safe_name)[:512],
        retention_policy=retention_policy,
        origin_assignment_id=origin_assignment_id,
    )
    document = get_document(upload.document_id, owner_id)
    if document is None:
        raise RuntimeError("Published knowledge upload has no document record")
    # Existing owner/hash content is canonical.  ``persist_knowledge_upload``
    # applies the monotonic task_only -> retained promotion before returning;
    # parsing belongs only to the process that created this upload.
    if not upload.created:
        return document

    try:
        text = await extract_text(safe_name, content)
        chunks = chunk_text(text)
        if not chunks:
            raise ValueError("Document produced no usable text chunks.")
        replace_document_chunks(document.id, chunks)
        return update_document(document.id, owner_id, status="ready", chunk_count=len(chunks)) or document
    except Exception:
        update_document(document.id, owner_id, status="failed", error_code="parse_failed")
        if retention_policy == "task_only":
            # Publication succeeded but the document can never be attached if
            # parsing fails.  Record cleanup now; the worker owns physical
            # deletion/retry and quota remains charged until it succeeds.
            from backend.db.knowledge_storage_repository import (
                request_task_only_cleanup_if_unreferenced,
            )
            from backend.domain.knowledge_storage import (
                KNOWLEDGE_CLEANUP_TASK_ATTACH_FAILED,
            )

            try:
                request_task_only_cleanup_if_unreferenced(
                    document.id,
                    owner_id,
                    reason=KNOWLEDGE_CLEANUP_TASK_ATTACH_FAILED,
                )
            except Exception:
                # Preserve the parser failure seen by the caller.  Publication
                # gave every unattached task-only record a durable grace
                # deadline, so the cleanup worker can still reclaim it if this
                # eager enqueue attempt hits a transient database failure.
                logger.warning(
                    "Failed to enqueue task-only knowledge parse cleanup; document_id=%s",
                    document.id,
                    exc_info=True,
                )
        raise


def document_file(document: KnowledgeDocument, owner_id: str) -> StoredFile | None:
    if not document.stored_file_id:
        return None
    return get_file(file_id=document.stored_file_id, owner_id=owner_id)


def remove_document(*, document: KnowledgeDocument, owner_id: str):
    """Durably request cleanup; the background worker performs object delete."""
    return request_document_deletion(document.id, owner_id)
