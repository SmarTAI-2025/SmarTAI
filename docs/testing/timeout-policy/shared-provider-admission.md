# Shared provider cooldown and learned admission

This change is stacked on PR #303 (180-second response attempts and at most
three attempts including the first). No extra model probes are sent.

## Admission and queue

Each credential + endpoint + provider type + model has its own quota group.
Every send and retry must satisfy the group's cooldown, minimum start interval,
rolling user RPM and in-flight limit, plus existing endpoint/host/memory guards.
A completed call frees one slot; this is not batch-at-a-time execution.

All owners in the service event loop share one ordered queue. Select the oldest
eligible logical call. Temporarily skip cooling, paced or capacity-blocked calls
without moving them to the tail. Retries retain the logical call's original
order; new logical calls and child tasks obtain new orders. Do not preempt calls
already admitted. Host OS permits and pacing/cooldown locks also work across
local processes; strict FIFO is local to the scheduler, not a distributed queue.

## Cooldown and stopping

An ordinary 429 always freezes new sends for 65 seconds. An explicit longer
Retry-After extends this floor. A late refusal from the same pre-cooldown wave
joins the existing deadline instead of resetting it. Already admitted calls
finish normally. A timeout or other retry during cooling waits only the
remaining group delay (or its own longer explicit unavailable-server hint).
A pure response timeout creates no cooldown.

Explicit exhausted daily/zero/balance quota remains a terminal verdict.
Otherwise, count independent admission cycles, not parallel refusals. Stop new
work after three consecutive cycles with no post-cooldown success; an actual
parameter adjustment grants two extra cycles once per run. Do not renew the
extra allowance on each adjustment. A late successful response belongs to the
cycle in which it was sent, even if the next cooldown has already started.
Pending siblings prevent prematurely declaring a complete cycle unsuccessful.
Already admitted results and other quota groups remain intact.

Host cooldown/RPM files retain their stable lock inodes and legacy JSON format.
A complete write-ahead record is synced and atomically published before an
in-place update. After a writer exits mid-write, the next lock holder replays
that record before admitting work. A backward monotonic-clock jump durably
discards the previous clock epoch, so a retained stale cooldown cannot return
when uptime catches up. Existing malformed unjournaled data is repaired with
one persisted 65-second quiet period (or the longer RPM interval); active valid
Retry-After deadlines remain in force. Filesystem/permission errors still fail
closed. Do not remove lock files while workers run. Recovery requires updated
workers; unchanged old binaries do not understand a pending journal.

Stopping a group immediately interrupts its capacity waiters, including retries
after an uncertain timeout and explicit daily/zero/balance exhaustion. Whether
this attempt is in flight is tracked separately from whether an earlier attempt
may have been billed. Stopping an unsent retry must not erase that uncertainty.

## Learning and persistence

Unknown BYOK starts at interval T=1 second and C=5 in-flight calls. With explicit
RPM and no saved profile, T starts at 60/RPM instead of imposing a hidden 60 RPM
cap. Explicit RPM is always an interval floor T >= 60/RPM and retains its rolling-window guard;
automatic latency-derived concurrency and server bounds still apply.

Recalibrate after two recovered independent cooldown incidents without stable
history, or three with stable history. Capture per-second admission buckets;
wait for their outstanding outcomes before using success counts. Do not treat
unreturned work as failures or infer a precise vendor quota from generic 429s.

- If each incident rejects new concurrency before any recent call has finished,
  and existing calls later succeed, try C'=max(1,min(C-1,rejected_active-1)); keep T.
- Otherwise keep C and try r'=min(0.9*r,0.9*N), T'=60/r', where r=60/T and N is
  the median confirmed successful start count in the incident windows. With no
  useful successful count, use a bounded 20% rate reduction, never divide by zero.
- A recalibration may correct upward using at most two real-work trials after
  at least three successes covering 60 seconds of actual start times and real
  backlog. Try min((good_rate+observed_upper)/2,1.1*good_rate,user_RPM). For C,
  increase by at most one; do not retest an adjacent already-rejected integer.
  On 429, always cool, roll back the trial and stop upward exploration.
- Stable unchanged configurations do not chase the ceiling. Edited configs
  reuse the previous estimate, with at most one bounded healthy trial.

A separate nullable JSON field stores T, C, response EWMA, config revision,
generation and validation state. Provisional results survive short batches;
unvalidated upward trials do not replace a validated baseline. Owner/config
revision plus generation compare-and-swap prevents stale writers. Learning
never changes the user's RPM, verification metadata or workflow fingerprint.

An identity moving to a new quota group detaches its old control and fences
older registry snapshots. With queued demand, each scheduler batches one
credential-free metadata refresh per five seconds; this also discovers edits,
deletions and disabled configurations made by another process. Refreshing does
not occupy model slots or block other groups; a database failure retains the
last known protection and backs off metadata reads for 30 seconds. Existing
shared cooldowns, recorded starts and admitted responses remain intact.

A rejected learning CAS immediately reloads metadata. A competing worker's
newer profile is adopted instead of blindly renumbering a stale candidate.
Subsequent observations can publish against that generation. Late database
reads or save acknowledgements cannot replace a newer configuration binding.

## Offline comparison

90 logical calls; at most five server slots. Entries are completed / 429 / seconds.
No real provider calls, provider account balances or classroom accuracy claims.

| Constraint and response | Shared only | Old simultaneous reduction | This policy | Reused profile |
|---|---|---|---|---|
| 6 RPM, 3s | 90 / 56 / 962 | 90 / 3 / 1150 | 90 / 3 / 1008 | 90 / 0 / 940 |
| 6 RPM, 22s | 90 / 56 / 1266 | 90 / 3 / 1189 | 90 / 3 / 1049 | 90 / 0 / 959 |
| 6 RPM, mixed 3–166s | 90 / 14 / 1196 | 90 / 5 / 1439 | 90 / 6 / 1161 | 90 / 0 / 1040 |
| 2 concurrent, 22s | 90 / 131 / 2893 | 90 / 3 / 3336 | 90 / 3 / 1127 | 90 / 0 / 991 |
| 1 concurrent, 3s | 54 / 213 / 3461 | 90 / 3 / 6662 | 90 / 3 / 460 | 90 / 0 / 270 |

Initial fast low-RPM batches can be slower than shared-only cooldown; the aim is
less repeated failure with reusable operating parameters, not universal speedup.
Slow responses and scarce server capacity do not cause unnecessary model probes.

Reproduction: `test_adaptive_admission_simulation.py` compares policies using a
discrete-event clock; `test_provider_shared_cooldown.py` also exercises real
provider/retry/admission boundaries with synthetic transports. Queue regressions
cover cross-owner FIFO, skipped cooldowns, original retry order and cancellation.
Frontend tests cover both model countdowns appearing together and resuming
independently, plus existing stop/continue flows at desktop and mobile widths.

Combined local validation on the PR303 base: 8,679 backend tests passed and
105 optional/environment tests skipped; 1,791 frontend tests and 11 Chromium
stop/continue/countdown scenarios passed. An isolated PostgreSQL instance passed
the new migration and owner/config/generation persistence checks. No production
deployment or real model calls were performed.
