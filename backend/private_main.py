"""Separate administrator API entry point.

Run only behind a private gateway or on loopback. The public ``backend.main``
application never mounts these routes. There is no shared default password.
"""
from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.admin import router as admin_router
from backend.api.auth import router as auth_router
from backend.config import settings


def create_private_app() -> FastAPI:
    if os.getenv("SMARTAI_ADMIN_PRIVATE_ENABLED", "false").lower() != "true":
        raise RuntimeError("Private admin API requires SMARTAI_ADMIN_PRIVATE_ENABLED=true")
    app = FastAPI(title="SmarTAI Private Administration")
    app.include_router(auth_router)
    app.include_router(admin_router)
    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "healthy", "service": "private-admin"}
    admin_origin = os.getenv("SMARTAI_ADMIN_FRONTEND_ORIGIN", "").strip()
    if admin_origin:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[admin_origin],
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
        )
    return app


app = create_private_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("SMARTAI_ADMIN_PORT", "8001")))
