# Independent reliability acceptance (B1 / B2 / B3 / B4)

Production baseline: `109acaec30db917b52c0ca827f15a4b5830647e8` (merged PR250/251).
This directory's older T4 corpus and reports remain historical evidence. These
new files do not change production code or reuse T4's mocked structured replies.


## B4 fixed-candidate recheck: passed

On `d023fc9012078496785a28f6adc2692f6110beca`, all 68 original contracts,
18 safety gates and 31 incremental cases pass. The two original cross-line
scope failures retain identical inputs and expectations. Three added controls
cover following-line negation, English hypotheses and independent formula lines.
See the [B4 recheck report](reports/B4-ACCEPTANCE-d023fc90.md).
This is selected offline-contract acceptance, not live model or production quality.

## Historical B4 on abf6: incremental acceptance failed

On `abf6da1f24a560e3f160e01cd33b45889e535b09`, the original 68 contracts and
18 safety gates pass; 26 of 28 new boundaries pass. Two new failures retain
cross-line negation/hypothesis scope expectations. See the concise
[B4 report](reports/B4-ACCEPTANCE-abf6da1f.md) and its small evidence file.
No previous inputs/oracles or production code were changed.

## B3 fixed candidate: offline contract passed

The unchanged semantic gates passed on
`e69aec05aa33351ffb248526919d6996abc2bf6a`. See the
[B3 acceptance report](reports/B3-ACCEPTANCE-e69aec05.md).

- 42/42 raw JSON case contracts and 26/26 boundary probes passed.
- 18/18 persisted recovery/safety probes and 40/40 harness/regression tests passed.
- All 40 historical T4 cases are included; 38 eligible score intervals passed,
  with only the two intentionally injected model failures lacking scores.
- Actual raw replay coverage: scores 40/42, detailed 33/42, ledgers 31/42;
  24/42 cases made 33 real tool calls (19 refuted, 13 supported, 1 unknown).
  All six checks-empty opportunities actually ran tools.

R13/R14 now keep the original no-repeat wording and score 9/8; C08/C09 also
score 9/8 under their original rules. R29 reaches the real worker, returns
unknown, retains score 10 and adds no feedback request. B changed only report
labels/candidate metadata, with no fixture, oracle, parser, scoring or test change.
These are synthetic program-contract passes, not real model quality or general
proof-completeness validation. PR252/254 integration is a separate candidate.
Historical B1/B2 evidence remains intact below.

## Historical B2 fixed candidate: acceptance failed

CORE marked `integration_ready` at
`d51a8474c275115abb46ac2961f0954d0470ed2f`. See the
[B2 acceptance findings](reports/B2-ACCEPTANCE-d51a8474.md),
[raw JSON report](reports/reliability-b2-d51a8474.md), and
[safety report](reports/reliability-b2-safety-d51a8474.md).

- Independent explicit gate: **65 passed / 3 failed** (42 authored cases plus
  26 boundary probes). R13/R14 lose the deductions plan for an explicit
  no-repeat policy; R29 is rejected before CAS instead of producing unknown
  evidence. These original expectations remain red.
- Persistence/cancellation/storage/reporter/public-event gate: **18 passed**.
- Harness and existing candidate regressions plus compatibility controls:
  **40 passed**. This suite alone never establishes capability acceptance.
- Older T4 replay: 40/40 included, 36 useful scores; K05/K06 are intended
  failures, while C08/C09 unexpectedly lose their explicit deductions plan.
  The older adapter intentionally refuses to invent a happy fallback for those
  cases. Do not interpret its null outcomes as proof production cannot return
  a direct grade.

`reliability_v2.py` translates only internal wire fields. Frozen B1 corpus,
oracles, teacher/student text and all historical reports remain byte-identical.
The adapter supplies original `source_quote`, explicit unlimited repetition for
per-error rules, original error policy, declared error-origin verdict, omission
claims, and actual evidence IDs. Unconfirmed applicability stays uncertain;
no score is derived from the oracle or tool. Source repair uses the real shared
format/domain repair call. Reporter faults require `CheckpointWriteError` with
the original `OSError` cause and no fallback result.

For privacy/recovery fixtures, the unchanged synthetic answer's numeric span
and private canary are separate source claims, so v2 can bind the numeric check
without dropping the canary from private trace redaction tests. Source metrics
separate statements from omissions and never use final claims to invent a
missing initial draft. First parsed drafts may still contain validation errors.

Run the explicit gate as its own pytest target (not alongside its parent test
directory, which can suppress this non-test_ file during collection). Existing
reproduction commands below use disposable configuration and no network.
Historical B1 and preparation sections below describe their original runs;
the linked B2 report is the current fixed-candidate result.

## What the raw JSON run proves

`reliability.py` supplies **raw JSON strings through provider.ainvoke**. The real
`grade_batch`, rubric compiler, JSON extraction/format handling/repair, assessment
validation, math subprocess, feedback, ledger, expert aggregation and public DTO
projection execute. Knowledge retrieval returns an empty corpus; socket network
entry points are denied. Compiler/verifier wrappers only record and await the
original implementation. There is no live-provider option.

R01 is a complete positive path: a scripted incorrect 41 receives a real numeric
refutation against 42, the scripted feedback retracts credit, the real ledger
returns 0 and the public steps add to 0. This proves program wiring, **not that a
model independently notices the mistake**. R02 exposes the original TeX source
being modified by the parser and losing detailed grading.

`reliability_cases.py` has 42 hand-authored cases and separate `wire` / `oracle`
sections. The provider receives only `wire`. Some responses are intentionally
false, inconsistent or malformed. Score oracles are not read to manufacture a
passing provider response. A fallback returns a fixed synthetic 4 (or the explicit
mixed-expert 2); that never passes a normal structured/ledger gate. Chinese/Arabic
amounts, error continuation, independent errors, alternative proof, missing
obligations, contradictions, result/process distinctions, physics, constants
pi/e/i, invented conditions/tolerance, review failure and budgets are represented.

Six supported tool opportunities have **checks=[]**. They test applicability
scheduling separately from the deliberately prefilled checks in tool execution
tests. Abstract group proof has its own explicit not-applicable gate. Unknown
tools are distinct from not-applicable and from zero tools without a decision.

`reliability_probes.py` adds 26 probes: real JSON source fields (3), the agreed
source locator API (14), retirement of old versions/plans/traces (5), and isolated
in-process cancellation/retry/wrong-engine guards (4). The absent matcher makes
all 14 API probes fail on baseline; it does **not** mean the baseline accepts all
malicious quotes. R32–R36 separately demonstrate current rejection of changed
exponents, quantifiers, conditions, compressed derivations and merged digits.
Likewise version-retirement failures are planned integration gates, not evidence
that a v1 engine was previously wrong to read its own v1 data.

## Baseline evidence and interpretation

- [Machine report](reports/reliability-baseline-109acaec.json) and
  [case report](reports/reliability-baseline-109acaec.md): 42/42 retained in the
  denominator, including one provider failure and one cancellation. 40 have a
  score, 33 retain at least one detailed assessment, 26 retain an item ledger;
  17 cases pass their selected contract assertions. Only 10 both retain a
  detailed assessment and pass the case contract. Retention is not valid reasoning.
- Supported checks-empty opportunities execute 0/6. Refuted-feedback parse
  failure retains a score but exposes no specific conflict advice. Multi-expert
  review executes three post-assessment requests against a one-pass policy.
- 26 boundary probes: 5 pass / 21 fail. The explicit pytest gate totals
  **22 passed / 46 failed** (68), with no xfail or skip.
- Harness integrity suite: **8 passed**. This is a harness result, not capability
  acceptance. All runtime calls are synthetic; upstream calls and billed cost are 0.
- [Prior real observations](reports/reliability-real-observations-20261023.json)
  are anonymous aggregates transcribed from a protected read-only audit, with
  source hashes. Four repeated rounds over the same 14 answers yielded 56 scores,
  19 detailed results, 37 direct fallbacks, 5 deduction ledgers, and zero tool
  evidence. They are not 56 independent answers or a teacher accuracy benchmark.

The report separately records initial raw-wire quote containment, the first
retained parsed draft (or final draft when no original is retained), and final
source containment. Literal containment is diagnostic, not mathematical truth.
Missing applicability/review diagnostics are null, not guessed successes.
Format-repair entries are counted in addition to feedback/review entries. Case
wall time includes compilation, tools, feedback, retry delays and failures.
The main summary covers the 42 chain cases; extra boundary probe calls are
reported in their own section and are not included in that provider-entry count.

## Reproduce without credentials or live storage

Run from the repository/worktree root using the project Python. The runner
initializes test configuration in a fresh empty temporary directory before any
backend settings import; it does not load repository `.env` or real databases.
Use a new output filename; keep the committed baseline immutable.

```sh
PYTHONDONTWRITEBYTECODE=1 /opt/anaconda3/envs/smartai/bin/python \
  -m tools.evaluation.math_grading.reliability \
  --output /private/tmp/math-reliability-replay.json --assert-contract
```

Expected baseline exit is 1. Omit `--assert-contract` only for measurement; that
exit 0 means the report was generated, not that capability passed.

Run the green harness integrity tests with isolated configuration:

```sh
PYTHONDONTWRITEBYTECODE=1 /opt/anaconda3/envs/smartai/bin/python -c '
import os, sys, pytest
sys.path.insert(0, os.getcwd())
from tools.evaluation.math_grading.run import isolated_imports
with isolated_imports():
    raise SystemExit(pytest.main(["-q", "-p", "no:cacheprovider",
        "backend/tests/math_grading_evaluation/test_reliability.py"]))'
```

For the intentionally red capability gate, replace the final filename with
`backend/tests/math_grading_evaluation/acceptance_reliability.py`. Its name keeps
known-red B1 gates outside default `test_*.py` discovery; run it **explicitly** for
acceptance. It is not a skipped test or an acceptance waiver.

## Historical B1 handoff: B2 was pending

At B1 delivery CORE is `source_interface_ready`, not `integration_ready`. Do not
run an uncommitted moving core tree and report final acceptance. Await its exact
integration commit/path plus A's schema. Current wire envelopes target v1. R20
(exemption), R27 (missing obligation), R37 (source correction) and R41 (separate
source field) intentionally expose missing interfaces; their finalized envelope
names must be adapted at B2 without changing the semantic expectation or score.
Prompt routing and diagnostic observation may likewise need an explicit adapter.
An unrecognized prompt is a harness failure, never silently a capability pass.

B2 must verify one bounded local source correction preserves valid claims,
exemption and error policy execution, sourced conditions, meaningful applicability
decisions, mixed-path review, shared budget, failed-review advice and new version
recovery. The four current recovery probes are in-process only: persisted run/
checkpoint recovery, actual cancellation during a worker, reporter/storage
fault propagation and public event redaction were **unverified in B1**. Their
production integration tests must be inspected or supplemented in B2. No real
model quality claim can be restored by these synthetic tests; paid retesting and
the remaining global 6-call budget belong solely to the coordinating task.

## Historical B2 preparation on the baseline

The subsequent authorized preparation adds
`backend/tests/math_grading_evaluation/acceptance_reliability_safety.py` and the
isolated `reliability_safety` runner. It still executes baseline production code,
not a moving CORE tree or a final integrated candidate. Original B1 fixtures,
semantic expectations and baseline reports remain unchanged.

```sh
PYTHONDONTWRITEBYTECODE=1 /opt/anaconda3/envs/smartai/bin/python \
  -m tools.evaluation.math_grading.reliability_safety \
  --output /private/tmp/math-reliability-safety-replay.json
```

The runner initializes a fresh SQLite database and storage before backend import.
The explicit test file refuses collection without that disposable configuration,
before any autouse database reset can run. Synthetic users/questions/provider
configuration are created only there. The fake key is not a real credential;
registry selection returns the raw JSON fixture provider, never a network SDK.
All selected tests, including setup errors or unfinished cases, remain in the
report denominator. A nonzero exit is an acceptance failure, not hidden as xfail.

Baseline preparation [safety report](reports/reliability-b2-safety-109acaec.md) / [JSON](reports/reliability-b2-safety-109acaec.json):
**18 selected, 15 passed, 3 failed**, five pre-existing SWIG deprecation warnings.

Passing probes use real repositories and disposable persistence to show:

- Stopping between expert checkpoint and final aggregation preserves a valid
  zero. Two failed-only retries reload the exact plans and expert without a new
  provider entry; original history stays unchanged. This uses new run/worker
  objects and real database round trips, not an operating-system restart.
- Changed criterion/setup cannot reuse failed-only work; a fresh whole-batch run
  has no inherited plans/votes. A deliberately obsolete engine cannot reuse
  mathematical scores, plans or experts. This is a generic engine guard; specific
  v1 retirement remains a separate final-integration gate.
- Revoked leases prevent late checkpoint writes. Storage failures at plan,
  expert and final-result boundaries propagate `CheckpointWriteError`; no
  fallback grade/final trace is published. The real final transaction rolls back.
- A real `math_verification.py --worker` process is cancelled after stdin
  transmission starts. It is killed/reaped (return code -9), its PID disappears,
  and a subsequent actual verification succeeds. No dummy subprocess or fake
  mathematical evidence replaces it.
- Private source canaries exist in real persisted plans/traces, while paginated
  public activity history, public progress and live reporter events omit them.
  Pagination advances past filtered private events; result DTOs omit raw traces.
  Public scoring steps may intentionally quote student evidence; this is not a
  blanket assertion that authorized result pages contain no student text.

The three red probes expose two orchestration boundaries for `OSError`:

| Fault injection | Observed baseline behavior | Required behavior |
|---|---|---|
| Reporter before plan request | returns holistic, zero provider entries | propagate the reporter fault |
| Reporter after successful plan parsing | returns holistic, one provider entry | propagate, not replace the parsed plan |
| Reporter before per-question assessment | returns all_failed; two persisted results with null score | propagate the runtime fault, not classify it as model failure |

The equivalent `CheckpointWriteError` probes pass. These cases inject exceptions
raised by reporter methods; they do not redefine intentionally best-effort
progress event sinks as transactional checkpoints. No production repair was made.
The main source boundaries are `math_grading_context.prepare_math_plans`' broad
compilation fallback and the skill/agent assessment exception handling. CORE must
verify these same semantic expectations against its final integration commit.

SQLite safety coverage is now established on the baseline within the above
limits. PostgreSQL/distributed execution, real models, production behavior and
the final v2 integrated candidate remain unverified; B2 still awaits an explicit
`integration_ready` handoff with exact commit/path.
