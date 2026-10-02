"""Keep maintenance exclusion around startup, requests and worker shutdown."""
import os
import asyncio
from contextlib import asynccontextmanager, ExitStack


def install_maintenance_lifespan(app):
    previous = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        with ExitStack() as stack:
            guarded = False
            if os.environ.get("SMARTAI_ADMIN_MAINTENANCE_DIR") or os.environ.get("SMARTAI_ADMIN_RESET_ENABLED", "").lower() == "true":
                from backend.services.admin_reset import maintenance_service_guard, scope_from_settings
                stack.enter_context(maintenance_service_guard(scope_from_settings()))
                guarded = True
            from backend.config import settings
            if settings.runtime_environment == "production":
                from backend.db.session import admin_schema_ready
                if not admin_schema_ready():
                    raise RuntimeError("Database migrations must be applied before starting the service")
            try:
                async with previous(application):
                    yield
            finally:
                if guarded:
                    # to_thread work may survive cancellation of its awaiter.
                    # Maintenance services own their loop until process exit.
                    await asyncio.get_running_loop().shutdown_default_executor()
    app.router.lifespan_context = lifespan
