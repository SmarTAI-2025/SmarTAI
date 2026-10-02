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


def create_private_app() -> FastAPI:
    if os.getenv("SMARTAI_ADMIN_PRIVATE_ENABLED", "false").lower() != "true":
        raise RuntimeError("Private admin service requires explicit enablement")
    app = FastAPI(title="SmarTAI Private Administration")
    app.state.private_admin = True
    import asyncio
    from contextlib import suppress
    worker = {"task": None}
    @app.on_event("startup")
    async def start_closure_worker():
        from backend.services.account_closure import account_closure_loop
        worker["task"] = asyncio.create_task(account_closure_loop())
    @app.on_event("shutdown")
    async def stop_closure_worker():
        if worker["task"] is not None:
            worker["task"].cancel()
            with suppress(asyncio.CancelledError):
                await worker["task"]
    allowed_auth = {"/auth/login", "/auth/logout", "/auth/refresh", "/auth/me",
                    "/auth/password-reset/request", "/auth/password-reset/confirm", "/auth/password-change"}
    # Do not mount public registration or dormant role-invitation routes here.
    from fastapi import APIRouter
    auth = APIRouter()
    auth.routes = [route for route in auth_router.routes if route.path in allowed_auth]
    app.include_router(auth, prefix="/api")
    management = APIRouter()
    management.routes = [route for route in admin_router.routes if "/invites" not in route.path]
    app.include_router(management, prefix="/api")
    app.include_router(accounts_router, prefix="/api")
    from backend.api.admin_monitoring import router as monitoring_router
    from backend.api.admin_maintenance import router as maintenance_router
    app.include_router(monitoring_router, prefix="/api")
    app.include_router(maintenance_router, prefix="/api")
    from backend.api.admin_business_config import router as business_config_router
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
    install_maintenance_lifespan(app)
    return app


app = create_private_app()
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("SMARTAI_ADMIN_PORT", "8001")))
