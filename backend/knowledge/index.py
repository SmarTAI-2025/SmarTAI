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
from backend.knowledge.snapshots import INDEX_VERSION, LEGACY_INDEX_VERSION, SUPPORTED_INDEX_VERSIONS
from backend.domain.errors import InvalidTransition
from backend.knowledge.terms import concept_terms, QUERY_BOILERPLATE

LEGACY_STOP_WORDS = frozenset("the a an of is are to in on and or for with that this prove show let find given using determine".split())
STOP_WORDS = frozenset(("the a an of is are to in on and or for with that this prove show let find given using determine "
    "what why how does do can please explain definition theorem exercise chapter textbook about it its be as by from").split())


def tokenize(text, *, legacy=False):
    normalized = unicodedata.normalize("NFKC", text).lower().replace("−", "-")
    identifier_pattern = r"(?<![\w.])\d+(?:\.\d+)+(?![\w.])" if legacy else r"(?<![\w.])\d+(?:\.\d+){2,}(?![\w.])"
    stop_words = LEGACY_STOP_WORDS if legacy else STOP_WORDS
    words = [word for word in re.findall(r"[a-z0-9_]+", re.sub(identifier_pattern, " ", normalized)) if word not in stop_words]
    # Bigrams distinguish terms such as 同态/同构 without a large dictionary.
    # Isolated characters remain searchable, but do not dominate whole queries.
    for run in re.findall(r"[\u3400-\u9fff]+", normalized):
        words.extend("c:" + run[i:i + 2] for i in range(len(run) - 1))
        if len(run) == 1:
            words.append("c:" + run)
    words.extend("id:" + value for value in re.findall(identifier_pattern, normalized))
    # A bare 1.2 or an explicitly labelled exercise may be an ID; 9.8 in
    # ordinary prose/equations is a decimal, not an exercise constraint.
    if not legacy:
        words.extend("id:" + value for value in re.findall(
            r"(?:exercise|problem|例题|习题|练习|题号)\s*[:：]?\s*(\d+\.\d+)(?![\w.])", normalized))
        if re.fullmatch(r"\s*\d+\.\d+\s*", normalized):
            words.append("id:" + normalized.strip())
    words.extend("math:" + variable + "^" + exponent for variable, exponent in
                 re.findall(r"([a-z])\s*\^\s*[{(]?\s*([+-]?\d+)\s*[})]?", normalized))
    return words


def index_terms(text):
    normalized = unicodedata.normalize("NFKC", text).lower()
    return tokenize(normalized) + sorted(concept_terms(normalized))


@dataclass(frozen=True)
class IndexedChunk:
    id: str
    document_id: str
    content_version: str
    content: str
    metadata: dict


class KnowledgeIndex:
    def __init__(self, chunks, *, version=INDEX_VERSION):
        if version not in SUPPORTED_INDEX_VERSIONS:
            raise InvalidTransition("Unknown knowledge index version.", code="knowledge_content_version_unavailable")
        self.version = version
        self.legacy = version == LEGACY_INDEX_VERSION
        self.chunks = tuple(chunks)
        self.positions = {chunk.id: i for i, chunk in enumerate(self.chunks)}
        pool = {}
        def corpus():
            for chunk in chunks:
                terms = tokenize(chunk.content, legacy=True) if self.legacy else index_terms(chunk.content)
                yield [pool.setdefault(term, term) for term in (terms or ["__empty__"])]
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
        terms = set(index_terms(QUERY_BOILERPLATE.sub(" ", query)))
        terms.update(term for term in tokenize(query) if term.startswith("id:"))
        if self.legacy:
            terms = set(tokenize(query, legacy=True))
        salient = sorted((term for term in terms if term in self.bm25.idf),
                         key=lambda term: (term.startswith(("id:", "math:")), self.bm25.idf[term]), reverse=True)[:96]
        if not salient:
            return []
        raw = self.bm25.get_scores(salient)
        maximum = float(np.max(raw)) or 1
        ranked = []
        identifiers = {term for term in terms if term.startswith("id:")}
        formulas = {term for term in terms if term.startswith("math:")}
        concepts = {term for term in terms if term.startswith("term:")}
        lexical = {term for term in terms if not term.startswith(("id:", "math:", "term:"))}
        formula_only = formulas and not concepts and not identifiers and all(
            len(t) <= 1 or t.lstrip("+-").isdigit() for t in lexical)
        for i, score in enumerate(raw):
            if score <= 0:
                continue
            frequencies = self.bm25.doc_freqs[i]
            # An explicit exercise ID must not degrade to an unrelated lexical
            # hit. Likewise, incidental words cannot substitute for a named
            # technical concept when none of those concepts occurs here.
            if not self.legacy and identifiers and not identifiers.intersection(frequencies):
                continue
            if not self.legacy and concepts and not concepts.intersection(frequencies):
                continue
            if not self.legacy and formula_only:
                if not formulas.issubset(frequencies):
                    continue
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
            # Relative ranking is not confidence that a passage supports a
            # conclusion; the citation contract keeps that distinction.
            selected.append((chunk, min(1., score / best)))
            if len(selected) >= k:
                break
        return selected

    def adjacent(self, anchors, limit):
        """Add only source-contiguous spans, each with its own unchanged citation."""
        selected = list(anchors)
        seen = {chunk.id for chunk, _ in anchors}
        page_counts = {}
        for chunk, _ in anchors:
            key = (chunk.document_id, chunk.metadata.get("page_number"))
            page_counts[key] = page_counts.get(key, 0) + 1
        for chunk, score in anchors:
            position = self.positions[chunk.id]
            for offset in (1, -1):
                target = position + offset
                if not 0 <= target < len(self.chunks):
                    continue
                other = self.chunks[target]
                key = (other.document_id, other.metadata.get("page_number"))
                if (other.id in seen or other.document_id != chunk.document_id or other.content_version != chunk.content_version
                        or not chunk.metadata.get("page_number") or other.metadata.get("page_number") != chunk.metadata["page_number"]
                        or (not self.legacy and page_counts.get(key, 0) >= 2)):
                    continue
                left, right = (chunk, other) if offset == 1 else (other, chunk)
                if not (type(left.metadata.get("end")) is int and type(right.metadata.get("start")) is int
                        and right.metadata["start"] <= left.metadata["end"] <= right.metadata["start"] + 512):
                    continue
                selected.append((other, score * .5))
                seen.add(other.id)
                page_counts[key] = page_counts.get(key, 0) + 1
                if len(selected) >= limit:
                    return selected
        return selected


def load_index(refs):
    versions = {ref.get("index_version", INDEX_VERSION) for ref in refs}
    if len(versions) > 1 or not versions.issubset(SUPPORTED_INDEX_VERSIONS):
        raise InvalidTransition("Incompatible knowledge index versions.", code="knowledge_content_version_unavailable")
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
    return KnowledgeIndex(chunks, version=next(iter(versions), INDEX_VERSION))


class KnowledgeIndexCache:
    """Bounded LRU, absolute TTL and single-flight build for concurrent grading."""
    def __init__(self, *, max_bytes=96 * 1024 * 1024, max_entries=16, ttl_seconds=900):
        self.max_bytes, self.max_entries, self.ttl = max_bytes, max_entries, ttl_seconds
        self._lock = threading.Lock()
        self._build_gate = threading.Semaphore(1)
        self._items, self._pending = OrderedDict(), {}
        self.bytes = self.builds = self.hits = 0

    def get(self, owner_id, refs):
        key = (owner_id, tuple(sorted((r["document_id"], r["content_version"], r["chunk_count"], r["source_sha256"],
                                     r.get("index_version", INDEX_VERSION)) for r in refs)))
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
