# B2 independent acceptance: not passed

2026-10-23 Asia/Singapore. CORE integration candidate:
`d51a8474c275115abb46ac2961f0954d0470ed2f`.
Candidate source checkout: `/private/tmp/smartai-math-evidence-20261023`.
Independent checkout: `/Users/annie/.codex/worktrees/math-reliability-b-20261023/SmarTAI`;
branch `codex/math-reliability-b2-20261023`.

Production files are unchanged from that exact candidate. Only B-owned evaluation
code/tests and reports change. No subagents, real providers, credentials, live DB,
production access, push, PR, merge or deployment was used.

## Gates and evidence

| Gate | Observed result | Interpretation |
|---|---|---|
| Independent raw JSON cases | 39/42 contracts pass; 40/42 useful scores; 33/42 detailed; 29/42 item ledgers; 30/42 detailed plus contract pass | Scored/structured/valid-program-contract remain different facts |
| Source/parser/version/recovery probes | 26/26 pass | Includes real source preservation, conservative matching and v1 retirement |
| Explicit capability pytest | **65 passed, 3 failed**, 23.43s | No skip/xfail; final acceptance remains red |
| Persisted run/recovery and safety gate | **18 passed**, 6.05s, 5 existing SWIG warnings | Disposable SQLite, real worker cancellation and fault propagation; not distributed/production proof |
| Harness, existing candidate tests and new adapter controls | **40 passed**, 14.32s | Includes 35 existing tests and 5 compatibility/positive controls; not a capability substitute |
| Historical T4 replay | 40/40 included; 36 useful scores; 38/40 availability expectations met | K05/K06 are expected model failures; C08/C09 are unexpected plan regressions |
| Checks-empty opportunities | **6/6 actually executed** | Initial model checks stay empty; numeric/symbolic/units/pi/e/i opportunities go through real tools |
| External model requests/cost | 0 / 0 | Scripted raw JSON, real local math worker |

Raw results: [cases and probes](reliability-b2-d51a8474.json),
[readable cases](reliability-b2-d51a8474.md),
[safety](reliability-b2-safety-d51a8474.json),
[readable safety](reliability-b2-safety-d51a8474.md),
[older T4 replay](candidate-b2-d51a8474.json).
Original B1/preparation reports remain untouched.

## Failures retained for CORE

### Explicit no-repeat policy is rejected for ordinary wording

R13/R14 teacher source is unchanged: `每个错误扣1分；同源后续错误不重复扣分。`.
The v2 wire includes original rule `source_quote`, unlimited repetition,
`error_policy=no_repeat_deduction`, and the exact policy source. The declared
first error is explicitly refuted when referenced by `caused_by`.

Both plans return `compilation_status=holistic`,
`compilation_reason=unsupported_error_policy`. The independent intended results
remain R13=9 (pure consequence does not deduct twice) and R14=8 (new independent
error does deduct). Actual synthetic results are both 4 from the fixed fallback
fixture, with no required deductions plan. **4 is not a measured model score.**

The policy recognizer in `backend/services/math_rubric_plan.py` (`_NO_REPEAT`,
`_source_policies`) accepts a narrow set of prefixes and then rejects other
phrases containing `不重复扣`/`不再扣`. Separate positive controls changing only
the policy wording to `同源错误不重复扣分` produce the correct 9/8 deductions.
Those controls do not replace R13/R14 or change their original teacher text.

Historical T4 C08/C09 independently expose the same boundary:

- C08: `满分10；首次数值错误扣1分；正确承接错误值不再扣；结论不另扣。`
- C09: `满分10；每个独立数值错误扣1分；正确承接不再扣；结论不另扣。`

They previously had explicit deductions plans; now both report
`unsupported_error_policy`. The historical adapter deliberately refuses an
unexpected direct fallback, so its two null scores demonstrate replay/plan
failure, **not proof that a real model cannot provide a fallback grade**.

### Unknown function request never reaches the worker

R29's original quote and request are `f(1)`. It still receives a detailed score
10, zero feedback requests, and no false refutation. However, the planned check
is rejected as `student_expression_not_bound`, with no evidence and
`tool_applicability=not_applicable`.

`math_tool_planning.expression` only accepts calls in the verifier's supported
function list, so unknown functions fail binding before CAS. The independent
original gate requires actual `unknown` tool evidence and remains red. A separate
real-worker control confirms that the verifier itself returns `unknown` for this
request. This is a coverage/diagnostic contract gap, not a demonstrated wrong
deduction or reason to allow unsupported function evaluation.

## Adapter changes and preserved expectations

- The 42-case B1 corpus and 40-case T4 corpus are unchanged, including original
  teacher/student text, adversarial wrong amounts/quotes, score expectations and
  empty initial checks. Historical report hashes remain unchanged.
- `reliability_v2.py` supplies literal source_quote and new internal policy,
  omission and evidence-judgment fields. It receives only wire replies, never
  acceptance oracles. It does not repair adversarial quotations or infer scores.
- Feedback uses supplied evidence IDs. It reports applies only for an already
  authored matching verdict, otherwise uncertain. It never promotes a tool
  result to a whole-answer judgment or changes the score to match the oracle.
- Local source repair uses the real shared parser/domain repair call. R37 keeps
  the valid first claim and repairs only the invalid original quote once. R27
  now retains the originally declared missing obligation as an omission claim.
- Source diagnostics distinguish raw wire, first parsed draft and final claims.
  A first draft can contain validation errors; missing drafts are not fabricated
  from the final result. Empty omission quotes do not count as student matches.
- Reporter faults still must interrupt the operation with no replacement result.
  The v2 type is `CheckpointWriteError`, with the original `OSError` as cause;
  all previously failing reporter boundaries now pass that same semantic gate.
- The privacy fixture keeps its unchanged synthetic answer and canary. Numeric
  source and canary are separate actual claims for v2 binding; the canary still
  exists in private traces and must remain absent from public events.
- Older candidate replay remains a secondary test with mocked structured model
  boundaries. Its new validation callback is honored. The primary 42-case gate
  uses actual `provider.ainvoke` JSON, parser, compiler, source checks, tools,
  feedback, ledger and public DTO projection.

## Reproduction and limits

From the independent checkout, with disposable imports:

```sh
PYTHONDONTWRITEBYTECODE=1 /opt/anaconda3/envs/smartai/bin/python \
  -m tools.evaluation.math_grading.reliability \
  --output /private/tmp/b2-recheck.json --assert-contract
PYTHONDONTWRITEBYTECODE=1 /opt/anaconda3/envs/smartai/bin/python \
  -m tools.evaluation.math_grading.reliability_safety \
  --output /private/tmp/b2-safety-recheck.json
PYTHONDONTWRITEBYTECODE=1 /opt/anaconda3/envs/smartai/bin/python \
  -m tools.evaluation.math_grading.run --mode candidate-replay \
  --adapter tools.evaluation.math_grading.candidate:evaluate \
  --output /private/tmp/b2-t4-recheck.json
```

Use `RELIABILITY.md`'s isolated pytest wrapper for the explicit capability file
and the normal test directory **as separate invocations**. Pytest can suppress
the non-test_ file when it is supplied together with its parent directory.
Logs: `/private/tmp/math-b2-explicit-gate.log`,
`/private/tmp/math-b2-final-gates.log`, `/private/tmp/math-b2-safety.log`,
`/private/tmp/math-b2-candidate-replay.log`.

No synthetic pass rate is model accuracy or teacher-gold validation. General
proof completeness/theorem-condition coverage is not implemented in this
candidate. PostgreSQL/distributed restarts, real model behavior, frontend and
production were not tested here. No comparison or integration with PR252/254
is claimed; those belong to the coordinating task and require another exact SHA.

Next: CORE resolves or explicitly adjudicates the retained failures and publishes
another fixed candidate. B reruns the same semantic gate on that candidate;
this report does not authorize weakening its expectations.
