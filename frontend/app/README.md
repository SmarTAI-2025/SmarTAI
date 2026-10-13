# SmarTAI React App

Vite React + TypeScript rewrite for the teacher-facing SmarTAI app.

Current status: merged into `main` under `frontend/app`.

## Scope

- Visible now: Frontier landing page at `/`, live demo at `/frontier/live`,
  teacher auth at `/login`, and the teacher workspace at `/dashboard` (task
  grading workflow, task-scoped KB, BYOK experts, settings).
- Hidden now: student app, courses, assignments, LMS/LTI/SSO.
- Backend remains FastAPI. This app calls the existing REST APIs directly.

See:

- `plans/REACT_REWRITE_STATUS_CN.md`
- `plans/FRONTEND_MIGRATION_ENGINEERING_PLAN_CN.md`

## Local Development

This app does not need the `smartai` Conda environment. Run the FastAPI backend
from the repo root in the `smartai` environment, then run the React app with
Node.js + npm. Use Node **22.16.0**, recorded in `.node-version`; the frontend
and E2E CI jobs read this same file. It matches the current Cloudflare Pages
production `NODE_VERSION` setting. Actions themselves use Node 24 internally,
independently of the Node version used to build this app.

Install locked dependencies and start the dev server:

```bash
npm ci
npm run dev
```

Default backend:

```bash
VITE_SMARTAI_BACKEND_URL=http://localhost:8000
```

If `frontend/app/.env` does not exist, the app falls back to
`http://localhost:8000`. To make the choice explicit, copy `.env.example` to
`.env` and set one of these values:

```bash
# Local backend
VITE_SMARTAI_BACKEND_URL=http://localhost:8000

# Existing AWS backend HTTPS origin
VITE_SMARTAI_BACKEND_URL=https://<your-aws-api-host>
```

The `.env` file is ignored by git and only affects your machine. Restart
`npm run dev` after changing it. The Settings page shows the backend URL baked
into the current dev server or static build.

## Public Static Deploy

The current frontend is deployed on Cloudflare Pages and calls the existing
AWS Lightsail backend. Keep the existing Cloudflare project and backend origin
when enabling the integrated landing page and demo:

```text
Root directory: frontend/app
Build command: npm ci && npm run build
Build output directory: dist
Environment variable:
  VITE_SMARTAI_BACKEND_URL=https://<your-aws-api-host>
```

On the FastAPI backend, add every public frontend origin to `FRONTEND_URLS`.
Keep entries comma-separated with no spaces and no trailing slash:

```text
FRONTEND_URLS=https://smartai-univ.pages.dev,http://localhost:5173,http://127.0.0.1:5173
```

The `localhost` entries are only needed when the local React dev server talks to
a deployed backend. They are not required for public-only access.

## Integrated Frontier demo

The landing page and its assets need no model credentials. Its registration /
login button opens the existing `/login` flow. `/frontier` remains an alias for
old links; the Demo button enters through `/frontier/enter`.

Live execution uses the same AWS API origin, task preparation, recognition,
grading and result APIs as the teacher workspace. It has no separate grading
engine. Each visitor receives an isolated task owner and a tab-local access
token; entering or renewing the demo does not replace a teacher login or its
refresh cookie. Demo credentials cannot manage accounts or teacher BYOK keys.
Stage details reuse the normal task pages without a login step. The demo shell
omits the workspace, BYOK, personal library and account navigation. Leaving for
the landing page or login asks for confirmation; confirmation deletes the
guest's tasks through the normal cleanup flow and revokes its session. Cancel
keeps the current run. Teacher session expiry still returns to `/login`.

Before enabling execution after merge, apply the normal backend database
migrations (`alembic upgrade head`, including `0028_frontier_demo_usage`) and
configure the existing AWS service environment using the root `.env.example`:

- Set `SMARTAI_FRONTIER_DEMO_ENABLED=true`. The demo uses main's existing shared
  provider settings and adapters; for the current Gemini setup, configure
  `SMARTAI_GEMINI_API_KEY` and `SMARTAI_GEMINI_MODEL` on AWS. There is no separate
  demo model configuration or legacy provider implementation. No secret belongs
  in Cloudflare's `VITE_*` settings.
- Demo admission has its own durable global daily session, request and estimated
  input-token limits. Defaults are 20 sessions, 500 logical model calls and
  500,000 estimated input tokens, with a 5-second session issuance cooldown.
  Daily limits reset at UTC midnight. Access tokens last 20 minutes by default
  and can renew within the same guest session, up to its fixed two-hour limit.
  These are application admission limits, not provider billing totals.
- Keep `SMARTAI_SHARED_POOL_ENABLED=false` for the normal BYOK teacher product.
  Enabling the demo does not enable teacher shared-model fallback. Setting
  `SMARTAI_FRONTIER_DEMO_ENABLED=false` stops new demo sessions and model calls.

This code change does not configure or deploy either hosted service. The
existing registration/email setup and AWS/Cloudflare deployment remain in use.

## Checks

```bash
npm run typecheck
npm run build
```
