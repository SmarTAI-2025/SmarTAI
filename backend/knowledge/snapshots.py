"""Immutable readable prefixes; capture under the knowledge owner write gate."""
from __future__ import annotations

from sqlalchemy import func, select

from backend.db.models import KnowledgeChunkRecord, KnowledgeDocumentRecord
from backend.db.knowledge_ingestion_repository import live_document
from backend.db.knowledge_storage_repository import lock_knowledge_owner_in_session
from backend.db.session import session_scope
from backend.domain.errors import InvalidTransition, ValidationError

LEGACY_INDEX_VERSION = "bm25-cjk2-ids-v1"
INDEX_VERSION = "bm25-cjk2-terms-v2"
SUPPORTED_INDEX_VERSIONS = frozenset({LEGACY_INDEX_VERSION, INDEX_VERSION})


def freeze_in_session(session, owner_id, document_ids, expected=None):
    lock_knowledge_owner_in_session(session, owner_id)
    ids = sorted(set(document_ids))
    if len(ids) > 20:
        raise ValidationError("knowledge_storage_manifest_invalid", code="knowledge_storage_manifest_invalid")
    refs = []
    if expected is not None and (not isinstance(expected, list) or len(expected) != len(ids)):
        raise ValidationError("knowledge_storage_manifest_invalid", code="knowledge_storage_manifest_invalid")
    for document_id in ids:
        doc = live_document(session, document_id, owner_id)
        if expected is None:
            if doc.status not in {"ready", "partial"} or doc.chunk_count <= 0:
                raise InvalidTransition("Selected knowledge has no readable content yet.", code="knowledge_content_not_ready")
            ref = dict(document_id=doc.id, content_version=doc.active_version or "legacy",
                chunk_count=doc.chunk_count, source_sha256=doc.sha256, index_version=INDEX_VERSION,
                coverage_complete=(doc.ingestion_summary or {}).get("coverage_complete", doc.status == "ready"),
                warning_pages=(doc.ingestion_summary or {}).get("warning_pages", 0),
                total_pages=(doc.ingestion_summary or {}).get("total_pages"))
        else:
            matches = [item for item in expected if isinstance(item, dict) and item.get("document_id") == doc.id]
            if len(matches) != 1:
                raise ValidationError("knowledge_storage_manifest_invalid", code="knowledge_storage_manifest_invalid")
            ref = dict(matches[0])
            if (type(ref.get("chunk_count")) is not int or not 0 < ref["chunk_count"] <= 1_000_000
                    or not isinstance(ref.get("content_version"), str) or len(ref["content_version"]) > 64
                    or ref.get("source_sha256") != doc.sha256 or ref.get("index_version") not in SUPPORTED_INDEX_VERSIONS):
                raise ValidationError("knowledge_storage_manifest_invalid", code="knowledge_storage_manifest_invalid")
        actual = session.scalar(select(func.count()).select_from(KnowledgeChunkRecord).where(
            KnowledgeChunkRecord.document_id == doc.id, KnowledgeChunkRecord.content_version == ref["content_version"],
            KnowledgeChunkRecord.chunk_index >= 0, KnowledgeChunkRecord.chunk_index < ref["chunk_count"]))
        if actual != ref["chunk_count"]:
            raise InvalidTransition("Knowledge content version is unavailable.", code="knowledge_content_version_unavailable")
        refs.append(ref)
    return refs


def freeze_documents(owner_id, document_ids):
    with session_scope() as session:
        return freeze_in_session(session, owner_id, document_ids)
