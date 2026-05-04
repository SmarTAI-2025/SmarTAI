"""
Tests for the task-scoped in-memory RAG implementation.

Covers:
  - chunk_text behavior (limits, overlap, empty input)
  - InMemoryTaskRetriever scope isolation
  - top-k retrieval ranking
  - doc-level remove + task-level cleanup
  - dim mismatch / embedder switch / over-limit guards
  - idempotency (find_doc_by_hash)

We use a deterministic stub embedder (no network) so these are unit tests in
the strict sense — no LLM, no rank_bm25, no langchain. The OpenAICompatible
embedder gets exercised at integration time only.
"""
from __future__ import annotations

import pytest
import numpy as np

from backend.rag.chunker import (
    chunk_text,
    MAX_CHUNKS_PER_DOC,
    DEFAULT_CHUNK_WORDS,
)
from backend.rag.embedder import Embedder
from backend.rag.store import InMemoryTaskRetriever, MAX_DOCS_PER_TASK


# ─── Stub embedder ───────────────────────────────────────────────────────────


class _CharEmbedder(Embedder):
    """Deterministic embedder: hashes the first 8 chars to a 16-dim vector.

    Cosine similarity ≈ 1.0 for prefix-matching strings. Good enough to test
    that retrieval picks the right chunks; we DON'T claim semantic accuracy.
    """

    name = "char-embed-test"
    dim = 16

    @staticmethod
    def _encode(text: str) -> np.ndarray:
        v = np.zeros(16, dtype=np.float32)
        prefix = (text or "").lower()[:8]
        for i, ch in enumerate(prefix):
            v[i] = (ord(ch) % 31) / 31.0
        n = float(np.linalg.norm(v))
        if n > 0:
            v = v / n
        return v

    async def embed(self, texts):
        return np.stack([self._encode(t) for t in texts])

    async def score(self, query, vectors, *, chunk_texts=None):
        q = self._encode(query)
        if vectors.shape[0] == 0:
            return np.zeros(0, dtype=np.float32)
        return vectors @ q


# ─── chunk_text ──────────────────────────────────────────────────────────────


def test_chunk_text_empty():
    assert chunk_text("") == []
    assert chunk_text("   \n  ") == []


def test_chunk_text_short_input():
    chunks = chunk_text("hello world")
    assert chunks == ["hello world"]


def test_chunk_text_overlap():
    text = " ".join(f"w{i}" for i in range(120))  # 120 words
    chunks = chunk_text(text, chunk_words=50, overlap_words=10)
    # step = 40, 120 / 40 = 3 windows starting at 0, 40, 80
    assert len(chunks) == 3
    # Last 10 words of chunk[0] = first 10 words of chunk[1]
    last10_of_first = chunks[0].split()[-10:]
    first10_of_second = chunks[1].split()[:10]
    assert last10_of_first == first10_of_second


def test_chunk_text_truncates_at_max_chunks(monkeypatch):
    # Give it more words than MAX_CHUNKS_PER_DOC * step would consume
    n_words = (MAX_CHUNKS_PER_DOC + 50) * 50
    text = " ".join("w" for _ in range(n_words))
    chunks = chunk_text(text, chunk_words=50, overlap_words=0)
    assert len(chunks) == MAX_CHUNKS_PER_DOC


# ─── InMemoryTaskRetriever ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retriever_empty_scope_returns_empty():
    r = InMemoryTaskRetriever()
    out = await r.retrieve("anything", k=5, scope="T_unknown")
    assert out == []


@pytest.mark.asyncio
async def test_retriever_no_scope_returns_empty():
    """scope=None must short-circuit even if a task happens to be indexed."""
    r = InMemoryTaskRetriever()
    await r.add_document(
        task_id="T1", doc_id="d1", filename="f.txt", sha256="abc",
        chunks=["alpha beta", "gamma delta"], embedder=_CharEmbedder(),
    )
    out = await r.retrieve("alpha", k=5, scope=None)
    assert out == []


@pytest.mark.asyncio
async def test_retriever_scope_isolation():
    r = InMemoryTaskRetriever()
    await r.add_document(
        task_id="T_a", doc_id="d1", filename="a.txt", sha256="a",
        chunks=["alpha alpha alpha"], embedder=_CharEmbedder(),
    )
    await r.add_document(
        task_id="T_b", doc_id="d2", filename="b.txt", sha256="b",
        chunks=["beta beta beta"], embedder=_CharEmbedder(),
    )
    # Scope T_a returns only chunks indexed under T_a (source="a.txt") and
    # never leaks T_b's content. Scores aren't compared — they're embedder-
    # specific. The contract is: retrieved sources match the scope.
    out_a = await r.retrieve("alpha", k=5, scope="T_a")
    assert len(out_a) == 1
    assert out_a[0].source == "a.txt"
    assert "alpha" in out_a[0].content

    out_b = await r.retrieve("alpha", k=5, scope="T_b")
    # Scope T_b must only return T_b's own chunks (sourced from b.txt).
    # Whether the score is high enough to pass the >0 filter depends on the
    # embedder; what matters is no T_a leakage.
    for c in out_b:
        assert c.source == "b.txt"


@pytest.mark.asyncio
async def test_retriever_top_k_ranks_by_similarity():
    r = InMemoryTaskRetriever()
    await r.add_document(
        task_id="T1", doc_id="d1", filename="multi.txt", sha256="x",
        chunks=[
            "alpha keyword one",
            "beta something else",
            "alpha matches better",
        ],
        embedder=_CharEmbedder(),
    )
    out = await r.retrieve("alpha", k=2, scope="T1")
    # Both "alpha"-prefixed chunks should outrank "beta"
    assert len(out) == 2
    contents = [c.content for c in out]
    assert all("alpha" in c for c in contents)
    # Scores are sorted descending
    assert out[0].score >= out[1].score


@pytest.mark.asyncio
async def test_retriever_idempotency_via_hash():
    r = InMemoryTaskRetriever()
    await r.add_document(
        task_id="T1", doc_id="d1", filename="f.txt", sha256="HASH_AAA",
        chunks=["foo bar"], embedder=_CharEmbedder(),
    )
    found = r.find_doc_by_hash("T1", "HASH_AAA")
    assert found is not None
    assert found.doc_id == "d1"
    assert r.find_doc_by_hash("T1", "HASH_OTHER") is None
    assert r.find_doc_by_hash("T_other", "HASH_AAA") is None


@pytest.mark.asyncio
async def test_retriever_remove_doc():
    r = InMemoryTaskRetriever()
    await r.add_document(
        task_id="T1", doc_id="d1", filename="a.txt", sha256="a",
        chunks=["alpha"], embedder=_CharEmbedder(),
    )
    await r.add_document(
        task_id="T1", doc_id="d2", filename="b.txt", sha256="b",
        chunks=["alpha beta"], embedder=_CharEmbedder(),
    )
    assert r.remove_doc("T1", "d1") is True
    docs = r.list_docs("T1")
    assert len(docs) == 1
    assert docs[0]["doc_id"] == "d2"
    # Remove non-existent
    assert r.remove_doc("T1", "d_missing") is False


@pytest.mark.asyncio
async def test_retriever_remove_task_clears_all():
    r = InMemoryTaskRetriever()
    await r.add_document(
        task_id="T1", doc_id="d1", filename="a.txt", sha256="a",
        chunks=["alpha"], embedder=_CharEmbedder(),
    )
    assert r.task_count() == 1
    assert r.remove_task("T1") is True
    assert r.task_count() == 0
    out = await r.retrieve("alpha", k=5, scope="T1")
    assert out == []


@pytest.mark.asyncio
async def test_retriever_max_docs_per_task():
    r = InMemoryTaskRetriever()
    for i in range(MAX_DOCS_PER_TASK):
        await r.add_document(
            task_id="T1", doc_id=f"d{i}", filename=f"{i}.txt", sha256=f"h{i}",
            chunks=["c"], embedder=_CharEmbedder(),
        )
    # The (N+1)-th add must raise
    with pytest.raises(ValueError, match="max"):
        await r.add_document(
            task_id="T1", doc_id="d_extra", filename="x.txt", sha256="hx",
            chunks=["c"], embedder=_CharEmbedder(),
        )


@pytest.mark.asyncio
async def test_retriever_rejects_dim_mismatch():
    """Adding a doc with vectors of a different dim must blow up cleanly."""
    class _OtherDimEmbedder(_CharEmbedder):
        dim = 8

        @staticmethod
        def _encode(text):
            v = np.zeros(8, dtype=np.float32)
            for i, ch in enumerate((text or "").lower()[:4]):
                v[i] = (ord(ch) % 31) / 31.0
            n = float(np.linalg.norm(v))
            if n > 0:
                v = v / n
            return v

    r = InMemoryTaskRetriever()
    e1 = _CharEmbedder()
    await r.add_document(
        task_id="T1", doc_id="d1", filename="a.txt", sha256="a",
        chunks=["alpha"], embedder=e1,
    )
    # Same name but different dim → embedder-name check passes but dim check trips
    e1_alias = _OtherDimEmbedder()
    e1_alias.name = e1.name
    with pytest.raises(ValueError):
        await r.add_document(
            task_id="T1", doc_id="d2", filename="b.txt", sha256="b",
            chunks=["beta"], embedder=e1_alias,
        )


@pytest.mark.asyncio
async def test_retriever_rejects_embedder_switch():
    """Mixing a BM25 doc with a dense doc in one task must raise."""
    r = InMemoryTaskRetriever()
    e_a = _CharEmbedder()
    await r.add_document(
        task_id="T1", doc_id="d1", filename="a.txt", sha256="a",
        chunks=["alpha"], embedder=e_a,
    )
    class _OtherEmbedder(_CharEmbedder):
        name = "different-embedder"
    with pytest.raises(ValueError, match="embedder"):
        await r.add_document(
            task_id="T1", doc_id="d2", filename="b.txt", sha256="b",
            chunks=["beta"], embedder=_OtherEmbedder(),
        )
