"""Run a bounded local retrieval diagnostic. Never opens provider credentials."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import time

from backend.knowledge.index import IndexedChunk, KnowledgeIndex
from backend.knowledge.snapshots import INDEX_VERSION, SUPPORTED_INDEX_VERSIONS


def evaluate(corpus, *, index_version=INDEX_VERSION):
    pages = corpus["pages"]
    ids = [f"{page['book']}:{page['page']}" for page in pages]
    if len(ids) != len(set(ids)) or len(pages) > 10000 or len(corpus["queries"]) > 1000:
        raise ValueError("Duplicate pages or diagnostic size limit exceeded")
    chunks = [IndexedChunk(key, page["book"], "diagnostic-v1", page["text"],
              dict(page_number=page["page"], start=0, end=len(page["text"]))) for key, page in zip(ids, pages)]
    indexes, results, timings = {}, [], []
    for case in corpus["queries"]:
        expected = set(case["expected"])
        if not expected.issubset(ids):
            raise ValueError("Unknown expected page")
        scope = tuple(sorted(case.get("books", {p["book"] for p in pages})))
        started = time.perf_counter()
        if scope not in indexes:
            indexes[scope] = KnowledgeIndex([chunk for chunk in chunks if chunk.document_id in scope], version=index_version)
        found = indexes[scope].search(case["query"], 10)
        timings.append((time.perf_counter() - started) * 1000)
        matches = [chunk.id for chunk, _ in found]
        results.append(dict(id=case["id"], kind=case["kind"],
            recall_at_5=len(expected.intersection(matches[:5])) / len(expected) if expected else None,
            recall_at_10=len(expected.intersection(matches)) / len(expected) if expected else None,
            abstained=not matches, expected_pages=sorted(expected), matched_pages=matches))
    kinds = {}
    for kind in sorted({r["kind"] for r in results}):
        selected = [r for r in results if r["kind"] == kind]
        positive = [r for r in selected if r["expected_pages"]]
        negative = [r for r in selected if not r["expected_pages"]]
        kinds[kind] = dict(count=len(selected), recall_at_5=statistics.mean(r["recall_at_5"] for r in positive) if positive else None,
            recall_at_10=statistics.mean(r["recall_at_10"] for r in positive) if positive else None,
            abstention_rate=statistics.mean(r["abstained"] for r in negative) if negative else None)
    return dict(provenance=corpus["provenance"], index_version=index_version, provider_calls=0, query_count=len(results),
        latency_ms_including_cold=dict(p50=statistics.median(timings), p95=sorted(timings)[int(len(timings) * .95)]),
        by_kind=kinds, cases=results,
        limitation="Page retrieval is not a semantic-support verdict. Synthetic diagnostic, not a held-out teacher evaluation.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", nargs="?", type=Path, default=Path(__file__).with_name("retrieval_cases.json"))
    parser.add_argument("--index-version", choices=sorted(SUPPORTED_INDEX_VERSIONS), default=INDEX_VERSION)
    args = parser.parse_args()
    with args.corpus.open("rb") as stream:
        content = stream.read(16 * 1024 * 1024 + 1)
    if len(content) > 16 * 1024 * 1024:
        parser.error("Corpus exceeds 16 MiB")
    print(json.dumps(evaluate(json.loads(content), index_version=args.index_version), ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
