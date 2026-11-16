"""Validate semantics first, then report measured memory and replay throughput."""
import json
from pathlib import Path
import statistics
import sys

root = Path(sys.argv[1])
def read(name):
    return json.loads((root / f"{name}.json").read_text())

for mode in ("normal", "pressure"):
    old_trials = [read(f"baseline-{mode}-{i}") for i in range(1, 4)]
    new_trials = [read(f"candidate-{mode}-{i}") for i in range(1, 4)]
    for before, after in zip(old_trials, new_trials):
        for key in ("calls", "corrections", "plans_digest", "prompts_digest", "scores_digest"):
            assert before[key] == after[key], (mode, key)
        assert after["peak_prepared"] <= before["peak_prepared"]
    fields = ("seconds", "peak_rss_mib", "peak_prepared", "peak_active")
    before = {key: statistics.median(t[key] for t in old_trials) for key in fields}
    after = {key: statistics.median(t[key] for t in new_trials) for key in fields}
    # Broad guard for runner noise; measurements remain visible, not only pass/fail.
    assert after["seconds"] < before["seconds"] * 1.25, (mode, before, after)
    assert after["peak_rss_mib"] < before["peak_rss_mib"] + 8, (mode, before, after)
    print(f"{mode}: peak RSS {before['peak_rss_mib']:.2f} -> {after['peak_rss_mib']:.2f} MiB; "
          f"elapsed {before['seconds']:.3f} -> {after['seconds']:.3f}s; "
          f"prepared {before['peak_prepared']} -> {after['peak_prepared']}; "
          f"active {before['peak_active']} -> {after['peak_active']}")
before, after = read("baseline-progress"), read("candidate-progress")
for projection in ("polls", "history_polls"):
    for field in ("peak_mib", "seconds"):
        old = statistics.median(p[field] for p in before[projection])
        new = statistics.median(p[field] for p in after[projection])
        assert new < old * 1.1, (projection, field, old, new)
        print(f"{projection} {field}: {old:.3f} -> {new:.3f}")
allocator = read("allocator")
assert allocator["supported"] and allocator["released_mib"] > 64, allocator
assert allocator["pause_seconds"] < 1, allocator
print(f"allocator: released {allocator['released_mib']:.2f} MiB in {allocator['pause_seconds']:.3f}s")
