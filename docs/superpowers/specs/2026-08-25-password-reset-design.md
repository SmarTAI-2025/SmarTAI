# PR B Password Reset Design

## Goal

Add email-based password reset for registered USTC accounts. A successful reset changes the password, consumes the one-time link, revokes every refresh session, and immediately invalidates access tokens issued before the reset.

## Scope

- `POST /auth/password-reset/request` accepts only an email and always returns the same public response.
- `POST /auth/password-reset/confirm` accepts a one-time token and an 8-128 character password.
- Reset emails use the existing Gmail SMTP sender and fixed public frontend origin.
- The login page links to a request form; a reset page accepts the token from the URL fragment and submits the new password.
- No automatic login occurs after reset.

## Security And Data Flow

`password_reset_requests` is independent from `email_verification_requests`. It stores the user id, SHA-256 token digest, timestamps, supersession/consumption state, delivery status, and a safe delivery error code. Raw tokens, passwords, SMTP credentials, and account-existence details are never persisted, returned, or logged.

The request endpoint normalizes the email and applies the configured allowed-domain rule. It creates and sends a reset message only when the email maps to an active user. Unknown, inactive, malformed, or disallowed addresses receive the same `reset_link_requested` response; only valid existing accounts consume rate-limit budget and trigger SMTP delivery. A newer request supersedes earlier pending requests for that user.

The confirm endpoint locks the matching row and, in one database transaction, verifies expiry/supersession/consumption, updates the user password hash and `auth_invalid_before`, marks the reset row consumed, and revokes all unrevoked refresh sessions for that user. Concurrent confirmation can commit at most once. No token or session is returned.

JWTs already contain `iat`. Authentication will reject a token when its `iat` is less than or equal to the user's `auth_invalid_before` value, so reset invalidates existing access tokens immediately. Existing refresh rotation also checks the revoked rows and the user remains active.

## Errors And Limits

Stable reset errors are `password_reset_link_expired`, `password_reset_link_already_used`, `password_reset_link_invalid`, `password_reset_rate_limited`, and `password_reset_unavailable`. Rate limits reuse the existing email-flow configuration: 60 seconds between sends, ten per email per hour, and twenty per source IP per hour. Responses include `Retry-After` only for rate limiting.

## Email And Frontend

The message subject is `SmarTAI 密码重置`. It states that the link expires in 30 minutes, includes an action button and plain-text URL, and says that recipients can ignore it if they did not request a reset. It contains no old/new password or tracking pixel.

The frontend adds `/forgot-password` and `/reset-password` public routes. The login page links to the request route. The reset page reads `#token=...`, never submits it until the user confirms a new password, displays localized stable errors, and returns to login after success.

## Verification

Backend tests cover neutral responses, valid and invalid links, expiry, supersession, one-time use, concurrent confirmation, SMTP failure rollback, password replacement, refresh-session revocation, and immediate JWT invalidation. Frontend tests/build verify the new routes and API types. Migration tests verify a single Alembic head and SQLite/PostgreSQL-compatible schema changes.
