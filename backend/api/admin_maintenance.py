"""Read-only reset preview. No HTTP deletion endpoint exists."""
import os
from fastapi import APIRouter, Depends, Response
from backend.auth import require_admin
from backend.models import User

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/maintenance/preview")
def reset_preview(response: Response, current: User = Depends(require_admin)):
    response.headers["Cache-Control"] = "no-store"
    from backend.config import settings
    if settings.runtime_environment not in {"development", "test"}:
        return {"available": False, "reason": "production_execution_not_enabled", "environment": settings.runtime_environment}
    if os.environ.get("SMARTAI_ADMIN_RESET_ENABLED", "").lower() != "true":
        return {"available": False, "reason": "isolated_reset_not_configured", "environment": settings.runtime_environment}
    try:
        from backend.services.admin_reset import preview_reset, scope_from_settings, ResetError
        return {"available": True, **preview_reset(scope_from_settings())}
    except ResetError:
        return {"available": False, "reason": "scope_needs_offline_validation", "environment": settings.runtime_environment}
    except Exception:
        response.status_code = 503
        return {"available": False, "reason": "preview_unavailable"}
