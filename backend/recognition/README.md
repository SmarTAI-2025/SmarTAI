# Recognition Foundation (Work Items A-D2a)

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
| C2b | Shared execution budgets, dispatch and image-source reader |
| C2c | Bounded scan localization and unified Agent orchestration |
| D1 | Faithful evidence assembly, purpose checks and honest coverage |
| D2a | Strict repair response, same-provider adapter and bounded extra dispatch |
| D2b | Agent repair selection, source binding and recorded final assembly |
| D3 | Durable evidence, layered owner-scoped cache and submit-once recovery |
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

D still must supply fusion, actual bounded extra calls and durable caching.
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

## C2b Shared Execution Boundary

`budget.RecognitionBudget` belongs to one source, policy and frozen engine route.
The future Agent must create it before localization and pass the same instance
to every reader and repair stage. A reader creates a budget only for standalone
use; this default is not permission to reset it between workflow stages. Total,
locator (360 seconds) and read (600 seconds) deadlines include local work and
capacity waits. Phase entry is idempotent and never extends the global deadline.
Locator calls have a separate cap of 12; detail calls retain the 12 initial / 18
total limits. Recovery and repair share one extra dispatch per initial region.
These are bounded batch limits, not book quotas or promised response times.

Dispatch reserves first. Cancelled or ambiguous submissions retain their calls,
regions and unknown usage. A completed failure is not eligible for empty-output
recovery. The same region cannot silently be submitted as another initial call,
including across batches sharing the ledger. Capacity remains process-local and
must be application-scoped; persistent submit-once ownership is still required.

Bounded LLM output requests consume the shared output allowance. Known usage
replaces the reservation; unknown usage conservatively retains it. A reported
overrun is retained and blocks further calls. Unbounded OCR adapters retain
call/time/region limits but do **not** pretend to enforce token allowances:
`charged_output_tokens=None` and `reserved_output_tokens=0` mean no enforceable
token accounting, not free usage. Unknown usage stays `None` and known usage is
reported separately. This is execution accounting, never a provider invoice.

`runtime.run_initial_read()` is the one frozen-route initial dispatch boundary
used by PDF and image readers. `image_executor.read_image_plan()` uses C2a's
killable preparation, no invented native text or verified blankness, and pixel
geometry rather than fictional PDF points. Units retain both requested regions
and actual outward-rounded pixel crops; payloads remain ephemeral. Plans freeze
`source_kind`, so dual-input OCR engines select image mode for image files while
preserving their document batching capability for PDFs.

Per-batch `usage` covers only that reader's units. `budget.snapshot()` covers the
entire workflow, including preceding localization and pending submissions; these
two views must not be added together. No new storage, endpoint, engine, hidden
companion, fallback, retry or real-model accuracy claim is introduced in C2b.

## C2c Bounded Localization And Agent

`agents.recognition_agent.RecognitionAgent.read()` is the unified PDF/image raw
evidence workflow. It authorizes the caller/source identity and byte digest,
freezes one injected engine and policy, then shares one C2b budget across local
inspection, optional scan localization and reading. It does not choose a vendor,
fetch credentials, create business records, merge evidence or perform repairs.

Explicit source-page hints come first. Native full identifiers and compound
section/local labels retain exact text proofs. Section-prefix index candidates
remain pending until inspected, including candidates beyond the detail budget;
filtering them away would falsely make an early exercise unique. Nearby pages
may be inspected as context but do not inherit sections merely by adjacency.

An unresolved target can use the selected engine's declared `target_location`
capability. OCR-only engines without it never acquire an LLM companion. The LLM
adapter reuses the same provider, request bounds, metadata, errors and identity
guards for a dedicated locator prompt. It defaults to one contact sheet per call
for compatibility; an explicitly declared two-image capability permits two.
Each sheet contains at most eight labeled source pages, never printed book page
numbers. The local index window is at most 500 pages, scan calls at most 12, and
target detail pages at most `min(policy.max_detail_pages, 2 * targets + 4)`.
These limits do not implement whole-book knowledge ingestion; H remains separate.

`scan_locator.py` accepts strict bounded JSON (optionally one complete JSON
fence), complete exact target identities and only actually submitted source
pages. Duplicate keys, nonfinite values, extra fields, wrong pages, truncated,
empty or refused responses cannot become successful locations. No paid JSON
repair or resubmission occurs. A visible uncertain cue goes to full-resolution
reading instead of additional low-resolution search, while its target remains
unresolved. All scan suggestions remain unverified and scoped to inspected pages;
they neither prove question completeness nor absence from the rest of the book.

Malformed/failed localization or a scan tool error halts downstream dispatch,
retaining earlier native detail. Competing native identities cannot be erased by
a low-resolution guess. Overbroad page selection is explicit, never silently
truncated to the first candidates. Workflow artifacts retain sheet geometry and
payload digests, raw responses, native proofs, all call outcomes and global budget
snapshots, but not transient image payloads. Removing paid-call evidence invalidates
the artifact. A scan/read workflow always has `recognition_complete=False`.

Offline tests use generated PDFs/images and fake model outcomes to verify these
boundaries. The actual AA seven-target file can also exercise native orchestration
with zero model calls. Neither test establishes real-model OCR quality; D-J and
authorized real-model ablations remain necessary before any acceptance claim.

Local AA orchestration smoke (2026-09-28): the historical 15-page,
1,527,595-byte fixture with SHA-256
`6a8e6d6feab72f3eddcc16cea36f2c8536c1264837ac39bb0603389ccf06e620`
requested `1.1.5`, `1.1.7`, `1.1.20`, `1.1.29`, `1.1.31`, `1.2.3`,
`1.2.16`. With no engine, native inspection selected source pages 1 and 4;
the four targets `1.1.20`, `1.1.29`, `1.1.31`, `1.2.16` remained unlocated.
The result retained `target_location_needs_hint`,
`visual_capability_unavailable` and `recognition_complete=False`. The single
local run took 0.564 seconds and made zero model calls. This is a local
orchestration check, not OCR accuracy, latency-distribution or user acceptance
evidence. The private fixture is not included in this repository change.

## D1 Evidence Assembly Boundary

`RecognitionAgent.recognize(..., prompt_version=...)` adds a deterministic
assembly after the same read workflow. It reports progress without resetting
existing counters. `fusion.assemble_recognition()` is also callable on validated
raw evidence. Neither path makes an additional model call. The caller supplies
the actual adapter prompt version; the API has no implicit vendor selection.

Whole-page native/visual candidates can be compared because their input scope
matches. The selected span is exactly one retained raw candidate, not a guessed
hybrid. Both candidates remain available. NFC, CRLF and explicit outer math
wrappers are the only allowed equivalence comparison; digits, negatives,
exponents, command words and internal whitespace cannot be normalized away.
Submissions prefer available visual evidence so native text cannot silently
correct handwriting. Refused output is never adopted, empty visual output never
erases native text, and partial/error outcomes prevent complete page coverage.

`quality.assess_candidates()` labels disagreement, incomplete coverage, visible
uncertainty and limited packaging defects. It does not check mathematical
correctness, fix student errors, invent identities or authorize another call.
Labels are uncalibrated and at most `medium`; even agreement is not proof of
missing-line absence. Located target labels retain `unverified_targets` until
downstream target-content checks establish the requested bodies. A page read
does not make an entire exercise, document or knowledge corpus accurate.

The assembly retains its complete raw workflow. Image pages carry actual pixel
dimensions, never invented PDF point dimensions. Multi-page document-only output
remains in `unaligned_units`, not falsely attached to page one. Crop output is
also retained there when its placement relative to native text is unproven;
automatic spatial composition/alignment is not implemented in D1. These units
are not appended to overlapping native text to manufacture completeness, and
their pages remain incomplete. Downstream callers must inspect both coverage
and the independent candidates, not treat `final_markdown` alone as success.

Native/page or final-render length limits produce an explicit assembly error
with the complete raw workflow still present; no first-N-character truncation.
Malformed original PDFs preserve their failed workflow without an invented page.
Round-trip validators rederive the document and enforce frozen scope, policy,
engine, overlapping native snapshots and paid call evidence. This does not
replace durable artifact hashes, authorized storage access or submit-once locks.

Usage is the workflow ledger including localization, never reader-plus-ledger
double counting. Each call retains its actual requested output-token bound.
Workflow validation reconciles settled/pending outcomes, known/unknown tokens,
reservations, charges and overrun flags against those call records. Reader-only
usage is checked separately. Unknown tokens remain unknown. `result_cache_key` is a future
identity over owner/source/scope, policy, engine capabilities/fingerprint,
assembly and prompt versions; D1 does not read or write any cache. D2/D3 still
own bounded extra calls, alignment/repair decisions and persistent recovery.
Existing upload/business routes, real-model quality, whole-book RAG and UI
acceptance remain later items. `recognition_complete` is still false.

D1 local AA smoke reused the seven-target file above with no engine: 0.626
seconds in one local run, zero model calls, 5,869 native characters retained and
an equal JSON round-trip. Four targets remained missing; the other three were
located but their complete bodies still unverified. Both selected pages stayed
unprocessed for required visual evidence, and confidence remained low. This
checks honest evidence retention only, not improved OCR accuracy.

## D2a Repair Protocol Boundary

`EngineRepairInputV1` permits one inspected page image, an explicit target region,
bounded raw native/visual candidates and the exact pre-repair text. It retains
the same injected provider and limits output to 2,048 tokens. The dedicated
prompt requests `keep_native`, `keep_visual`, `replace` or `still_unknown` with a
visible source cue. It prohibits solving, completing missing conditions and
correcting student mistakes. This instruction is not a measured fidelity claim.

`repair_response.parse_repair_response()` accepts only a complete bounded JSON
object (or one complete JSON fence). Unknown/duplicate fields, nonfinite values,
truncation, refusal, native PDF JSON masquerading as a model response and invalid
keep/replace operations fail closed. Invalid or unknown output preserves the
original text. Replacements require a nonempty source cue and remain explicitly
unverified and subject to review; a model-generated cue cannot prove fidelity.
There is no paid JSON repair, answer checking or automatic high-confidence label.

`runtime.run_repair_call()` shares the original live budget, route, deadline and
capacity. Recovery and patch consume the same one-extra-per-initial-region credit;
failed or pending initial submissions cannot authorize either. Unsupported
engines and disabled policy make zero extra calls. Explicit submission uncertainty
stays pending even when an adapter omits its redundant flag. Error-bearing empty
results cannot be relabeled as successful empty transcriptions and retried.

This item exposes the protocol and dispatch boundary only. The Agent does not
trigger repair yet. D2b must prove that the initial budget key, unit, page,
purpose, region, original candidates and inspected source image all belong
together, then retain repair outcomes and reconcile final usage. A caller-supplied
key alone is not evidence of that relationship. D3 still owns durable recovery.
No upload route, credential behavior, new vendor or paid service is added here.
