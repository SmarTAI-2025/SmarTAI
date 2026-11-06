"""Deterministic versioned knowledge fixtures; never used by runtime writers."""
import time
import uuid

from backend.db.models import KnowledgeChunkRecord, KnowledgeDocumentRecord
from backend.db.session import session_scope


def replace_document_chunks(document_id: str, chunks: list[str]) -> None:
    with session_scope() as session:
        record = session.get(KnowledgeDocumentRecord, document_id)
        if record is None:
            return
        from backend.db.knowledge_storage_repository import lock_knowledge_owner_in_session
        lock_knowledge_owner_in_session(session, record.owner_id)
        version = "native_" + uuid.uuid4().hex
        session.add_all([
            KnowledgeChunkRecord(id=f"chunk_{uuid.uuid4().hex[:16]}", document_id=document_id,
                                 content_version=version, chunk_index=index, content=content,
                                 chunk_metadata=dict(content_version=version, source_sha256=record.sha256), token_count=len(content.split()))
            for index, content in enumerate(chunks)
        ])
        if record:
            record.active_version = version
            record.chunk_count = len(chunks)
            record.status = "ready"
            record.error_code = None
            record.updated_at = time.time()
