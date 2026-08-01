# backend/main.py
"""
SmarTAI FastAPI application entry point.

Runs the V2 agents/skills/tools architecture exclusively. The legacy V1
routers (backend/routers/* + backend/correct/*) have been removed from the
codebase post-migration; only `SMARTAI_GRADING_ENGINE=v2` is valid here.
"""
import sys
import os
import logging
import random

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ─── Proxy MUST be set before ANY Google/LangChain imports ────────────────────
# Google's HTTP client reads HTTP_PROXY at import time, not at call time.
from backend.config import settings as _settings
if _settings.http_proxy:
    os.environ["HTTP_PROXY"] = _settings.http_proxy
    os.environ["HTTPS_PROXY"] = _settings.https_proxy
else:
    os.environ.pop("HTTP_PROXY", None)
    os.environ.pop("HTTPS_PROXY", None)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    app = FastAPI(title="SmarTAI")

    # ─── Engine guard ─────────────────────────────────────────────────────
    # V1 routers were removed in the post-migration cleanup. We refuse to
    # start under the old engine setting rather than crash later on a missing
    # `backend.routers` module — gives a clearer error message.
    from backend.config import settings
    engine = settings.grading_engine
    if engine != "v2":
        raise RuntimeError(
            f"Invalid SMARTAI_GRADING_ENGINE={engine!r}; only 'v2' is supported "
            f"(V1 routers were removed). Unset the env var or set it to 'v2'."
        )
    logger.info(f"Starting SmarTAI with GRADING_ENGINE={engine}")
    if settings.http_proxy:
        # Proxy URLs can contain credentials.  Keep startup diagnostics useful
        # without copying the configured value into ordinary logs.
        logger.info("HTTP proxy enabled")

    # ── Wire up task-scoped knowledge retriever (RAG MVP) ─────────────
    # Replaces the default NoOpRetriever set in backend/tools/knowledge.py.
    # Pure in-memory; chunks + vectors live keyed by task_id so grading
    # skills can scope retrieval via `scope=self.task_id`. State is lost
    # on process restart — by design (matches "测一两个 task 退出失效").
    from backend.knowledge.retriever import CombinedKnowledgeRetriever
    from backend.tools.knowledge import set_retriever
    set_retriever(CombinedKnowledgeRetriever())

    # ── V2: new agents/skills/tools architecture ──────────────────────
    # Resource-oriented normalized routers remain canonical.  The /tasks façade
    # below is a presentation adapter over those same repositories for the Figma
    # teacher workflow; it does not restore the removed TaskStore/JobStore.
    from backend.api.auth import router as auth_router
    from backend.api.users import router as users_router
    from backend.api.admin import router as admin_router
    from backend.api.courses import router as courses_router
    from backend.api.assignments import router as assignments_router
    from backend.api.submissions import router as submissions_router
    from backend.api.knowledge import router as knowledge_router, assignment_router as assignment_knowledge_router
    from backend.api.grading_runs import router as grading_runs_router
    from backend.api.results import router as results_router
    from backend.api.experts import router as experts_router
    from backend.api.tasks import router as tasks_router
    from backend.api.task_preparation import router as task_preparation_router
    from backend.api.materials import router as materials_router
    from backend.api.tags import router as tags_router
    from backend.api.analytics import router as analytics_router

    app.include_router(auth_router)
    app.include_router(users_router)
    app.include_router(admin_router)
    app.include_router(courses_router)
    app.include_router(assignments_router)
    app.include_router(submissions_router)
    app.include_router(knowledge_router)
    app.include_router(assignment_knowledge_router)
    app.include_router(grading_runs_router)
    app.include_router(results_router)
    app.include_router(experts_router)
    app.include_router(tasks_router)
    app.include_router(task_preparation_router)
    app.include_router(materials_router)
    app.include_router(tags_router)
    app.include_router(analytics_router)

    logger.info(
        "V2 routers loaded: auth, users, admin, courses, assignments, submissions, "
        "knowledge, grading-runs, results, experts, tasks, task-preparation, "
        "course-materials, tags, analytics"
    )

    # ─── CORS ─────────────────────────────────────────────────────────────
    origins = settings.frontend_urls.split(",")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ─── Health / root ────────────────────────────────────────────────────
    @app.get("/")
    def read_root():
        return {
            "message": "SmarTAI Backend is running",
            "engine": engine,
            "status": "success",
        }

    @app.get("/health")
    async def health_check():
        import psutil
        process = psutil.Process(os.getpid())
        memory_info = process.memory_info()
        return {
            "status": "healthy",
            "engine": engine,
            "memory_usage_mb": round(memory_info.rss / 1024 / 1024, 2),
            "cpu_percent": process.cpu_percent(),
        }

    @app.get("/ready")
    async def readiness_check():
        from fastapi.responses import JSONResponse
        from backend.db.session import database_ready
        from backend.storage import get_storage
        database_ok = database_ready()
        storage_ok = get_storage().ready()
        payload = {"status": "ready" if database_ok and storage_ok else "not_ready",
                   "database": database_ok, "storage": storage_ok}
        return JSONResponse(payload, status_code=200 if database_ok and storage_ok else 503)

    # ─── Sandbox concurrency cap ──────────────────────────────────────────
    # The grading pipeline fans out via nested asyncio.gather (students ×
    # questions × test cases) — without a global semaphore, a single batch can
    # spawn hundreds of subprocesses and OOM the host. 8 is a conservative
    # default; tunable via SMARTAI_SANDBOX_CONCURRENCY env var.
    @app.on_event("startup")
    async def _init_sandbox():
        from backend.tools.sandbox_runtime import init_sandbox_semaphore
        limit = int(os.environ.get("SMARTAI_SANDBOX_CONCURRENCY", "8"))
        init_sandbox_semaphore(limit=limit)

    # ─── Seed pre-baked test accounts (kept out of the repo) ──────────────
    try:
        if settings.database_auto_create:
            from backend.db.session import create_schema
            create_schema()
        from backend.auth.seed import seed_test_users
        seed_test_users()
    except Exception as e:
        logger.warning(f"test users seeding skipped: {e}")

    # ─── Grading worker lifecycle (Task 7) ────────────────────────────────
    # One poller per process claims queued grading runs through the DB lease
    # predicate, so multiple processes are safe and shutdown simply stops
    # claiming new work (no unexpired lease is released). The loop is best-
    # effort: a crashed process leaves its lease to expire, after which another
    # worker reclaims the run.
    _grading_worker: dict[str, object] = {"task": None}

    @app.on_event("startup")
    async def _start_grading_worker():
        import asyncio as _asyncio
        from backend.config import settings as _s
        from backend.services.grading_runs import worker_loop

        async def _run():
            try:
                await worker_loop(worker_id=_s.grading_worker_id)
            except _asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("grading worker loop exited")

        _grading_worker["task"] = _asyncio.create_task(_run())

    @app.on_event("shutdown")
    async def _stop_grading_worker():
        import asyncio as _asyncio

        task = _grading_worker.get("task")
        if task is not None:
            task.cancel()
            try:
                await task
            except _asyncio.CancelledError:
                pass
            except Exception:
                pass

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    from backend.config import settings

    port = settings.backend_port or int(os.environ.get("BACKEND_PORT", random.randint(8000, 9000)))
    logger.info(f"Starting FastAPI on http://localhost:{port}")
    uvicorn.run(app, host="localhost", port=port)
