"""Map DomainError subtypes to a stable FastAPI error envelope.

Frontends branch on the stable ``code`` string and never parse the natural-
language message, so wording can evolve without breaking clients. Keeping this
in one place means every normalized router reports failures identically.
"""
from __future__ import annotations

import math

from fastapi import HTTPException, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from backend.domain.errors import DomainError


async def request_validation_error_response(_request, exc: RequestValidationError) -> JSONResponse:
    # JSON numbers such as 1e999 decode to infinity. Preserve validation details,
    # replacing only non-finite values that JSONResponse cannot serialize.
    errors = jsonable_encoder(exc.errors(), custom_encoder={
        float: lambda value: value if math.isfinite(value) else None,
    })
    return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": errors})


def domain_error_response(exc: DomainError) -> JSONResponse:
    error = {"code": exc.code, "message": exc.message}
    if exc.details is not None:
        error["details"] = exc.details
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": error},
    )


def map_domain_error(exc: DomainError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.message)
