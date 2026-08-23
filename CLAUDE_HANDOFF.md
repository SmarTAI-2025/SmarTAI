# Claude Collaboration Handoff

## Current Task (2026-08-13): Database Week 2 recovery

Complete DB-W2-1 through DB-W2-4 from
`active_beta_launch/active_beta_launch/数据库持久化与任务恢复工作安排.md`.
The detailed implementation plan is
`docs/superpowers/plans/2026-08-13-database-week2-recovery.md`.

### Baseline and delivery

- Worktree: `D:\project-of-python\Teacher\SmarTAI\.worktrees\db-operation-leases`
- Current branch: `codex/db-operation-leases`
- Baseline: `6a0f76b`, the head of open stacked PR #18.
- PR #18 depends on PR #17; do not rebase this work directly onto `main` until
  those dependencies merge.
- Deliver Week 2 as sequential commits/stacked branches. Do not commit or push;
  Codex will review, verify, commit, push, and create PRs.
- Preserve unrelated work and do not modify root `render-requirements.txt` or
  untracked `active_beta_launch/` content.

### Mandatory scope and process

- Execute only the plan's current task when Codex names one.
- Use TDD: add the focused test, run it and record the intended RED, then make
  the smallest implementation and record GREEN.
- Reuse `workflow_operations`, normalized repositories, stored-file metadata,
  and object storage. Never create another JobStore/TaskStore or put original
  bytes, full OCR text, raw model responses, or provider keys in operation JSON.
- Keep owner predicates, attempt fencing, lease-token fencing, bounded JSON,
  idempotent commits, and stable error codes explicit.
- Do not change OCR output semantics, student identity/matching rules, grading
  semantics, frontend behavior, quota policy, or public API meaning.
- PR #23 and PR #26 overlap `task_facade.py` and tests. Do not copy their
  unmerged business behavior into this branch; report the exact conflict/rebase
  points instead.

### Current implementation assignment: DB-W2-2A worker core only

Implement only the worker-core half of Task 2 from the Week 2 plan.
The current branch is `codex/db-workflow-worker`, based on W2-1 commit
`740bf02` / PR #34. Allowed production changes are focused settings in
`backend/config.py` and a single-purpose `backend/services/workflow_worker.py`;
allowed tests are `backend/tests/test_workflow_worker.py`. Do not modify
`backend/main.py` in this assignment. Do not register any of the four
production operation types yet:
their handlers do not become durable until W2-3/W2-4. Tests may use synthetic
handler names. Do not modify `task_facade.py`, OCR/Agent code, or public APIs.

Implement a small `WorkflowWorker` with an explicit immutable handler mapping,
one poll/tick API, a continuous run API, per-claim heartbeat lifecycle, bounded
in-flight task tracking, and stop/shutdown APIs. It must poll only registered
types, tolerate another worker winning a listed row, stop handler mutation on
`LeaseLost`, map handler failures without exposing payloads, cancel/await its
own tasks on shutdown, and never bulk-clear database leases. Keep the API easy
for a later FastAPI lifespan wrapper. Do not implement that wrapper now.

Record modified files, RED/GREEN commands and results, design concerns, and
remaining verification gaps in a new `Claude -> Codex (DB-W2-2)` section.

## Claude -> Codex (DB-W2-2A worker core)

Claude Code produced the settings, worker core, and focused tests, but its
non-interactive command again reached the outer timeout before returning a
summary. Codex stopped the orphaned process and reviewed the implementation.

### Codex review corrections

- Corrected the checkpoint test: terminal completion is itself checkpoint
  revision 3, not revision 2.
- Made shutdown bounded with `workflow_shutdown_seconds`; handlers that swallow
  cancellation leave their database lease to expire instead of blocking app
  shutdown indefinitely.
- A heartbeat infrastructure error now fences/cancels the handler once the
  locally confirmed lease deadline passes. The worker no longer continues paid
  work indefinitely when it cannot prove lease ownership.
- Removed the worker's reverse import of private `task_facade._SAFE_ERROR_CODES`.
  Typed `DomainError` codes remain stable; unexpected exceptions use
  `workflow_failed` without importing the facade or logging exception payloads.

### Verification

- Review RED command for bounded shutdown, uncertain heartbeat expiry, and
  facade independence: `3 failed` for the intended missing behaviors.
- Same focused command after fixes: `3 passed in 3.10s`.
- `python -m pytest backend/tests/test_workflow_worker.py -q`:
  `26 passed in 28.94s`.
- `python -m pytest backend/tests/test_workflow_operation_leases.py backend/tests/test_workflow_operation_checkpoints.py backend/tests/test_task_background_workflows.py -q`:
  `65 passed in 86.18s`.
- `git diff --check`: passed; only CRLF conversion notices were emitted.

W2-2B FastAPI lifespan wiring is intentionally not included in this core
commit. No production operation handler is registered yet.

## Codex verification (DB-W2-2B application lifecycle)

The application now starts one empty-handler `WorkflowWorker` loop per process
and stops it before bounded worker shutdown. Because the handler mapping is
empty, this commit cannot claim any production operation. Shutdown never calls
`release_operation`; active leases are left to expire.

- Lifecycle RED: `test_app_starts_and_stops_empty_workflow_worker` failed
  because no worker was constructed.
- Lifecycle GREEN: `python -m pytest backend/tests/test_workflow_worker_lifecycle.py -q`
  -> `2 passed`.
- Combined worker/app/grading regression:
  `python -m pytest backend/tests/test_workflow_worker.py backend/tests/test_workflow_worker_lifecycle.py backend/tests/test_auth_persistence.py backend/tests/test_grading_run_lifecycle.py -q`
  -> `69 passed, 30 warnings in 91.33s`.

The warnings are FastAPI's `on_event` deprecation notices. The repository
already uses `on_event` for sandbox and grading worker lifecycle; converting
all lifecycle hooks to a lifespan context is deferred to a focused integration
cleanup rather than mixed into DB-W2-2.

### Codex review fixes required after timed-out first pass

The first Claude invocation timed out while tests were still running and did
not write its required summary. Preserve the useful implementation, but fix
these issues with focused RED tests before declaring DB-W2-1 complete:

1. `claim_operation` currently treats a live lease held by the same
   `worker_id` as claimable and rotates its token. Two concurrent coroutines in
   one process can therefore both receive a successful claim. Any live lease,
   including one with the same worker ID, must reject a second claim; only an
   unleased or expired row may be claimed/reclaimed.
2. The lease consistency check allows null owner/token with non-null expiry or
   heartbeat. Tighten it so an inactive lease has all four lease fields null,
   while an active lease has owner, token, expiry, and heartbeat non-null.
3. Validate non-empty bounded `worker_id` and `lease_token` inputs before SQL,
   using stable validation codes rather than leaking database/string-length
   failures. Add boundary tests.
4. Run the focused tests in a command that terminates; diagnose the previous
   hang if it repeats. Record exact RED and GREEN outputs plus all modified
   files and the live-PostgreSQL skip/result in `Claude -> Codex (DB-W2-1)`.

Do not start DB-W2-2, commit, or push.

## Claude -> Codex (DB-W2-1)

Both non-interactive Claude Code invocations wrote their changes to disk but
hit the outer 15-minute and 10-minute command timeouts before returning a
final response. Codex stopped the orphaned task processes, reviewed the diff,
and performed independent verification. The missing Claude final response is
a process gap; no implementation result is inferred from it.

### Implemented files

- `backend/db/migrations/versions/0008_operation_leases.py`
- `backend/db/workflow_repository.py`
- `backend/tests/test_workflow_operation_leases.py`
- `backend/tests/test_migration_roundtrip.py`
- `backend/tests/test_postgres_integration.py`

The change adds a four-field operation lease with a consistency constraint,
claim index, random token rotation on reclaim, atomic claim/heartbeat/release,
bounded claim polling, and attempt-plus-token fencing for checkpoint and
terminal writes. New attempts clear all lease state.

### Codex review correction

The first pass allowed a live lease to be claimed again by the same
`worker_id`. Codex rejected that behavior because two coroutines in one process
share a worker ID and could both receive success. The corrected predicate
allows only unleased or expired rows; focused tests cover sequential and
concurrent same-worker claims. The lease check was also tightened so all four
lease fields are either null or non-null, and worker/token inputs now use
stable validation errors.

### Independent verification

- `python -m pytest backend/tests/test_workflow_operation_leases.py -q`:
  `25 passed in 21.60s`.
- `python -m pytest backend/tests/test_workflow_operation_checkpoints.py backend/tests/test_workflow_source_outcomes.py backend/tests/test_task_background_workflows.py backend/tests/test_migration_roundtrip.py -q`:
  `79 passed, 34 warnings in 120.70s`; warnings are the existing Alembic
  `path_separator` deprecation warning.
- `python -m pytest backend/tests/test_postgres_integration.py -q -rs`:
  `8 skipped`; `SMARTAI_TEST_POSTGRES_URL` is not configured locally.
- `python -m alembic heads`: `0008_operation_leases (head)` after integration
  with the source-outcome diagnostics migration already on `main`.
- `git diff --check`: passed; only CRLF conversion notices for this handoff
  file were emitted by subsequent diff commands.

### Remaining gap

The live PostgreSQL one-winner and same-worker fencing tests were added but
could not run locally. GitHub Actions or a configured PostgreSQL test service
must execute them before merge. W2-2 is not included in this change.

## Current Task

Fix the three failed GitHub Actions jobs for commit
`62b46893481c45493d660dcb6193b1c4ad5097ba` on branch
`codex/normalized-learning-workflow-code-only`.

The user explicitly requested that terminal Claude Code implement the fix.
Do not commit, push, or modify unrelated user-owned untracked files.

## Codex -> Claude

### Evidence and root causes

1. `Backend (SQLite)` never starts the test suite:
   `/opt/hostedtoolcache/Python/3.12.13/x64/bin/python: No module named pytest`.
   The workflow installs `render-requirements.txt`, which does not include
   `pytest`.
2. `Backend (PostgreSQL)` fails at `alembic upgrade head` because PostgreSQL
   rejects integer defaults for boolean columns. The first failure is
   `users.is_active BOOLEAN DEFAULT 1`; the baseline migration also uses
   integer boolean defaults for `provider_configs.enabled` and
   `grade_results.requires_review`.
3. `E2E (Playwright)` starts Uvicorn successfully, but `wait-on` repeatedly
   sends `HEAD /ready`. FastAPI returns `405 Method Not Allowed` because the
   route is GET-only, so the readiness wait times out after 30 seconds.

### Scope

- Fix only the CI dependency declaration, PostgreSQL-compatible migration
  boolean defaults, and E2E readiness probe.
- Add focused regression tests before production/config changes where
  practical. Run each new test and confirm it fails for the intended reason
  before applying the fix.
- Prefer a CI-side explicit GET readiness probe rather than broadening the
  application's endpoint semantics solely for `wait-on`.
- Ensure all boolean defaults in the baseline migration are portable between
  SQLite and PostgreSQL, not only the first failing column.
- Preserve existing behavior and avoid unrelated refactors.
- Do not touch `.agent-collab/`, `AGENT_HANDOFF_CN.md`,
  `PROJECT_STATUS_REPORT_CN.md`, or the root `package-lock.json`.

### Acceptance criteria

- The backend CI environment installs `pytest` before invoking it.
- The PostgreSQL form of the baseline migration emits valid boolean defaults
  for every boolean column while remaining valid on SQLite.
- The E2E readiness check uses HTTP GET and recognizes `/ready` as available.
- Focused regression tests pass.
- `python -m pytest backend/tests -q` passes locally.
- Frontend typecheck/unit/build commands remain passing if affected.
- Record modified files, red/green commands and results, remaining verification
  gaps, and any risks in `Claude -> Codex` below.

## Claude -> Codex

Claude implemented the scoped CI fixes in:

- `.github/workflows/ci.yml`
- `backend/db/migrations/versions/0001_normalized_learning.py`
- `backend/tests/test_migration_roundtrip.py`
- `backend/tests/test_ci_workflow.py`

The terminal session did not return its final summary before it was stopped
after producing no output for several minutes. The changes were independently
reviewed and verified by Codex. No commit or push was performed.

Independent verification:

- `python -m pytest backend/tests/test_migration_roundtrip.py::test_postgresql_upgrade_uses_portable_boolean_defaults backend/tests/test_ci_workflow.py -q`: `3 passed`.
- CI-equivalent SQLite suite: `python -m pytest backend/tests -q`: `200 passed, 4 skipped`.
- Frontend `npm run audit:scope`: passed.
- Frontend `npm run typecheck`: passed.
- Frontend `npm test`: `10 files, 34 tests passed`.
- Frontend `npm run build`: passed.

The red-test output from the interrupted Claude session was not observed, so
that part of the TDD audit remains a process gap; the focused regression tests
and full verification are green.

## Follow-up: CI Run 29800660868

The first pushed fix correctly made E2E and frontend jobs pass, but exposed two
additional dependency/schema-boundary failures:

1. SQLite now installs `pytest`, but 41 async tests fail with `async def
   functions are not natively supported` and `PytestUnknownMarkWarning` for
   `pytest.mark.asyncio`. The CI test environment also needs
   `pytest-asyncio`.
2. PostgreSQL migration succeeds, but the PostgreSQL integration fixture calls
   `Base.metadata.create_all()`. ORM columns in `backend/db/models.py` still
   emit `BOOLEAN DEFAULT 1/0` for `UserRecord.is_active`,
   `ProviderConfigRecord.enabled`, and `GradeResultRecord.requires_review`.
   PostgreSQL rejects the integer default before the integration tests run.

Implement only this follow-up. Add focused regression tests first and observe
the expected failures, then make the minimal fixes. Update the Claude -> Codex
section with the new verification results and remaining gaps. Do not commit or
push; Codex will review and integrate.

## Claude -> Codex (Follow-up: CI Run 29800660868)

Claude implemented the follow-up with TDD (red → green) for the two diagnosed
failures. No commit or push was performed.

### Modified files

- `.github/workflows/ci.yml` — both backend jobs (`backend-sqlite`,
  `backend-postgres`) now install `pytest-asyncio` alongside `pytest` in their
  `Install dependencies` step. The E2E job is unaffected (it does not run the
  pytest suite).
- `backend/db/models.py` — three ORM boolean `server_default` values switched
  from integer literals to portable boolean literals so
  `Base.metadata.create_all()` agrees with the already-fixed Alembic baseline
  migration on PostgreSQL:
  - `UserRecord.is_active`: `text("1")` → `text("true")`
  - `ProviderConfigRecord.enabled`: `text("1")` → `text("true")`
  - `GradeResultRecord.requires_review`: `text("0")` → `text("false")`
- `backend/tests/test_ci_workflow.py` — added
  `test_sqlite_backend_job_installs_pytest_asyncio` and
  `test_postgres_backend_job_installs_pytest_asyncio`. They parse the workflow
  YAML and assert each backend job's `Install dependencies` step contains
  `pytest-asyncio`.
- `backend/tests/test_orm_postgres_boolean_defaults.py` (new) — renders
  `Base.metadata.create_all()` DDL for the PostgreSQL dialect via
  `CreateTable(...).compile(dialect=postgresql.dialect())` and asserts the
  three boolean columns emit `DEFAULT true` / `DEFAULT false` rather than
  `DEFAULT 1` / `DEFAULT 0`.

### TDD red phase (observed before fixes)

`python -m pytest backend/tests/test_ci_workflow.py::test_sqlite_backend_job_installs_pytest_asyncio backend/tests/test_ci_workflow.py::test_postgres_backend_job_installs_pytest_asyncio backend/tests/test_orm_postgres_boolean_defaults.py -q`:
`3 failed`.

- `test_sqlite_backend_job_installs_pytest_asyncio`: failed because the
  SQLite job's install step contained `pip install pytest` but not
  `pytest-asyncio`.
- `test_postgres_backend_job_installs_pytest_asyncio`: same root cause for the
  PostgreSQL job.
- `test_orm_boolean_columns_emit_portable_postgres_defaults`: failed with
  `assert '1' == 'true'` for `is_active`, confirming the ORM metadata rendered
  `BOOLEAN DEFAULT 1` on PostgreSQL.

### Green verification

- New regression tests:
  `python -m pytest backend/tests/test_ci_workflow.py::test_sqlite_backend_job_installs_pytest_asyncio backend/tests/test_ci_workflow.py::test_postgres_backend_job_installs_pytest_asyncio backend/tests/test_orm_postgres_boolean_defaults.py -q`:
  `3 passed`.
- CI-equivalent SQLite suite:
  `python -m pytest backend/tests -q`: `203 passed, 4 skipped` (was
  `200 passed, 4 skipped`; the three new tests account for the increase).
- Migration + ORM DDL guards together:
  `python -m pytest backend/tests/test_migration_roundtrip.py backend/tests/test_ci_workflow.py backend/tests/test_orm_postgres_boolean_defaults.py -v`:
  `10 passed`.
- Async-heavy files (confirm async infrastructure still works):
  `python -m pytest backend/tests/test_calculation_skill.py backend/tests/test_programming_skill.py backend/tests/test_multi_expert_robustness.py backend/tests/test_rag_inmemory.py -q`:
  `50 passed`.
- SQLite DDL portability check (manual): `CreateTable(...).compile(dialect=sqlite.dialect())`
  renders `BOOLEAN DEFAULT true` / `DEFAULT false` for the three columns, which
  SQLite accepts as aliases for `1`/`0`.

### Risks

- A live PostgreSQL service was not available locally (Docker unavailable), so
  the PostgreSQL `create_all()` path is covered by the dialect-rendered DDL
  regression test rather than a live run. The Alembic migration's
  `test_postgresql_upgrade_uses_portable_boolean_defaults` already covers the
  migration DDL; the new test covers the ORM `create_all()` DDL so both schema
  producers are pinned to portable literals.
- `pytest-asyncio` is installed without a version pin in CI, matching the
  existing un-pinned `pytest` install. If a future `pytest-asyncio` release
  changes the default `asyncio_mode` or breaks the `@pytest.mark.asyncio` mark
  handling, the async tests could regress. The project has no `pyproject.toml`
  / `pytest.ini` `asyncio_mode` setting today; the async tests currently rely
  on `@pytest.mark.asyncio` decorators, which work under the default mode.

### Remaining verification gaps

- The live PostgreSQL service-migration job and the
  `backend/tests/test_postgres_integration.py` fixture (which calls
  `Base.metadata.create_all()` indirectly via `create_schema` in other tests,
  and runs `alembic upgrade head` in its own fixture) were not executed
  against a real PostgreSQL instance locally. They are covered by the
  dialect-rendered DDL tests; final confirmation requires the GitHub Actions
  PostgreSQL service container.
- The E2E (Playwright) job was not in scope for this follow-up and was not
  re-run; the previous fix (explicit `curl` GET probe for `/ready`) is
  unchanged.

## Codex Review

The diff matches the diagnosed failures. CI installs `pytest` explicitly in
the two backend jobs that invoke it, the migration uses PostgreSQL boolean
literals for all three boolean columns, and E2E polls `/ready` with explicit
GET requests instead of `wait-on`'s HEAD probe. No unrelated user-owned files
were changed. A live PostgreSQL service and Playwright browser run were not
available locally (Docker is unavailable); the PostgreSQL DDL is covered by
the dialect-rendered regression test and the E2E readiness command is covered
by the workflow regression test.
