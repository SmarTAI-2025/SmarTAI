from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy.exc import IntegrityError

from backend.db.file_repository import StoredFile, delete_file_record, get_file, save_file
from backend.db.knowledge_repository import (
    KnowledgeDocument,
    create_document,
    delete_document,
    get_document_by_hash,
    replace_document_chunks,
    update_document,
)
from backend.rag.chunker import MAX_FILE_BYTES, chunk_text, extract_text
from backend.storage import get_storage


async def ingest_document(*, owner_id: str, original_name: str, content: bytes,
                          content_type: str | None = None, title: str | None = None) -> KnowledgeDocument:
    if len(content) > MAX_FILE_BYTES:
        raise ValueError(f"Knowledge file too large ({len(content)} bytes > {MAX_FILE_BYTES}).")
    safe_name = Path(original_name).name or "knowledge.txt"
    digest = hashlib.sha256(content).hexdigest()
    existing = get_document_by_hash(owner_id, digest)
    if existing is not None:
        return existing

    # ``knowledge_documents`` de-duplicates by (owner_id, sha256), so its PK
    # must be owner-aware as well. A digest-only ID collided when two teachers
    # uploaded identical bytes even though the SQL uniqueness rule permits it.
    identity = hashlib.sha256(f"{owner_id}\0{digest}".encode("utf-8")).hexdigest()
    document_id = f"doc_{identity[:16]}"
    stored: StoredFile | None = None
    try:
        # Create the document row first (stored_file_id=None) so the subsequent
        # stored_files row can reference it via knowledge_document_id under
        # SQLite's immediate FK check; we back-fill stored_file_id after saving.
        try:
            document = create_document(
                document_id=document_id,
                owner_id=owner_id,
                stored_file_id=None,
                title=(title or Path(safe_name).stem or safe_name)[:512],
                original_name=safe_name,
                content_type=content_type,
                size_bytes=len(content),
                sha256=digest,
            )
        except IntegrityError:
            # A concurrent upload by the same owner may win the unique
            # (owner_id, sha256) race. Return that canonical record instead of
            # creating a second object-storage copy.
            concurrent = get_document_by_hash(owner_id, digest)
            if concurrent is not None:
                return concurrent
            raise
        stored = save_file(storage=get_storage(), owner_id=owner_id, kind="personal_knowledge",
                           original_name=safe_name, content=content, content_type=content_type,
                           storage_prefix=f"users/{owner_id}/knowledge/{document_id}",
                           knowledge_document_id=document_id)
        update_document(document.id, owner_id, stored_file_id=stored.id)
        text = await extract_text(safe_name, content)
        chunks = chunk_text(text)
        if not chunks:
            raise ValueError("Document produced no usable text chunks.")
        replace_document_chunks(document.id, chunks)
        return update_document(document.id, owner_id, status="ready", chunk_count=len(chunks)) or document
    except Exception:
        if stored is not None:
            update_document(document_id, owner_id, status="failed", error_code="parse_failed")
            # Keep the original file for user deletion/retry; metadata records the failure.
        raise


def document_file(document: KnowledgeDocument, owner_id: str) -> StoredFile | None:
    if not document.stored_file_id:
        return None
    return get_file(file_id=document.stored_file_id, owner_id=owner_id)


def remove_document(*, document: KnowledgeDocument, owner_id: str) -> None:
    stored = document_file(document, owner_id)
    delete_document(document.id, owner_id)
    if stored is not None:
        get_storage().delete(stored.storage_key)
        delete_file_record(file_id=stored.id, owner_id=owner_id)
