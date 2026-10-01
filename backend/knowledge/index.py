"""Local BM25 index reuse; no embedding requests or model-assisted retrieval."""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import Future
from dataclasses import dataclass
import hashlib
import re
import sys
import threading
import time
import unicodedata

import numpy as np
from rank_bm25 import BM25Okapi
from sqlalchemy import func, or_, select

from backend.db.models import KnowledgeChunkRecord
from backend.db.session import session_scope
from backend.knowledge.snapshots import INDEX_VERSION
from backend.domain.errors import InvalidTransition

STOP_WORDS = frozenset("the a an of is are to in on and or for with that this prove show let find given using determine".split())


def tokenize(text):
    normalized = unicodedata.normalize("NFKC", text).lower().replace("−", "-")
    identifier_pattern = r"(?<![\w.])\d+(?:\.\d+)+(?![\w.])"
    words = [word for word in re.findall(r"[a-z0-9_]+", re.sub(identifier_pattern, " ", normalized)) if word not in STOP_WORDS]
    # Bigrams distinguish terms such as 同态/同构 without a large dictionary.
    # Isolated characters remain searchable, but do not dominate whole queries.
    for run in re.findall(r"[\u3400-\u9fff]+", normalized):
        words.extend("c:" + run[i:i + 2] for i in range(len(run) - 1))
        if len(run) == 1:
            words.append("c:" + run)
    words.extend("id:" + value for value in re.findall(identifier_pattern, normalized))
    words.extend("math:" + variable + "^" + exponent for variable, exponent in
                 re.findall(r"([a-z])\s*\^\s*[{(]?\s*([+-]?\d+)\s*[})]?", normalized))
    return words


@dataclass(frozen=True)
class IndexedChunk:
    id: str
    document_id: str
    content_version: str
    content: str
    metadata: dict


class KnowledgeIndex:
    def __init__(self, chunks):
        self.chunks = tuple(chunks)
        self.positions = {chunk.id: i for i, chunk in enumerate(self.chunks)}
        pool = {}
        def corpus():
            for chunk in chunks:
                yield [pool.setdefault(term, term) for term in (tokenize(chunk.content) or ["__empty__"])]
        self.bm25 = BM25Okapi(corpus()) if chunks else None
        if self.bm25:
            # Okapi's negative/common-term IDF otherwise makes a one-chunk
            # document return no hit even on an exact relevant query.
            self.bm25.idf = {term: max(.001, value) for term, value in self.bm25.idf.items()}
        self.size_bytes = sum(sys.getsizeof(c.content) + sys.getsizeof(c.metadata) + 256 for c in chunks)
        if self.bm25:
            self.size_bytes += sum(sys.getsizeof(d) + 28 * len(d) for d in self.bm25.doc_freqs)
            self.size_bytes += sys.getsizeof(self.bm25.idf) + sum(sys.getsizeof(k) + 24 for k in self.bm25.idf)

    def search(self, query, k):
        if self.bm25 is None or not query.strip() or k <= 0:
            return []
        terms = set(tokenize(query))
        salient = sorted((term for term in terms if term in self.bm25.idf),
                         key=lambda term: (term.startswith(("id:", "math:")), self.bm25.idf[term]), reverse=True)[:96]
        if not salient:
            return []
        raw = self.bm25.get_scores(salient)
        maximum = float(np.max(raw)) or 1
        ranked = []
        identifiers = {term for term in terms if term.startswith("id:")}
        formulas = {term for term in terms if term.startswith("math:")}
        for i, score in enumerate(raw):
            if score <= 0:
                continue
            frequencies = self.bm25.doc_freqs[i]
            covered = sum(term in frequencies for term in salient) / len(salient)
            exact_id = sum(term in frequencies for term in identifiers) / max(1, len(identifiers))
            exact_formula = sum(term in frequencies for term in formulas) / max(1, len(formulas))
            ranked.append((.6 * float(score) / maximum + .4 * covered + 2 * exact_id + exact_formula, i))
        ranked.sort(key=lambda item: (-item[0], self.chunks[item[1]].id))
        selected, seen, page_counts = [], set(), {}
        best = ranked[0][0] if ranked else 1
        for score, index in ranked:
            chunk = self.chunks[index]
            digest = hashlib.sha256(chunk.content.strip().encode()).digest()
            page_key = (chunk.document_id, chunk.metadata.get("page_id") or chunk.metadata.get("page_number") or chunk.id)
            if digest in seen or page_counts.get(page_key, 0) >= 2:
                continue
            seen.add(digest)
            page_counts[page_key] = page_counts.get(page_key, 0) + 1
            selected.append((chunk, min(1., score / best)))
            if len(selected) >= k:
                break
        return selected

    def adjacent(self, anchors, limit):
        """Add only source-contiguous spans, each with its own unchanged citation."""
        selected = list(anchors)
        seen = {chunk.id for chunk, _ in anchors}
        for chunk, score in anchors:
            position = self.positions[chunk.id]
            for offset in (1, -1):
                target = position + offset
                if not 0 <= target < len(self.chunks):
                    continue
                other = self.chunks[target]
                if (other.id in seen or other.document_id != chunk.document_id or other.content_version != chunk.content_version
                        or not chunk.metadata.get("page_number") or other.metadata.get("page_number") != chunk.metadata["page_number"]):
                    continue
                left, right = (chunk, other) if offset == 1 else (other, chunk)
                if not (type(left.metadata.get("end")) is int and type(right.metadata.get("start")) is int
                        and right.metadata["start"] <= left.metadata["end"] <= right.metadata["start"] + 512):
                    continue
                selected.append((other, score * .5))
                seen.add(other.id)
                if len(selected) >= limit:
                    return selected
        return selected


def load_index(refs):
    predicates = [((KnowledgeChunkRecord.document_id == ref["document_id"])
        & (KnowledgeChunkRecord.content_version == ref["content_version"])
        & (KnowledgeChunkRecord.chunk_index < ref["chunk_count"])) for ref in refs]
    if not predicates:
        return KnowledgeIndex([])
    with session_scope() as session:
        count, chars = session.execute(select(func.count(), func.coalesce(func.sum(func.length(KnowledgeChunkRecord.content)), 0))
            .where(or_(*predicates))).one()
        if count > 100000 or chars > 16 * 1024 * 1024:
            raise InvalidTransition("Selected knowledge exceeds the local index resource budget.", code="knowledge_index_resource_limit")
        rows = session.scalars(select(KnowledgeChunkRecord).where(or_(*predicates))
            .order_by(KnowledgeChunkRecord.document_id, KnowledgeChunkRecord.chunk_index))
        chunks = [IndexedChunk(row.id, row.document_id, row.content_version, row.content, dict(row.chunk_metadata or {})) for row in rows]
    return KnowledgeIndex(chunks)


class KnowledgeIndexCache:
    """Bounded LRU, absolute TTL and single-flight build for concurrent grading."""
    def __init__(self, *, max_bytes=96 * 1024 * 1024, max_entries=16, ttl_seconds=900):
        self.max_bytes, self.max_entries, self.ttl = max_bytes, max_entries, ttl_seconds
        self._lock = threading.Lock()
        self._build_gate = threading.Semaphore(1)
        self._items, self._pending = OrderedDict(), {}
        self.bytes = self.builds = self.hits = 0

    def get(self, owner_id, refs):
        key = (owner_id, INDEX_VERSION, tuple(sorted((r["document_id"], r["content_version"], r["chunk_count"], r["source_sha256"]) for r in refs)))
        with self._lock:
            now = time.monotonic()
            for old in list(self._items):
                if now - self._items[old][0] >= self.ttl:
                    self.bytes -= self._items.pop(old)[1].size_bytes
            if key in self._items:
                self.hits += 1
                self._items.move_to_end(key)
                return self._items[key][1]
            future = self._pending.get(key)
            builder = future is None
            if builder:
                future = self._pending[key] = Future()
        if not builder:
            return future.result()
        try:
            with self._build_gate:
                index = load_index(refs)
            with self._lock:
                self.builds += 1
                if index.size_bytes <= self.max_bytes:
                    while self._items and (self.bytes + index.size_bytes > self.max_bytes or len(self._items) >= self.max_entries):
                        self.bytes -= self._items.popitem(last=False)[1][1].size_bytes
                    self._items[key] = (time.monotonic(), index)
                    self.bytes += index.size_bytes
                self._pending.pop(key, None)
                future.set_result(index)
            return index
        except BaseException as exc:
            with self._lock:
                self._pending.pop(key, None)
                future.set_exception(exc)
            raise


INDEX_CACHE = KnowledgeIndexCache()
