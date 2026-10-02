from __future__ import annotations

from pathlib import Path
from starlette.concurrency import run_in_threadpool

from backend.db.file_repository import StoredFile, get_file
from backend.db.knowledge_repository import KnowledgeDocument, get_document, request_document_deletion
from backend.rag.chunker import MAX_FILE_BYTES
from backend.services.knowledge_storage import persist_knowledge_upload
from backend.storage import get_storage
from backend.tools.file_processing import inspect_upload_content

KNOWLEDGE_UPLOAD_EXTENSIONS = {".pdf", ".txt", ".md", ".markdown"}


async def ingest_document(*, owner_id: str, original_name: str, content: bytes,
                          content_type: str | None = None, title: str | None = None,
                          retention_policy: str = "retained", origin_assignment_id: str | None = None,
                          registry=None, recognition_route_id=None, native_only: bool = False) -> KnowledgeDocument:
    """Persist before queueing. Upload completion is not whole-book completion."""
    from backend.db.knowledge_ingestion_repository import queue_document
    from backend.knowledge.ingestion import frozen_configuration, KnowledgeIngestionWorker

    if not content or len(content) > MAX_FILE_BYTES:
        raise ValueError("Knowledge files must contain 1 byte to 64 MiB.")
    safe_name = Path(original_name).name or "knowledge.txt"
    if Path(safe_name).suffix.lower() not in KNOWLEDGE_UPLOAD_EXTENSIONS:
        raise ValueError("Knowledge uploads support PDF, TXT and Markdown; convert other formats to PDF.")
    media_type = inspect_upload_content(content, safe_name, content_type).content_type
    visual = media_type == "application/pdf"
    if media_type.startswith("image/"):
        raise ValueError("Unsupported knowledge document type.")
    if registry is None and not native_only:
        from backend.services.task_facade import _registry_for_owner
        registry = await run_in_threadpool(_registry_for_owner, owner_id)
    configuration = frozen_configuration(owner_id, registry, recognition_route_id, native_only=native_only)
    upload = await run_in_threadpool(persist_knowledge_upload,
        storage=get_storage(), owner_id=owner_id, original_name=safe_name, content=content,
        content_type=media_type, title=(title or Path(safe_name).stem or safe_name)[:512],
        retention_policy=retention_policy, origin_assignment_id=origin_assignment_id,
        ingestion_configuration=configuration)
    document = get_document(upload.document_id, owner_id)
    if document is None:
        raise RuntimeError("Published knowledge upload has no document record")
    if not upload.created and document.status == "ready":
        return document
    # Repair a crash between original publication and queue creation as well.
    job_id = await run_in_threadpool(queue_document, document.id, owner_id, configuration)
    if not visual and len(content) <= 256 * 1024:
        await KnowledgeIngestionWorker(registry_factory=lambda _: registry).run_once(job_id)
    return get_document(document.id, owner_id) or document


def document_file(document: KnowledgeDocument, owner_id: str) -> StoredFile | None:
    return get_file(file_id=document.stored_file_id, owner_id=owner_id) if document.stored_file_id else None


def remove_document(*, document: KnowledgeDocument, owner_id: str):
    return request_document_deletion(document.id, owner_id)
