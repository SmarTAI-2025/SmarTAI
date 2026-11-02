# B3 independent acceptance: offline contract passed

Evidence date: 2026-10-23 21:25 Asia/Singapore.
Exact candidate: **`e69aec05aa33351ffb248526919d6996abc2bf6a`**.
Source checkout: `/private/tmp/smartai-math-evidence-20261023`.
Independent checkout: `/Users/annie/.codex/worktrees/math-reliability-b-20261023/SmarTAI`.
Branch: `codex/math-reliability-b3-20261023`.

The candidate contains B2 as `6be8360c`, R29's tool-binding fix `cd62424a`,
the frontend-test-only synchronization `322e08f1`, and A's policy fix as
`e69aec05` (original A patch `331b282e`). B fetched the full fixed SHA only.
The separate PR252/254 integration SHA is outside this acceptance. PR253 is excluded.

## Gate results

| Gate | B3 result | Evidence boundary |
|---|---|---|
| Original raw JSON case contracts | **42/42 passed** | Same teacher/student text, amounts, hostile examples and score oracles |
| Original source/parser/version/recovery probes | **26/26 passed** | Includes v1 rejection and conservative source boundaries |
| Explicit capability pytest | **68 passed**, 21.86s, exit 0 | Same acceptance file; no skip/xfail or expectation change |
| Persisted recovery/safety pytest | **18 passed**, 5.82s, exit 0 | Same tests; 5 existing SWIG warnings |
| Harness/compatibility/positive controls | **40 passed**, 13.73s, exit 0 | Same tests as B2; separate from capability acceptance |
| Independent historical T4 replay | **40/40 availability expectations met; 38/38 expected score intervals met** | All 40 remain in the report; K05/K06 intentionally lack scores |

Machine and readable reports:
[raw cases/probes](reliability-b3-e69aec05.json),
[raw summary](reliability-b3-e69aec05.md),
[safety](reliability-b3-safety-e69aec05.json),
[safety summary](reliability-b3-safety-e69aec05.md),
[T4 replay](candidate-b3-e69aec05.json).

## Actual coverage, with explicit denominators

| Observation | Raw JSON corpus | Meaning |
|---|---|---|
| Useful score | **40/42 (95.2%)** | R30 provider failure and R31 cancellation deliberately have no score |
| At least one detailed assessment | **33/42 (78.6%)** | Does not itself establish mathematical correctness |
| Actual item ledger | **31/42 (73.8%)** | Awards or deductions retained in the real scoring path |
| Detailed plus the case's independent contract passed | **33/42 (78.6%)** | B2 was 30/42; all selected detailed cases now pass their program contract |
| Cases with actual tool execution | **24/42 (57.1%)** | Counted at real verify_math entry, not inferred from a prompt/claim |
| Actual tool invocations | **33** | 19 refuted, 13 supported, 1 unknown; no fake verifier result |
| Predeclared checks-empty opportunities actually executed | **6/6 (100%)** | Initial checks remain empty; numeric/symbolic/units/pi/e/i |
| Cases with a review provider entry | **23/42** | 23 entries total; scripted provider, no upstream calls |

The 42-case corpus deliberately includes malformed or adversarial structures.
Eight cases contain at least one direct fallback: R12, R21, R32–R36 and R38.
R21 has both a detailed and a direct expert, so those case counts overlap.
These expected rejection/fallback paths are not reclassified as detailed success.
Nine cases have explicit not_applicable; nine lack applicability instrumentation
because of failed/cancelled/direct paths. Missing diagnostics are not passes.

The secondary T4 replay has **38/40 useful scores, 37/40 detailed assessments,
19/40 item ledgers**. Its structured model boundary and two tool faults are mocked,
as before; do not add its evidence counts to the actual-worker coverage above.

## Original failed cases now pass unchanged

| Case | Original contract | B3 actual result |
|---|---|---|
| R13 follow-through | Same-origin consequence does not deduct twice; score 9 | deductions plan, score **9**, contract passes |
| R14 independent second error | New independent error still deducts; score 8 | deductions plan, score **8**, contract passes |
| R29 unknown function | Real unknown evidence; useful score 10; no feedback request | Actual worker returns **unknown / unsupported**, score **10**, zero feedback entries |
| T4 C08 | Correct continuation of wrong value does not add a deduction | Original rule retained; deductions plan, score **9** |
| T4 C09 | Independent later arithmetic error still deducts | Original rule retained; deductions plan, score **8** |

R13/R14 still use `每个错误扣1分；同源后续错误不重复扣分。`.
C08/C09 still use their original `正确承接错误值不再扣` / `正确承接不再扣`
and `结论不另扣` clauses. B did not replace them with the easier positive-control
wording. The original R29 request is still `f(1)`; unsupported mathematics remains
unknown, not supported or refuted merely because source binding succeeded.

## Independence and unchanged boundaries

- B made no production-code changes. The tested production tree equals the exact
  candidate. B3 only updates candidate/report labels and adds these reports/index.
- Both frozen corpora, all B test files, wire adaptation behavior, scoring
  expectations, fault injections, candidate adapter and prior reports are
  unchanged from B2. Report provenance records source hashes.
- Main replay uses raw provider.ainvoke JSON through real parsing, validation,
  shared source repair, compiler, math worker, feedback, ledger and DTO projection.
  Intermediate scoring/parsing is not mocked to make the main gate pass.
- Safety still requires typed reporter errors with original OSError causes,
  no replacement results after storage failures, durable valid-zero recovery,
  input/version/lease isolation, actual process cancellation/reap, and absence
  of private source canaries from public activity/progress.
- No subagents, external models, real credentials, live DB, production, GUI,
  push, PR, merge or deployment. Upstream model calls and billed cost are zero.

## Reproduction

Use the isolated CLI commands in `RELIABILITY.md`, changing output paths to new
files. The three B3 commands are:

```sh
PYTHONDONTWRITEBYTECODE=1 /opt/anaconda3/envs/smartai/bin/python \
  -m tools.evaluation.math_grading.reliability \
  --output /private/tmp/b3-recheck.json --assert-contract
PYTHONDONTWRITEBYTECODE=1 /opt/anaconda3/envs/smartai/bin/python \
  -m tools.evaluation.math_grading.reliability_safety \
  --output /private/tmp/b3-safety-recheck.json
PYTHONDONTWRITEBYTECODE=1 /opt/anaconda3/envs/smartai/bin/python \
  -m tools.evaluation.math_grading.run --mode candidate-replay \
  --adapter tools.evaluation.math_grading.candidate:evaluate \
  --output /private/tmp/b3-t4-recheck.json
```

The explicit capability file was run separately through isolated_imports;
do not combine its non-test_ path with its parent test directory and assume it
was collected. Logs: `/private/tmp/math-b3-explicit-gate.log`,
`/private/tmp/math-b3-harness.log`, `/private/tmp/math-b3-safety.log`,
`/private/tmp/math-b3-raw.log`, `/private/tmp/math-b3-t4.log`.

**Passed scope: this fixed candidate's selected offline program contracts.**
Synthetic scores are not model accuracy, teacher-gold evaluation, general proof
completeness or theorem-condition verification. PostgreSQL/distributed restart,
frontend/GUI, production and main+PR252+254 integration were not tested here.
Real-model and combined-candidate acceptance remain with the coordinating task.
