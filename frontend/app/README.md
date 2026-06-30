# SmarTAI React App

Vite React + TypeScript rewrite for the teacher-facing SmarTAI app.

Current branch: `codex/vite-react-frontend`

## Scope

- Visible now: teacher auth, task grading workflow, task-scoped KB, BYOK experts, settings.
- Hidden now: student app, courses, assignments, LMS/LTI/SSO.
- Backend remains FastAPI. This app calls the existing REST APIs directly.

See:

- `plans/REACT_REWRITE_STATUS_CN.md`
- `plans/FRONTEND_MIGRATION_ENGINEERING_PLAN_CN.md`

## Local Development

```bash
npm install
npm run dev
```

Default backend:

```bash
VITE_SMARTAI_BACKEND_URL=http://localhost:8000
```

## Checks

```bash
npm run typecheck
npm run build
```

