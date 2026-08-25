# Email Verification Registration Design

## Scope

This change implements PR A from `GMAIL_SMTP_VERIFIED_REGISTRATION_HANDOFF_CN.md`:
public registration is replaced by a verified school-email flow. Password reset,
JWT invalidation, and all-device logout are intentionally deferred to PR B.

## Architecture

The backend will add a reusable SMTP sender configured entirely through
`SMARTAI_SMTP_*`, `SMARTAI_MAIL_*`, and `SMARTAI_PUBLIC_FRONTEND_URL`. Business
services depend on a small `EmailSender` protocol so tests use an in-memory fake
and production uses STARTTLS/SSL SMTP without provider-specific branches.

Registration requests are stored in a dedicated `email_verification_requests`
table. The table stores normalized identity fields, a bcrypt password hash, a
SHA-256 token digest, expiry/cooldown timestamps, and delivery state; raw tokens
and passwords are never persisted. A request creates no `UserRecord` until its
token is confirmed. Request and verify endpoints re-check the configured domain
allowlist, and verification creates exactly one `teacher` user and marks the
request consumed in one transaction.

The old anonymous `/auth/register` route will reject requests in production and
will no longer accept role, invite-code, or admin fields for public registration.
Existing admin invite registration remains available as a separate controlled
path for current test/administration workflows.

## API

- `POST /auth/register/request` accepts `username`, `email`, and an 8-128
  character password and returns `202 verification_required` with an opaque
  request id, 30-minute expiry, and 60-second resend cooldown.
- `POST /auth/register/resend` accepts a request id, supersedes the previous
  token, and sends a replacement subject to cooldown/rate limits.
- `POST /auth/register/verify` accepts a raw token and creates the teacher
  account, returning `{\"status\": \"registered\"}`. It never logs in.

Links use only the configured frontend origin and the hash fragment format
`/register/verify#token=...`; opening a link does not consume it.

## Security and failure behavior

Allowed domains are parsed from a comma-separated setting. Matching is exact or
dot-boundary subdomain matching after lowercasing, trimming, and removing one
trailing dot. Empty configuration fails closed except for explicit `*`.

SMTP failures return a stable delivery error and do not leave a usable pending
request. Public responses do not reveal whether an existing username/email was
found. Logs contain only request ids and stable error codes. Rate limits are
implemented with the documented 60-second resend cooldown and configurable
per-email/per-IP hourly caps.

## Frontend

The registration page will submit the new request endpoint and show a neutral
“check your email” state. A small verification page will read the hash token,
call the verify endpoint only after the user clicks a confirmation button, and
return to login on success. No SMTP or backend secret is exposed to Vite.

## Testing

Backend tests cover domain boundaries, fail-closed configuration, password and
role rejection, request/verify/login flow, token isolation/expiry/replay,
resend invalidation, SMTP failure rollback, and absence of plaintext secrets.
Frontend tests cover request success/error states and explicit verification
confirmation. The full backend pytest suite and frontend Vitest/build are run
before completion; real Gmail delivery is a manual smoke test using the local
`.env` only.
