"""Map DomainError subtypes to a stable FastAPI error envelope.

Frontends branch on the stable ``code`` string and never parse the natural-
language message, so wording can evolve without breaking clients. Keeping this
in one place means every normalized router reports failures identically.
"""
from __future__ import annotations

from fastapi import HTTPException, status
from fastapi.responses import JSONResponse

from backend.domain.errors import DomainError


def domain_error_response(exc: DomainError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


def map_domain_error(exc: DomainError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.message)
