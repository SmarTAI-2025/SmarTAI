# Grading memory regression replay

The memory-only change starts from `88ca8facdab4948f35381e4e32ad1bdc47c4807f`.
It expires recognition bytes even after recognition becomes idle; prepares at
most 50 complete math contexts per provider before admission; reuses a single
progress-event snapshot without loading unused final-trace payloads; and asks
Linux's allocator to return free pages when the idle scheduler observes low
memory (at most once per 30 seconds). Existing admission reserves and limits,
provider retry policy, prompts, frozen scoring rules and settlement are retained.

Shared/Demo guards use their underlying runtime model as the preparation
identity; fresh wrappers from registry lookups share the same window, while
independent clients with the same public name remain independent. Review's
existing no-call exits run before queueing; only an actually needed review
prepares its model context under a permit. Regression tests create 217 fresh
wrappers through real local Demo/shared registries, build actual grading
prompts, then block synthetic responses at the provider boundary. They verify
the 50-context cap, bulk cancellation/restart, and both the no-review fast path
and the queued necessary-review path under a saturated preparation window.

`grading_memory_benchmark.py` runs against a supplied source checkout in a fresh
process and temporary test directory. It never builds a network client. The
same seven frozen synthetic proof rubrics and 31 submissions run through the
actual batch/expert/structured-output/settlement/aggregation pipeline with two
fixed-response providers, the actual local admission scheduler, a 10-slot fake
host, and a 150 ms response delay. Each run must finish exactly 434 calls and
217 corrections. SHA-256 digests compare all prompts, frozen plans and scored
step evidence across revisions. This is not real-model quality or latency evidence.

The Linux Actions workflow runs three separate-process pairs in each condition:

- **Normal:** no simulated host memory restriction; checks queue allocation and
  throughput without memory throttling.
- **Pressure:** a 1906 MiB simulated host; other-process usage is held constant
  by calculating available memory as `740 MiB - (current real RSS - import RSS)`.
  Both revisions first touch 128 MiB of subsequently freed native arenas and
  populate 64 MiB of recognition cache with expired entries. The candidate runs
  one expiry tick; a separate lifecycle test verifies the real timer and shutdown.
  The original scheduler still decides actual admission from measured RSS.
- **Progress:** actual SQLite projection with 3,560 progress events, 434 saved
  expert assessments and 217 final traces; five polls record traced peak
  allocations and elapsed time. PostgreSQL checks verify the same SQL projection,
  owner filtering, cursors and checkpoint counts.
  The expanded activity endpoint is measured separately: five requests for a
  200-row page of final traces verify unchanged cursors/has-more while avoiding
  private scoring payloads. Its SQL projection also omits expert checkpoint and
  rubric-plan payloads, which are not used by the public activity projection.
- **Allocator:** a separate Linux process measures physical RSS released from
  free native arenas and the duration of GC plus `malloc_trim(0)`. Unsupported
  allocators keep the existing admission guard.

Example (use absolute source and output paths):

```sh
python tools/testing/grading_memory_benchmark.py --source /path/to/checkout \
  --mode normal --output /tmp/candidate-normal.json
```

The workflow preserves every raw measurement and compares medians. Its broad
25% time tolerance accounts for hosted-runner noise; review the actual numbers,
not just the passing status. Peak RSS includes imports, synthetic preconditioning,
live results and allocator retention. It is not an estimate of maximum supported
classroom size or a promise of production model speed. No production task is
modified or replayed by this benchmark.
