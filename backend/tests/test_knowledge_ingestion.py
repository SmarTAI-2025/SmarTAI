import asyncio
import hashlib
import uuid
from types import SimpleNamespace

import fitz
import pytest
from sqlalchemy import select

from backend.db import knowledge_ingestion_repository as repo
from backend.db.knowledge_repository import get_document, list_chunks
from backend.db.models import UserRecord, KnowledgeIngestionRecord, KnowledgePageRecord, KnowledgeEvidenceRecord
from backend.db.session import session_scope
from backend.knowledge.ingestion import KnowledgeIngestionWorker, POLICY
from backend.knowledge.service import ingest_document
from backend.rag.chunker import chunk_spans
from backend.services.knowledge_storage import persist_knowledge_upload
from backend.storage import get_storage


class Registry:
    def list_configs(self):
        return []

    def pick_default(self):
        return None


def owner():
    value = "knowledge-" + uuid.uuid4().hex[:12]
    with session_scope() as session:
        session.add(UserRecord(id=value, username=value, password_hash="fake", role="teacher", is_active=True))
    return value


def pdf(pages=1):
    with fitz.open() as doc:
        for n in range(pages):
            page = doc.new_page()
            page.insert_text((50, 50), f"Chapter {n + 1}: field theory and finite groups")
        return doc.tobytes()


def queued(body=None):
    who = owner()
    body = body or pdf()
    upload = persist_knowledge_upload(storage=get_storage(), owner_id=who, original_name="book.pdf", content=body,
                                     content_type="application/pdf", retention_policy="retained")
    job_id = repo.queue_document(upload.document_id, who,
        dict(policy_version=POLICY, prompt_version="faithful-reader-v1", route_id=None, route_fingerprint=None))
    return who, upload.document_id, job_id


def test_lossless_chinese_and_long_code_after_old_500_limit():
    text = ("中文公式 x^{-1} = y\n" + "z" * 5000 + "\n") * 250
    chunks = chunk_spans(text)
    assert len(chunks) > 500
    reconstructed, end = "", 0
    for chunk in chunks:
        assert chunk["content"] == text[chunk["start"]:chunk["end"]]
        assert chunk["start"] <= end and len(chunk["content"]) <= 2000
        reconstructed += chunk["content"][end - chunk["start"]:]
        end = chunk["end"]
    assert reconstructed == text


@pytest.mark.asyncio
async def test_small_text_same_pipeline_ready_without_model():
    who = owner()
    doc = await ingest_document(owner_id=who, original_name="book.txt", content="群论\n映射 f(x)=x".encode(), registry=Registry())
    assert doc.status == "ready", doc.public()
    assert doc.ingestion_summary["coverage_complete"] is True
    assert list_chunks([doc.id])[0].chunk_metadata["start"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["book.png", "book.docx", "book.pptx", "book.rst"])
async def test_public_knowledge_upload_does_not_expand_confirmed_formats(name):
    with pytest.raises(ValueError, match="support PDF, TXT and Markdown"):
        await ingest_document(owner_id=owner(), original_name=name, content=b"synthetic", registry=Registry())


@pytest.mark.asyncio
async def test_pdf_batches_resume_native_evidence_and_paginated_manifest():
    who, doc_id, job_id = queued(pdf(25))
    worker = KnowledgeIngestionWorker(registry_factory=lambda _: Registry())
    await worker.run_once(job_id)
    first = get_document(doc_id, who)
    assert first.status == "partial", first.public()
    assert first.ingestion_summary["processed_pages"] == 24
    assert len(list_chunks([doc_id])) == 24
    await worker.run_once(job_id)
    final = get_document(doc_id, who)
    assert final.status == "ready", final.public()
    assert final.ingestion_summary["processed_pages"] == 25
    assert len(list_chunks([doc_id])) == 25
    assert len(repo.manifest(doc_id, who, limit=10)["pages"]) == 10
    with session_scope() as session:
        assert session.scalar(select(KnowledgeEvidenceRecord.id)) is not None
        pages = list(session.scalars(select(KnowledgePageRecord)))
        assert all(not p.operation["checkpoint"]["calls"] for p in pages)
    assert await worker.run_once(job_id) is False


def test_failed_new_version_keeps_published_chunks_and_cancel_fences():
    who, doc_id, job_id = queued()
    job = repo.claim_next(job_id=job_id)
    repo.initialize(job, 1)
    page = repo.next_pages(job)[0]
    repo.prepare_page(job, page.id, batch_extra_remaining=2)
    repo.finish_page(job, page.id, text="original theorem", state="searchable", evidence={})
    old = get_document(doc_id, who).active_version
    second_id = repo.queue_document(doc_id, who, job.configuration, new_version=True)
    second = repo.claim_next(job_id=second_id)
    repo.initialize(second, 1)
    page = repo.next_pages(second)[0]
    repo.prepare_page(second, page.id, batch_extra_remaining=2)
    repo.finish_page(second, page.id, state="failed", evidence={"error_code": "bad_scan"})
    assert get_document(doc_id, who).active_version == old
    assert [c.content for c in list_chunks([doc_id])] == ["original theorem"]
    third_id = repo.queue_document(doc_id, who, job.configuration, new_version=True)
    third = repo.claim_next(job_id=third_id)
    repo.initialize(third, 1)
    page = repo.next_pages(third)[0]
    repo.cancel(doc_id, who)
    from backend.domain.errors import LeaseLost, NotFound
    with pytest.raises(LeaseLost):
        repo.prepare_page(third, page.id, batch_extra_remaining=2)
    with pytest.raises(NotFound):
        repo.manifest(doc_id, owner())


def test_extra_budgets_survive_worker_batch_restart():
    who, doc_id, job_id = queued()
    job = repo.claim_next(job_id=job_id)
    repo.initialize(job, 1000)
    pages = repo.next_pages(job)
    first = repo.prepare_page(job, pages[0].id, batch_extra_remaining=2)
    second = repo.prepare_page(job, pages[1].id, batch_extra_remaining=2)
    third = repo.prepare_page(job, pages[2].id, batch_extra_remaining=2)
    assert first.extra_allowed and second.extra_allowed and not third.extra_allowed
    assert len(repo.manifest(doc_id, who)["pages"]) == 100


@pytest.mark.asyncio
async def test_large_native_upload_is_queued_not_rejected_at_5mib():
    who = owner()
    body = b"group theorem\n" * 420000
    assert len(body) > 5 * 1024 * 1024
    doc = await ingest_document(owner_id=who, original_name="large.txt", content=body, registry=Registry())
    assert doc.status == "processing" and doc.ingestion_summary["status"] == "queued"


@pytest.mark.asyncio
async def test_background_capacity_yields_to_waiting_interactive():
    from backend.recognition.runtime import RecognitionCapacity
    capacity = RecognitionCapacity(global_limit=1)
    order = []
    async def enter(name, background):
        async with capacity.lease(name, background=background):
            order.append(name)
    async with capacity.lease("active"):
        background = asyncio.create_task(enter("book", True))
        foreground = asyncio.create_task(enter("question", False))
        await asyncio.sleep(0)
    await asyncio.gather(background, foreground)
    assert order == ["question", "book"] and not capacity._owners


@pytest.mark.asyncio
async def test_pdf_over_five_mib_is_persisted_and_queued():
    from PIL import Image
    from io import BytesIO
    stream = BytesIO()
    Image.new("RGB", (1536, 1536), (233, 242, 211)).save(stream, format="BMP")
    with fitz.open() as book:
        page = book.new_page()
        page.insert_image(page.rect, stream=stream.getvalue())
        body = book.tobytes(deflate=False)
    assert 5 * 1024 * 1024 < len(body) < 64 * 1024 * 1024
    doc = await ingest_document(owner_id=owner(), original_name="scanned.pdf", content=body, registry=Registry())
    assert doc.ingestion_summary["status"] == "queued" and doc.size_bytes == len(body)


@pytest.mark.asyncio
async def test_cancelled_paid_page_is_never_resubmitted_on_resume_or_retry(monkeypatch):
    from contextlib import asynccontextmanager
    from backend.tests.test_recognition_recheck import Engine, source_file
    from backend.knowledge import ingestion
    engine = Engine(initial=["literal text"])
    entered = asyncio.Event()
    calls = []
    async def blocked(request):
        calls.append(request)
        entered.set()
        await asyncio.Event().wait()
    engine.recognize = blocked
    @asynccontextmanager
    async def adapter(**_):
        yield engine
    monkeypatch.setattr(ingestion, "recognition_engine", adapter)
    who = owner()
    body, media = source_file()
    upload = persist_knowledge_upload(storage=get_storage(), owner_id=who, original_name="scan.png", content=body,
                                     content_type=media, retention_policy="retained")
    config = dict(policy_version=POLICY, prompt_version="faithful-reader-v1", route_id=None, route_fingerprint=None)
    job_id = repo.queue_document(upload.document_id, who, config)
    worker = KnowledgeIngestionWorker(registry_factory=lambda _: Registry())
    task = asyncio.create_task(worker.run_once(job_id))
    await asyncio.wait_for(entered.wait(), timeout=15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with session_scope() as session:
        row = session.get(KnowledgeIngestionRecord, job_id)
        row.lease_expires_at = 0
    await worker.run_once(job_id)
    doc = get_document(upload.document_id, who)
    assert doc.ingestion_summary["failed_pages"] == 1
    assert repo.manifest(doc.id, who)["pages"][0]["error_code"] == "provider_submit_uncertain"
    retry = repo.queue_document(doc.id, who, config, new_version=True)
    await worker.run_once(retry)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_successful_pages_reused_in_explicit_gap_retry(monkeypatch):
    who, doc_id, job_id = queued(pdf(2))
    worker = KnowledgeIngestionWorker(registry_factory=lambda _: Registry())
    await worker.run_once(job_id)
    old = get_document(doc_id, who)
    with session_scope() as session:
        config = session.get(KnowledgeIngestionRecord, job_id).configuration
    retry = repo.queue_document(doc_id, who, config, new_version=True)
    await worker.run_once(retry)
    assert get_document(doc_id, who).active_version != old.active_version
    pages = repo.manifest(doc_id, who)["pages"]
    assert all(p["reused_page_id"] for p in pages)
    assert len(list_chunks([doc_id])) == 2
