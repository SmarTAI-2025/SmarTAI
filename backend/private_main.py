"""Private admin API and optional independently built SPA. Bind to loopback."""
from __future__ import annotations
import os
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from backend.api.admin import router as admin_router
from backend.api.admin_accounts import router as accounts_router
from backend.api.auth import router as auth_router


def create_private_app(*, maintenance_only: bool = False) -> FastAPI:
    if os.getenv("SMARTAI_ADMIN_PRIVATE_ENABLED", "false").lower() != "true":
        raise RuntimeError("Private admin service requires explicit enablement")
    app = FastAPI(title="SmarTAI Private Administration")
    from fastapi.exceptions import RequestValidationError
    @app.exception_handler(RequestValidationError)
    async def safe_validation_error(request, exc):
        # Pydantic includes raw input in errors, including SecretStr inputs.
        # Do not reflect passwords or other credential fields into responses.
        return JSONResponse({"detail": [{"loc": error["loc"], "msg": error["msg"], "type": error["type"]} for error in exc.errors()]}, status_code=422)
    app.state.private_admin = True
    app.state.maintenance_only = maintenance_only
    import asyncio
    from contextlib import suppress
    worker = {"task": None}
    @app.on_event("startup")
    async def start_closure_worker():
        if maintenance_only:
            return
        from backend.services.account_closure import account_closure_loop
        worker["task"] = asyncio.create_task(account_closure_loop())
    @app.on_event("shutdown")
    async def stop_closure_worker():
        if worker["task"] is not None:
            worker["task"].cancel()
            with suppress(asyncio.CancelledError):
                await worker["task"]
    allowed_auth = {"/auth/login", "/auth/logout", "/auth/refresh", "/auth/activity", "/auth/me",
                    "/auth/password-reset/request", "/auth/password-reset/confirm", "/auth/password-change"}
    # Do not mount public registration or dormant role-invitation routes here.
    from fastapi import APIRouter
    auth = APIRouter()
    if maintenance_only:
        allowed_auth = {"/auth/login", "/auth/me", "/auth/logout"}
    auth.routes = [route for route in auth_router.routes if route.path in allowed_auth]
    app.include_router(auth, prefix="/api")
    management = APIRouter()
    management.routes = [route for route in admin_router.routes if "/invites" not in route.path]
    if not maintenance_only:
        app.include_router(management, prefix="/api")
        app.include_router(accounts_router, prefix="/api")
    from backend.api.admin_monitoring import router as monitoring_router
    from backend.api.admin_maintenance import router as maintenance_router
    if not maintenance_only:
        app.include_router(monitoring_router, prefix="/api")
    maintenance = APIRouter()
    maintenance.routes = [route for route in maintenance_router.routes if maintenance_only or route.path.endswith("/preview")]
    app.include_router(maintenance, prefix="/api")
    from backend.api.admin_business_config import router as business_config_router
    if not maintenance_only:
        app.include_router(business_config_router, prefix="/api")

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS", "HEAD"], include_in_schema=False)
    def unknown_api(path: str):
        raise HTTPException(404)

    @app.get("/health")
    def health():
        return {"status": "healthy", "service": "private-admin"}

    @app.get("/ready")
    def ready():
        from backend.db.session import database_ready, admin_schema_ready
        ok = database_ready() and admin_schema_ready()
        return JSONResponse({"status": "ready" if ok else "not_ready", "schema": ok}, status_code=200 if ok else 503)

    origin = os.getenv("SMARTAI_ADMIN_FRONTEND_ORIGIN", "").strip().rstrip("/")
    if origin:
        app.add_middleware(CORSMiddleware, allow_origins=[origin], allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key"])
    static = Path(os.getenv("SMARTAI_ADMIN_DIST", str(Path(__file__).resolve().parents[1] / "frontend/app/dist-admin"))).resolve()
    if (static / "admin.html").is_file():
        if (static / "assets").is_dir():
            app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")
        if (static / "brand").is_dir():
            app.mount("/brand", StaticFiles(directory=static / "brand"), name="brand")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path.startswith(("api/", "assets/", "brand/")) or path == "api":
                raise HTTPException(404)
            return FileResponse(static / "admin.html", headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
    from backend.services.admin_lifecycle import install_maintenance_lifespan
    if not maintenance_only:
        install_maintenance_lifespan(app)
    else:
        from contextlib import ExitStack
        console = ExitStack()
        @app.on_event("startup")
        async def single_maintenance_console():
            import fcntl
            from backend.api.admin_maintenance import configured_scope
            from backend.services.admin_reset import _safe_path
            scope = configured_scope()
            control = _safe_path(scope.maintenance_dir)
            control.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(control / "maintenance-console.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            console.callback(os.close, descriptor)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if scope.engine.dialect.name == "postgresql":
                    from sqlalchemy import text
                    connection = console.enter_context(scope.engine.connect())
                    if not connection.execute(text("SELECT pg_try_advisory_lock(734720260103)")).scalar_one():
                        raise RuntimeError("Another maintenance console is running")
                    console.callback(lambda: connection.execute(text("SELECT pg_advisory_unlock(734720260103)")))
            except BaseException:
                console.close()
                raise
        @app.middleware("http")
        async def maintenance_fence(request, call_next):
            from backend.api.admin_maintenance import _future
            if (_future is not None and not _future.done()) and request.url.path.startswith("/api/") and request.url.path not in {"/api/admin/maintenance/status", "/api/admin/maintenance/execute"}:
                return JSONResponse({"detail": {"code": "maintenance_in_progress", "message": "全站清理期间暂不接受其他请求。"}}, status_code=503)
            return await call_next(request)
        @app.on_event("shutdown")
        async def finish_maintenance():
            from backend.api.admin_maintenance import _future
            if _future is not None:
                await asyncio.to_thread(lambda: _future.result())
            console.close()
    return app


app = create_private_app()
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("SMARTAI_ADMIN_PORT", "8001")))
