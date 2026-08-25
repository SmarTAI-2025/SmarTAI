# Database Week 2 Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete DB-W2-1 through DB-W2-4 with durable workflow-operation leases, restart-safe dispatch, object-backed recognition inputs, bounded checkpoints, and idempotent commits.

**Architecture:** Extend the existing `workflow_operations` row instead of creating another job store. An operation attempt is the user-visible retry generation; a random lease token is the worker fencing generation and rotates on every claim or reclaim. A small workflow worker polls claimable rows and dispatches registered handlers by `operation_type`; handlers load owner-scoped stored-file references and bounded checkpoints, heartbeat while paid work runs, and commit through existing normalized repository transactions.

**Tech Stack:** Python 3.12, FastAPI lifespan, asyncio, SQLAlchemy, Alembic, SQLite, PostgreSQL, pytest.

---

## File Structure

- Add sequential Alembic revisions after `0006_operation_checkpoints`; never edit `0001` through `0006`.
- Keep lease and claim primitives in `backend/db/workflow_repository.py` because they guard writes to the same row.
- Add `backend/services/workflow_worker.py` for polling, handler registration, heartbeat lifecycle, dispatch, and shutdown only.
- Add focused recovery adapters under `backend/services/` only when they avoid growing `task_facade.py`; normalized domain commits remain in their current repositories/facade transactions.
- Modify `backend/services/task_facade.py` only to persist source references/checkpoints and expose handler entry points. Do not change OCR, matching, grading, or product semantics.
- Modify `backend/main.py` only in the W2-2 startup integration commit.
- Add focused tests under `backend/tests/`; preserve explicit live-PostgreSQL skips when `SMARTAI_TEST_POSTGRES_URL` is absent.

## Invariants

1. `(operation.id, attempt)` identifies a retry generation; `(lease_token, lease_expires_at)` identifies the only worker allowed to mutate that generation.
2. Claim/reclaim rotates `lease_token`. A previous worker cannot heartbeat, checkpoint, commit terminal state, or release after rotation or expiry.
3. Claiming is one conditional database update. Process locks and long transactions are not correctness mechanisms.
4. Original upload bytes live in object storage and `stored_files`; operation JSON contains only bounded metadata and stable IDs.
5. Checkpoints describe completed paid stages and artifact references. They do not copy confirmed questions, submissions, raw model responses, or full OCR text into operation JSON.
6. Domain commits and the terminal operation transition are atomic and idempotent. A replay either observes the existing commit or performs the only formal revision write; no distributed exactly-once claim is made for external provider calls.
7. Owner predicates apply to every operation and stored-file read. Wrong-owner and missing IDs expose the same `not_found` behavior.
8. A process shutdown stops claiming new work, lets handlers cancel, and never clears another worker's lease.

### Task 1: DB-W2-1 operation lease schema and repository contract

- [ ] Write failing tests in `backend/tests/test_workflow_operation_leases.py` for one-winner concurrent claim, claimable pending/unleased running rows, expired reclaim with token rotation, heartbeat extension, owner isolation, release, cancellation, and stale/expired token fencing.
- [ ] Run the focused tests and record RED because lease fields and APIs are absent.
- [ ] Add `lease_owner`, `lease_token`, `lease_expires_at`, and `lease_heartbeat_at` plus claim indexes in the ORM and a reversible `0008` migration after `0007_source_outcome_diagnostics`. Lease owner/token are nullable together; expiry is required for an active lease.
- [ ] Implement `claim_operation`, `heartbeat_operation`, and `release_operation` as conditional updates. Add a bounded `list_claimable_operations` query for supported operation types.
- [ ] Require `expected_lease_token` for checkpoint and terminal writes used by workers. Preserve legacy call compatibility only for non-worker code until its W2 handler is migrated; do not allow an unleased writer when an active lease exists.
- [ ] Reset lease fields on a new attempt. Make terminal transitions clear only their own matching lease.
- [ ] Add migration round-trip/offline PostgreSQL DDL tests and a configured live PostgreSQL one-winner claim test.
- [ ] Run focused lease/checkpoint/source/background tests, `python -m alembic heads`, and `git diff --check`; commit W2-1 separately.

### Task 2: DB-W2-2 workflow worker core

- [ ] Write failing tests in `backend/tests/test_workflow_worker.py` for supported-type polling, duplicate dispatch, unknown type exclusion, heartbeat renewal, handler success/failure, lease loss, cancellation, graceful shutdown, and reclaim after simulated process death.
- [ ] Add worker settings for stable per-process ID, lease duration, heartbeat interval, poll interval, and bounded batch size.
- [ ] Implement `WorkflowWorker` with an explicit handler mapping. It polls only registered operation types, claims each row, starts a heartbeat task, invokes one async handler with an immutable leased-operation context, and stops mutating immediately on lease loss.
- [ ] Map known domain failures to their stable codes and unexpected failures to the existing operation-specific fallback without logging payloads.
- [ ] Add FastAPI lifespan startup/shutdown wiring in a separate commit. Startup creates one worker task per app process; shutdown signals it, awaits bounded cancellation, and does not bulk-release leases.
- [ ] Keep request `BackgroundTasks` behavior until each concrete operation is migrated in Tasks 3-5. The worker must not claim an operation type until its durable handler is registered.
- [ ] Run worker, application startup, and existing grading-worker lifecycle tests; commit worker core and startup integration separately.

### Task 3: DB-W2-3 problem extraction recovery

- [ ] Write failing tests proving queueing saves the source object/metadata before the operation is dispatchable, payload/checkpoint contains only the owner-scoped stored-file ID, and a fresh process can load the input without request bytes.
- [ ] Persist the problem source through the existing storage/file repository contract, with stable hash, MIME, size, object key, owner, assignment, attempt, and ordering.
- [ ] Add a `problem_extraction` durable handler that reconstructs the owner provider registry, loads the stored file through owner predicates, and resumes from explicitly saved safe checkpoints.
- [ ] Checkpoint after source text extraction using an artifact reference rather than copying large OCR text into operation JSON. If no durable intermediate artifact contract exists, checkpoint only before paid work and after the normalized draft commit; do not claim mid-stage savings that are not durable.
- [ ] Heartbeat around long OCR/LLM stages and pass lease token into every checkpoint/final commit.
- [ ] Make the normalized question replacement and terminal operation transition one transaction guarded by attempt plus lease token. Replaying the same input hash must observe the existing terminal result and create no duplicate question revision.
- [ ] Remove `problem_extraction` from request-memory dispatch only after restart, fake-provider call-count, lease-loss, and idempotent-commit tests pass.
- [ ] Run focused problem extraction, storage, checkpoint, worker, and facade tests; commit separately.

### Task 4: DB-W2-3 submission parsing recovery

- [ ] Write failing tests proving all submission source objects and their outcome rows persist before dispatch, a fresh process loads them by owner, and per-source completed checkpoints are reused.
- [ ] Build on the existing W1 source/outcome repository. Do not duplicate PR #26's product/UI semantics; keep outcome codes already frozen by the domain contract.
- [ ] Add a `submission_recognition` durable handler that loads stored sources, reconstructs provider/roster options from bounded metadata, and records a checkpoint only after a source's OCR/parse artifact is durable.
- [ ] Reuse completed source artifacts on retry so fake-provider call counts do not increase for completed sources. Preserve stable error outcomes for failed sources.
- [ ] Guard the normalized submission revision commit and terminal transition by owner, attempt, lease token, and workflow revision. Replays must not create duplicate formal revisions.
- [ ] Remove request-memory dispatch for submission recognition only after restart, mixed outcome, lease-loss, owner isolation, and one-commit tests pass.
- [ ] Run submission source/outcome, storage, worker, facade, and PostgreSQL tests; commit separately.

### Task 5: DB-W2-4 material import and AI completion alignment

- [ ] Write parameterized failing tests for `material_import` and `ai_completion`: fresh-process dispatch, duplicate dispatch, lease loss, retry from the latest durable checkpoint, stale workflow revision, and at-most-one formal patch revision under idempotent replay.
- [ ] Persist any uploaded material through stored-file references. Store AI completion target IDs and revision inputs as bounded metadata; never store provider keys or full raw responses.
- [ ] Register both durable handlers using the same worker/lease/checkpoint context. Do not add operation-specific worker loops or a second state machine.
- [ ] Keep existing question patch CAS semantics and add lease-token fencing to the same transaction. A stale lease or workflow revision must roll back both patches and operation status.
- [ ] Remove request-memory dispatch for each type only after its tests pass.
- [ ] Run auxiliary operation, facade integrity, task finalization, worker, migration, and PostgreSQL tests; commit each operation type independently.

### Task 6: Cross-Week-2 verification and stacked PRs

- [ ] Re-read DB-W2-1 through DB-W2-4 and map every acceptance item to a test or an explicit residual blocker.
- [ ] Run the complete backend suite. Run PostgreSQL integration when `SMARTAI_TEST_POSTGRES_URL` is configured; otherwise report the skip and retain dialect/concurrency SQL evidence.
- [ ] Run migration upgrade/downgrade/upgrade checks, `python -m alembic heads`, `git diff --check`, and inspect every diff for owner predicates, payload bounds, secrets, and forbidden business changes.
- [ ] Update `CLAUDE_HANDOFF.md` with red/green commands, modified files, review fixes, residual risks, and exact PR dependencies.
- [ ] Push each branch and create stacked PRs in this order: W2-1 based on current `main` (which already contains the source-outcome/checkpoint foundation); W2-2 based on W2-1; problem extraction based on W2-2; submission parsing based on problem extraction; W2-4 operation PRs based on the preceding approved recovery pattern.
- [ ] Every PR body must state baseline SHA, single problem, before/after contract, migration/rollback behavior, tests, dependencies, conflicts with PR #23/#26, and unimplemented follow-ups.
