# Recognition Foundation (Work Items A-D3b2b2)

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
| D3a | Versioned evidence codec, cache identities and owner-bound persistence |
| D3b1 | Authorized cache lookup and bounded local evidence reuse |
| D3b2a | Terminal final reuse receipts and Agent local-cache injection |
| D3b2b1 | Exact per-call artifact lookup/record and separate occurrence receipts |
| D3b2b2 | Versioned workflow reuse provenance and current execution accounting |
| D3c | Durable pre-submit checkpoint, restart and uncertain-submit recovery |
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

### D3b2b2 Versioned Workflow Reuse

`RecognitionAgent(..., call_service=...)` opts into V2 workflow, read-batch,
assembly and invocation contracts. Omitting the service retains the original V1
path and serialized fields. The V2 aggregate shares scope checks, source tools,
fusion and repair selection with V1; it does not reinterpret old artifacts.
An occurrence keeps the original typed leaf, digest, source and storage reference
alongside its current position. Historical unit IDs and known/unknown tokens stay
intact. Final V2 results use a distinct assembly identity and payload kind.

`RecognitionBudget.logical_snapshot()` counts both fresh and cached occurrences
against page/region/call limits. The original spending snapshot counts only new
reservations. Hits never reserve/settle a fake call or consume historical tokens;
a successful initial hit may permit one source-bound patch, never an empty
recovery. Recheck selection uses logical remaining calls but fresh token spend.
Repair proposals remain ineligible for successful cache reuse.

Lookup precedes dispatch. Miss permits the current caller-owned execution path;
unsuccessful, corrupt, unavailable and deleted-source outcomes stop it. A failed
result write retains the paid candidate and spending ledger and halts subsequent
dispatch, including rechecks. Cancellation propagates; it is not a refund or
retry permission. **Durable pre-submit ownership and restart protection are still
D3c**, required before any business endpoint enables this path. Exact cache keys
include output bounds; changing that bound can produce a legitimate cache miss.
No runtime success is a fidelity/accuracy certificate.

### D3b2b1 Per-Call Reuse Boundary

`services/recognition_calls.py` adds assignment-bound lookup/record for locator,
initial read and repair leaves. Keys come from the frozen actual engine input,
including output limit, pixels, page mapping, purpose, route, capabilities,
policy and prompt version. A hit also checks the leaf against those submitted
inputs and rechecks live source availability after validation. Non-pixel-aligned
image crops retain both the requested region and its outward-rounded effective
region; they are checked against the corresponding preparation fields.

Storage lookup names now include a versioned, complete source-binding digest.
Two identical uploads under different stored-file IDs cannot poison each other's
latest-cache lookup. Old names remain readable and are consulted only when no
new scoped row exists. An intact legacy row for another original is a miss, not
corruption; it never authorizes a new submission. Direct historical reads still
require the exact source. A failed/corrupt scoped row never falls back to legacy
success. If the latest legacy row matches the source, its failure/uncertainty
remains unsuccessful; legacy lookup does not scan or fall back to older rows.

`RecognitionCallReceiptV2` wraps the original V1 artifact without changing its
unit ID, fields or digest. The new occurrence ID belongs to this execution only.
A hit has zero current model dispatches, tokens and model duration, while the
referenced evidence retains its original known/unknown usage and duration.
Artifact I/O duration is separate. Uncertain submissions cannot report settled
tokens. Historical projections are not deduplicated invoices. Independent static
gzip fixtures lock the old V1 visual, locator, repair and assembly wire formats.

This service has no engine, fallback, automatic get-or-dispatch, logical budget
credit or pre-submit authority. The caller must obtain source-bound inputs from
authorized tools and only record its own fresh dispatched outcome. Schema/hash
checks cannot prove arbitrary pixels came from the original. Failed evidence is
stored without being reused as success; repair proposals always return
`not_success`, including syntactically valid keep decisions. Bad storage, source
cleanup or a newer failed result never automatically retries an older paid call.
The injected store supplies progress events and assignment deletion fences.

The original D3b2b1 service did not connect to the Agent; D3b2b2 supplies the
versioned integration described above. D3c supplies durable job/checkpoint/recovery
authority. Business wiring and
actual OCR/whole-book quality remain E-J; cache integrity is not OCR accuracy.

### D3a Persistence Boundary

`cache_identity.py` distinguishes native, render, visual, patch and final layers.
Every key includes owner, input SHA256, MIME, tool version and exact request
parameters. Model layers also freeze purpose, route/model fingerprint, complete
capabilities, policy, prompt version and actual submitted payload digests. Local
native/render evidence does not depend on a model or purpose. Final assemblies
include the original business source binding. Keys never contain credentials and
do not authorize access. Standalone call results cannot reconstruct their entire
request from a digest; their producer must compute the identity from the actual
request, and the future cache consumer must compare against its current request.

`artifact_codec.py` persists six typed evidence kinds as canonical, single-member
gzip JSON with schema and payload hashes. Decode is bounded to 16 MiB compressed
and 64 MiB decompressed; concatenated members, trailing bytes, truncated streams,
duplicate JSON keys and nonfinite values are rejected. These byte ceilings are
not process-memory guarantees. Render pixels, credentials and arbitrary payload
dictionaries are not persisted. These are integrity checks, not encryption or
proof of transcription fidelity. Storage retains sensitive source-derived text
under the project's existing access and retention controls.

`services/recognition_artifacts.py` accepts injected storage and emits progress.
Database/storage/codec work runs off the event loop. Save validates the authorized
owner, original stored-file row, SHA/MIME, availability and exactly one business
link. Assignment artifacts reuse existing restart-safe write intents and optional
operation/attempt/lease fences. Revision and knowledge save/load explicitly fail
closed. Plain revision artifact writes do not join task deletion's intent drain;
the current library deletion worker collects its canonical original, not new
derived objects. G/H must enroll all recognition artifacts in their actual
lifecycles before enabling these bindings. The shared codec and identities do
not imply storage support for those bindings. Task-linked submission sources can
use the assignment path after the application authorizes that association.
Callers must first authorize the business operation. Legacy
student-owned sources do not become teacher-owned merely by changing a source-ref.

Load checks owner, business link, storage backend, kind, MIME, bounded size, object
SHA, envelope identity, source and payload hash. It does not reopen the original,
so original cleanup does not erase existing evidence. Recognition artifacts are
not added to original-file cleanup kinds. Missing/corrupt evidence differs from
transient storage failure. Neither outcome invokes a model or permits resubmission.

Failures, empty outcomes, pending submissions, low quality and assembly overflow
remain persistable. `cacheable_success` is computed after revalidation, not trusted
from stored JSON; it never means teacher acceptance or measured OCR accuracy.
Repair proposals (including keep decisions) are not successful cache entries. D3a
does not enable automatic cache hits, deduct old usage from a current budget,
resume jobs or replace the existing Baidu uncertain-submit guard. Those changes
belong to separate D3b/D3c PRs before business wiring.

### D3b1 Local Reuse Boundary

`RecognitionArtifactStore.find_success()` freezes mutable inputs before awaiting,
rechecks the original and live task, and queries only the newest exact identity
in that owner/task. Hit, miss, unsuccessful, corrupt, unavailable and unavailable
source are distinct outcomes. A newer failure does not fall back to an older
success. Storage failure never silently starts a model. Locator warnings and low
quality visual candidates cannot become success hits; mathematical mistakes are
not OCR errors. Locator hits remain location suggestions, not verified geometry.

`RecognitionByteCache` is injected process-local storage, capped at 64 MiB of
payloads and 512 entries with an absolute 30-minute TTL and LRU eviction. Native
and render keys include the exact source/business binding. It contains immutable
bytes, not credentials, model replies or exported PDF payloads. These limits are
not RSS or distributed guarantees; expired entries are removed during accesses.

`RecognitionLocalEvidenceReader` verifies actual source bytes, current original
availability and task deletion state before reuse and after local work. Native
index/detail results use durable artifacts; prepared/rendered/contact-sheet
pixels use RAM only. Every hit is paired with its current typed request, validated
and returned as a fresh copy. Corrupt evidence fails explicitly, not as a miss.
Cache checks, hashing and local work share a finite deadline. Provider work is
absent. Artifact I/O runs in threads and may drain after cancellation; assignment
write intents still enforce deletion/fencing. This callable service is not yet
injected into upload routes. D3b2a now injects it into the callable Agent path;
D3b2b still must implement per-call reuse and current execution accounting.

### D3b2a Terminal Reuse Boundary

The Agent and standalone PDF/image readers and recheck accept an optional,
source-bound local reader. Index/detail/export/render/contact-sheet/image/recheck
paths use the same injected reader and remaining phase budget; the default path
is unchanged. Local reuse does not itself reuse a model response. Business
upload routes remain unchanged.

`services/recognition_results.py` provides terminal final-artifact lookup and
recording, not get-or-dispatch. Only a successful, exact request/purpose/route/
policy/prompt/source hit returns an invocation receipt. It never reruns a model,
re-enters recheck with a historical assembly, or treats a miss as permission to
submit. Failures, unknown submissions and low confidence remain stored without
becoming terminal hits. Source access is rechecked after receipt validation.

`invocation.py` is a separate new contract. Original V1 evidence models, their
serialized field sets, raw candidate tokens and artifact hashes are untouched.
A reused final result has zero current dispatch usage and the original usage
in a separate historical field, including unknown tokens. A newly recorded run
reports its actual original ledger. Artifact I/O timing is separate from model
workflow timing. These projections are not invoices, authorization or job leases.

The caller must record only its own fresh run; D3c must enforce that provenance
through its durable pre-submit/result checkpoint. This API alone does not prove
single submission, and a record/storage failure is not permission to repeat a
paid call. There is still no per-locator/read/patch cache hit in this item.
D3b2b needs a new versioned per-call ledger: adding default fields to V1 payloads
would change old hashes, and settling historical candidates in a new live budget
would misattribute their cost. All four cache-layer goals remain required.

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

## D2b Source-Bound Agent Rechecks

`RecognitionAgent.read()` still returns initial raw evidence only.
`RecognitionAgent.recognize()` now retains that exact initial snapshot, assembles
it, then uses the **same live budget** for optional rechecks. It never reconstructs
reservation authority from an artifact. The final assembly adds `repair_execution`:
selection/skip reasons, original-unit binding, prepared-image metadata and digest,
bounded raw responses, explicit decisions, stop reasons and final total usage.
The initial `raw.budget` remains initial-only; it is not overwritten or added to
the final totals a second time. No persistent cache or restart recovery is added.

The pure selector accepts only one-to-one whole-page evidence with a concrete
empty outcome or actionable transcription/packaging issue. Wrong mathematics,
unfinished student work and a generic low confidence label do not trigger calls.
Partial pages, overlapping unaligned regions, multi-page Markdown, refused or
truncated output and contexts exceeding 6,000 characters per candidate are skipped
explicitly, not truncated or assigned an invented region. Two empty recoveries
and four patches share six extras and the original total-call limit. One region
gets at most one extra. Whole-workflow initial errors or pending submissions halt
all rechecks, including otherwise eligible pages.

Rechecks use authorized original bytes, the same purpose and frozen route,
existing killable media tools and the original region. D2b changes the evidence
prompt, not image quality: PDF scale stays two and image transformations/pixel
digests must match the initial read. PDF dimensions use the same MuPDF geometric
rounding as rendering, including fractional sizes and rotated pages. No document
parsing happens in the small parent-side geometric calculation.

Only an explicit valid decision can change final text. Initial native/visual
candidates remain untouched; a replacement has `adopted_from=repair` and records
before/proposal/after. Even a successful replacement remains low-confidence and
unverified. Empty recovery can restore evidence-read coverage for that full page,
never prove target completeness or OCR accuracy. Failed/malformed/uncertain
responses retain before-text and stop further extras. Cancellation retains the
live reservation; durable cancellation recovery remains D3's responsibility.

Serialization re-derives selection, source/geometry bindings, decisions, coverage,
and usage from retained evidence. A final render exceeding 400,000 characters
returns an explicit assembly-limit result with all initial and paid repair records
preserved; it neither clips text nor loses the completed call. This bounded
transcription artifact is not a whole-book knowledge store. H/I still implement
lossless page-batched ingestion and retrieval. Full business wiring and real-model
accuracy/teacher-effort acceptance remain E-J, not established by fake-engine tests.

## D: Durable Run Boundary

`RecognitionRunService` is the internal assignment-bound entry point for the
versioned harness. It freezes the final input identity, claims one stable child
operation and checkpoints a content-free pending dispatch before calling the
selected engine. Artifact writes and checkpoints use the same lease fence.
`retry_existing=False` preserves the operation's attempt and budget on expiry;
the repository's legacy default is unchanged.

A restart reuses confirmed successful leaves and retains cumulative physical
call/token usage separately from the current invocation. Pending submissions,
including cancellation or a lost result write, require review and never replay.
Unknown token usage remains unknown. A recovered final artifact does not call a
model. Completed execution still requires existing teacher review; it is not an
OCR accuracy claim. Missing coverage or a stopped read returns `needs_review`.

Call, shared-extra and output budgets survive restart. The original wall-clock
deadline survives too; resumed per-phase limits conservatively include elapsed
operation time, so recovery cannot replenish a phase. An incompatible exact
request (including changed output bounds) does not bypass duplicate-region
protection. The service does not register a new public endpoint or start work on
polling. Business adapters are implemented in the following stages.

Validation: seven durable integration/ledger cases and 26 existing V2 workflow
reuse cases pass together (33); the earlier budget run passed 76 budget cases.
These are synthetic local SQLite tests, not provider or deployment acceptance.

## E: Question Sources

Formal source preflight and mounted compatibility question imports use
`services.question_sources`. Visual inputs keep their assignment-bound original,
frozen owner route and durable evidence; plain text makes no OCR call. Library
inputs use an authorized original-file copy, not lossy knowledge chunks. Explicit
pages and target IDs bound recognition, including hierarchical textbook IDs.
Baidu batches one page so unaligned multi-page Markdown cannot invent page
provenance. Existing BMP/TIFF uploads use the same killable single-frame worker.

Recognition and structured question parsing are separate. Literal criteria may
be preserved, but parsing does not invent solutions or scores. Coverage gaps and
low-confidence evidence feed the existing final teacher review without changing
the major-question scoring contract or adding an intermediate confirmation.
Preflight remains a bounded synchronous request; it is not a background whole-book
ingestion job. Knowledge coverage and student-source lifecycle follow in H and G.

## F: Teacher Materials

Reference answers, rubrics and test-case sources now use the same assignment
adapter, including library originals. Their `purpose` remains distinct; rubric
and test-case vision is off by default in the API, UI and adapter. Native PDF
text still works without opting into vision. Recognition never runs test code.
OCR-only material matching requires an explicitly selected text parser; an
existing LLM route freezes that same provider/configuration through recovery.
No companion model is selected silently.

Material imports remain candidates until explicit apply. Recognition provenance
and uncertainty survive parsing; unsafe test candidates (low recognition,
low matching confidence or ambiguous question identity) cannot be applied.
Rejection preserves the plan and existing confirmed fields. The old auxiliary
upload endpoints now start the same candidate workflow instead of directly
overwriting questions. Uploaded references remain separate from generated ones.
The OCR-only initial question path preserves exactly headed rubrics/references
and flags test material requiring a text parser, without executing or inventing it.

The material-review original viewer reuses the existing bounded PDF/image UI.
Its source endpoint checks owner, task, operation, file availability, exact size
and digest and returns private no-store content. No OCR runs on preview.

Validation is synthetic: 73/78 impacted cases initially passed; five new-test
assertions/fixtures were corrected, then all nine new cases passed (one later
preview fixture correction included). Existing AddProblemsPage: nine passed;
TypeScript typecheck passed. New material-preview visual checks remain part of J.

## G: Student Submissions

Task batch ingestion and authenticated student/teacher uploads now share the
recognition adapter with purpose `submissions`. The selected LLM remains the
parser; OCR-only batch input retains its deterministic parser with no hidden
companion. Transcription preserves mistakes, crossed-out work and unfinished
code, while identity and question mapping remain separate review concerns.
Recognition uncertainty becomes answer flags, not a student-error judgment;
the existing final review clears the grading gate. No intermediate review step
was added, and the order-independent question mapping contract is unchanged.

Per-source parser results and pending submissions are durable. Exact-input
retry retains original files and reuses saved parses; cancellation or a lost
response cannot silently cause another paid submit. Configuration and question
versions are frozen. Original-source lineage remains distinct from retry IDs.

Authenticated single-student uploads stage an unpublished revision, save the
original before recognition, and publish answers atomically only on success.
The authenticated student ID wins over model-extracted identity. Failed uploads
and concurrent manual corrections retain the previous current revision. A
single upload is bounded to 64 MiB, 24 members, 200,000 transcript characters and
900 seconds; exceeded bounds fail explicitly, never truncate.

Revision evidence now uses task-lifecycle write intents and parent deletion
fences. Migration `0017_revision_recognition` extends the reservation constraint;
downgrade refuses to discard outstanding revision artifact intents. Teachers
can preview student-owned current originals through task authorization, with
file selection for multi-file uploads. Replaced revisions and other teachers
cannot use that current-original route. Preview is read-only and does no OCR.

The new tests use deterministic providers and injected storage/lease failures.
They prove persistence, isolation, recovery and review contracts, not measured
handwriting accuracy. Real-provider ablation and combined OCR/RAG acceptance
remain in J; the knowledge pipeline is the separate H/I stage.

## H: Whole-Book Knowledge Ingestion

Original publication atomically queues a separate knowledge ingestion version.
The personal, course-library and task upload routes return saved/processing
state; only small native documents finish inline. A durable background worker
processes at most 24 pages per claim. Each PDF/image page uses the same frozen
engine, codec and before-submit checkpoint as target recognition, through
knowledge-specific operation and artifact repositories. No assignment is faked.

`knowledge-economy-v1` preserves clean native text, including explicitly
unverified native math, and recognizes missing/risky visual content. Each page
has at most one initial and one extra call; extras are limited to two per fixed
24-page range and ceil(2% of pages) per requested ingestion. Background visual
calls share the owner limit of two and yield to waiting interactive work.
The original is bounded to 64 MiB, not the former 5 MiB limit. A PDF book is not
limited to target recognition's 24-page/900-second whole-request envelope.

Page states distinguish unprocessed, processing, searchable, warning, failed
and observed blank. Coverage is paginated. Pause/resume and explicit gap retry
do not replay ambiguous submits; already successful pages can be copied with
their original evidence. Partial content is labeled partial, and failed
reprocessing preserves the previously published version. Text chunks preserve
all source characters and offsets; neither 500 chunks nor a long Chinese/code
token silently cuts off the rest of a book.

Migration `0018_knowledge_ingestion` retains versioned chunks, page manifests
and bounded compressed recognition evidence in the database. These derived
rows use the original's owner/deletion gate and FK cascade, avoiding external
late-write orphan objects. Per-document safety guards are 128 MiB compressed
evidence and 32 Mi characters of retained chunk text (overlap included); a
reached guard is explicit failure, never successful truncation. The storage
API reports derived evidence bytes and indexed characters separately from the
existing raw-object quota. Text/Office parsing runs in a killable child; Office
embedded images or unhandled supplementary content keep coverage incomplete.

Five selected books are supported; the attachment request guard is now twenty.
UI coverage, pause/resume and gap retry reuse current owner credentials, with
no new provider setup screens. OCR precision, 1000/2500-page retrieval recall,
frozen grading citations and end-to-end preview UX remain I/J acceptance work.

## I: Versioned Local Retrieval and Citations

Knowledge retrieval reuses the existing `rank-bm25` dependency. The local index
adds Chinese bigrams, hierarchical exercise IDs and signed exponent tokens,
suppresses duplicate passages and adds source-contiguous neighboring spans
when the result budget allows. Native/OCR source text is never normalized in
place. No embedding service, OCR request or model-assisted retrieval is used.

Grading setup freezes the document IDs, published content version, readable
chunk prefix, source hash and index version in the input fingerprint. Later
page completion or replacement cannot change that grading run. Old versions
remain until document cleanup; owner/source availability is rechecked before
and after cache access. Selected documents with no readable content block
grading rather than disappearing silently from its evidence.

The single-flight LRU keeps up to 16 indexes / approximately 96 MiB with a
15-minute absolute TTL. An individual selection above 100,000 chunks or 16 Mi
characters fails explicitly; this is a local resource guard, not truncation.
Only one index builds at a time. Cache accounting estimates retained Python
objects; it is not a process RSS guarantee. Raw originals and all content
versions continue using the ingestion/storage boundaries described above.

The library and task setup expose content search. Retrieved references are
system-supplied, not model-certified claim support. Existing concept,
objective and proof grading carry those references into saved expert results;
teacher review can inspect the frozen passage and original page. PDF preview
mounts at most three canvases and can reach every page, including page 1000.
Calculation/programming prompt expansion is held for explicit data-egress
confirmation; it is not part of this stage's quality claim.

Synthetic capacity validation: five books, 2500 pages (largest 1000), 2,981,961
characters; 15 head/middle/tail exact-ID queries repeated twice, Recall@5 1.0,
one index build, approximately 10.08 MB retained index. On the local SQLite
validation environment: cold 0.829 s, hot p50 0.0037 s / p95 0.0046 s. These are
synthetic exact-match and cache results, not OCR accuracy, paraphrase recall,
production latency, whole-book ingestion time or measured teacher workload.

Tests cover version freezing, incremental prefix stability, cache ownership
and revocation, same-page adjacency, empty search, authenticated citation
reads, 2500-page capacity and long-PDF navigation. Real-provider ablation,
semantic holdout evaluation and combined end-to-end checks remain J work.
