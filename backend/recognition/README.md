# Recognition Foundation (Work Item A)

This package is not wired to an upload endpoint yet. It defines the first
version of the evidence and planning contracts; it does not establish an OCR
accuracy improvement. No new dependencies, migrations, settings, provider
calls, credential probes or billing services are introduced.

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
| A | Evidence, capabilities, policy, pure planner and engine port (this PR) |
| B | Killable PDF indexing, bounded detail and targeted rendering |
| C | Faithful reader and controlled existing-engine execution |
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
