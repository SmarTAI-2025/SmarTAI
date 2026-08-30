# Auth Registration Production Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or subagent-driven-development) to implement this plan task-by-task with review checkpoints. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the documented email-registration/password-reset contract with strict legacy-route removal, resend storm protection, production email configuration, HTTPS validation, and frontend resend UX.

**Architecture:** Public registration is represented only by the three email-verification endpoints. Verification-request rows remain the source of truth for per-email/IP send counts and are claimed under a row lock before SMTP; a failed send clears the claim, while a successful send supersedes the prior row. Production origin validation and Render environment declarations make deployment failures explicit without embedding secrets.

**Tech Stack:** FastAPI, Pydantic Settings, SQLAlchemy/Alembic, pytest, React/Vite/TypeScript, Vitest, Render Blueprint YAML.

---

### Task 1: Lock the public registration and configuration contracts with failing tests

**Files:**
- Modify: `backend/tests/test_email_registration.py`
- Create: `backend/tests/test_auth_registration_contract.py`
- Create: `backend/tests/test_render_email_contract.py`
- Modify: `frontend/app/src/types/auth.ts`
- Test: `backend/tests/test_email_registration.py`, `backend/tests/test_auth_registration_contract.py`, `backend/tests/test_render_email_contract.py`

- [x] **Step 1: Add failing backend tests** asserting `POST /auth/register` is absent (404), production rejects non-HTTPS public origins, development accepts loopback HTTP, and the configured email limit is 10.
- [x] **Step 2: Add a failing Render contract test** that parses `backend/render.yaml` and requires the eight email-related keys, with secrets marked `sync: false` and fixed values for security, domain, and public URL.
- [x] **Step 3: Add failing frontend type/test scaffolding** for a resend response carrying `request_id` and a request page retaining that id.
- [x] **Step 4: Run the focused tests and record the expected failures before production edits.

### Task 2: Remove the anonymous legacy registration path

**Files:**
- Modify: `backend/api/auth.py`
- Modify: `frontend/app/src/api/auth.ts`
- Modify: `frontend/app/src/api/hooks/auth.ts`
- Modify: `frontend/app/src/types/auth.ts`
- Modify: `backend/tests/test_auth_persistence.py`
- Modify: `backend/tests/test_admin_api.py`

- [x] **Step 1: Delete `RegisterRequest`, the `register()` FastAPI handler, and the unused client `register()`/`useRegister()` symbols.
- [x] **Step 2: Move existing invite-based test setup to `register_with_invite()` plus `create_refresh_session()` or the controlled seed helper; keep administrator invite API assertions intact.
- [x] **Step 3: Update the contract test to assert no route can create a user, access token, or refresh cookie through `/auth/register`.
- [x] **Step 4: Run auth and admin tests and confirm the only public registration paths are request/resend/verify.

### Task 3: Add persisted hourly resend limits and a concurrent send claim

**Files:**
- Modify: `backend/services/email_registration.py`
- Modify: `backend/db/models.py` only if a claim timestamp is required by the existing schema
- Create: migration only if the model change is required
- Modify: `backend/tests/test_email_registration.py`

- [x] **Step 1: Add failing tests for the 10-per-email/hour and 20-per-IP/hour resend caps, `Retry-After`, and a concurrent resend pair where only one sender call succeeds.
- [x] **Step 2: Run those tests against the current implementation and confirm they fail because resend only checks the 60-second cooldown.
- [x] **Step 3: Implement the smallest transaction-safe claim using existing row fields: count recent request rows by normalized email/source IP under shared process locks and PostgreSQL transaction advisory locks, create one pending replacement, and mark the previous row superseded before SMTP; on delivery failure rollback the replacement and restore the prior row state.
- [x] **Step 4: Keep request and resend limits configurable through `Settings`, set the default email limit to `10`, and return stable `registration_rate_limited` errors with `Retry-After`.
- [x] **Step 5: Run the registration suite including repeated and concurrent cases.

### Task 4: Enforce production HTTPS and complete Render email settings

**Files:**
- Modify: `backend/services/email_sender.py`
- Modify: `backend/config.py`
- Modify: `backend/render.yaml`
- Modify: `.env.example`
- Modify: `backend/tests/test_frontend_url_settings.py`
- Create: `backend/tests/test_email_sender_origin.py`

- [x] **Step 1: Add failing origin tests for production HTTP rejection, production HTTPS acceptance, loopback HTTP development acceptance, and query/fragment/credential rejection.
- [x] **Step 2: Implement environment-aware origin validation and set the default hourly email cap to 10 without changing the IP cap.
- [x] **Step 3: Add Render keys for `SMARTAI_SMTP_HOST`, `SMARTAI_SMTP_PORT`, `SMARTAI_SMTP_SECURITY`, `SMARTAI_SMTP_USERNAME`, `SMARTAI_SMTP_PASSWORD`, `SMARTAI_MAIL_FROM_ADDRESS`, `SMARTAI_ALLOWED_EMAIL_DOMAINS`, and `SMARTAI_PUBLIC_FRONTEND_URL`; use `sync: false` for secrets and the deployment-specific frontend URL so no plausible-but-invalid link can be deployed accidentally.
- [x] **Step 4: Add matching non-secret `.env.example` entries and tests that never assert or print a credential.
- [x] **Step 5: Run origin/config tests and parse the Render YAML.

### Task 5: Finish frontend resend UX and remove stale registration symbols

**Files:**
- Modify: `frontend/app/src/routes/RegisterPage.tsx`
- Modify: `frontend/app/src/api/auth.ts`
- Modify: `frontend/app/src/api/hooks/auth.ts`
- Modify: `frontend/app/src/types/auth.ts`
- Create: `frontend/app/src/routes/RegisterPage.test.tsx`

- [x] **Step 1: Add failing tests for retaining `request_id`, rendering resend only after a successful request, disabling it during the cooldown, and surfacing a stable rate-limit error.
- [x] **Step 2: Implement request state `{requestId, resendAfterSeconds}`, a countdown based on a monotonic deadline, and a resend mutation that updates the id returned by the server.
- [x] **Step 3: Keep the page free of invite fields and auth tokens; preserve the existing explicit verification-page click.
- [x] **Step 4: Run focused Vitest tests, typecheck, and the production build.

### Task 6: Document operations and execute the evidence matrix

**Files:**
- Create: `GMAIL_SMTP_CONFIGURATION_AND_FRONTEND_INTEGRATION_CN.md`
- Modify: `GMAIL_SMTP_VERIFIED_REGISTRATION_HANDOFF_CN.md`
- Modify: `.github/workflows/ci.yml` only if a missing test/dependency blocks the documented checks

- [x] **Step 1: Document local fake-sender testing, Render variable setup, HTTPS origin rules, resend limits (10 email/20 IP), legacy-route disposition, and a redacted Gmail smoke-test procedure.
- [x] **Step 2: Run `python -m compileall -q backend` and focused auth/config/Render tests.
- [x] **Step 3: Run the full backend suite; report exact pass/fail/skip counts and isolate pre-existing failures. (`938 passed, 10 skipped, 4 unrelated failures` in the final fresh local run.)
- [x] **Step 4: Run frontend Vitest, typecheck, and build; report any environment-only failures separately.
- [x] **Step 5: Attempt PostgreSQL integration using Docker or `SMARTAI_TEST_POSTGRES_URL`; if unavailable, report the exact reason and retain CI coverage. (Unavailable locally: Docker daemon stopped, URL unset, port 5432 unreachable.)
- [x] **Step 6: Run a fake-sender concurrency smoke test and, only with an explicit recipient available, run one real Gmail delivery without printing credentials or raw tokens. (Fake sender complete; real Gmail intentionally not run without an authorized recipient.)
- [x] **Step 7: Review `git diff --check`, secret scans, OpenAPI paths, and `git status`; commit only this feature's files and report remaining external verification honestly. (Feature files are isolated on the PR branch; unrelated workspace files remain uncommitted.)
