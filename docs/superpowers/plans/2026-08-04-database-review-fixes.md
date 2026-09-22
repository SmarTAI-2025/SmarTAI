# Database Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make source/outcome writes concurrency-safe, make terminal checkpoint state internally consistent, and remove only the approved disposable local artifacts.

**Architecture:** Preserve the existing DB-W1-1 and DB-W1-2 repository APIs. Resolve expected unique-key races by owner-scoped replay reads, serialize source registration against attempt retries with an operation row lock, and make terminal checkpoint summary/status/timestamp one CAS write while retaining focused applied-state transitions.

**Tech Stack:** Python, SQLAlchemy 2, Alembic, SQLite, PostgreSQL, pytest, Git worktrees.

---

## File Structure

- Modify `backend/db/source_outcome_repository.py` for concurrent outcome replay and operation row locking.
- Modify `backend/db/workflow_repository.py` for terminal checkpoint status validation and atomic persistence.
- Modify `backend/tests/test_workflow_source_outcomes.py` for concurrent replay and PostgreSQL locking-contract tests.
- Modify `backend/tests/test_workflow_operation_checkpoints.py` for terminal status consistency and legacy mutation guards.
- Modify the existing DB-W1-1/DB-W1-2 design and plan documents to reflect the corrected contracts.
- Delete only approved disposable files from the primary workspace; do not stage cleanup in the database branch.

### Task 1: Concurrent immutable outcome replay

**Files:**
- Modify: `backend/tests/test_workflow_source_outcomes.py`
- Modify: `backend/db/source_outcome_repository.py`

- [ ] **Step 1: Add concurrent replay tests**

Add a real `ThreadPoolExecutor` test that starts two identical `record_outcome()` calls behind a barrier and asserts one `(created=True)` plus one `(created=False)`. Add a second test with differing stable error codes and assert one success plus one `VersionConflict`.

```python
def write_outcome(error_code: str | None):
    barrier.wait()
    try:
        _row, created = source_outcome_repository.record_outcome(
            source_id=source.id,
            owner_id=owner_id,
            status="parsed",
            student_candidate=None,
            matched_answer_count=1,
            unknown_question_ids=[],
            stable_error_code=error_code,
            retryable=False,
        )
        return "saved", created
    except VersionConflict as exc:
        return "conflict", exc.code
```

- [ ] **Step 2: Run the tests and observe RED**

Run:

```powershell
python -m pytest backend/tests/test_workflow_source_outcomes.py -k "concurrent_outcome" -q
```

Expected: identical replay exposes `IntegrityError`; differing replay does not yet return the stable conflict contract.

- [ ] **Step 3: Implement owner-scoped conflict recovery**

Move the outcome insert into a `try` block. On `IntegrityError`, open a fresh transaction, join outcome to source with the owner predicate, and compare all immutable fields:

```python
except IntegrityError:
    with session_scope() as session:
        existing = session.scalar(
            select(WorkflowSourceOutcomeRecord)
            .join(WorkflowSourceItemRecord)
            .where(
                WorkflowSourceOutcomeRecord.source_id == source_id,
                WorkflowSourceItemRecord.owner_id == owner_id,
            )
        )
        if existing is None:
            raise NotFound("workflow_source")
        if not _same_outcome(existing, ...):
            raise VersionConflict("Workflow source outcome already exists.")
        return _outcome_dto(existing), False
```

- [ ] **Step 4: Run focused and file-level tests GREEN**

```powershell
python -m pytest backend/tests/test_workflow_source_outcomes.py -k "concurrent_outcome" -q
python -m pytest backend/tests/test_workflow_source_outcomes.py -q
```

Expected: all selected tests pass without raw SQLAlchemy exceptions.

### Task 2: Attempt-fenced source registration

**Files:**
- Modify: `backend/tests/test_workflow_source_outcomes.py`
- Modify: `backend/db/source_outcome_repository.py`

- [ ] **Step 1: Add the locking-contract test**

Compile the owner-scoped operation query for PostgreSQL and assert it ends in `FOR UPDATE`. Keep the existing stale-attempt behavior test as the functional assertion.

```python
statement = source_outcome_repository._owned_operation_for_update_statement(
    operation_id="op-1",
    assignment_id="assignment-1",
    owner_id="owner-1",
)
sql = str(statement.compile(dialect=postgresql.dialect()))
assert "FOR UPDATE" in sql
```

- [ ] **Step 2: Run the locking test and observe RED**

```powershell
python -m pytest backend/tests/test_workflow_source_outcomes.py -k "operation_row_lock" -q
```

Expected: helper is absent or generated SQL lacks `FOR UPDATE`.

- [ ] **Step 3: Add and use the locked query**

Create one focused helper and use it in `register_source()` before validating the attempt:

```python
def _owned_operation_for_update_statement(*, operation_id, assignment_id, owner_id):
    return (
        select(WorkflowOperationRecord)
        .where(
            WorkflowOperationRecord.id == operation_id,
            WorkflowOperationRecord.assignment_id == assignment_id,
            WorkflowOperationRecord.owner_id == owner_id,
        )
        .with_for_update()
    )
```

The transaction then checks `operation.attempt == expected_attempt` and inserts the source while retaining the lock.

- [ ] **Step 4: Run source tests GREEN**

```powershell
python -m pytest backend/tests/test_workflow_source_outcomes.py -k "operation_row_lock or stale" -q
python -m pytest backend/tests/test_workflow_source_outcomes.py -q
```

### Task 3: Atomic terminal checkpoint state

**Files:**
- Modify: `backend/tests/test_workflow_operation_checkpoints.py`
- Modify: `backend/db/workflow_repository.py`

- [ ] **Step 1: Add terminal consistency tests**

Update terminal writes to supply `terminal_status="done"`. Assert one CAS write persists `status == "done"`, non-null `completed_at`, and the summary. Add validation tests for summary without status, status without summary, active terminal statuses, and status length. Add a test that generic `update_operation()` rejects mutation after a terminal summary.

```python
saved = workflow_repository.save_operation_checkpoint(
    operation.id,
    owner_id=owner_id,
    expected_attempt=operation.attempt,
    expected_checkpoint_revision=0,
    stage="completed",
    checkpoint={"processed_sources": 20},
    terminal_summary=terminal,
    terminal_status="done",
)
assert saved.status == "done"
assert saved.completed_at is not None
```

- [ ] **Step 2: Run terminal tests and observe RED**

```powershell
python -m pytest backend/tests/test_workflow_operation_checkpoints.py -k "terminal" -q
```

Expected: `terminal_status` is not accepted and pending operations can still carry a terminal summary.

- [ ] **Step 3: Implement terminal status validation and CAS values**

Add `terminal_status: str | None = None`. Validate the paired fields before opening a transaction:

```python
if (terminal_summary is None) != (terminal_status is None):
    raise ValidationError("Terminal summary and status must be supplied together.", code="invalid_operation_terminal_state")
if terminal_status is not None and (
    not isinstance(terminal_status, str)
    or not terminal_status
    or len(terminal_status) > 32
    or terminal_status in {"pending", "running"}
):
    raise ValidationError("Invalid terminal operation status.", code="invalid_operation_terminal_status")
```

When terminal fields are present, include `status=terminal_status` and `completed_at=now` in the existing revision-and-attempt CAS update.

- [ ] **Step 4: Guard generic legacy updates**

Add `WorkflowOperationRecord.terminal_summary.is_(None)` to generic `update_operation()` and classify a failed matching-attempt write with a terminal summary as `InvalidTransition(code="operation_already_terminal")`. Keep retry resetting terminal fields through `create_operation()`.

- [ ] **Step 5: Run checkpoint and background workflow tests GREEN**

```powershell
python -m pytest backend/tests/test_workflow_operation_checkpoints.py -q
python -m pytest backend/tests/test_task_background_workflows.py -q
```

### Task 4: Contract documentation

**Files:**
- Modify: `docs/superpowers/specs/2026-08-02-workflow-source-outcomes-design.md`
- Modify: `docs/superpowers/plans/2026-08-02-workflow-source-outcomes.md`
- Modify: `docs/superpowers/specs/2026-08-04-workflow-operation-checkpoints-design.md`
- Modify: `docs/superpowers/plans/2026-08-04-workflow-operation-checkpoints.md`

- [ ] **Step 1: Document concurrency behavior**

State that concurrent identical outcome inserts replay as `created=False`, differing inserts conflict, and source registration locks the operation row on PostgreSQL before checking attempt.

- [ ] **Step 2: Document terminal pairing**

Add `terminal_status` to the repository signature and specify paired validation, atomic status/summary/timestamp persistence, generic mutation rejection, and controlled applied-state transitions.

- [ ] **Step 3: Check documentation consistency**

```powershell
rg -n "terminal_status|concurrent.*outcome|FOR UPDATE|IntegrityError" docs/superpowers
git diff --check
```

Expected: design and plan use the same signature and error codes.

### Task 5: Approved local cleanup

**Files:**
- Delete: `.agent-collab/`
- Delete: `AGENT_HANDOFF_CN.md`
- Delete: `PROJECT_STATUS_REPORT_CN.md`
- Delete: `package-lock.json`
- Delete: `active_beta_launch/__MACOSX/`
- Preserve: `active_beta_launch/active_beta_launch/`

- [ ] **Step 1: Resolve and verify deletion targets**

Use `Resolve-Path` for every target and confirm each resides under `D:\project-of-python\Teacher\SmarTAI`. Confirm the preserved directory contains the 12 reviewed Markdown files.

- [ ] **Step 2: Delete only approved targets with PowerShell**

Use `Remove-Item -LiteralPath` on the exact resolved targets. Use `-Recurse` only for `.agent-collab` and `active_beta_launch/__MACOSX`.

- [ ] **Step 3: Verify cleanup**

```powershell
git status --short --untracked-files=all
Get-ChildItem -LiteralPath active_beta_launch\active_beta_launch -File
```

Expected: only the preserved 12-document work package remains untracked in the primary workspace.

### Task 6: Regression verification and commits

**Files:** review only.

- [ ] **Step 1: Run database regression tests**

```powershell
python -m pytest backend/tests/test_workflow_source_outcomes.py backend/tests/test_workflow_operation_checkpoints.py backend/tests/test_task_background_workflows.py backend/tests/test_storage_persistence.py backend/tests/test_migration_roundtrip.py -q
```

- [ ] **Step 2: Run PostgreSQL evidence**

```powershell
python -m pytest backend/tests/test_postgres_integration.py -k "source_outcome or operation_checkpoint" -q
```

Expected: pass with `SMARTAI_TEST_POSTGRES_URL`; otherwise two explicit skips.

- [ ] **Step 3: Inspect migration and diff state**

```powershell
python -m alembic heads
git diff --check
git status --short --branch
```

Expected: one head, `0005_operation_checkpoints`; only planned database/test/doc files changed.

- [ ] **Step 4: Commit implementation separately**

Stage only repository, focused test, and contract document files. Commit with:

```powershell
git commit -m "fix: harden workflow persistence races"
```

Do not stage or commit primary-workspace cleanup artifacts.
