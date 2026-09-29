# PDF Evidence Tool (Work Item B)

`read_pdf_evidence(bytes, request, progress=...)` runs only local PDF operations.
It does not invoke OCR, choose credentials, change upload limits, or persist
knowledge. The caller must authorize and load the source bytes first.

| Request | Result | Bound |
| --- | --- | --- |
| `PdfIndexRequest` | Page observations and exact-boundary target matches, no body text | 500-page window, 512 KiB response |
| `PdfPagesRequest(operation="detail")` | Native text, block offsets, rotated-page geometry | 24 pages, 8 MiB response |
| `PdfRenderRequest` | PNG of one visible-page region | Scale <= 2, side <= 8192, <= 16M pixels |
| `PdfPagesRequest(operation="export_pages")` | PDF page subset and original page-number mapping | 24 pages, 8 MiB response |

All page numbers are one-based; detail also provides a zero-based page index.
Normalized regions use the visible, rotated page with origin at its top left.
Text offsets are Unicode code-point half-open intervals. Native text is retained
without model interpretation, correction, or math normalization. A page index
contains observed risks, not a verified reading order or calibrated confidence.

`complete_window=true` means only the requested index window was inspected. It
never certifies whole-document recognition or retrieval coverage. The total page
count remains available for later windows. Out-of-range pages, oversize pages or
responses fail explicitly; no successful partial JSON or clipped text is returned.

The technical worker ceiling is 10,000 pages and 100 MiB input, not a teacher
quota or a promise that every such file is supported within one operation. The
default deadline is 10 seconds, with an explicit maximum of 30 seconds. Callers
can narrow local windows/batches after a bounded failure; they must not silently
omit failed pages or reuse this batch ceiling as whole-book capacity.

The existing PDF inspection/extraction commands remain unchanged. All modes use
the same process slots and cancellation-safe launch/kill/drain/reap ownership.
The parent caps streamed stdout independently, validates the response against
the requested scope and strips raw parser/validation errors. Progress uses safe
fixed text and increments its own factual counter without clearing caller metrics.

The input, structure, output, pixel and time caps are **not an OS-level resident
memory guarantee**. Some structure limits are checked after PyMuPDF returns;
the parser and document library may allocate memory before those checks. Keep
runtime capacity/load validation separate from protocol tests, and measure real
book memory usage before increasing concurrency or deployment limits.

```python
from backend.tools.pdf_evidence import PdfIndexRequest, PdfPagesRequest, read_pdf_evidence

index = await read_pdf_evidence(
    authorized_pdf_bytes,
    PdfIndexRequest(start_page=501, window_pages=500, targets=["1.2.16"]),
    progress=progress,
)
detail = await read_pdf_evidence(
    authorized_pdf_bytes, PdfPagesRequest(pages=[502, 503]), progress=progress,
)
```

Tests use synthetic PDFs, real subprocess round trips, crop/rotation pixel checks,
and owned fake process handles for deterministic cancellation races. These tests
do not establish handwritten OCR accuracy, whole-book ingestion, or RAG quality.
