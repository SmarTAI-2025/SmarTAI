# Optional proof audit: implementation and paired live check

Scope: PR255 only, based on `2ff684fc6e8d9918eb18ffca18faaa858f8e6ea6`.
No frontend/public DTO, grading limit, timeout or concurrency setting changed.
No subagents, PR257 integration, merge or deployment in this increment.

## Implementation contract

- The existing scoring JSON completes first. A separate `PROOF_AUDIT` JSON
  appendix records key conclusions, rules, necessary conditions and their source
  or predecessor IDs. It does not add scoring items or award/deduct points.
- Audit parsing happens after strict scoring validation and outside its repair
  boundary. Missing, malformed, truncated, oversized or invalid audit records
  cannot invalidate a valid detailed grade or request a format repair.
- Source lines without audit mapping remain unconfirmed diagnostics. They do
  not establish a student omission. Unknown references and duplicate records
  are ignored locally; a duplicate cannot clear an earlier concrete concern.
- A specific unresolved mathematical concern needs an actual claim plus a
  rubric source or scoring-item link. Only then can it use the existing shared
  review opportunity. Tools, proof concerns and experts share that opportunity;
  no new review loop is added. An exhausted budget preserves the grade and an
  optional suggestion through the existing explanation field.
- Missing/broken notes in a review preserve the valid reviewed score and any
  previously unresolved concern. Teacher waivers and already-accounted issues
  do not trigger another review. Ordinary true conclusions are not confused
  with valid derivations.
- The common system-prompt builder accepts an internal output instruction with
  its original default unchanged. Proof grading/review explicitly permit the
  appendix; otherwise its old single-JSON requirement suppresses the notes.
  Calculation, concept, programming and direct fallback retain their output
  formats.

This validates references and explicit consistency, not mathematical entailment
or completeness of every inference. A valid source ID does not prove a theorem
condition, and absence of a conflict does not prove the entire answer correct.

## Offline verification

- Full backend: **5824 passed, 76 skipped**, 510.75 s. Skips retain their existing
  environment requirements. Deprecation warnings are not counted as failures.
- Relevant grading regression: **1127 passed**; the final full backend run also
  covers the subsequent system-prompt adjustment.
- Independent capability/boundary acceptance: **99 passed**; disposable-storage
  safety acceptance: **18 passed**.
- New tests include malformed/missing/truncated notes, uncovered source lines,
  invalid/duplicate references, teacher waivers, a true conclusion with invalid
  reasoning, preserved core validation, missing raw response text, and one
  combined tool/proof or multi-expert review. They use fixed provider responses;
  these counts are not evidence of live model accuracy.

Initial broader tests exposed adapters without raw response text; optional
reading was corrected without weakening the original score assertions. The
first live preflight exposed the conflicting single-JSON system instruction;
its results are retained separately and excluded from the final comparison.

## Frozen live comparison

One retained Gemini `gemini-3.5-flash-lite` configuration, identical questions,
student text, scoring plans and grading settings. Configured RPM: 10; effective
provider concurrency: 20; response budget: 300 s. Settings were not changed.
The real `grade_batch`/expert/skill/parser/scorer path ran locally. The GUI/OCR
was not rerun: original questions and the explicit teacher rubric came from the
previous GUI-created frozen run. No production records were written.

- Two previously authorized seven-question submissions: 14 results. Reference
  ranges come from the prior independent reading, **not per-item teacher gold**.
- Twelve synthetic answers across continuity/differentiability, L'Hopital's
  conditions, a rubric-exempt missing branch, alternative proofs, a correct
  conclusion with invalid reasoning, and reversing a theorem. Answers and score
  expectations were frozen before requests and never edited to fit results.
- Plans were prepared once and reused verbatim by both versions. Five original
  questions used deductions, two used holistic plans. The six synthetic questions
  used source-preserving holistic plans because the unchanged compiler declined
  their follow-through wording. Holistic plans still produced detailed claims;
  they are not direct-grading fallbacks.
- Order: baseline submission 1, final candidate submission 1, final candidate
  submission 2, baseline submission 2; then candidate/baseline synthetic batches.
  Initial unsuccessful candidate preflight is excluded, not overwritten.

| Observation | Baseline | Candidate |
| --- | ---: | ---: |
| Completed detailed grades | 26/26 | 26/26 |
| Direct grading fallbacks | 0 | 0 |
| New mandatory handling from audit | 0 | 0 |
| Logical score-format repairs | 2/26 | 2/26 |
| Actual grading attempts, including failures/retries | 50 | 49 |
| Failed attempts | 22 | 21 |
| Valid audit notes in eligible proof grades | Not present | 22/24 |
| Valid audit step records | Not present | 33 |
| Original grades inside frozen reference ranges | 9/14 | 9/14 |
| Synthetic scores equal frozen expected scores | 7/12 | 7/12 |
| Original mean absolute distance outside reference range | 0.786 | 0.857 |
| Synthetic mean absolute score error | 2.833 | 3.167 |
| Median complete item time, all 26 | 93.90 s | 62.61 s |
| Observed item p95, nearest rank, all 26 | 166.93 s | 132.18 s |
| Sum of two seven-question batch times | 166.20 s | 158.33 s |
| Twelve-answer synthetic batch time | 170.02 s | 137.83 s |
| Successful-response input tokens | 154,825 | 166,295 |
| Successful-response output tokens | 18,482 | 23,394 |

Both score repairs concerned the existing score/claim schema or error-origin
validation, not the appendix. One candidate repair request also failed at the
network layer, so actual repair attempts were 3 versus 2. Two candidate proof
grades had unusable notes; their detailed grades remained intact. The live
sample did not raise concrete audit review concerns or schedule CAS checks;
combined review-budget behavior is covered by the explicit offline tests, not
claimed as exercised by this live batch.

### Interpretation and limits

This run retained detailed grading and added inspectable proof records without
increasing logical format repairs or introducing mandatory review. It **does not
show a score-accuracy improvement**. Some holistic grades substantially ignored
the teacher's explicit per-error deduction amount despite receiving the complete
rubric. Original-work changes include one score improving from 9 to 10 and another
falling from 10 to 8. Mean absolute score errors worsened in this small sample.
These model judgments are not hard-coded away or converted into fabricated tool
evidence. Existing compiler limitations and model adherence remain separate work.

Observed latency did not exceed the proposed +15% median target, but this is not
a causal speed improvement or a guarantee: baseline had 21 `ConnectError` and
one `GoogleAPIError`; candidate had 21 `ConnectError`. These were local-proxy
connection failures, not recorded response timeouts. Their timing and the RPM
queue strongly affect item latency. Output tokens increased 26.6%, input tokens
7.4%; a healthy high-throughput provider may expose more of that added generation
time. The small-sample p95 is descriptive. No inference about relay Flash/Pro
timeout behavior follows from this official-Gemini comparison, and raising a
response timeout is not a demonstrated remedy for TLS connection failures.

The implementation's isolation contract passes. Overall mathematical accuracy,
complete proof verification and a universal <=15% latency overhead are **not**
declared accepted.

## Reproducibility and privacy

- Frozen input SHA256:
  `8400ffc24713cfa6493ab30dc01f8a85732ab955780e17be9726589286ff31fb`
- Shared plan SHA256:
  `01529b867d5671c75451829c0c9636a4d298f17643022ffa0075b8e880781281`
- Candidate product-file SHA256 (proof prompt, base skill, math skill, audit
  service concatenated in that order):
  `81240008b6d78cf304c4c30b6e021c3abceddbba80408475e7c5979ba9023440`
- Private ignored evidence lives under
  `data/local-product-qa-20261022/evidence/proof-audit-20261024-*`.
  It includes per-request timing, raw responses, score traces and final metrics.
  Student text, credentials and private response payloads are excluded from Git.
- The grading comparison counts above are exact. Initial plan preparation had
  incomplete instrumentation; the local campaign conservatively reserved 20
  additional attempts instead of claiming an exact total. There were 126 tracked
  campaign attempts, plus that reservation, within a 160-attempt/four-hour guard.
  This guard belongs only to the ignored evaluation script; product limits remain
  unchanged. Retained BYOK configurations were not deleted or edited.
