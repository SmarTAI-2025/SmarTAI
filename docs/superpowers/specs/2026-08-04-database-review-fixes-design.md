# Database Review Fixes Design

## Goal

Close the concurrency and state-consistency gaps found while reviewing the
local DB-W1-1 and DB-W1-2 changes, while keeping the implementation inside the
database repository boundary and removing only clearly disposable local
artifacts from the primary workspace.

## Scope

The code changes are limited to:

- `backend/db/source_outcome_repository.py`;
- `backend/db/workflow_repository.py`;
- their focused repository tests;
- the existing DB-W1-1 and DB-W1-2 design/plan documents when their contracts
  need clarification.

No API, Facade, Agent, OCR, frontend, grading-policy, worker-dispatch, or
provider behavior changes are included.

## Concurrent Outcome Replay

`record_outcome()` keeps its immutable outcome contract. If concurrent callers
write the same source outcome, exactly one insert succeeds. A loser that hits
the primary-key constraint re-reads the owner-scoped row:

- identical content returns the existing outcome with `created=False`;
- different content raises the existing `VersionConflict`;
- missing or wrong-owner sources remain indistinguishable.

The repository must not leak a raw SQLAlchemy `IntegrityError` for an expected
idempotent race.

## Attempt-Fenced Source Registration

Source registration must not rely on a read-then-insert attempt check. The
owner-scoped operation row is locked for the registration transaction on
databases that support row locks. The existing retry path updates the same row,
so registration and retry serialize around the operation attempt. After the
lock is acquired, the repository verifies the expected attempt before inserting
the source.

SQLite keeps its current transactional behavior; PostgreSQL supplies the
cross-process row-lock guarantee required by the production path. Tests cover
the generated locking query and the stable stale-attempt result without adding
process locks.

## Terminal Checkpoint Consistency

`save_operation_checkpoint()` treats a non-null `terminal_summary` as the
attempt's terminal processing CAS write. That write atomically stores the
summary and sets the operation status and completion timestamp. The API gains
an optional `terminal_status` argument. A terminal summary requires a non-empty
status of at most 32 characters other than the active `pending` and `running`
states; a nonterminal checkpoint cannot supply `terminal_status`. This supports
the existing operation-specific terminal processing states such as `ready`,
`done`, and `error` without introducing a second status vocabulary.

Once a terminal summary exists, further checkpoint writes and generic legacy
`update_operation()` mutations are rejected for that attempt. Existing focused
atomic transitions may still advance a durable processing result such as
`ready` to its applied state. Retry still increments the attempt and resets all
checkpoint fields through the existing retry transition. Existing nonterminal
checkpoint callers remain source-compatible.

## Local Cleanup

Delete only the reviewed disposable artifacts from the primary workspace:

- `.agent-collab/`, including its embedded repository, caches, demo notes, and
  test database;
- stale root `AGENT_HANDOFF_CN.md` and `PROJECT_STATUS_REPORT_CN.md`;
- the empty root `package-lock.json`;
- `active_beta_launch/__MACOSX/` metadata.

Keep `active_beta_launch/active_beta_launch/` because it contains the current
role, sequencing, release-gate, and acceptance documents used to judge the
database work. No cleanup files are added to the database feature commit.

## Testing

Tests are added before implementation and observed failing for:

1. concurrent identical outcome writes leaking `IntegrityError`;
2. concurrent different outcome writes not producing `VersionConflict`;
3. registration not locking/fencing the operation attempt;
4. terminal summaries being persisted on nonterminal operations;
5. legacy updates mutating an operation after a terminal summary.

Verification includes the focused source-outcome, checkpoint, migration,
background-workflow, and storage tests; Alembic head inspection; diff checking;
and the configured PostgreSQL tests when `SMARTAI_TEST_POSTGRES_URL` is
available. A skipped live PostgreSQL test is reported as missing runtime
evidence, not as a pass.
