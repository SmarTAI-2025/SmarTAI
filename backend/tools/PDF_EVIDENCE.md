# PDF and Image Evidence Tools (Work Items B / C2a)

`read_pdf_evidence(bytes, request, progress=...)` runs only local PDF operations.
It does not invoke OCR, choose credentials, change upload limits, or persist
knowledge. The caller must authorize and load the source bytes first.

| Request | Result | Bound |
| --- | --- | --- |
| `PdfIndexRequest` | Page observations and exact-boundary target matches, no body text | 500-page window, 512 KiB response |
| `PdfPagesRequest(operation="detail")` | Native text, block offsets, rotated-page geometry | 24 pages, 8 MiB response |
| `PdfRenderRequest` | PNG of one visible-page region | Scale <= 2, side <= 8192, <= 16M pixels |
| `PdfPagesRequest(operation="export_pages")` | PDF page subset and original page-number mapping | 24 pages, 8 MiB response |
| `PdfContactSheetRequest` | Labeled PNG and exact global-page / tile-image mapping | 8 pages, tile long edge 64-1024 (default 768), 8 MiB response |

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

## Contact Sheet Coordinates

Contact sheets contain complete rendered pages, including their rotation and
annotations. They are reduced previews for a later locator, not recognition
results or a replacement for native evidence / full-resolution target rendering.
`Page N` labels and `page_numbers` always mean the source PDF's global one-based
page number, **not the page number printed in a textbook**.

Each `PdfContactTile` retains page size / rotation and exposes two pixel-edge
rectangles: `tile_bbox_pixels` includes its label and padding;
`page_bbox_pixels` contains only the actual rendered page image. `page_region`
normalizes the latter against the entire contact sheet. To map a point from the
sheet back to normalized visual-page coordinates, subtract the page rectangle's
origin and divide by its width / height. Labels and padding are not source-page
evidence. The parent validates grid geometry, ordering, the page-number mapping,
and the PNG header dimensions without decoding pixels.

## Image Preparation

`read_image_evidence(authorized_image_bytes, ImagePrepareRequest(...))` reuses the
same isolated worker process lifecycle and shared slots. It accepts declared
`image/jpeg`, `image/png`, `image/webp`, `image/bmp` or `image/tiff`, checks the decoded format matches, and
rejects multi-frame, truncated, corrupt, unsupported-mode or oversized input.
Pillow is imported lazily: existing PDF-only worker operations do not require a
Pillow import. Neither API resolves files, selects providers, performs OCR,
uploads data, or grants access to source bytes.

The source limit is 10 MiB, 16,000,000 pixels and 8192 pixels per side, checked
before full decoding. Pillow decompression-bomb warnings become failures. Full
decoding is still required even for a small requested crop. Supported source
modes are `1`, `L`, `LA`, `P`, `RGB`, `RGBA` and `CMYK`; unsupported bit depth / mode
fails rather than silently reducing precision.

`ImagePreparedResult` records the SHA-256 of the original bytes, source MIME,
mode, raw dimensions and EXIF orientation. Missing orientation means 1; invalid
orientation fails. All eight valid orientations are supported.
`source_to_oriented_matrix` is a 2x3 affine transform of **pixel-edge coordinates**
from raw source to the correctly oriented image, not a pixel-center index map.
The requested `region` is normalized against that oriented visual image.
`crop_box_pixels` rounds its near edges down and far edges up, preserving the
requested coverage. `effective_region` records the actual outward-rounded crop;
it can differ slightly from the requested region.

The output is a fresh metadata-free RGB PNG. EXIF, GPS, ICC and textual metadata
are not carried into the result. Alpha and PNG color-key transparency (including
RGB/L/P transparency) are flattened onto white; CMYK and other supported modes
use Pillow's RGB conversion. This is a declared color / visibility transform,
**not a promise of byte-identical raw pixels or color-managed ICC fidelity**.
There is no resampling, denoising, automatic contrast enhancement or lossy
compression. Cropping happens before color conversion; orientation 1 avoids a
full transpose copy, and intermediates are released before PNG encoding.

The complete JSON response, including base64 and provenance, must fit 8 MiB.
PNG encoding is bounded, and a large / high-entropy photo may explicitly fail
with `image_response_too_large`; the tool never silently lowers its resolution.
Any future resize strategy needs a separate explicit policy and trace. Limits
are technical operation ceilings, not product upload entitlements or an OS RSS
guarantee. Orientation and decoding can still allocate full-image buffers.

Image-specific safe failures use `image_invalid`, `image_input_too_large`,
`image_format_mismatch`, `image_multiframe_unsupported`,
`image_orientation_invalid`, `image_mode_unsupported`,
`image_pixel_limit_exceeded`, `image_response_too_large` and
`image_processing_failed`. For compatibility, `PdfEvidenceError` remains the
shared carrier; request/protocol/busy/timeout/process failures retain the existing
`pdf_*` codes. No raw decoder, source text or validation detail is exposed.
Progress increments `images_prepared` without clearing parent metrics.

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

## C2a Verification Snapshot (2026-09-28)

- Existing PDF client / worker tests: 114 passed. New media worker / client tests
  and independent pixel acceptance: 75 passed (63 + 12), including EXIF 1-8,
  alpha / color-key transparency, CMYK, format mismatch, multiframe rejection,
  outward crop geometry, thin-line preservation, rotated PDF tiles, safe limits,
  real subprocess round trips and shared lifecycle timeout / cancellation.
- Three synthetic 4000x4000 RGB striped PNGs through real CLI subprocesses are
  recorded below. Each used a fresh measurement process and one worker child;
  macOS `resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss` reports bytes.
  Times include worker startup. These are individual resource observations,
  **not p95, a worst-case bound, a recognition measurement, or two-concurrent-
  worker / actual deployment-host peak-memory acceptance**.
- The local AA textbook's first eight global PDF pages rendered to a 1560x3208
  contact sheet and were visually inspected. The observed 0.427 seconds was a
  direct worker-function call, excluding subprocess startup, not p95. Its printed
  page labels differ from global PDF page numbers. No model/provider was called.

| Synthetic PNG variant | Input / JSON bytes | Status / output pixels | Seconds | Worker peak RSS bytes (MiB) |
| --- | --- | --- | --- | --- |
| RGB, EXIF absent (orientation 1) | 56,936 / 76,543 | ok / 4000x4000 | 0.319 | 188,874,752 (180.12) |
| RGB, EXIF orientation 6 | 56,974 / 75,727 | ok / 4000x4000 | 0.306 | 190,775,296 (181.94) |
| RGB, PNG tRNS white color key, orientation 1 | 56,954 / 76,543 | ok / 4000x4000 | 0.329 | 270,270,464 (257.75) |

The original-byte SHA-256 values, in the same order, are:

```text
5eb7da3e5c1bbd755a31ff79627abb3d996aed7933ab3ccac837707e3c7076d1
c263b32272bcf52399ec538c4056f8714bb54a0a288ab7207932e83ba7609751
84c1dc3af3a42029fc24f2b20dac2b3790d6552ab6e2cdb4bf013b8a65435063
```

For reproducibility, each RGB source is white with `(20, 40, 60)` horizontal
rectangles spanning x=0..3999 and y=`k*80`..`k*80+2` for k=0..49, saved with
Pillow's default PNG encoder. Only the named EXIF / transparency metadata
differs. The request is `image_prepare`, `content_type="image/png"`, full region,
sent to `_pdf_worker.py evidence-v1` with source bytes on stdin. Exact PNG hashes
can vary with encoder versions. The tRNS path necessarily uses an additional
cropped RGBA image and alpha mask; resource limits are still not RSS isolation.

These results cover media preparation only. Scan question localization, image
recognition orchestration, caller authorization, caching, whole-book ingestion
and grading integrations are separate work items, not delivered by C2a.
