# Workflow Operation Checkpoints Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete DB-W1-2 with bounded, attempt-fenced operation checkpoints and atomic checkpoint revision CAS while preserving all existing interfaces.

**Architecture:** Add five checkpoint columns to the existing `workflow_operations` row and one additive repository command. The command validates bounded recovery metadata and owner-scoped artifact references, then performs a single attempt-and-revision conditional update; existing operation creation and mutation APIs remain compatible.

**Tech Stack:** Python, SQLAlchemy, Alembic, SQLite, PostgreSQL, pytest.

---

## File Structure

- Create `backend/db/migrations/versions/0006_workflow_operation_checkpoints.py` for additive checkpoint columns and reversible SQLite/PostgreSQL migration behavior.
- Create `backend/tests/test_workflow_operation_checkpoints.py` for the focused repository contract.
- Modify `backend/db/workflow_repository.py` for ORM fields, bounded JSON validation, retry reset, and checkpoint CAS.
- Modify `backend/tests/test_migration_roundtrip.py` for schema/default/constraint/preservation evidence.
- Modify `backend/tests/test_postgres_integration.py` for PostgreSQL DDL and live persistence/concurrency evidence.

No API, Facade, Agent, OCR, frontend, `main.py`, file repository, source outcome repository, or old migration changes are permitted.

### Task 1: Checkpoint migration and ORM state

- [ ] Add a failing migration test asserting `workflow_operations` has `checkpoint_revision`, `checkpoint_stage`, `checkpoint`, `artifact_refs`, and `terminal_summary`, plus a named non-negative revision check.
- [ ] Run `python -m pytest backend/tests/test_migration_roundtrip.py -k operation_checkpoint -q` and observe RED because revision `0005` and the columns do not exist.
- [ ] Add the five ORM fields with Python defaults matching the schema.
- [ ] Add migration revision `0006_operation_checkpoints`, using batch alter for SQLite compatibility, server defaults for existing rows, and a downgrade that removes only the new columns.
- [ ] Re-run the focused migration test and make it GREEN.

### Task 2: Bounded creation and legacy updates

- [ ] Write failing repository tests that `create_operation()` rejects a payload over 4 MiB and progress over 64 KiB, and that `update_operation()` rejects the same shapes without changing the persisted row.
- [ ] Run the focused tests and observe RED because the existing repository has no bounds.
- [ ] Add one compact UTF-8 JSON size validator that requires dict values, rejects non-serializable/non-finite JSON, and raises field-specific `ValidationError` codes.
- [ ] Apply validation before opening the write transaction in both creation/retry and legacy update paths. Keep the legacy creation and update signatures unchanged; the additive checkpoint API may gain optional checkpoint-specific arguments.
- [ ] Re-run the focused tests and existing background workflow tests GREEN.

### Task 3: Checkpoint CAS and owner isolation

- [ ] Write a failing test for `save_operation_checkpoint()` from revision zero, asserting revision one, stage, checkpoint, ordered de-duplicated artifact references, and fresh-read persistence.
- [ ] Assert a missing or wrong-owner operation raises the same `NotFound`; assert missing and wrong-owner artifacts also share `NotFound("stored_file")`.
- [ ] Run the test and observe RED because the function is absent.
- [ ] Implement stage, checkpoint, artifact list, terminal summary, and paired terminal status validation. Require summary and status together; status must be non-empty, at most 32 characters, and cannot be `pending` or `running`. Validate each stored file with owner and assignment predicates in the checkpoint transaction.
- [ ] Implement a single conditional update matching operation ID, owner ID, expected attempt, and expected checkpoint revision, incrementing the revision expression atomically.
- [ ] Classify a failed update as `NotFound`, `stale_operation_attempt`, or `stale_checkpoint_revision` using an owner-scoped re-read. Run the tests GREEN.

### Task 4: Concurrent CAS, terminal summary, and retry fencing

- [ ] Write a failing concurrency test where two repository calls use revision zero and assert exactly one success plus one `stale_checkpoint_revision` conflict.
- [ ] Write failing terminal tests: reject unpaired or invalid terminal status; save summary and status atomically with `completed_at`; read the state repeatedly; then reject another checkpoint and generic `update_operation()` with `operation_already_terminal`.
- [ ] Write a failing retry test: fail and recreate the same operation, assert attempt increments and all checkpoint fields reset, then assert the old attempt receives `stale_operation_attempt`.
- [ ] Run each test in RED before filling the missing behavior.
- [ ] Add the terminal predicate and retry reset to the existing conditional updates. Make the terminal CAS persist summary, status, and completion timestamp together; fence generic operation mutation after a terminal summary while preserving focused atomic terminal transitions such as `ready -> applied`.
- [ ] Run all checkpoint and background workflow tests GREEN.

### Task 5: Migration preservation and PostgreSQL evidence

- [ ] Add a failing `0004 -> 0005 -> 0004 -> 0005` migration test that seeds an operation at `0004`, checks defaults at `0005`, writes checkpoint values, downgrades without changing legacy operation fields, and upgrades cleanly again.
- [ ] Add PostgreSQL offline DDL assertions for the five columns and named revision check.
- [ ] Add a configured live PostgreSQL test covering initial state, checkpoint persistence, owner isolation, and one-winner CAS. Preserve the explicit skip when `SMARTAI_TEST_POSTGRES_URL` is absent.
- [ ] Implement only migration fixes exposed by these tests, then run both focused files GREEN or report the explicit PostgreSQL environment skip.

### Task 6: Week 1 verification and commit

- [ ] Re-read the DB-W1-1 and DB-W1-2 acceptance lists and map every item to a test or a stated integration boundary.
- [ ] Run `python -m pytest backend/tests/test_workflow_operation_checkpoints.py backend/tests/test_workflow_source_outcomes.py backend/tests/test_task_background_workflows.py backend/tests/test_storage_persistence.py backend/tests/test_migration_roundtrip.py -q`.
- [ ] Run the complete backend suite in stable groups if the repository's known OmniDocBench environment test still contains its hard-coded Linux interpreter; distinguish baseline failure from changes.
- [ ] Run `python -m alembic heads`, expecting only `0006_operation_checkpoints`.
- [ ] Run `git diff --check` and inspect the diff for forbidden files or changed existing signatures.
- [ ] Commit the DB-W1-2 implementation separately from DB-W1-1 and stop before Week 2 lease work.
