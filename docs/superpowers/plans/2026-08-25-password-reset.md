# Password Reset Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement PR B email password reset for USTC accounts, including one-time reset links, atomic password/session invalidation, immediate JWT invalidation, and frontend flows.

**Architecture:** Add a password-reset table and service separate from registration verification. The service sends through the existing SMTP sender, performs confirm operations in one SQLAlchemy transaction, revokes all refresh sessions, and stores a user invalidation timestamp checked during JWT authentication. Public frontend routes call two unauthenticated API endpoints and never receive a token after reset.

**Tech Stack:** FastAPI, Pydantic Settings, SQLAlchemy/Alembic, bcrypt, PyJWT, React 19, React Router, TanStack Query, Vitest.

---

### Task 1: Extend the normalized auth schema

**Files:**
- Modify: `backend/db/models.py`
- Create: `backend/db/migrations/versions/0013_password_reset_requests.py`
- Test: `backend/tests/test_migration_roundtrip.py`

- [ ] **Step 1: Write failing schema tests**

Add a migration test that upgrades a fresh SQLite database and asserts `users.auth_invalid_before` is nullable and `password_reset_requests` contains `user_id`, `token_digest`, timestamps, supersession/consumption fields, delivery fields, and indexes. Assert the migration chain has one head.

- [ ] **Step 2: Run the focused migration test**

Run: `python -m pytest backend/tests/test_migration_roundtrip.py -q`

Expected: FAIL because revision `0013_password_reset_requests` and the new columns/table do not exist.

- [ ] **Step 3: Implement the ORM and Alembic migration**

Add `UserRecord.auth_invalid_before: float | None`. Add `PasswordResetRequestRecord` with a foreign key to `users`, SHA-256 digest length, `created_at`, `expires_at`, `resend_available_at`, `superseded_at`, `consumed_at`, `delivery_status`, `last_delivery_error_code`, and `source_ip`. Create migration `0013` with `down_revision = "0012_email_verification_requests"`, indexes on user/email/time/token digest, and a nullable `users.auth_invalid_before` column.

- [ ] **Step 4: Run migration tests again**

Run: `python -m pytest backend/tests/test_migration_roundtrip.py -q`

Expected: PASS with one migration head.

- [ ] **Step 5: Commit**

```bash
git add backend/db/models.py backend/db/migrations/versions/0013_password_reset_requests.py backend/tests/test_migration_roundtrip.py
git commit -m "feat: add password reset persistence"
```

### Task 2: Add password-reset service and email template

**Files:**
- Modify: `backend/services/email_sender.py`
- Create: `backend/services/password_reset.py`
- Test: `backend/tests/test_password_reset.py`

- [ ] **Step 1: Write failing service tests**

Cover: neutral response for unknown email, real delivery for active allowed-domain user, 128-byte-class token generation and digest-only persistence, superseding old requests, 60-second/email/hour/IP limits, SMTP failure rollback, invalid/expired/superseded/consumed tokens, and concurrent confirm where only one call succeeds. Use the existing fake sender pattern and a temporary SQLite database.

- [ ] **Step 2: Run tests to verify failure**

Run: `python -m pytest backend/tests/test_password_reset.py -q`

Expected: FAIL because the service, model, and reset message do not exist.

- [ ] **Step 3: Implement the service**

Add `generate_reset_token()` using `secrets.token_urlsafe(96)` and `digest_token()` using SHA-256. Implement `request_password_reset(email, source_ip, sender)` with normalized email/domain checks, neutral response for every non-sendable case, request supersession, configured limits, and delivery rollback. Implement `confirm_password_reset(token, new_password)` with row locking, bcrypt hashing, user update, token consumption, all-session revocation, and `auth_invalid_before = now` in the same `session_scope()` transaction. Raise the five stable error codes from the handoff.

- [ ] **Step 4: Add `password_reset_message()`**

Generate `/reset-password#token=...` from `_frontend_origin()`. Return subject `SmarTAI 密码重置`, text/plain content, and HTML with one button plus escaped plain-text URL. Never include a password or tracking element.

- [ ] **Step 5: Run service tests**

Run: `python -m pytest backend/tests/test_password_reset.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/services/email_sender.py backend/services/password_reset.py backend/tests/test_password_reset.py
git commit -m "feat: implement password reset service"
```

### Task 3: Expose API endpoints and enforce immediate JWT invalidation

**Files:**
- Modify: `backend/api/auth.py`
- Modify: `backend/auth/__init__.py`
- Modify: `backend/db/auth_repository.py`
- Test: `backend/tests/test_password_reset.py`

- [ ] **Step 1: Write failing API/auth tests**

Assert `POST /auth/password-reset/request` returns the same `202` body for existing and missing addresses, confirm returns `200 {"status":"password_reset"}` without access token or cookie, old password fails and new password succeeds, all refresh rows are revoked, and an access token issued before reset is rejected by protected auth while a token issued after reset works.

- [ ] **Step 2: Run tests to verify failure**

Run: `python -m pytest backend/tests/test_password_reset.py -q`

Expected: FAIL because routes are absent and JWT decoding does not check `iat`.

- [ ] **Step 3: Add FastAPI request models/routes**

Add strict request models for `{email}` and `{token,new_password}`. Map `RegistrationError`-style reset errors to HTTP status and `Retry-After`. Register `/auth/password-reset/request` and `/auth/password-reset/confirm` without authentication dependencies.

- [ ] **Step 4: Enforce token invalidation**

Include `auth_invalid_before` when loading a persisted user. In `get_optional_user`, read the JWT `iat` and reject the token when `iat <= user_record.auth_invalid_before`. Keep demo-token behavior unchanged. Add repository helper behavior needed to revoke every unrevoked refresh session for a user inside the existing transaction.

- [ ] **Step 5: Run backend auth tests**

Run: `python -m pytest backend/tests/test_password_reset.py backend/tests/test_email_registration.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/api/auth.py backend/auth/__init__.py backend/db/auth_repository.py backend/tests/test_password_reset.py
git commit -m "feat: add password reset auth endpoints"
```

### Task 4: Add frontend API types, routes, and login entry point

**Files:**
- Modify: `frontend/app/src/types/auth.ts`
- Modify: `frontend/app/src/api/auth.ts`
- Modify: `frontend/app/src/api/hooks/auth.ts`
- Modify: `frontend/app/src/main.tsx`
- Modify: `frontend/app/src/routes/LoginPage.tsx`
- Create: `frontend/app/src/routes/ForgotPasswordPage.tsx`
- Create: `frontend/app/src/routes/ResetPasswordPage.tsx`
- Modify: `frontend/app/src/lib/authErrors.ts`
- Test: `frontend/app/src/routes/*.test.tsx` or the existing auth test location

- [ ] **Step 1: Write failing frontend tests**

Render the login page and assert a “忘记密码” link points to `/forgot-password`. Render the request page and assert it calls the reset-request API and shows the neutral success state. Render the reset page with a hash token and assert it requires matching 8-128 character passwords, calls confirm, and never stores/returns a token.

- [ ] **Step 2: Run focused frontend tests to verify failure**

Run: `npm test -- --run frontend/app/src/routes`

Expected: FAIL because the routes, hooks, and link do not exist.

- [ ] **Step 3: Implement types/API/hooks**

Add `PasswordResetRequest`, `PasswordResetRequestResponse`, and `PasswordResetConfirmResponse` types, API functions, and `useRequestPasswordReset`/`useConfirmPasswordReset` mutations. Extend `localizedAuthError` for stable reset error codes without exposing account existence.

- [ ] **Step 4: Implement pages and route registration**

Use existing `AuthFrame`, `AuthCard`, `Field`, `Input`, `AuthPasswordInput`, `AuthError`, `Button`, and i18n patterns. `/forgot-password` accepts an email and always shows the same success copy after a `202`. `/reset-password` parses `window.location.hash`, submits only after the user confirms the new password, handles missing/invalid tokens, and links back to `/login` after success. Add both public routes in `main.tsx` and the login-page link.

- [ ] **Step 5: Run frontend tests and build**

Run: `npm test -- --run frontend/app/src/routes`; `npm run build`

Expected: focused tests pass and TypeScript/Vite build exits 0.

- [ ] **Step 6: Commit**

```bash
git add frontend/app/src
git commit -m "feat: add password reset frontend flow"
```

### Task 5: Full verification and local handoff

**Files:**
- Test: backend and frontend suites
- Modify: only files required by failing verification

- [ ] **Step 1: Run backend focused and full auth/migration tests**

Run: `python -m pytest backend/tests/test_password_reset.py backend/tests/test_email_registration.py backend/tests/test_migration_roundtrip.py -q` and then `python -m pytest backend/tests -q`.

Expected: focused tests pass; any unrelated pre-existing failures are recorded with their exact test names.

- [ ] **Step 2: Run frontend verification**

Run from `frontend/app`: `npm test -- --run`; `npm run build`.

Expected: build passes; existing unrelated test failures are distinguished from PR B failures.

- [ ] **Step 3: Inspect migration and security artifacts**

Run `alembic heads`, inspect the migration SQL/table definitions, and search the changed files for raw token/password logging or SMTP secret output. Confirm the reset API responses contain no token, access token, refresh token, or account-existence signal.

- [ ] **Step 4: Commit verification fixes and report**

Commit only fixes required by verification, leave unrelated user files untouched, and report exact test/build outcomes plus the local URLs only if services are intentionally restarted.
