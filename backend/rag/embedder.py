"""
Embedder abstractions for task-scoped RAG.

Strategy (matches the user's "BYOK 加什么用什么" preference):

  1. Look at the live ExpertRegistry. If any provider is OpenAI-compatible
     (zhipu / openai), use its API key + base_url to embed via
     `langchain_openai.OpenAIEmbeddings`. Both Zhipu (`embedding-3`,
     2048-dim) and OpenAI (`text-embedding-3-small`, 1536-dim) speak the same
     wire format so we get one code path.

  2. Otherwise — user only configured Anthropic / Gemini, neither of which
     expose a 1st-class embedding endpoint we can hit through the same SDK
     — fall back to BM25 keyword retrieval (`rank_bm25`). Worse semantics,
     but RAG still works without any extra LLM calls.

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
    """Wraps langchain_openai.OpenAIEmbeddings with a custom base_url.

    Works identically against:
      - api.openai.com (`text-embedding-3-small`)
      - open.bigmodel.cn/api/paas/v4 (Zhipu `embedding-3`)
    so a single class covers both. The parent ExpertRegistry already enforces
    api_key validity at registration time, so we don't re-validate.
    """

    def __init__(self, *, api_key: str, base_url: str, model: str, provider_type: str):
        self.api_key = api_key
        self.base_url = base_url
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
    # Need raw configs (with api_key intact) — registry stores the full
    # ProviderConfig keyed by provider_id.
    with registry._lock:  # type: ignore[attr-defined]
        configs = list(registry._configs.values())  # type: ignore[attr-defined]

    by_type = {}
    for c in configs:
        if c.enabled and c.provider_type not in by_type:
            by_type[c.provider_type] = c

    for ptype in ("zhipu", "openai"):
        cfg = by_type.get(ptype)
        if cfg is None:
            continue
        from backend.config import settings
        # Choose base_url: explicit > settings default > openai default
        if ptype == "zhipu":
            base_url = cfg.base_url or settings.zhipu_api_base
        else:
            base_url = cfg.base_url or settings.openai_api_base
        model = _OPENAI_COMPAT_EMBED_MODELS[ptype]
        logger.info(f"RAG embedder: OpenAICompatible({ptype}, {model}, base={base_url})")
        return OpenAICompatibleEmbedder(
            api_key=cfg.api_key,
            base_url=base_url,
            model=model,
            provider_type=ptype,
        )

    logger.info("RAG embedder: BM25 fallback (no openai/zhipu BYOK keys configured)")
    return BM25Embedder()
