"""Owner-only BYOK management for specialized OCR providers.

These endpoints manage one Baidu Unlimited-OCR credential record.  They do not
register an LLM expert, create a shared provider, or change OCR task selection.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError

from backend.auth import require_teacher
from backend.config import settings
from backend.db.ocr_provider_repository import (
    BAIDU_UNLIMITED_OCR_PROVIDER_TYPE,
    OCRCredentialMetadata,
    delete_baidu_unlimited_ocr_credential,
    get_baidu_unlimited_ocr_credential_metadata,
    get_current_baidu_unlimited_ocr_credential_metadata,
    set_baidu_unlimited_ocr_verification,
    upsert_baidu_unlimited_ocr_credential,
)
from backend.models import User
from backend.services.baidu_unlimited_ocr_factory import (
    BaiduCredentialVerifier,
    OCRCredentialEncryptionUnavailableError,
    OCRCredentialNotFoundError,
    get_baidu_unlimited_ocr_credential_verifier,
    get_owner_baidu_unlimited_ocr_credentials,
)


logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/ocr/providers/baidu-unlimited-ocr",
    tags=["ocr-providers"],
)


_SAFE_VERIFICATION_CODES = {
    "provider_auth_failed",
    "provider_permission_denied",
    "provider_rate_limited",
    "provider_timeout",
    "provider_unavailable",
    "provider_unreachable",
    "provider_request_failed",
    "provider_response_invalid",
}


def _not_found() -> HTTPException:
    return HTTPException(
        status.HTTP_404_NOT_FOUND,
        detail={"code": "ocr_credential_not_found"},
    )


def _credential_changed() -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT,
        detail={"code": "ocr_credential_changed"},
    )


def _encryption_unavailable(*, credentials_were_submitted: bool) -> HTTPException:
    if settings.runtime_environment == "production":
        message = (
            "Service configuration is temporarily unavailable. Contact an "
            "administrator."
        )
    else:
        message = (
            "Server BYOK encryption is not configured. Set "
            "SMARTAI_PROVIDER_ENCRYPTION_KEY to a private random value and "
            "restart the backend."
        )
    if credentials_were_submitted:
        message = f"{message} These credentials were not saved."
    return HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "provider_encryption_not_configured",
            "message": message,
        },
    )


def _iso_timestamp(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def _metadata_response(metadata: OCRCredentialMetadata) -> dict[str, object]:
    return {
        "credential_id": metadata.id,
        "provider_type": metadata.provider_type,
        "credentials_configured": True,
        "verification_status": metadata.verification_status,
        "last_checked_at": _iso_timestamp(metadata.last_checked_at),
        "verification_error_code": metadata.verification_error_code,
    }


async def _read_credential_body(request: Request) -> tuple[str, str]:
    """Parse AK/SK without FastAPI echoing secret values in validation errors."""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "ocr_credential_payload_invalid"},
        ) from None
    if not isinstance(payload, dict) or set(payload) != {"api_key", "secret_key"}:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "ocr_credential_payload_invalid"},
        )
    api_key = payload.get("api_key")
    secret_key = payload.get("secret_key")
    if not isinstance(api_key, str) or not isinstance(secret_key, str):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "ocr_credential_payload_invalid"},
        )
    api_key = api_key.strip()
    secret_key = secret_key.strip()
    if not api_key or not secret_key:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "ocr_credential_fields_blank"},
        )
    if len(api_key) > 512 or len(secret_key) > 512:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "ocr_credential_fields_too_long"},
        )
    return api_key, secret_key


@router.put("")
async def put_baidu_unlimited_ocr_credential(
    request: Request,
    current: User = Depends(require_teacher),
):
    """Create or replace this teacher's fixed-provider BYOK credential."""
    api_key, secret_key = await _read_credential_body(request)
    master_key = settings.provider_encryption_key.strip()
    if not master_key:
        raise _encryption_unavailable(credentials_were_submitted=True)
    try:
        metadata = upsert_baidu_unlimited_ocr_credential(
            current.id,
            api_key=api_key,
            secret_key=secret_key,
            master_key=master_key,
        )
    except IntegrityError as exc:
        logger.warning(
            "OCR credential save conflict; provider_type=%s exception_type=%s",
            BAIDU_UNLIMITED_OCR_PROVIDER_TYPE,
            type(exc).__name__,
        )
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "ocr_credential_conflict"},
        ) from exc
    return {"status": "credentials_stored", **_metadata_response(metadata)}


@router.get("")
def get_current_baidu_unlimited_ocr_credential(
    current: User = Depends(require_teacher),
):
    """Return the current owner's fixed-provider metadata, never its secrets."""
    metadata = get_current_baidu_unlimited_ocr_credential_metadata(current.id)
    if metadata is None:
        raise _not_found()
    return _metadata_response(metadata)


@router.get("/configuration")
def get_baidu_unlimited_ocr_configuration(
    current: User = Depends(require_teacher),
):
    """Return a stable empty-or-configured view for the BYOK settings page."""
    metadata = get_current_baidu_unlimited_ocr_credential_metadata(current.id)
    if metadata is None:
        return {
            "provider_type": BAIDU_UNLIMITED_OCR_PROVIDER_TYPE,
            "credentials_configured": False,
            "credential_id": None,
            "verification_status": "not_configured",
            "last_checked_at": None,
            "verification_error_code": None,
        }
    return _metadata_response(metadata)


@router.get("/{credential_id}")
def get_baidu_unlimited_ocr_credential(
    credential_id: str,
    current: User = Depends(require_teacher),
):
    metadata = get_baidu_unlimited_ocr_credential_metadata(
        current.id,
        credential_id,
    )
    if metadata is None:
        raise _not_found()
    return _metadata_response(metadata)


@router.delete("/{credential_id}")
def remove_baidu_unlimited_ocr_credential(
    credential_id: str,
    current: User = Depends(require_teacher),
):
    if not delete_baidu_unlimited_ocr_credential(current.id, credential_id):
        raise _not_found()
    return {
        "status": "credentials_deleted",
        "credential_id": credential_id,
        "provider_type": BAIDU_UNLIMITED_OCR_PROVIDER_TYPE,
    }


@router.post("/{credential_id}/verify")
async def verify_baidu_unlimited_ocr_credential(
    credential_id: str,
    current: User = Depends(require_teacher),
    verifier: BaiduCredentialVerifier = Depends(
        get_baidu_unlimited_ocr_credential_verifier
    ),
):
    """Verify AK/SK by token exchange only; OCR access and quota remain untested."""
    # Resolve ownership before checking the encryption key.  Wrong-owner and
    # nonexistent ids therefore retain the same 404 projection.
    if get_baidu_unlimited_ocr_credential_metadata(current.id, credential_id) is None:
        raise _not_found()
    if not settings.provider_encryption_key.strip():
        raise _encryption_unavailable(credentials_were_submitted=False)
    try:
        credential = get_owner_baidu_unlimited_ocr_credentials(
            current.id,
            credential_id,
        )
    except OCRCredentialNotFoundError:
        raise _not_found() from None
    except (OCRCredentialEncryptionUnavailableError, ValueError) as exc:
        logger.error(
            "OCR credential decrypt unavailable; provider_type=%s exception_type=%s",
            BAIDU_UNLIMITED_OCR_PROVIDER_TYPE,
            type(exc).__name__,
        )
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "provider_credentials_unavailable"},
        ) from None

    try:
        # This call must only exchange AK/SK for an access token.  Its return
        # value is intentionally discarded so a token can never reach the API.
        await asyncio.wait_for(
            verifier(api_key=credential.api_key, secret_key=credential.secret_key),
            timeout=30,
        )
    except Exception as exc:
        error_code = _safe_verification_code(exc)
        checked_at = time.time()
        updated = set_baidu_unlimited_ocr_verification(
            current.id,
            credential_id,
            expected_updated_at=credential.metadata.updated_at,
            verification_status="failed",
            checked_at=checked_at,
            error_code=error_code,
        )
        if not updated:
            raise _credential_changed() from None
        logger.warning(
            "OCR credential verification failed; provider_type=%s "
            "exception_type=%s code=%s",
            BAIDU_UNLIMITED_OCR_PROVIDER_TYPE,
            type(exc).__name__,
            error_code,
        )
        raise HTTPException(
            _verification_http_status(error_code),
            detail={"code": error_code},
        ) from None

    checked_at = time.time()
    updated = set_baidu_unlimited_ocr_verification(
        current.id,
        credential_id,
        expected_updated_at=credential.metadata.updated_at,
        verification_status="credentials_verified",
        checked_at=checked_at,
    )
    if not updated:
        raise _credential_changed()
    return {
        "status": "credentials_verified",
        "credential_id": credential_id,
        "provider_type": BAIDU_UNLIMITED_OCR_PROVIDER_TYPE,
        "verification_scope": "credentials_only",
        "service_readiness": "not_tested",
        "verified_at": _iso_timestamp(checked_at),
    }


def _safe_verification_code(exc: Exception) -> str:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "provider_timeout"
    value = getattr(exc, "code", None)
    if isinstance(value, str) and value in _SAFE_VERIFICATION_CODES:
        return value
    return "provider_verification_failed"


def _verification_http_status(error_code: str) -> int:
    if error_code == "provider_rate_limited":
        return status.HTTP_429_TOO_MANY_REQUESTS
    if error_code in {
        "provider_timeout",
        "provider_unavailable",
        "provider_unreachable",
    }:
        return status.HTTP_503_SERVICE_UNAVAILABLE
    if error_code in {"provider_auth_failed", "provider_permission_denied"}:
        return status.HTTP_422_UNPROCESSABLE_ENTITY
    return status.HTTP_502_BAD_GATEWAY
