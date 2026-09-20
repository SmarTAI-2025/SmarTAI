# Email Verification Registration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or subagent-driven-development) to implement this plan task-by-task with review checkpoints.

**Goal:** Replace public anonymous registration with a USTC-domain email verification flow delivered through configurable Gmail SMTP.

**Architecture:** Add a dedicated verification-request ORM record and migration, a token/domain/SMTP service layer with injectable sender, and three auth endpoints that only create a teacher after transactional token confirmation. Update the React registration route to request verification and add a click-to-confirm page.

**Tech Stack:** FastAPI, Pydantic Settings, SQLAlchemy/Alembic, bcrypt, Python stdlib `smtplib`, React/Vite/TypeScript, Vitest.

---

### Task 1: Add configuration and domain/token primitives

**Files:**
- Modify: `backend/config.py`
- Modify: `.env.example`
- Test: `backend/tests/test_email_registration.py`

- [ ] **Step 1: Write failing tests** for `normalize_email`, `email_domain_allowed`, production fail-closed empty domains, explicit `*`, and 128-bit token generation/digest non-reversibility.
- [ ] **Step 2: Run `pytest backend/tests/test_email_registration.py -q` and verify the import/attribute failures are expected.
- [ ] **Step 3: Add settings fields for SMTP host/port/security/credentials/from identity, public frontend URL, allowed domains, timeout, expiry, cooldown, and hourly limits; add pure helpers that normalize email and perform exact/dot-boundary matching.
- [ ] **Step 4: Run the focused tests and confirm all primitive tests pass.

### Task 2: Persist verification requests

**Files:**
- Modify: `backend/db/models.py`
- Create: `backend/db/migrations/versions/0013_email_verification_requests.py`
- Test: `backend/tests/test_email_registration.py`

- [ ] **Step 1: Add tests asserting the table stores only token digest/password hash, enforces request-id/token-digest uniqueness, and can be created in the current schema.
- [ ] **Step 2: Run the focused tests and verify they fail because the ORM record and migration are absent.
- [ ] **Step 3: Add `EmailVerificationRequestRecord` with normalized username/email, password hash, token digest, timestamps, superseded/verified timestamps, delivery status/error, source IP, and indexes/constraints; create the next migration from `0011_ocr_provider_credentials`.
- [ ] **Step 4: Run migration round-trip tests and focused persistence tests.

### Task 3: Implement injectable SMTP transport and registration service

**Files:**
- Create: `backend/services/email_sender.py`
- Create: `backend/services/email_registration.py`
- Modify: `backend/db/auth_repository.py`
- Test: `backend/tests/test_email_registration.py`

- [ ] **Step 1: Write failing tests for fake-sender request/verify flow, token isolation, expiry/replay, resend invalidation, SMTP failure rollback, and no plaintext token/password in DB or returned payloads.
- [ ] **Step 2: Run the focused tests and verify failures are due to missing sender/service behavior.
- [ ] **Step 3: Implement `EmailSender` protocol, stdlib SMTP STARTTLS/SSL sender with finite timeout and redacted stable errors, HTML+text message builder using configured frontend origin, and service functions that hash passwords, persist digest-only requests, atomically supersede/resend, and transactionally create a teacher on verify.
- [ ] **Step 4: Run focused tests; refactor only after green to keep business code independent of SMTP provider.

### Task 4: Expose secure FastAPI endpoints and close legacy public registration

**Files:**
- Modify: `backend/api/auth.py`
- Modify: `backend/main.py` only if dependency wiring is required
- Test: `backend/tests/test_email_registration.py`
- Modify: `backend/tests/test_auth_persistence.py` for changed public-registration expectations

- [ ] **Step 1: Add API tests for request (202), resend, verify success, domain rejection, extra `role`/`invite_code` rejection, legacy route rejection in production, and stable SMTP error responses.
- [ ] **Step 2: Run only these tests and confirm they fail against the current direct-registration route.
- [ ] **Step 3: Add strict request models with `extra='forbid'`, wire endpoints to the service and an injectable sender override for tests, return opaque request ids/stable error codes, and make `/auth/register` reject anonymous public registration while preserving admin invite registration behavior through its existing controlled use.
- [ ] **Step 4: Run the full backend auth/migration suite and inspect responses for secrets/tokens.

### Task 5: Update frontend registration and confirmation flow

**Files:**
- Modify: `frontend/app/src/types/auth.ts`
- Modify: `frontend/app/src/api/auth.ts`
- Modify: `frontend/app/src/api/hooks/auth.ts`
- Modify: `frontend/app/src/routes/RegisterPage.tsx`
- Create: `frontend/app/src/routes/RegisterVerifyPage.tsx`
- Modify: `frontend/app/src/main.tsx`
- Test: `frontend/app/src/routes/RegisterPage.test.tsx`
- Test: `frontend/app/src/routes/RegisterVerifyPage.test.tsx`

- [ ] **Step 1: Write failing Vitest tests for neutral “check your email” state, no local token/session after request, hash-token confirmation only after button click, and success/error navigation.
- [ ] **Step 2: Run `npm test -- --run ...` for the new tests and confirm expected failures.
- [ ] **Step 3: Replace invite-only public form with school-email/password form calling `/auth/register/request`; add `/register/verify` route that parses `location.hash`, confirms explicitly, and links back to login without auto-login.
- [ ] **Step 4: Run frontend tests and `npm run build`.

### Task 6: Environment, integration, and completion verification

**Files:**
- Modify: `.env.example`
- Modify: local `.env` only with user-provided SMTP values; never stage it
- Test: existing backend migration/auth suites and frontend suite

- [ ] **Step 1: Add non-secret SMTP examples and ensure `.env` is ignored; check `git diff --check` and secret scans.
- [ ] **Step 2: Run `pytest backend/tests/test_email_registration.py backend/tests/test_auth_persistence.py backend/tests/test_migration_roundtrip.py -q`.
- [ ] **Step 3: Run the complete backend pytest suite and frontend Vitest/build; record any pre-existing failures separately.
- [ ] **Step 4: Start backend/frontend locally, perform one real USTC delivery using the local `.env`, verify the received link requires an explicit click, then remove any captured token/log output.
- [ ] **Step 5: Review `git status`, confirm `.env` and raw secrets are untracked, and report exact verification evidence and remaining PR B scope.
