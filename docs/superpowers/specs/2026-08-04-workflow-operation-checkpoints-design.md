# Workflow Operation Checkpoints Design

## Goal

Implement `DB-W1-2` from `数据库持久化与任务恢复工作安排.md`: persist a bounded operation stage, checkpoint revision, artifact references, attempt-scoped terminal summary, and compare-and-swap writes without changing public APIs, Facade calls, Agent/OCR behavior, or the existing operation lifecycle.

## Scope And Boundaries

This change is limited to `backend/db/workflow_repository.py`, a new Alembic revision, and repository/migration/PostgreSQL tests. Existing `create_operation()`, `get_operation()`, and `update_operation()` signatures and behavior remain available to current callers.

The repository stores recovery metadata only. It does not store confirmed questions, complete submissions, raw file bytes, full OCR text, prompts, API keys, or raw model responses in the new checkpoint fields. It does not call OCR/LLM code, choose domain stages, dispatch workers, claim leases, reclaim expired work, or modify `task_facade.py`, `main.py`, API modules, frontend code, or migrations `0001` through `0004`.

## Existing Contract Reused

`workflow_operations` remains the single operation record and the source of truth for operation ID, owner, assignment, operation type, input hash, attempt, status, progress, legacy payload, error code, and timestamps. `DB-W1-2` extends that row instead of introducing another job store.

The existing unique key `(assignment_id, operation_type, input_hash)` continues to make repeated inputs return the same operation. Existing retry logic increments `attempt` on an error or expired operation. On retry it also resets all checkpoint fields, so evidence from the old attempt cannot be mistaken for progress by the new attempt.

## Schema

Migration `0006_operation_checkpoints` adds these columns to `workflow_operations`:

| Column | Contract |
|---|---|
| `checkpoint_revision` | Non-negative integer, default `0`. Incremented by exactly one for every successful checkpoint or terminal CAS write. |
| `checkpoint_stage` | Nullable stable stage ID, at most 64 characters. The repository validates shape but does not interpret its meaning. |
| `checkpoint` | Required JSON object, default `{}`. Contains only bounded recovery metadata. |
| `artifact_refs` | Required JSON list, default `[]`, containing stable stored-file IDs. |
| `terminal_summary` | Nullable JSON object. A bounded, durable summary for a terminal operation outcome. |

The migration backfills existing rows with revision `0`, empty checkpoint, and empty artifact references using server defaults. It adds a named check constraint requiring `checkpoint_revision >= 0`. Downgrade removes only these five columns. SQLite uses Alembic batch operations so upgrade/downgrade remains portable; PostgreSQL receives equivalent columns and constraint.

## Repository API

Add `save_operation_checkpoint()` as a keyword-only additive API:

```python
save_operation_checkpoint(
    operation_id: str,
    *,
    owner_id: str,
    expected_attempt: int,
    expected_checkpoint_revision: int,
    stage: str | None,
    checkpoint: dict,
    artifact_refs: list[str] | None = None,
    terminal_summary: dict | None = None,
    terminal_status: str | None = None,
) -> WorkflowOperationRecord
```

The write is one conditional SQL `UPDATE` matching operation ID, owner ID, attempt, and checkpoint revision. A success replaces the bounded checkpoint fields and increments `checkpoint_revision` atomically. Concurrent writers using the same expected revision therefore produce exactly one success.

Failure classification is owner-safe and stable:

- no owner-scoped operation: `NotFound("workflow_operation")`;
- owner-scoped operation with another attempt: `VersionConflict(code="stale_operation_attempt")`;
- matching attempt with another checkpoint revision: `VersionConflict(code="stale_checkpoint_revision")`.

Artifact references are deduplicated while preserving caller order. Every referenced `stored_files` row must match both owner and operation assignment. A missing and wrong-owner artifact both raise the same `NotFound("stored_file")`, without revealing cross-owner existence.

## Terminal And Replay Semantics

`terminal_summary` and `terminal_status` must be supplied together. A terminal status is a non-empty string of at most 32 characters and cannot be `pending` or `running`. Supplying only one field raises `invalid_operation_terminal_state`; an invalid status raises `invalid_operation_terminal_status`.

A terminal checkpoint CAS atomically persists the summary, status, completion timestamp, checkpoint fields, and incremented revision. A non-null `terminal_summary` is immutable for its attempt. Once set, another checkpoint write or generic `update_operation()` call is rejected with `InvalidTransition(code="operation_already_terminal")`. A focused repository transition that is explicitly designed for an existing terminal state, such as `ready -> applied`, may retain its own atomic transition contract. The persisted operation can be read repeatedly through the unchanged `get_operation()` call; refreshes and new processes see the same terminal state.

An exact replay using the already-consumed revision is also rejected as stale CAS rather than performing a second write. This makes caller behavior explicit: after a timeout, re-read the operation. If the returned terminal summary matches the caller's intended result, the operation is already complete; otherwise the caller lost the CAS and must not overwrite it.

Retrying an operation increments `attempt` and resets `checkpoint_revision`, `checkpoint_stage`, `checkpoint`, `artifact_refs`, and `terminal_summary`. Writes from the old attempt continue to receive `stale_operation_attempt` even though both attempts use revision zero at their start.

## Bounded JSON Contract

Bounds are measured using compact UTF-8 JSON (`ensure_ascii=False`) before any database write:

- legacy operation `payload`: at most 4 MiB;
- legacy operation `progress`: at most 64 KiB;
- checkpoint object: at most 64 KiB;
- artifact references: at most 100 IDs, each a non-empty string of at most 64 characters, and at most 8 KiB serialized;
- terminal summary object: at most 16 KiB;
- terminal status: non-empty, at most 32 characters, and not `pending` or `running`;
- checkpoint stage: at most 64 characters.

Oversized or structurally invalid values raise the existing `ValidationError` with stable field-specific codes. Bounds apply on `create_operation()`, `update_operation()`, and `save_operation_checkpoint()` so the repository cannot be bypassed through its legacy mutation entry points. The 4 MiB legacy payload ceiling preserves the existing 400,000-character source contract while creating an explicit finite database limit. JSON values must be serializable and finite; non-finite numbers are rejected.

## Concurrency And Transactions

The CAS uses a single `UPDATE ... WHERE attempt = :expected_attempt AND checkpoint_revision = :expected_revision`. It does not depend on process locks or a long transaction. SQLite tests prove deterministic stale-write behavior, while the configured PostgreSQL test runs two independent repository calls against the same revision and verifies one success and one stable conflict.

Artifact ownership is checked in the same transaction before the CAS update. A concurrent artifact deletion may cause the write to reject or leave only an ID that was valid at validation time because JSON references cannot carry a foreign key. The existing source/outcome schema remains the stronger normalized evidence path for per-file artifacts; operation artifact refs are bounded recovery pointers, not ownership truth.

## Migration And Compatibility

`0006` depends on `0005_workflow_source_outcomes`. No prior migration is edited. Existing rows read with empty checkpoint state after upgrade. Existing callers continue to receive the same ORM record with additive attributes. Existing nonterminal callers remain compatible: legacy creation/update signatures are unchanged, and the additive checkpoint API gains only the optional `terminal_status` argument.

Migration tests cover `0004 -> 0005 -> 0004 -> 0005`, data preservation, defaults, the revision check, and full `head -> base -> head`. PostgreSQL offline DDL and configured live persistence tests cover portable schema behavior.

## Test Matrix

Repository tests must demonstrate:

1. Initial checkpoint state is revision zero and survives a fresh database read.
2. A valid checkpoint increments revision and preserves stage, metadata, ordered artifact IDs, and attempt.
3. Two writes against one revision produce one success; the loser receives `stale_checkpoint_revision`.
4. A retried operation resets checkpoint fields and rejects the previous attempt with `stale_operation_attempt`.
5. Terminal summary and terminal status are paired, atomically persisted with `completed_at`, durable, repeat-readable, and cannot be overwritten by a later checkpoint or generic operation update.
6. Wrong-owner operation and artifact access is indistinguishable from absence.
7. Stage, JSON shape, serialized byte limits, artifact count/ID limits, non-finite values, and legacy payload/progress limits are enforced before persistence.
8. Existing operation and background workflow tests remain green without API or Facade changes.

## Review Gate

This is the second and final Week 1 database PR. After its migration, repository contract, concurrency tests, and regression suite pass, work stops at the Week 2 review gate. Lease, worker-core, reclaim, heartbeat, dispatch, and domain recovery wiring are deliberately excluded.
