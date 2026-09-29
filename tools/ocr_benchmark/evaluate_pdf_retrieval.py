"""Bounded, local native-PDF retrieval baseline; no OCR or provider calls."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import time

from backend.knowledge.index import IndexedChunk, KnowledgeIndex
from backend.rag.chunker import chunk_spans
from backend.tools.pdf_evidence import PdfIndexRequest, PdfPagesRequest, read_pdf_evidence


async def evaluate(body, cases):
    if len(body) > 64 * 1024 * 1024 or not 1 <= len(cases) <= 1000:
        raise ValueError("Diagnostic input limit exceeded")
    index = await read_pdf_evidence(body, PdfIndexRequest(window_pages=1))
    if index.total_pages > 1000:
        raise ValueError("Diagnostic page limit exceeded")
    for case in cases:
        if (not isinstance(case.get('query'), str) or len(case['query']) > 4000
                or not isinstance(case.get('expected_pages'), list)
                or any(type(p) is not int or not 1 <= p <= index.total_pages for p in case['expected_pages'])):
            raise ValueError("Invalid query or expected source page")
    chunks, page_stats, chars = [], [], 0
    for first in range(1, index.total_pages + 1, 24):
        detail = await read_pdf_evidence(body, PdfPagesRequest(pages=list(range(first, min(first + 24, index.total_pages + 1)))))
        for page in detail.pages:
            text = page.native_text
            chars += len(text)
            if chars > 16 * 1024 * 1024:
                raise ValueError("Diagnostic text limit exceeded")
            page_stats.append(dict(page=page.page_number, native_chars=len(text)))
            for i, span in enumerate(chunk_spans(text)):
                chunks.append(IndexedChunk(f"pdf:{page.page_number}:{i}", "pdf", "native-baseline", span['content'],
                    dict(page_number=page.page_number, start=span['start'], end=span['end'])))
    search = KnowledgeIndex(chunks)
    results = []
    for case in cases:
        started = time.perf_counter()
        found = search.search(case['query'], 10)
        pages = [chunk.metadata['page_number'] for chunk, _ in found]
        expected = set(case['expected_pages'])
        results.append(dict(case=case['id'], expected_pages=sorted(expected), top5_pages=pages[:5], top10_pages=pages,
            all_expected_in_top5=expected.issubset(pages[:5]) if expected else not pages,
            milliseconds=round((time.perf_counter() - started) * 1000, 3)))
    return dict(source_sha256=hashlib.sha256(body).hexdigest(), source_pages=index.total_pages,
        native_layer_only=True, provider_calls=0, index_version=search.version,
        chunk_count=len(chunks), page_stats=page_stats, cases=results,
        limitation="Actual local PDF native-layer baseline only. Not OCR, frontend ingestion, semantic-support or teacher acceptance evidence.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pdf', type=Path)
    parser.add_argument('queries', type=Path)
    args = parser.parse_args()
    with args.pdf.open('rb') as source:
        body = source.read(64 * 1024 * 1024 + 1)
    with args.queries.open('rb') as source:
        queries = source.read(1024 * 1024 + 1)
    if len(queries) > 1024 * 1024:
        parser.error('Query manifest exceeds 1 MiB')
    print(json.dumps(asyncio.run(evaluate(body, json.loads(queries))), ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
