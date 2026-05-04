"""
In-memory, task-scoped knowledge retriever.

Lives entirely in process RAM — by design. The user's product requirement is
"测一两个 task 时上传参考资料，用户退出后失效"; pairing that with Render's
free tier (15-min sleep wipes RAM, no disk persistence) means we get the
right semantics for free if we just don't persist.

Storage shape:
    self._tasks: dict[task_id] -> TaskKB
    TaskKB:
      docs: dict[doc_id] -> DocEntry  (filename, sha256, chunk_count, ts)
      chunks: list[KnowledgeChunk]    (flat across docs, source = filename)
      vectors: np.ndarray             (n, dim) — sentinel (n, 1) for BM25
      embedder_name: str              (locked at first add — can't mix dense+BM25)

Retrieve uses the same embedder it was indexed with. All access is async to
match the rest of the codebase, but we hold a `threading.RLock` because
TaskStore mutations come from FastAPI worker threads on top of asyncio.

Limits are enforced here (not at the API layer) so any caller — tests,
future endpoints, scripts — gets the same safety belt:
  - chunker.MAX_FILE_BYTES   (5 MB per upload)
  - chunker.MAX_CHUNKS_PER_DOC (500)
  - MAX_DOCS_PER_TASK = 3
  - MAX_TOTAL_CHUNKS_PER_TASK = 1500  (≤ 3 docs × 500 chunks)
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Dict, List, Optional

import numpy as np

from backend.tools.knowledge import KnowledgeChunk, KnowledgeRetriever
from backend.rag.embedder import Embedder

logger = logging.getLogger(__name__)


MAX_DOCS_PER_TASK = 3
MAX_TOTAL_CHUNKS_PER_TASK = 1500


@dataclass
class DocEntry:
    """Metadata for a single uploaded KB document within a task."""
    doc_id: str
    filename: str
    sha256: str
    chunk_count: int
    uploaded_at: float = field(default_factory=time.time)

    def public(self) -> Dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "filename": self.filename,
            "sha256": self.sha256,
            "chunk_count": self.chunk_count,
            "uploaded_at": self.uploaded_at,
        }


@dataclass
class TaskKB:
    docs: Dict[str, DocEntry] = field(default_factory=dict)
    chunks: List[KnowledgeChunk] = field(default_factory=list)
    chunk_doc_ids: List[str] = field(default_factory=list)  # parallel to chunks; for delete
    vectors: Optional[np.ndarray] = None                    # (n, dim) or sentinel (n, 1)
    embedder: Optional[Embedder] = None                     # locked at first add

    def total_chunks(self) -> int:
        return len(self.chunks)


class InMemoryTaskRetriever(KnowledgeRetriever):
    """KnowledgeRetriever that scopes its index by task_id.

    Returns [] for any unknown scope (matches NoOpRetriever semantics, so
    skills don't have to special-case "no KB configured").
    """

    def __init__(self) -> None:
        self._tasks: Dict[str, TaskKB] = {}
        self._lock = RLock()

    # ─── Public API ──────────────────────────────────────────────────────

    async def add_document(
        self,
        *,
        task_id: str,
        doc_id: str,
        filename: str,
        sha256: str,
        chunks: List[str],
        embedder: Embedder,
    ) -> DocEntry:
        """Embed `chunks` and append them to the task's index.

        Raises ValueError if:
          - the task already has MAX_DOCS_PER_TASK documents
          - adding `chunks` would exceed MAX_TOTAL_CHUNKS_PER_TASK
          - the task's existing index uses a different embedder name
            (we can't mix BM25 + dense vectors in one index)
        """
        if not chunks:
            raise ValueError("Cannot index an empty chunk list")

        # Embed BEFORE locking — embedding is the slow part and the lock
        # protects only the in-memory state mutation.
        new_vecs = await embedder.embed(chunks)

        with self._lock:
            kb = self._tasks.get(task_id) or TaskKB()
            if doc_id in kb.docs:
                raise ValueError(f"doc_id {doc_id!r} already exists in task {task_id!r}")
            if len(kb.docs) >= MAX_DOCS_PER_TASK:
                raise ValueError(
                    f"Task {task_id} already has {len(kb.docs)} KB docs "
                    f"(max {MAX_DOCS_PER_TASK}); delete one first."
                )
            if kb.total_chunks() + len(chunks) > MAX_TOTAL_CHUNKS_PER_TASK:
                raise ValueError(
                    f"Adding {len(chunks)} chunks would exceed task limit of "
                    f"{MAX_TOTAL_CHUNKS_PER_TASK} chunks (currently {kb.total_chunks()})"
                )
            if kb.embedder is not None and kb.embedder.name != embedder.name:
                raise ValueError(
                    f"Task {task_id} indexed with embedder {kb.embedder.name!r}; "
                    f"cannot add a doc with embedder {embedder.name!r}. "
                    f"Delete existing docs first to switch."
                )

            # Append chunks + their parallel doc_ids
            new_chunks = [
                KnowledgeChunk(content=c, source=filename, score=0.0)
                for c in chunks
            ]
            kb.chunks.extend(new_chunks)
            kb.chunk_doc_ids.extend([doc_id] * len(new_chunks))

            # Append vectors. Pad/init as needed.
            if kb.vectors is None or kb.vectors.shape[0] == 0:
                kb.vectors = new_vecs
            else:
                if kb.vectors.shape[1] != new_vecs.shape[1]:
                    raise ValueError(
                        f"Vector dim mismatch (existing {kb.vectors.shape[1]} "
                        f"vs new {new_vecs.shape[1]})"
                    )
                kb.vectors = np.concatenate([kb.vectors, new_vecs], axis=0)

            entry = DocEntry(
                doc_id=doc_id,
                filename=filename,
                sha256=sha256,
                chunk_count=len(chunks),
            )
            kb.docs[doc_id] = entry
            kb.embedder = embedder
            self._tasks[task_id] = kb

        logger.info(
            f"[KB] task={task_id} add_document doc_id={doc_id} "
            f"chunks={len(chunks)} embedder={embedder.name}"
        )
        return entry

    def list_docs(self, task_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            kb = self._tasks.get(task_id)
            if kb is None:
                return []
            return [d.public() for d in kb.docs.values()]

    def get_doc(self, task_id: str, doc_id: str) -> Optional[DocEntry]:
        with self._lock:
            kb = self._tasks.get(task_id)
            if kb is None:
                return None
            return kb.docs.get(doc_id)

    def find_doc_by_hash(self, task_id: str, sha256: str) -> Optional[DocEntry]:
        with self._lock:
            kb = self._tasks.get(task_id)
            if kb is None:
                return None
            for d in kb.docs.values():
                if d.sha256 == sha256:
                    return d
            return None

    def remove_doc(self, task_id: str, doc_id: str) -> bool:
        """Delete a single document from a task's index. Returns True if existed."""
        with self._lock:
            kb = self._tasks.get(task_id)
            if kb is None or doc_id not in kb.docs:
                return False

            keep_idx = [i for i, did in enumerate(kb.chunk_doc_ids) if did != doc_id]
            kb.chunks = [kb.chunks[i] for i in keep_idx]
            kb.chunk_doc_ids = [kb.chunk_doc_ids[i] for i in keep_idx]
            if kb.vectors is not None and kb.vectors.shape[0] > 0:
                kb.vectors = kb.vectors[keep_idx] if keep_idx else None
            kb.docs.pop(doc_id, None)

            # If the last doc was removed, also drop the embedder lock so the
            # user can later re-upload with a different (e.g. switched) BYOK.
            if not kb.docs:
                kb.embedder = None
                kb.vectors = None

            logger.info(f"[KB] task={task_id} remove_doc {doc_id}, remaining={len(kb.docs)}")
            return True

    def remove_task(self, task_id: str) -> bool:
        with self._lock:
            existed = self._tasks.pop(task_id, None) is not None
        if existed:
            logger.info(f"[KB] removed task {task_id}")
        return existed

    def task_count(self) -> int:
        with self._lock:
            return len(self._tasks)

    # ─── KnowledgeRetriever interface ────────────────────────────────────

    async def retrieve(
        self,
        query: str,
        k: int = 5,
        *,
        scope: Optional[str] = None,
    ) -> List[KnowledgeChunk]:
        """Top-k retrieval within a task scope. Returns [] if no KB for that task."""
        if not query or not query.strip():
            return []
        if not scope:
            return []

        with self._lock:
            kb = self._tasks.get(scope)
            if kb is None or not kb.chunks or kb.embedder is None:
                return []
            # Snapshot what we need so we can release the lock around the
            # async score call (embedder.score() may hit the network).
            chunks_snap = list(kb.chunks)
            doc_ids_snap = list(kb.chunk_doc_ids)
            vectors_snap = kb.vectors.copy() if kb.vectors is not None else np.zeros((0, 1), dtype=np.float32)
            embedder = kb.embedder

        try:
            scores = await embedder.score(
                query,
                vectors_snap,
                chunk_texts=[c.content for c in chunks_snap] if not embedder.is_dense else None,
            )
        except Exception as e:
            logger.warning(f"[KB] retrieve failed for task={scope}: {e}")
            return []

        if scores.shape[0] != len(chunks_snap):
            logger.warning(
                f"[KB] score shape {scores.shape} != n_chunks {len(chunks_snap)}; "
                f"returning empty"
            )
            return []

        n = min(int(k), len(chunks_snap))
        if n <= 0:
            return []
        # Argpartition for top-k, then sort descending
        top_idx = np.argpartition(-scores, kth=min(n - 1, len(scores) - 1))[:n]
        top_idx = top_idx[np.argsort(-scores[top_idx])]

        out: List[KnowledgeChunk] = []
        for idx in top_idx:
            i = int(idx)
            score = float(scores[i])
            if score <= 0.0:
                # BM25 returns 0 for no-match; cosine for orthogonal. Skip these.
                continue
            c = chunks_snap[i]
            out.append(KnowledgeChunk(
                content=c.content,
                source=c.source,
                score=score,
            ))
        if out:
            logger.debug(
                f"[KB] task={scope} retrieve(k={k}) → {len(out)} chunks "
                f"(top score={out[0].score:.3f})"
            )
        else:
            logger.debug(f"[KB] task={scope} retrieve(k={k}) → 0 chunks")
        return out
