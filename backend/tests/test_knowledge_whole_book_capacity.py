"""Opt-in full native-book worker benchmark; not a real OCR quality test."""
import json
import os
import time

import pytest
from sqlalchemy import func, select

from backend.tests.test_knowledge_ingestion import queued, pdf
from backend.knowledge.ingestion import KnowledgeIngestionWorker
from backend.db.knowledge_ingestion_repository import manifest
from backend.db.knowledge_repository import get_document
from backend.db.models import KnowledgeChunkRecord, KnowledgeEvidenceRecord
from backend.db.session import session_scope


@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get("SMARTAI_RUN_KNOWLEDGE_CAPACITY") != "1", reason="explicit full-book capacity benchmark")
async def test_thousand_native_pages_run_through_real_worker_without_truncation():
    who, document_id, job_id = queued(pdf(1000))
    worker = KnowledgeIngestionWorker(registry_factory=lambda _: None)
    start = time.perf_counter()
    first = None
    for batch in range(43):
        await worker.run_once(job_id)
        progress = manifest(document_id, who, limit=1)["summary"]
        if first is None:
            first = time.perf_counter() - start
        if progress["status"] in {"complete", "complete_with_warning", "partial", "paused", "cancelled"}:
            break
    assert progress["status"] in {"complete", "complete_with_warning"}, progress
    assert progress["total_pages"] == progress["searchable_pages"] == 1000
    assert progress["coverage_complete"]
    doc = get_document(document_id, who)
    with session_scope() as session:
        rows = list(session.scalars(select(KnowledgeChunkRecord).where(KnowledgeChunkRecord.document_id == doc.id)))
        evidence_bytes = session.scalar(select(func.sum(KnowledgeEvidenceRecord.size_bytes)).where(KnowledgeEvidenceRecord.document_id == doc.id))
    by_page = {row.chunk_metadata["page_number"]: row.content for row in rows}
    for page in (1, 500, 1000):
        assert f"Chapter {page}:" in by_page[page]
    assert len(by_page) == 1000
    assert progress["usage"]["initial_calls"] == progress["usage"]["patch_calls"] == 0
    print("WHOLE_BOOK_CAPACITY " + json.dumps(dict(pages=1000, batches=batch + 1,
        first_batch_seconds=round(first, 3), total_seconds=round(time.perf_counter() - start, 3),
        chunks=len(rows), derived_bytes=evidence_bytes, provider_calls=0, status=progress["status"], warning_pages=progress["warning_pages"]), sort_keys=True))
