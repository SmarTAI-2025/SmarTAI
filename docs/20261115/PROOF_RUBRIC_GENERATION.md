# Concrete proof rubrics: generation and validation

AI-created proof rubrics now retain concrete mathematical point steps. The material generator creates them directly from the question and its reference argument. No postprocessor replaces them with universal absent/partial/local deductions. Explicit teacher scoring modes and values remain authoritative.

## Normal path and output contracts

1. `generate_missing_question_materials` sends the original stem, existing reference and teacher rules, known part identities/budgets, requested fields, and the complete `WrittenRubricDefinition` JSON Schema. `PART_INSTRUCTION` specifies reference-grounded steps, meaningful partial progress, domain/case coverage, and concise criteria. When both materials are requested, one response returns the reference first and its scoring steps second. Neither step count nor allocation is hardcoded.
2. The response is JSON: `candidates[].target_id` selects the requested material. A reference has `text_value`; a criterion has `rubric_definition.part_rubric.parts[].steps[]`, with `id`, `criterion`, and `total_pts`. The existing source binder owns question identity, validates part/step IDs and budgets, and preserves teacher rules. This is not free prose later reverse-engineered into a ledger.
3. For fresh AI-only proof standards, `prepare_proof_steps` rejects a deduction-only or duplicate-criterion proposal and makes **one** independent semantic audit. Its input includes the original question, each part's scope, the accepted sibling/existing reference, and the concrete proposed steps. Its JSON output is `{"parts":[{"part_id":"p1","verdict":"consistent|inconsistent|uncertain","reason":"specific mathematical finding"}]}`. Every supplied part must occur exactly once. The audit cannot return replacement rules or edit points.
4. Acceptance keeps the generated steps unchanged. The ordinary draft binder/sealer includes the accepted reference, persists the frozen plan and fingerprints, and renders the teacher view from that same ledger. The existing grader locates evidence for each step and the existing settlement code adds the frozen awards. Correct condensed work can support multiple distinct steps. Substantively different methods continue to use the independent alternative policy.

The normal AI proof-material path therefore uses **one generation plus one audit**, versus the former generation + source-goal planner + source-goal audit. Ordinary deterministic checks make no model requests. Other existing teacher-policy or response-type branches are outside this count.

## Failure behavior and scope

- Format/types, known identities, exact budget totals and teacher allocations are checked programmatically. Mathematical validity, useful granularity, omissions, overlapping awards and inappropriate method restrictions require semantic review; these are not proved by a JSON Schema.
- An inconsistent or uncertain audit rejects only the unverified rubric, with its specific reason sent to the existing bounded material repair loop. Accepted sibling fields are retained. Repair asks only for outstanding targets; it is not a mandatory second generation. Independent audit calls retain their existing bounded transport/format repair budget.
- No reference is made globally mandatory. A criterion-only request can use an existing reference or derive an argument from the source. Teacher-authored rubrics continue to save under the existing teacher-rule workflow.
- No scoring engine, alternative-policy arithmetic, teacher-edit parser or frontend layout change is needed. Existing deduction ledgers still have settlement coverage, but their retired automatic generator is removed. Tests for its obsolete quote-to-tier compiler are replaced by direct step-generation/audit contracts and explicit frozen deduction fixtures.
- A step that needs ordinary partial credit must be split into independently assessable funded items. Saying “give partial credit” in prose does not create an executable award. Follow-through remains the distinct existing policy for sound downstream work using an earlier wrong value.

## Validation boundaries

Offline tests cover generation-call counts, sibling/existing/absent references, audit rejection and repair feedback, unchanged frozen rules, serialization/preparation reads, rendering, concrete partial-score settlement and compressed proofs. They use fixed provider replies and do **not** measure mathematical judgment quality.

A single real Gemini 3.5 Flash Lite preparation of the reported algebra question was attempted through an authorized AWS test-account relay capped at six requests. Two upstream requests were sent; the run ended with `provider_daily_quota_exceeded` and no accepted complete result. The relay was stopped. Real-model generation quality remains **unverified**, with no student grading, production task mutation, merge or deployment performed by this change.
