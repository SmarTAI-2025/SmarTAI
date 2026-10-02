import asyncio
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select

from backend.db import grading_repository, workflow_repository
from backend.db.knowledge_repository import get_document, replace_document_chunks, set_task_documents
from backend.db.models import AssignmentRecord, CourseRecord, KnowledgeChunkRecord
from backend.db.session import session_scope
from backend.knowledge.index import KnowledgeIndex, KnowledgeIndexCache, IndexedChunk, tokenize
from backend.knowledge.retriever import PersistentKnowledgeRetriever, _reference
from backend.knowledge.snapshots import freeze_documents
from backend.tests.test_knowledge_ingestion import owner
from backend.tests.test_personal_knowledge import _persist_ready_document


def assignment(who):
    with session_scope() as session:
        session.add(CourseRecord(id="course-" + who, name="Algebra", teacher_id=who))
        session.flush()
        session.add(AssignmentRecord(id="task-" + who, name="Exercises", teacher_id=who, course_id="course-" + who,
                                     status="draft", version=1))
    return "task-" + who


def document(who, chunks, name="algebra.txt"):
    return _persist_ready_document(owner_id=who, filename=name, content="\n".join(chunks).encode(), chunks=chunks)


def test_chinese_terms_and_hierarchical_ids_and_negative_exponents():
    texts = ["练习 1.1.50 群同构是双射同态 x^{1}", "练习 1.1.5 群同态保乘法 x^{-1}", "矩阵特征值及特征向量"]
    index = KnowledgeIndex([IndexedChunk(str(n), "doc", "v", text, {}) for n, text in enumerate(texts)])
    assert index.search("练习 1.1.5", 1)[0][0].id == "1"
    assert index.search("群同态 x^{-1}", 1)[0][0].id == "1"
    assert index.search("矩阵特征值", 1)[0][0].id == "2"
    assert index.search("thermodynamics entropy", 5) == []
    assert index.search("9.9.99", 5) == []
    assert "math:x^-1" in tokenize("x^{-1}") and "math:x^1" not in tokenize("x^{-1}")


@pytest.mark.asyncio
async def test_same_book_new_version_cannot_change_frozen_grading_context():
    who = owner()
    task = assignment(who)
    doc = document(who, ["Newton original force theorem"])
    set_task_documents(assignment_id=task, owner_id=who, document_ids=[doc.id])
    workflow = workflow_repository.ensure_workflow(assignment_id=task, owner_id=who)
    run = grading_repository.create_run_bundle(task, teacher_id=who, revision_ids=[],
        setup={"knowledge_scope": "all_task_docs"}, setup_fingerprint="test",
        input_manifest={"knowledge_document_ids": [doc.id]}, workflow_expected_revision=workflow.workflow_revision)
    frozen = workflow_repository.get_run_setup(run.id).input_manifest["knowledge_content_versions"]
    assert frozen[0]["content_version"] == doc.active_version
    replace_document_chunks(doc.id, ["Newton changed force theorem"])
    retriever = PersistentKnowledgeRetriever(cache=KnowledgeIndexCache())
    result = await retriever.retrieve("Newton force", scope="grading-run:" + run.id)
    assert len(result) == 1 and "original" in result[0].content
    assert result[0].citation["content_version"] == doc.active_version
    current = await retriever.retrieve("Newton force", scope=task)
    assert "changed" in current[0].content


@pytest.mark.asyncio
async def test_readable_prefix_freeze_excludes_later_pages_and_reuses_index():
    who = owner()
    doc = document(who, ["first section group theory"])
    refs = freeze_documents(who, [doc.id])
    with session_scope() as session:
        session.add(KnowledgeChunkRecord(id="later-chunk", document_id=doc.id, content_version=doc.active_version,
            chunk_index=1, content="later section topology", chunk_metadata={}))
    cache = KnowledgeIndexCache()
    retriever = PersistentKnowledgeRetriever(cache=cache)
    for _ in range(4):
        result = await retriever.retrieve_documents("group theory", owner_id=who, documents=[doc], refs=refs)
        assert result[0].content == "first section group theory"
    assert cache.builds == 1 and cache.hits == 3
    assert await retriever.retrieve_documents("topology", owner_id=who, documents=[doc], refs=refs) == []


@pytest.mark.asyncio
async def test_cached_text_revoked_by_cleanup_and_cross_owner_access():
    from backend.db.knowledge_storage_repository import request_document_cleanup
    who = owner()
    doc = document(who, ["private finite field evidence"])
    refs = freeze_documents(who, [doc.id])
    retriever = PersistentKnowledgeRetriever(cache=KnowledgeIndexCache())
    assert await retriever.retrieve_documents("field", owner_id=who, documents=[doc], refs=refs)
    assert await retriever.retrieve_documents("field", owner_id=owner(), documents=[doc], refs=refs) == []
    request_document_cleanup(doc.id, who)
    assert await retriever.retrieve_documents("field", owner_id=who, documents=[doc], refs=refs) == []


def test_single_flight_index_build_and_ttl(monkeypatch):
    from backend.knowledge import index as module
    calls = []
    def load(_):
        calls.append(1)
        time.sleep(.03)
        return KnowledgeIndex([IndexedChunk("1", "doc", "v", "group theory", {})])
    monkeypatch.setattr(module, "load_index", load)
    cache = KnowledgeIndexCache(ttl_seconds=60)
    refs = [dict(document_id="doc", content_version="v", chunk_count=1, source_sha256="hash")]
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: cache.get("teacher", refs), range(8)))
    assert len(calls) == 1 and all(value is results[0] for value in results)
    assert cache.bytes <= cache.max_bytes
    cache.ttl = 0
    cache.get("teacher", refs)
    assert len(calls) == 2


def test_system_citations_warn_incomplete_evidence_and_never_claim_support():
    from backend.tools.knowledge import KnowledgeChunk, context_text, citations
    chunk = KnowledgeChunk("x^{-1}", "textbook", 1, dict(citation_id="kb:a", coverage_complete=False,
        provenance="retrieved_reference_not_verified_claim"))
    assert "UNVERIFIED/PARTIAL" in context_text([chunk])
    assert citations([chunk])[0]["provenance"] == "retrieved_reference_not_verified_claim"
    assert "Do not invent" in context_text([])


def test_adjacent_span_keeps_separate_source_identity():
    first = IndexedChunk("1", "doc", "v", "群同态", dict(page_number=9, start=0, end=4))
    second = IndexedChunk("2", "doc", "v", "保乘法", dict(page_number=9, start=4, end=8))
    unrelated = IndexedChunk("3", "other", "v", "unrelated", dict(page_number=9, start=8, end=20))
    index = KnowledgeIndex([first, second, unrelated])
    assert [chunk.id for chunk, _ in index.adjacent(index.search("群同态", 5), 5)] == ["1", "2"]


@pytest.mark.asyncio
async def test_index_upgrade_preserves_legacy_frozen_runs_and_separates_cache():
    from backend.knowledge.snapshots import INDEX_VERSION, LEGACY_INDEX_VERSION, freeze_in_session
    who = owner()
    doc = document(who, ["特征值满足 Av=lambda v"])
    current = freeze_documents(who, [doc.id])
    legacy = [{**ref, "index_version":LEGACY_INDEX_VERSION} for ref in current]
    with session_scope() as session:
        assert freeze_in_session(session, who, [doc.id], expected=legacy) == legacy
    cache = KnowledgeIndexCache()
    retriever = PersistentKnowledgeRetriever(cache=cache)
    old = await retriever.retrieve_documents("特征值", owner_id=who, documents=[doc], refs=legacy)
    assert old[0].citation["index_version"] == LEGACY_INDEX_VERSION
    assert await retriever.retrieve_documents("eigenvalues", owner_id=who, documents=[doc], refs=legacy) == []
    new = await retriever.retrieve_documents("eigenvalues", owner_id=who, documents=[doc], refs=current)
    assert new[0].citation["index_version"] == INDEX_VERSION
    assert cache.builds == 2


def test_unknown_or_mixed_index_versions_are_not_silently_upgraded():
    from backend.knowledge.index import load_index
    from backend.knowledge.snapshots import INDEX_VERSION, LEGACY_INDEX_VERSION
    from backend.domain.errors import InvalidTransition
    with pytest.raises(InvalidTransition):
        load_index([dict(index_version="unknown")])
    with pytest.raises(InvalidTransition):
        load_index([dict(index_version=INDEX_VERSION), dict(index_version=LEGACY_INDEX_VERSION)])


@pytest.mark.asyncio
async def test_unavailable_original_revokes_cached_index():
    from backend.db.models import StoredFileRecord
    who = owner()
    doc = document(who, ["field extension theorem"])
    refs = freeze_documents(who, [doc.id])
    retriever = PersistentKnowledgeRetriever(cache=KnowledgeIndexCache())
    assert await retriever.retrieve_documents("field", owner_id=who, documents=[doc], refs=refs)
    with session_scope() as session:
        row = session.get(StoredFileRecord, doc.stored_file_id)
        row.availability_status = "unavailable"
    assert await retriever.retrieve_documents("field", owner_id=who, documents=[doc], refs=refs) == []


def test_search_citation_api_is_owner_scoped_and_read_only(monkeypatch):
    from fastapi.testclient import TestClient
    from backend.auth import create_token
    from backend.main import app
    from backend.knowledge import ingestion
    def forbidden(*args, **kwargs):
        pytest.fail("A retrieval or citation read must never run ingestion")
    monkeypatch.setattr(ingestion.KnowledgeIngestionWorker, "run_once", forbidden)
    who = owner()
    doc = document(who, ["群同态定理 x^{-1}"])
    client = TestClient(app)
    headers = {"Authorization": "Bearer " + create_token(who, "teacher")}
    response = client.post("/knowledge/search", headers=headers, json=dict(query="群同态", document_ids=[doc.id]))
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["provider_calls"] == 0 and data["matched"]
    citation = data["matches"][0]["citation"]
    url = f"/knowledge/documents/{doc.id}/citations/{citation['chunk_id']}"
    evidence = client.get(url, headers=headers)
    assert evidence.status_code == 200 and evidence.json()["content"] == "群同态定理 x^{-1}"
    assert evidence.json()["content_version"] == citation["content_version"]
    assert client.get(url + "/download", headers=headers).content == "群同态定理 x^{-1}".encode()
    assert client.get(url).status_code == 401
    outsider = {"Authorization": "Bearer " + create_token(owner(), "teacher")}
    assert client.get(url, headers=outsider).status_code == 404
    assert client.get(url + "/download", headers=outsider).status_code == 404
    assert client.post("/knowledge/search", headers=outsider, json=dict(query="群同态", document_ids=[doc.id])).status_code == 404


@pytest.mark.asyncio
async def test_five_books_2500_pages_capacity_and_exact_page_recall():
    import json
    import statistics
    who = owner()
    documents = []
    queries = []
    for book, pages in enumerate((1000, 500, 500, 250, 250), 1):
        texts = [f"练习 {book}.{page}.7 群同态矩阵特征值专题。 " + ("代数结构保乘法与核空间。 " * 90) for page in range(1, pages + 1)]
        doc = document(who, texts, f"synthetic-book-{book}.txt")
        with session_scope() as session:
            for row in session.scalars(select(KnowledgeChunkRecord).where(KnowledgeChunkRecord.document_id == doc.id)):
                row.chunk_metadata = dict(page_number=row.chunk_index + 1, unit="page", start=0, end=len(row.content))
        documents.append(doc)
        queries.extend((f"{book}.{page}.7", doc.id, page) for page in (1, pages // 2, pages))
    refs = freeze_documents(who, [doc.id for doc in documents])
    cache = KnowledgeIndexCache()
    retriever = PersistentKnowledgeRetriever(cache=cache)
    elapsed = []
    for query, expected_doc, expected_page in queries * 2:
        started = time.perf_counter()
        result = await retriever.retrieve_documents(query, 5, owner_id=who, documents=documents, refs=refs)
        elapsed.append(time.perf_counter() - started)
        assert result[0].citation["document_id"] == expected_doc
        assert result[0].citation["page_number"] == expected_page
        assert query in result[0].content
    hot = sorted(elapsed[1:])
    report = dict(pages=2500, largest_book_pages=1000, chars=sum(len(chunk.content) for chunk in next(iter(cache._items.values()))[1].chunks),
                  cold_seconds=round(elapsed[0], 4), hot_p50_seconds=round(statistics.median(hot), 4),
                  hot_p95_seconds=round(hot[int(len(hot) * .95)], 4), index_bytes=cache.bytes, builds=cache.builds,
                  synthetic_exact_id_recall_at_5=1.0, provider_calls=0)
    print("KNOWLEDGE_CAPACITY " + json.dumps(report, sort_keys=True))
    assert cache.builds == 1 and cache.bytes <= cache.max_bytes
    assert report["hot_p95_seconds"] < 2
