# Workflow time limits

## Durable jobs

The policies are centralized in `backend/services/workflow_deadlines.py`:

| Workflow | Active processing | Elapsed since first execution |
| --- | ---: | ---: |
| Question preparation, legacy extraction, material matching, AI completion | 90 minutes | 150 minutes |
| Submission recognition | 150 minutes | 210 minutes |
| Grading | 150 minutes | 210 minutes |

Question preparation includes source recognition, question expression, score allocation,
reference answers and grading criteria. Independent material/AI jobs receive their own
limits, not a fresh allowance for each stage inside one job.

The first durable claim starts elapsed time. The initial teacher/server queue is excluded.
After that, every wait, retry delay, requeue, recovery delay and restart counts toward the
same elapsed limit. Parallel work counts once. Active processing reuses the existing
submission clock and counts admitted model, PDF/image and sandbox work; admission queues
are excluded. The active cap stops new requests and permits already sent requests to finish
under their original per-call limits. The elapsed cap cancels local execution even while
waiting or draining.

The first-claim clock is stored in the operation checkpoint, or grading `started_at`, and
cannot be overwritten by a handler checkpoint. The active ledger is checkpointed on results
and every five seconds. Reclaiming the same attempt does not reset either ledger. As with
the previous submission ledger, a process crash can lose the final uncheckpointed activity
interval; the durable elapsed limit still prevents repeated restarts extending the run.
Old submission ledgers retain their elapsed activity when adopting the lower limit. For
pre-upgrade running operations without a first-claim field, existing progress `started_at`
is used when present; otherwise the first post-upgrade claim establishes the missing origin.

Teacher-initiated retry/continue creates a new attempt (or grading run), joins the current
queue, and starts fresh limits on admission. Verified results/checkpoints are reused through
the existing retry/manual-review paths. Elapsed-limit failures and uncertain provider
submissions require explicit duplicate-billing acknowledgement before resubmission.
A locally cancelled HTTP call cannot retract work already accepted by the model provider.

## Cancellation and cleanup

- A coordinator waits at most 30 seconds for child cancellation, then fences durable writes
  and retains the child as quarantined. Admission is closed before cancellation, so late
  children cannot submit new requests.
- PDF/image and sandbox process cleanup waits at most 30 seconds. A process whose exit is
  not confirmed retains its capacity permit. Confirmed late cleanup restores that permit.
- OCI capacity is retained until both launcher exit and container removal are confirmed.
  An unconfirmed removal fails closed rather than allowing unbounded container accumulation.
- Fresh Gemini clients use the supported cancellable async API inside limited workflows or
  HTTP requests. Client cleanup has its own 10-second bound. Unknown sent requests are not
  automatically replayed; documented rate-limit rejection retries remain unchanged.

A hard deadline begins cancellation at the stated time; bounded cleanup may finish afterward.
Resource quarantine is deliberate. Operators should investigate persistent exhausted capacity,
not increase concurrency to hide unreaped workers.

## Synchronous browser requests

These requests happen before or outside durable jobs and do not start a background job's clock.
Their limits are in `backend/api/request_deadlines.py`:

| Request | Backend | Browser |
| --- | ---: | ---: |
| Ask | 290 seconds | 305 seconds |
| Question/material source preflight, including upload parsing | 950 seconds | 1020 seconds |
| Per-question analysis | 25 seconds | 40 seconds |
| Expert probe | At most 60 seconds | 75 seconds |

Browser waits include cleanup without reducing backend processing time: preflight
can require 30 seconds of PDF transport draining followed by 30 seconds of process
reaping, plus 10 seconds delivery margin. Ask and per-question analysis have no
recognition observer; their reachable async finalizer is Gemini client close
(up to 10 seconds), leaving 5 seconds for response delivery. Expert probes keep
both their 60-second server cap and 75-second browser wait. Normal successful
responses return immediately; only the timeout waiting boundary changes.

Frontend values are in `src/api/requestTimeouts.ts`, `src/api/analytics.ts` and
`src/api/experts.ts`. Ordinary launch/poll request waits remain unchanged.

Short job-launch/poll HTTP timeouts are not interpreted as background execution limits.
Preflight cancellation terminalizes temporary drafts; an uncertain submitted request is retained
and never silently replayed.

## Other limits and settings

- Knowledge ingestion: one file has a persistent 2-hour elapsed deadline across page batches,
  beginning at first execution. See `FILE_TIMEOUT_SECONDS` in
  `backend/db/knowledge_ingestion_repository.py`; explicit retry retains completed pages.
- Knowledge search: 29-second backend budget behind the existing 30-second browser wait;
  optional rewriting is separately bounded. Partial/degraded results carry explicit warnings.
- PDF/image and local sandbox admission: 600 seconds. OCI host admission: 120 seconds.
  Provider capacity retains the pre-existing smaller `llm_timeout` bound, at most 600 seconds;
  previously unbounded provider preparation/admission steps are independently capped.
- `SMARTAI_BACKGROUND_PASS_TIMEOUT_SECONDS` (default 3600) bounds an individual idempotent
  delayed-source cleanup/maintenance pass. It is not a model-workflow or knowledge-file limit.
  A timed-out cleanup pass can be safely rescheduled by the existing maintenance policy.

The old unused job timeout field and its environment lookup were removed. Changing that old
variable has no effect; use the policy or setting named above for the intended scope.
