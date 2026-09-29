# Recognition Foundation (Work Items A-C2a)

This package is not wired to an upload endpoint yet. It defines the first
version of the evidence and planning contracts plus callable existing-engine
adapters; it does not establish an OCR accuracy improvement. No new dependencies,
migrations, settings, credential probes or billing services are introduced.
The existing upload routes remain unchanged until the later wiring items.

## Boundaries

- `models.py`: source attribution, raw/normalized candidates, page/span evidence,
  explicit coverage, bounded patch history and usage. A document renders its
  ordered spans; a span retains a candidate or an explicitly recorded patch.
  Normalization permits only NFC, CRLF conversion and outer math delimiters;
  it cannot change symbols, internal whitespace, identifiers or student errors.
  Question labels are hints, not scoring identities or new subquestion records.
- `planner.py`: a deterministic function over page observations, purpose,
  capabilities and policy. The caller must resolve the authorized, frozen route.
  Clean prose may use native text; mathematical/layout/image risks require visual
  evidence. Submissions require visual fidelity even with a clean text layer.
  Crops require complete localization evidence; otherwise the full page is used.
- `engine.py`: one provider-neutral transcription call. PDF/image payloads are
  ephemeral and excluded from repr/serialization. The executor must inspect and
  authorize them; the contract alone does not prove payload safety or ownership.
  Validation error strings hide inputs; structured diagnostic projections must
  also use `errors(include_input=False)` and never expose raw validation objects.

Every plan retains its source scope, requested targets, all page decisions,
policy snapshot and engine capabilities. The planner handles one bounded batch:
deferred/blocked pages do not disappear. `recognition_complete` is always false
on a plan. Coverage completion is meaningful only together with its declared
`targets`, `pages` or `document` scope. Knowledge ingestion needs a separate
durable whole-document manifest, not a 24-page artifact relabeled as a book.

The only initial strategy is explicitly **uncalibrated** and uses one selected
route. It optimizes the evidence path, not the choice of paid provider. It does
not invent costs, quality scores or latency estimates. Later measured profiles
may inform strategy selection after held-out evaluation; no hidden LLM planner
or automatic vendor fallback is permitted.

At most one extra call per region may be reserved, sharing recovery and repair
budgets. OCR-only engines cannot gain a semantic companion implicitly. Actual
deadlines, cancellation, usage accumulation, provider errors, progress, cache
authorization and submit-once recovery are execution responsibilities, not
implemented by A. The existing Baidu uncertain-submission boundary must remain.

## Sequential Delivery

Each item is tested and opened as a separate PR before the next is implemented.
Dependent PRs may stack; none is merged automatically.

| Item | Scope |
| --- | --- |
| A | Evidence, capabilities, policy, pure planner and engine port |
| B | Killable PDF indexing, bounded detail and targeted rendering |
| C1 | Faithful adapters, native locator and controlled PDF evidence execution |
| C2a | Killable image preparation and mapped PDF contact sheets |
| C2b | Bounded scan localization, shared budgets and unified input orchestration |
| D | Evidence merge, purpose checks, one bounded repair and durable caching |
| E | Problem extraction and compatibility routes |
| F | Teacher reference/rubric/test materials and confirmation gates |
| G | Student submission sources, faithful transcription and retry |
| H | Complete, resumable knowledge ingestion and lossless chunks |
| I | Retrieval quality, reusable indexes, citations and capacity |
| J | Cross-entry/UI regression, real-model ablation and acceptance |

Knowledge and high-fidelity transcription share primitives, but not the same
workflow or acceptance threshold. Parsed business structures belong downstream
and must not overwrite raw transcription. No new specialized OCR integrations
or commercial charging mechanisms are included in this series.

## Verification

Run the focused contracts with the project's Python environment:

```sh
python -m pytest -p no:cacheprovider \
  backend/tests/test_recognition_evidence.py \
  backend/tests/test_recognition_planner.py \
  backend/tests/test_recognition_engine.py
```

The fake engine cases establish replaceable interfaces, not vendor accuracy.
Tests cover coverage partitions, faithful rendering, explicit changes, page and
region identities, unknown usage, fixed-route planning, capability failures,
1000-page batch accounting and randomized deterministic budget bounds. Real
problem/student accuracy, knowledge recall and runtime UX remain unverified.

## C1 Execution Boundary

`skills/recognition_reader.py` implements the injected LLM and existing Baidu
ports. Prompts distinguish all six purposes and treat document instructions as
source data. Raw text is retained without math rewriting, whitespace stripping
or answer correction. A completed empty response remains empty. There is no
repair, retry, fallback, solving, scoring, JSON repair or hidden companion.

LLM requests send a per-call token limit through the selected SDK/wire protocol.
SDK visible text blocks are joined in order; reasoning/signature metadata is not
source text. Known token usage and a safe finish category are preserved; missing
usage remains unknown. Truncated/refused output is not upgraded to confidence.
Both local older SDKs and the deployment minimum versions are tested offline.

`executor.read_pdf_plan()` checks owner identity, byte digest, source type,
serialized plan and frozen engine capability snapshots before work. It uses B's
killable detail/render/export tool and emits progress without resetting outer
workflow counters. Source authorization and durable submit-once ownership remain
the caller's responsibility, not something a source-ref string proves.

One application-scoped `RecognitionCapacity` must be shared across purposes;
limits are process-local, not a distributed quota. No engine error triggers a
retry. Waiting, local reads and dispatch share the total deadline. Sync Gemini
SDK calls cannot be forcibly killed: on cancellation their leases remain held
until the SDK thread drains under its existing request timeout. This may exceed
the logical deadline and must not be marketed as hard real-time cancellation.

Plans preserve explicit document call groups. Up to the engine's page limit is
one submitted PDF group, not one billed call per page, including mixed-mode
plans. A multi-page Markdown response has `output_mapping=document_only`;
submitted input page numbers never prove output-to-page alignment. Unknown OCR
token usage is not recorded as zero and OCR-only cannot silently invoke an LLM.

The read-batch artifact is not the final recognition document. It preserves
native pages, independent visual units, failed/unprocessed pages and the exact
unprocessed regions. `partial_pages` is derived from these regions. D must use
all of these before deciding coverage or quality; a nonempty unit is not proof
that its page is complete. Rendering payload bytes are not serialized.

`locator.py` is a pure native-evidence locator. It keeps complete literal
boundaries, chapter/section plus local-label proofs and exact raw offsets. A
same-page ambiguity can select full-page context without claiming a unique
question; cross-page duplicate identities are not resolved by taking the first.
Unlocated means absent from the supplied evidence only, not from a whole book.
The locator never inherits a section or continuation from mere page adjacency.

C2b still must supply bounded scan localization and unified image execution; D
still must supply fusion, the shared one-extra-call budget and durable caching.
No foundational result establishes end-user OCR quality or full 05/06 completion.

## C2a Media Boundary

The existing killable worker now prepares JPEG/PNG/WebP input and bounded PDF
contact sheets. See [tool contracts](../tools/PDF_EVIDENCE.md). Image preparation
records the source digest, EXIF transform, requested and actual crop, and output
dimensions. It does not denoise, resize, invent native PDF coordinates or call a
model. Explicit size/decode errors never become successful empty transcription.

A contact sheet is a low-resolution locator input only, not a transcription or
substitute for the full-resolution region. Its labels are one-based source PDF
page numbers, which can differ from printed book page numbers. Image rectangles
exclude added labels and padding. A model's future interpretation must remain a
candidate location, not automatically verified geometry or complete coverage.

Input/output limits protect each local operation, not whole-book quotas. Files
are prepared from already-authorized bytes with no credential, cache or storage
lookup. There are still no changes to existing upload routes in C2a.
