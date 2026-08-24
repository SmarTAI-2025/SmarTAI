"""
Embedder abstractions for task-scoped RAG.

Strategy:

  1. Only an exact official OpenAI or Zhipu route may use dense embeddings.
     The base URL comes from the reviewed provider catalog, never from a user
     supplied relay record.

  2. Every custom URL or protocol override falls back to local BM25 keyword
     retrieval (`rank_bm25`). This prevents an implicit `/embeddings` request
     from sending course material to a relay selected only for model calls.

The ABC is `Embedder` with two methods: `embed(texts) -> np.ndarray` for
indexing and `score(query, vectors)` for retrieval. BM25 fakes a vector
space by returning a (n_docs,) similarity vector so the store can use one
top-k path for both backends.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import List, Optional, TYPE_CHECKING

import numpy as np

from backend.llm.endpoint_policy import (
    is_user_defined_provider_endpoint,
)
from backend.llm.provider_catalog import catalog_entry, effective_wire_protocol

if TYPE_CHECKING:
    from backend.llm.registry import ExpertRegistry

logger = logging.getLogger(__name__)


# Default embedding models per OpenAI-compatible provider type. Users can
# always override by configuring a custom model string at BYOK time, but the
# experts API only stores chat-completion models, so we hardcode embedding
# defaults here.
_OPENAI_COMPAT_EMBED_MODELS = {
    "zhipu": "embedding-3",
    "openai": "text-embedding-3-small",
}


class Embedder(ABC):
    """Common embedder interface used by InMemoryTaskRetriever."""

    name: str = "Embedder"
    dim: int = 0

    @abstractmethod
    async def embed(self, texts: List[str]) -> np.ndarray:
        """Embed a list of texts. Returns float32 array shape (n, dim).

        BM25 returns shape (n, 1) — a placeholder column that the store
        ignores, while the retriever falls back to .score() at query time.
        """
        ...

    @abstractmethod
    async def score(
        self,
        query: str,
        vectors: np.ndarray,
        *,
        chunk_texts: Optional[List[str]] = None,
    ) -> np.ndarray:
        """Return a (n_docs,) similarity score vector against `query`.

        For dense embedders, `chunk_texts` is unused (we use precomputed
        vectors). For BM25, vectors are ignored and chunk_texts drive the
        scoring.
        """
        ...

    @property
    def is_dense(self) -> bool:
        return True


# ─── OpenAI-compatible embedder (zhipu / openai) ─────────────────────────────


class OpenAICompatibleEmbedder(Embedder):
    """Wrap OpenAIEmbeddings for two exact official endpoints only.

    Works identically against:
      - api.openai.com (`text-embedding-3-small`)
      - open.bigmodel.cn/api/paas/v4 (Zhipu `embedding-3`)
    so a single class covers both. A second constructor-level check rejects
    every user-defined endpoint even if a caller bypasses the registry picker.
    """

    def __init__(self, *, api_key: str, base_url: str, model: str, provider_type: str):
        if provider_type not in _OPENAI_COMPAT_EMBED_MODELS:
            raise ValueError("embedding_provider_not_supported")
        entry = catalog_entry(provider_type)
        if base_url.rstrip("/").casefold() != entry.default_base_url.casefold():
            raise ValueError("embedding_endpoint_not_official")
        self.api_key = api_key
        self.base_url = entry.default_base_url
        self.model = model
        self.provider_type = provider_type
        self.name = f"{provider_type}:{model}"
        self._client = None

    def _get_client(self):
        if self._client is None:
            from langchain_openai import OpenAIEmbeddings
            self._client = OpenAIEmbeddings(
                model=self.model,
                api_key=self.api_key,
                base_url=self.base_url,
                # check_embedding_ctx_length=False: zhipu's embedding-3 doesn't
                # play nice with langchain's default 8192-token chunking — our
                # chunker already produces ~500-word windows so this is safe.
                check_embedding_ctx_length=False,
            )
        return self._client

    async def embed(self, texts: List[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 1), dtype=np.float32)
        client = self._get_client()
        # langchain_openai's aembed_documents handles batching internally
        vectors = await client.aembed_documents(list(texts))
        arr = np.asarray(vectors, dtype=np.float32)
        if arr.ndim != 2 or arr.shape[0] != len(texts):
            raise ValueError(
                f"OpenAICompatibleEmbedder got unexpected shape {arr.shape} "
                f"for {len(texts)} inputs"
            )
        # L2-normalize once at index time so retrieval is plain dot product
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        arr = arr / norms
        if self.dim == 0:
            self.dim = arr.shape[1]
        return arr

    async def score(
        self,
        query: str,
        vectors: np.ndarray,
        *,
        chunk_texts: Optional[List[str]] = None,
    ) -> np.ndarray:
        if vectors.shape[0] == 0:
            return np.zeros(0, dtype=np.float32)
        client = self._get_client()
        q_vec = await client.aembed_query(query)
        q = np.asarray(q_vec, dtype=np.float32)
        n = float(np.linalg.norm(q))
        if n > 0:
            q = q / n
        # vectors already normalized → cosine = dot
        return vectors @ q


# ─── BM25 keyword fallback ───────────────────────────────────────────────────


class BM25Embedder(Embedder):
    """Keyword-based retrieval using rank_bm25.

    Stores no real embeddings — `embed()` returns a (n, 1) sentinel array so
    the store has something to persist. Retrieval recomputes BM25 scores on
    the fly using `chunk_texts` provided by the store. Slow if there are
    100k chunks; fine for our 500-chunks-per-task cap.

    Tokenization: lowercase + split on Unicode word boundaries; for Chinese
    we fall back to character-level so CJK queries still match.
    """

    name = "bm25"
    dim = 1

    @property
    def is_dense(self) -> bool:
        return False

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        import re
        # Latin words
        words = re.findall(r"[A-Za-z0-9]+", text.lower())
        # CJK chars individually so simple Chinese queries still land
        cjk = re.findall(r"[一-鿿]", text)
        return words + cjk

    async def embed(self, texts: List[str]) -> np.ndarray:
        # Sentinel — store keeps real chunk_texts and recomputes at query time
        return np.zeros((len(texts), 1), dtype=np.float32)

    async def score(
        self,
        query: str,
        vectors: np.ndarray,
        *,
        chunk_texts: Optional[List[str]] = None,
    ) -> np.ndarray:
        if not chunk_texts:
            return np.zeros(0, dtype=np.float32)
        try:
            from rank_bm25 import BM25Okapi
        except ImportError:
            logger.warning("rank_bm25 not installed; BM25 fallback returns zeros")
            return np.zeros(len(chunk_texts), dtype=np.float32)

        tokenized_corpus = [self._tokenize(c) for c in chunk_texts]
        bm25 = BM25Okapi(tokenized_corpus)
        q_tokens = self._tokenize(query)
        if not q_tokens:
            return np.zeros(len(chunk_texts), dtype=np.float32)
        scores = bm25.get_scores(q_tokens)
        arr = np.asarray(scores, dtype=np.float32)
        # Normalize to ~[0, 1] for comparability with cosine
        max_s = float(arr.max()) if arr.size else 0.0
        if max_s > 0:
            arr = arr / max_s
        return arr


# ─── Picker ──────────────────────────────────────────────────────────────────


def pick_embedder(registry: "ExpertRegistry") -> Embedder:
    """Choose the best embedder given the user's BYOK config.

    Priority: zhipu > openai > BM25 fallback. Order is deterministic so a
    given task always uses the same embedder across uploads (mixing dense +
    BM25 indexes within one task would silently break retrieval).
    """
    # This trusted backend-only method preserves owner scoping while keeping
    # API keys out of public registry responses.
    configs = registry.list_enabled_configs()

    by_type = {}
    for c in configs:
        if not c.enabled or c.provider_type in by_type:
            continue
        try:
            entry = catalog_entry(c.provider_type)
            protocol = effective_wire_protocol(c.provider_type, c.wire_protocol)
        except ValueError:
            continue
        if (
            c.provider_type not in _OPENAI_COMPAT_EMBED_MODELS
            or protocol != entry.wire_protocol
            or is_user_defined_provider_endpoint(
                c.provider_type,
                c.base_url,
                protocol,
            )
        ):
            continue
        by_type[c.provider_type] = c

    for ptype in ("zhipu", "openai"):
        cfg = by_type.get(ptype)
        if cfg is None:
            continue
        base_url = catalog_entry(ptype).default_base_url
        model = _OPENAI_COMPAT_EMBED_MODELS[ptype]
        logger.info("RAG embedder: OpenAICompatible(%s, %s)", ptype, model)
        return OpenAICompatibleEmbedder(
            api_key=cfg.api_key,
            base_url=base_url,
            model=model,
            provider_type=ptype,
        )

    logger.info("RAG embedder: BM25 fallback (no openai/zhipu BYOK keys configured)")
    return BM25Embedder()
