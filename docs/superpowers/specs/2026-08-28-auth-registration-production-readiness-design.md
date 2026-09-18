# Auth Registration Production Readiness Design

## Goal

Finish the documented PR A/PR B registration contract without leaving an
anonymous invite-registration bypass, while making resend safe against email
storms and making Render production configuration explicit.

## Decisions

- Remove the public `POST /auth/register` route entirely. The OpenAPI contract
  exposes only `/auth/register/request`, `/auth/register/resend`, and
  `/auth/register/verify` for public registration. Existing invite tables,
  administrator invite APIs, repository helpers, and controlled seed/CLI tools
  remain available for internal or future workflows, but no anonymous HTTP
  path consumes an invite to create a user.
- Count registration sends from persisted verification-request rows. Resend
  enforces the 60-second per-flow cooldown plus configurable hourly limits of
  10 messages per normalized email and 20 messages per source IP. A database
  row lock and a pre-send claim prevent concurrent resend calls from both
  sending; failed delivery rolls the claim back so a safe retry remains
  possible.
- `_frontend_origin()` allows local `http://localhost` and loopback origins in
  development/test, but requires HTTPS for production. It rejects credentials,
  query/fragment components, and malformed origins in every environment.
- Render declares all non-secret fixed email settings and all secret values as
  dashboard-injected `sync: false` entries. Production uses the deployed HTTPS
  frontend URL and `ustc.edu.cn` as the initial allowed domain.
- The frontend stores the request id returned by the request endpoint and
  exposes a resend action with server-driven cooldown and stable error text.
  It never stores registration tokens or creates an auth session.

## Verification

Automated coverage will include route absence, hourly email/IP limits, concurrent
resend/verify single-winner behavior, production HTTPS validation, Render YAML
contract, frontend resend state, and the existing auth/migration suites. Local
SQLite and frontend checks run in this workspace. PostgreSQL integration runs
when Docker or `SMARTAI_TEST_POSTGRES_URL` is available; otherwise the report
must say skipped. Real Gmail delivery remains an explicit smoke test requiring a
user-provided USTC recipient and must never log credentials or raw tokens.
