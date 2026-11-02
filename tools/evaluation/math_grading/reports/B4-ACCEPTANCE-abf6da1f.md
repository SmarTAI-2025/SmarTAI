# B4 independent incremental acceptance: failed

Fixed production candidate: `abf6da1f24a560e3f160e01cd33b45889e535b09`.
B branch: `codex/math-reliability-b4-20261023`. No production code or prior
case/oracle/test changes. [Small machine evidence](reliability-b4-abf6da1f-small.json)
contains exact inputs, requests, source hashes and test summaries.

| Explicit gate | Result |
|---|---|
| Original 42 raw JSON contracts + 26 probes | 68 passed, 23.04 s |
| Original persistence/cancellation/fault/public-event safety | 18 passed, 6.61 s |
| New independent policy/relation boundaries | **26 passed / 2 failed**, 4.49 s |

No skips or xfails. Safety output has five existing SWIG warnings and fourteen
JUnit `record_property` format warnings; these are not test failures.

## Blocking source-scope counterexamples

The new independent gate preserves each full claim:

```text
不能推出以下等式：
7+8=16。
```

```text
假设如下等式成立：
7+8=16。
```

Expected: retain the negation/hypothesis scope; do not schedule an unconditional
relation check, and reject an explicit request treating the embedded relation
as the assertion of that full claim.

Actual: both produce an `explicit_relation` request comparing `16` with `7+8`,
with no conditions, and `binding_error` returns `None`. `_statements` splits the
source at the newline, discards the unsupported preceding scope and accepts the
remaining formula. This violates the same conservative treatment already used
for same-line negations/hypotheses. It does **not** prove a wrong final score:
this independent boundary test stops at planning/binding, before model feedback
and scoring.

A focused module comparison against the immediate parent returns zero automatic
checks for both inputs; abf6 returns one each. This demonstrates the new automatic
scheduling behavior, not a full prior-candidate acceptance run or a claim that
all prior explicit binding behavior was correct.

The implementation owner should preserve scope across separators or reject the
ambiguous whole claim. B retains both failing expectations and makes no fix to
production code.

## Passing new boundaries

- Complete Chinese-prefixed and separately delimited relations reach the real
  worker, including dimensional mismatch, conversion and unsupported `f(2)`.
- Subexpressions, changed exponent/long decimal, missing units, nonadjacent terms,
  malformed relations, unclosed delimiters, same-line negation/hypothesis and
  unknown prose remain rejected.
- Approximate relations require sourced tolerance; chains generate only adjacent
  complete relations, with actual worker checks.
- Raw JSON compilation preserves the blank holistic route plus the nonblank
  deductions route, one 1-point rule and both complete punctuated exemptions.
  Duplicate allocation receives one bounded repair. Removed conditions and
  joined policies are rejected.

The first increment run had five **test-harness** errors because the newly
written fixed provider used `LLMResponse.text` instead of `content/provider`.
Only that response envelope was corrected. The complete 28-case rerun retains
all initial inputs/assertions and leaves the same two source-scope failures.
Initial and final logs are separately retained under `/private/tmp`.

## Reproduce

Run each target separately from the B checkout with the project Python:

```sh
PYTHONDONTWRITEBYTECODE=1 /opt/anaconda3/envs/smartai/bin/python - <<'PY'
import os, sys, pytest
sys.path.insert(0, os.getcwd())
from tools.evaluation.math_grading.run import isolated_imports
with isolated_imports():
    raise SystemExit(pytest.main(['-q', '-p', 'no:cacheprovider', '--tb=short',
        'backend/tests/math_grading_evaluation/acceptance_reliability_b4.py']))
PY
```

Replace the target with `acceptance_reliability.py` or
`acceptance_reliability_safety.py` for the original two gates. Disposable SQLite,
fixed JSON and denied network only; no credentials or model calls. B did not
accept the separate `d69a04667e929fd485cc9023d29dbb7d074b2b79` integration,
PostgreSQL, browser, production or real model quality. Await a new fixed
candidate after the implementation owner handles these counterexamples.
