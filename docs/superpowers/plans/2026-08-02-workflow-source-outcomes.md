# Workflow Source Outcomes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement DB-W1-1 durable ordered workflow sources, immutable per-file outcomes, retry/artifact lineage, owner isolation, and batch conservation without changing existing APIs or services.

**Architecture:** Add two normalized tables behind a focused source outcome repository. Each source binds an existing stored file to an existing workflow operation attempt; its optional immutable outcome stores only the frozen domain result. Stored files and workflow operations remain authoritative, and summaries are derived from rows rather than copied into operation JSON.

**Tech Stack:** Python, SQLAlchemy, Alembic, SQLite, PostgreSQL, pytest.

---

## File Structure

- Create backend/db/source_outcome_repository.py for ORM records, DTOs, validation, owner-scoped commands/queries, lineage, and summaries.
- Create backend/db/migrations/versions/0004_workflow_source_outcomes.py for the reversible schema.
- Create backend/tests/test_workflow_source_outcomes.py for repository contract tests.
- Modify backend/db/base.py only to register the new ORM metadata.
- Modify backend/tests/test_migration_roundtrip.py for schema and preservation evidence.
- Modify backend/tests/test_postgres_integration.py for focused PostgreSQL evidence.

No API, Facade, Agent, frontend, main.py, file_repository.py, workflow_repository.py, or migration 0001-0003 file is modified.

### Task 1: Migration and ORM schema

**Files:** migration, source_outcome_repository.py, base.py, test_migration_roundtrip.py.

- [ ] Write a failing migration test that adds workflow_source_items and workflow_source_outcomes to the expected table set and inspects their exact columns, foreign keys, unique constraints, checks, and indexes.
- [ ] Run: python -m pytest backend/tests/test_migration_roundtrip.py -k "normalized_tables or source_outcome_migration" -q
  Expected RED: the two tables do not exist.
- [ ] Add WorkflowSourceItemRecord and WorkflowSourceOutcomeRecord with named constraints matching the approved design. Import the module from base.py so create_all and Alembic metadata include it.
- [ ] Add revision 0004_workflow_source_outcomes with down_revision 0003_assignment_workflow_facade. Create source before outcome; downgrade outcome before source. Do no backfill and touch no old table.
- [ ] Re-run the focused migration tests.
  Expected GREEN: both pass.

### Task 2: Source registration and owner isolation

**Files:** source_outcome_repository.py, test_workflow_source_outcomes.py.

- [ ] Add real repository fixtures for two owners, assignments, workflows, operations, and assignment-linked stored files. Write a failing test for register_source(owner_id, assignment_id, operation_id, expected_attempt, order_index, stored_file_id, retry_of_source_id=None).
- [ ] Assert the returned source includes original name, SHA-256, MIME, size, and storage key from stored_files. Assert absent and wrong-owner resources both raise NotFound with the same code.
- [ ] Run the single test and observe RED because register_source is absent.
- [ ] Implement an immutable source DTO and one-transaction validation of assignment owner, operation owner/assignment/attempt, stored-file owner/assignment, and optional retry parent.
- [ ] Add a PostgreSQL SQL-compilation test proving source registration locks an owner- and assignment-scoped operation row with `FOR UPDATE` before attempt validation.
- [ ] Implement identical replay as created=False. Convert same-position/different-file or lineage collisions to the existing VersionConflict. Use stale_operation_attempt for an owner-scoped operation at another attempt.
- [ ] Add RED/GREEN tests for identical replay, conflict, negative attempt/order validation, and stale attempt.

### Task 3: Immutable outcomes and bounded evidence

**Files:** source_outcome_repository.py, test_workflow_source_outcomes.py.

- [ ] Write a failing test for record_outcome with the four frozen statuses, nullable student candidate, zero matched count, bounded unknown question IDs, stable error code, retryable flag, and optional artifact file.
- [ ] Assert zero and empty lists survive fresh reads; identical replay returns created=False; changed replay raises VersionConflict; wrong-owner and absent sources are indistinguishable.
- [ ] Run the single test and observe RED because record_outcome is absent.
- [ ] Implement validation: only parsed, parse_failed, identity_conflict, no_matching_answer; non-negative count; at most 100 string question IDs; each at most 64 characters; compact UTF-8 JSON at most 8192 bytes.
- [ ] Validate the artifact with owner and assignment predicates. Insert one immutable outcome and compare repeat requests field-for-field.
- [ ] Add real concurrent insert tests: identical writes must return one `created=True` and one `created=False`; differing writes must return one success and one `VersionConflict` without exposing a raw `IntegrityError`.
- [ ] Recover an outcome uniqueness race by rolling back and performing an owner-scoped re-read, then compare every immutable field to classify idempotent replay versus conflict.
- [ ] Add parametrized repository validation tests and direct SQLAlchemy IntegrityError tests for invalid status and negative count. Run and make them GREEN.

### Task 4: Ordered reads, conservation, and lineage

**Files:** source_outcome_repository.py, test_workflow_source_outcomes.py.

- [ ] Write a failing 20-file test: register all 20, then persist 18 parsed, one parse_failed, and one identity_conflict.
- [ ] Assert list_sources returns all 20 in order. Assert summary is uploaded=20, success=18, failed=1, conflict=1, pending=0, complete=True, and uploaded equals the three terminal buckets.
- [ ] Assert an incomplete batch reports pending explicitly and no_matching_answer increments failed without changing its stored status.
- [ ] Run the conservation test and observe RED because list and summary functions are absent.
- [ ] Implement get_source, list_sources, get_outcome, and summarize_sources with owner predicates in every SQL query and an outer join for pending rows.
- [ ] Write a failing retry test using the existing create_operation retry behavior. Register the next-attempt source with retry_of_source_id and verify its predecessor and artifact linkage.
- [ ] Implement the minimal owner-scoped lineage composition and run all source outcome tests GREEN.

### Task 5: Migration preservation and PostgreSQL evidence

**Files:** test_migration_roundtrip.py, test_postgres_integration.py.

- [ ] Write a 0003 to 0004 to 0003 to 0004 test. Seed owner/course/assignment/workflow/operation/stored-file at 0003; verify those exact rows survive upgrade and downgrade; verify the recreated new tables are empty and usable.
- [ ] Run the focused migration test. If it exposes downgrade ordering or constraint defects, correct only migration 0004 and re-run.
- [ ] Add PostgreSQL offline DDL assertions for both tables, portable Boolean DDL, and outcome-before-source downgrade ordering.
- [ ] Add a pg_database repository test covering register, outcome, summary, and wrong-owner behavior. Preserve the existing explicit skip when SMARTAI_TEST_POSTGRES_URL is absent.
- [ ] Run: python -m pytest backend/tests/test_migration_roundtrip.py -q
- [ ] Run: python -m pytest backend/tests/test_postgres_integration.py -k source_outcome -q
  Expected: pass when PostgreSQL exists; otherwise report the explicit skip as missing local PostgreSQL runtime evidence.

### Task 6: Contract and regression verification

**Files:** review only.

- [ ] Run git diff against the implementation baseline and verify no task_facade.py, main.py, API, Agent, frontend, workflow_repository.py, file_repository.py, or old migration changed.
- [ ] Run: python -m pytest backend/tests/test_workflow_source_outcomes.py backend/tests/test_storage_persistence.py backend/tests/test_task_background_workflows.py backend/tests/test_migration_roundtrip.py -q
- [ ] Run: python -m pytest backend/tests -q
- [ ] Run: python -m alembic heads
  Expected: only 0004_workflow_source_outcomes is head.
- [ ] Run git diff --check and inspect git status. Preserve unrelated untracked user files.
- [ ] Stage only the six implementation files listed in this plan and commit as feat: persist workflow source outcomes.
- [ ] Stop for DB-W1-1 schema/repository Review. Do not begin DB-W1-2.
