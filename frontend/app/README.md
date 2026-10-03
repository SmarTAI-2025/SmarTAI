# SmarTAI React App

Vite React + TypeScript rewrite for the teacher-facing SmarTAI app.

Current status: merged into `main` under `frontend/app`.

## Scope

- Visible now: teacher auth, task grading workflow, task-scoped KB, BYOK experts, settings.
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

# Render backend
VITE_SMARTAI_BACKEND_URL=https://<your-backend>.onrender.com
```

The `.env` file is ignored by git and only affects your machine. Restart
`npm run dev` after changing it. The Settings page shows the backend URL baked
into the current dev server or static build.

## Public Static Deploy

Deploy this app as a static Vite site:

```text
Root directory: frontend/app
Build command: npm ci && npm run build
Build output directory: dist
Environment variable:
  VITE_SMARTAI_BACKEND_URL=https://<your-backend>.onrender.com
```

On the FastAPI backend, add every public frontend origin to `FRONTEND_URLS`.
Keep entries comma-separated with no spaces and no trailing slash:

```text
FRONTEND_URLS=https://smartai-course.pages.dev,http://localhost:5173,http://127.0.0.1:5173
```

The `localhost` entries are only needed when the local React dev server talks to
a deployed backend. They are not required for public-only access.

## Checks

```bash
npm run typecheck
npm run build
```
