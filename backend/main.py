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
        logger.info(f"Proxy enabled: {settings.http_proxy}")

    # ── Wire up task-scoped knowledge retriever (RAG MVP) ─────────────
    # Replaces the default NoOpRetriever set in backend/tools/knowledge.py.
    # Pure in-memory; chunks + vectors live keyed by task_id so grading
    # skills can scope retrieval via `scope=self.task_id`. State is lost
    # on process restart — by design (matches "测一两个 task 退出失效").
    from backend.rag.store import InMemoryTaskRetriever
    from backend.tools.knowledge import set_retriever
    set_retriever(InMemoryTaskRetriever())

    # ── V2: new agents/skills/tools architecture ──────────────────────
    from backend.api.ingest import prob_router, hw_router
    from backend.api.grading import router as grading_router
    from backend.api.experts import router as experts_router
    from backend.api.human_edit import router as human_edit_router
    from backend.api.auth import router as auth_router
    from backend.api.users import router as users_router
    from backend.api.courses import router as courses_router
    from backend.api.assignments import router as assignments_router
    from backend.api.students import router as students_router
    from backend.api.tasks import router as tasks_router
    from backend.api.analytics import router as analytics_router

    app.include_router(prob_router)
    app.include_router(hw_router)
    app.include_router(grading_router)
    app.include_router(experts_router)
    app.include_router(human_edit_router)
    app.include_router(auth_router)
    app.include_router(users_router)
    app.include_router(courses_router)
    app.include_router(assignments_router)
    app.include_router(students_router)
    app.include_router(tasks_router)
    app.include_router(analytics_router)

    logger.info("V2 routers loaded: prob_preview, hw_preview, ai_grading, experts, human_edit, auth, users, courses, assignments, students, tasks, analytics")

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
        from backend.auth.seed import seed_test_users
        seed_test_users()
    except Exception as e:
        logger.warning(f"test users seeding skipped: {e}")

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    from backend.config import settings

    port = settings.backend_port or int(os.environ.get("BACKEND_PORT", random.randint(8000, 9000)))
    logger.info(f"Starting FastAPI on http://localhost:{port}")
    uvicorn.run(app, host="localhost", port=port)
