# B4 fixed-candidate recheck: passed

Production candidate: `d023fc9012078496785a28f6adc2692f6110beca`.
[Small evidence](reliability-b4-d023fc90-small.json) records exact inputs,
observations, source hashes and logs. The [abf6 failure report](B4-ACCEPTANCE-abf6da1f.md)
and its original evidence remain byte-identical to local commit `29d792d2`.

| Independent gate | Result |
|---|---|
| Original 42 raw JSON contracts + 26 probes | **68 passed**, 23.35 s |
| Original persistence/cancellation/fault/public-event safety | **18 passed**, 6.36 s |
| B4 original 28 boundaries + 3 added controls | **31 passed**, 6.10 s |

All three processes exited 0, with no skips or xfails. Safety has five existing
SWIG deprecation warnings. No original input, assertion or score expectation
was changed; only two negative cases and one positive control were appended.

The two exact prior failures now produce zero automatic checks and reject the
explicit unqualified request with `student_expression_not_bound`:

```text
不能推出以下等式：
7+8=16。
```

```text
假设如下等式成立：
7+8=16。
```

Added controls cover a following-line negation, an English hypothesis without a
colon, and two independent complete formula lines. The last control still
schedules both intended relations, binds both and reaches the real worker with
supported results. The fix therefore does not simply disable all multiline checks.

The original B4 full-policy compilation, bounded repair, units, tolerance,
adjacent-term and arbitrary-subexpression rejection cases also pass unchanged.
B changed only its evaluation tests, documentation and reports. The core owner
made the production fix. No network models, credentials, subagents or live
storage were used; no production code or other owners' tests were modified.

This closes the selected B4 offline-contract failures on this exact candidate.
It does not establish real model accuracy, general proof completeness,
PostgreSQL/browser/production acceptance, or acceptance of another combined SHA.

## Reproduce

Run each of `acceptance_reliability.py`, `acceptance_reliability_safety.py` and
`acceptance_reliability_b4.py` separately using the following isolated command
with the corresponding filename:

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
