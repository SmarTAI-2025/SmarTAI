"""
Experts API router — BYOK (Bring Your Own Key) management endpoints.

  POST /experts/keys        — register a new provider
  GET  /experts/available   — list available providers (redacted keys)
  POST /experts/select      — enable/disable specific providers
  DELETE /experts/{id}      — remove a provider
"""
from __future__ import annotations

import asyncio
import logging
import ssl
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from backend.auth import require_teacher
from backend.config import settings
from backend.db.provider_repository import (
    delete_provider_config,
    get_provider_config,
    list_provider_configs,
    set_provider_enabled,
    set_provider_verification,
    set_provider_vision_verification,
    update_provider_config,
    upsert_provider_config,
)
from backend.llm.endpoint_policy import (
    CUSTOM_PROVIDER_RISK_ACK_VERSION,
    CUSTOM_PROVIDER_TYPE,
    ProviderEndpointError,
    normalize_provider_endpoint,
    resolve_public_endpoint,
)
from backend.llm.providers import VisionImage
from backend.llm.registry import (
    ExpertRegistry,
    get_scoped_expert_registry,
    provider_encryption_not_configured_error,
)
from backend.models import ProviderConfig, ProviderType, User

logger = logging.getLogger(__name__)

_custom_probe_lock = threading.Lock()
_custom_probe_last_at: dict[tuple[str, str], float] = {}

router = APIRouter(prefix="/experts", tags=["experts"])


class AddKeyRequest(BaseModel):
    provider_type: ProviderType
    api_key: str = Field(min_length=1, max_length=512)
    model: str = Field(min_length=1, max_length=200)
    base_url: Optional[str] = Field(default=None, max_length=512)
    display_name: Optional[str] = Field(default=None, max_length=120)
    max_concurrent: int = Field(default=5, ge=1, le=10)
    rpm: int = Field(default=0, ge=0, le=10_000)
    risk_ack_version: Optional[str] = Field(default=None, max_length=64)


class SelectRequest(BaseModel):
    provider_id: str
    enabled: bool


class UpdateKeyRequest(BaseModel):
    api_key: Optional[str] = Field(default=None, max_length=512)
    model: str = Field(min_length=1, max_length=200)
    base_url: Optional[str] = Field(default=None, max_length=512)
    display_name: Optional[str] = Field(default=None, max_length=120)
    max_concurrent: int = Field(default=5, ge=1, le=10)
    rpm: int = Field(default=0, ge=0, le=10_000)
    risk_ack_version: Optional[str] = Field(default=None, max_length=64)


_PROVIDER_CATALOG = (
    {
        "provider_type": "gemini",
        "display_name": "Google Gemini",
        "docs_url": "https://ai.google.dev/gemini-api/docs",
        "console_url": "https://aistudio.google.com/app/apikey",
        "usage_url": "https://aistudio.google.com/usage",
    },
    {
        "provider_type": "openai",
        "display_name": "OpenAI",
        "docs_url": "https://platform.openai.com/docs",
        "console_url": "https://platform.openai.com/api-keys",
        "usage_url": "https://platform.openai.com/usage",
    },
    {
        "provider_type": "zhipu",
        "display_name": "Zhipu AI",
        "docs_url": "https://docs.bigmodel.cn/",
        "console_url": "https://open.bigmodel.cn/usercenter/apikeys",
        "usage_url": "https://open.bigmodel.cn/console/overview",
    },
    {
        "provider_type": "deepseek",
        "display_name": "DeepSeek",
        "docs_url": "https://api-docs.deepseek.com/",
        "console_url": "https://platform.deepseek.com/api_keys",
        "usage_url": "https://platform.deepseek.com/usage",
    },
    {
        "provider_type": "moonshot",
        "display_name": "Moonshot (Kimi)",
        "docs_url": "https://platform.moonshot.cn/docs",
        "console_url": "https://platform.moonshot.cn/console/api-keys",
        "usage_url": "https://platform.moonshot.cn/console/account",
    },
    {
        "provider_type": "qwen",
        "display_name": "Qwen (通义千问)",
        "docs_url": "https://help.aliyun.com/zh/model-studio/",
        "console_url": "https://bailian.console.aliyun.com/",
        "usage_url": "https://bailian.console.aliyun.com/",
    },
    {
        "provider_type": "anthropic",
        "display_name": "Anthropic",
        "docs_url": "https://docs.anthropic.com/en/docs",
        "console_url": "https://platform.claude.com/settings/keys",
        "usage_url": "https://platform.claude.com/usage",
    },
)


def _validated_provider_base_url(
    provider_type: str,
    value: Optional[str],
) -> tuple[Optional[str], str]:
    try:
        return normalize_provider_endpoint(provider_type, value)
    except ProviderEndpointError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": exc.code},
        ) from exc
def _validate_custom_request(
    provider_type: str,
    base_url: str | None,
    risk_ack_version: str | None,
    *,
    owner_id: str,
) -> tuple[str | None, str]:
    if provider_type == CUSTOM_PROVIDER_TYPE:
        if not settings.custom_provider_endpoints_enabled:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail={"code": "custom_provider_endpoints_disabled"},
            )
        if risk_ack_version != CUSTOM_PROVIDER_RISK_ACK_VERSION:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "provider_endpoint_risk_ack_required"},
            )
        _check_custom_probe_limit(owner_id, "save")
    normalized, endpoint_identity = _validated_provider_base_url(provider_type, base_url)
    if provider_type == CUSTOM_PROVIDER_TYPE:
        assert normalized is not None
        try:
            resolve_public_endpoint(normalized)
        except ProviderEndpointError as exc:
            raise HTTPException(
                _verification_http_status(exc.code),
                detail={"code": exc.code},
            ) from exc
    return normalized, endpoint_identity


def _check_custom_probe_limit(owner_id: str, operation: str) -> None:
    now = time.monotonic()
    cooldown = max(0, settings.custom_provider_verification_cooldown_seconds)
    key = (owner_id, operation)
    with _custom_probe_lock:
        expiry = now - (2 * cooldown)
        for existing_key, checked_at in list(_custom_probe_last_at.items()):
            if checked_at < expiry:
                _custom_probe_last_at.pop(existing_key, None)
        previous = _custom_probe_last_at.get(key, 0.0)
        if now - previous < cooldown:
            retry_after = max(1, int(cooldown - (now - previous) + 0.999))
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "code": "provider_endpoint_probe_rate_limited",
                    "retry_after_seconds": retry_after,
                },
                headers={"Retry-After": str(retry_after)},
            )
        _custom_probe_last_at[key] = now


@router.post("/keys")
def add_key(
    request: AddKeyRequest,
    current: User = Depends(require_teacher),
    registry: ExpertRegistry = Depends(get_scoped_expert_registry),
):
    """Register or update a provider's API key."""
    api_key = request.api_key.strip()
    model = request.model.strip()
    if not api_key or not model:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "expert_fields_blank"},
        )
    base_url, endpoint_identity = _validate_custom_request(
        request.provider_type,
        request.base_url,
        request.risk_ack_version,
        owner_id=current.id,
    )
    config = ProviderConfig(
        provider_type=request.provider_type,
        api_key=api_key,
        model=model,
        base_url=base_url,
        endpoint_identity=endpoint_identity,
        enabled=request.provider_type != CUSTOM_PROVIDER_TYPE,
        display_name=(request.display_name.strip() if request.display_name else None),
        max_concurrent=request.max_concurrent,
        rpm=request.rpm,
    )
    if not settings.provider_encryption_key:
        raise provider_encryption_not_configured_error(api_key_was_submitted=True)
    if request.provider_type == CUSTOM_PROVIDER_TYPE:
        stored_configs = list_provider_configs(
            current.id,
            master_key=settings.provider_encryption_key,
        )
        custom_configs = [
            stored
            for stored in stored_configs
            if stored.config.provider_type == CUSTOM_PROVIDER_TYPE
        ]
        already_exists = any(
            stored.config.endpoint_identity == endpoint_identity
            and stored.config.model == model
            for stored in custom_configs
        )
        if (
            not already_exists
            and len(custom_configs) >= settings.custom_provider_max_per_owner
        ):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={"code": "custom_provider_limit_reached"},
            )
    try:
        record = upsert_provider_config(
            current.id,
            config,
            master_key=settings.provider_encryption_key,
            risk_ack_version=(
                request.risk_ack_version
                if request.provider_type == CUSTOM_PROVIDER_TYPE
                else None
            ),
        )
    except IntegrityError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "expert_provider_conflict"},
        ) from exc
    # A teacher with no saved BYOK configuration may have received a transient
    # shared-pool registry for this request. Persist the custom configuration,
    # but never inject it into that shared object; the next owner-scoped request
    # rebuilds a registry from this saved record only.
    provider_id = record.id
    if not registry.uses_shared_pool():
        provider_id = registry.register(
            config,
            provider_id=record.id,
            risk_ack_version=record.risk_ack_version,
            risk_ack_at=record.risk_ack_at,
        )
    return {
        "status": "success",
        "provider_id": provider_id,
        "base_url": base_url if request.provider_type == CUSTOM_PROVIDER_TYPE else None,
        "verification_status": "unverified",
    }


@router.get("/available")
def list_available(
    current: User = Depends(require_teacher),
    registry: ExpertRegistry = Depends(get_scoped_expert_registry),
):
    """List all configured providers with redacted API keys.

    Each item contains: provider_id, provider_type, model, base_url, enabled,
    display_name, max_concurrent. The frontend dropdown uses provider_id as
    value and display_name as label.
    """
    return registry.list_configs()


@router.get("/catalog")
def provider_catalog(current: User = Depends(require_teacher)):
    """Return fixed, non-secret links used by the BYOK settings screen."""
    catalog = list(_PROVIDER_CATALOG)
    if settings.custom_provider_endpoints_enabled:
        catalog.append({
            "provider_type": CUSTOM_PROVIDER_TYPE,
            "display_name": "Custom OpenAI-compatible service",
            "custom": True,
            "risk_ack_version": CUSTOM_PROVIDER_RISK_ACK_VERSION,
        })
    return catalog


@router.post("/select")
def select_provider(
    request: SelectRequest,
    current: User = Depends(require_teacher),
    registry: ExpertRegistry = Depends(get_scoped_expert_registry),
):
    """Enable or disable a specific provider."""
    stored = (
        get_provider_config(
            current.id,
            request.provider_id,
            master_key=settings.provider_encryption_key,
        )
        if settings.provider_encryption_key
        else None
    )
    if (
        stored is not None
        and stored.config.provider_type == CUSTOM_PROVIDER_TYPE
        and not settings.custom_provider_endpoints_enabled
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={"code": "custom_provider_endpoints_disabled"},
        )
    if (
        request.enabled
        and stored is not None
        and stored.config.provider_type == CUSTOM_PROVIDER_TYPE
        and (
            stored.verification_status != "verified"
            or stored.risk_ack_version != CUSTOM_PROVIDER_RISK_ACK_VERSION
        )
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "provider_endpoint_not_verified"},
        )
    if set_provider_enabled(current.id, request.provider_id, request.enabled):
        return {"status": "success", "provider_id": request.provider_id, "enabled": request.enabled}
    return {"status": "not_found", "message": f"Provider {request.provider_id} not found."}


@router.put("/{provider_id}")
def update_provider(
    provider_id: str,
    request: UpdateKeyRequest,
    current: User = Depends(require_teacher),
    registry: ExpertRegistry = Depends(get_scoped_expert_registry),
):
    """Update one persisted BYOK entry without requiring the key again."""
    if not settings.provider_encryption_key:
        raise provider_encryption_not_configured_error(
            api_key_was_submitted=request.api_key is not None,
        )
    existing = get_provider_config(
        current.id,
        provider_id,
        master_key=settings.provider_encryption_key,
    )
    if existing is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Provider not found")
    model = request.model.strip()
    provided_key = request.api_key.strip() if request.api_key else ""
    if not model:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "expert_fields_blank"},
        )
    base_url, endpoint_identity = _validate_custom_request(
        existing.config.provider_type,
        request.base_url,
        request.risk_ack_version,
        owner_id=current.id,
    )
    config = ProviderConfig(
        provider_type=existing.config.provider_type,
        api_key=provided_key or existing.config.api_key,
        model=model,
        base_url=base_url,
        endpoint_identity=endpoint_identity,
        enabled=(
            False
            if existing.config.provider_type == CUSTOM_PROVIDER_TYPE
            else existing.config.enabled
        ),
        display_name=(
            request.display_name.strip()
            if request.display_name and request.display_name.strip()
            else None
        ),
        max_concurrent=request.max_concurrent,
        rpm=request.rpm,
    )
    try:
        updated = update_provider_config(
            current.id,
            provider_id,
            config,
            master_key=settings.provider_encryption_key,
            risk_ack_version=(
                request.risk_ack_version
                if existing.config.provider_type == CUSTOM_PROVIDER_TYPE
                else None
            ),
        )
    except IntegrityError as exc:
        # Unique (owner, provider_type, model) conflicts are the only expected
        # persistence failure here. Keep the response stable and do not expose
        # driver text or encrypted-record details.
        logger.warning(
            "BYOK update failed; exception_type=%s",
            type(exc).__name__,
        )
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "expert_provider_conflict"},
        ) from exc
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Provider not found")
    if not registry.uses_shared_pool():
        registry.register(
            config,
            provider_id=provider_id,
            risk_ack_version=updated.risk_ack_version,
            risk_ack_at=updated.risk_ack_at,
        )
    return {
        "status": "success",
        "provider_id": provider_id,
        "verification_status": "unverified",
    }


@router.post("/{provider_id}/verify")
async def verify_provider(
    provider_id: str,
    current: User = Depends(require_teacher),
    registry: ExpertRegistry = Depends(get_scoped_expert_registry),
):
    """Perform one bounded verification call and return only a safe summary."""
    stored = (
        get_provider_config(
            current.id,
            provider_id,
            master_key=settings.provider_encryption_key,
        )
        if settings.provider_encryption_key
        else None
    )
    provider = registry.get(provider_id, include_unverified=True)
    if stored is None or provider is None or registry.uses_shared_pool():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Provider not found")
    if provider.provider_type == CUSTOM_PROVIDER_TYPE:
        if not settings.custom_provider_endpoints_enabled:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail={"code": "custom_provider_endpoints_disabled"},
            )
        if stored.risk_ack_version != CUSTOM_PROVIDER_RISK_ACK_VERSION:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={"code": "provider_endpoint_risk_ack_required"},
            )
        _check_custom_probe_limit(current.id, "verify_text")
    try:
        timeout_seconds = max(
            5,
            min(
                int(settings.llm_timeout),
                int(settings.custom_provider_verification_timeout_seconds),
            ),
        )
        response = await asyncio.wait_for(
            provider.ainvoke([
                HumanMessage(content="Reply with exactly SMARTAI_TEXT_PROBE_OK.")
            ]),
            timeout=timeout_seconds,
        )
        if (
            provider.provider_type == CUSTOM_PROVIDER_TYPE
            and str(response.content).strip() != "SMARTAI_TEXT_PROBE_OK"
        ):
            raise ProviderEndpointError("provider_endpoint_protocol_mismatch")
    except Exception as exc:
        error_code = _verification_error_code(exc)
        persisted = set_provider_verification(
            current.id,
            provider_id,
            verification_status="failed",
            checked_at=time.time(),
            error_code=error_code,
            expected_updated_at=stored.updated_at,
        )
        if not persisted:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={"code": "expert_verification_stale"},
            ) from exc
        logger.warning(
            "BYOK verification failed; provider_type=%s exception_type=%s code=%s",
            provider.provider_type,
            type(exc).__name__,
            error_code,
        )
        raise HTTPException(
            _verification_http_status(error_code),
            detail={"code": error_code, "provider_id": provider_id},
        ) from exc
    checked_at = time.time()
    persisted = set_provider_verification(
        current.id,
        provider_id,
        verification_status="verified",
        checked_at=checked_at,
        expected_updated_at=stored.updated_at,
    )
    if not persisted:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "expert_verification_stale"},
        )
    return {
        "status": "success",
        "provider_id": provider_id,
        "verification_status": "verified",
        "last_checked_at": datetime.fromtimestamp(
            checked_at, tz=timezone.utc
        ).isoformat(),
        "verified_at": datetime.fromtimestamp(
            checked_at, tz=timezone.utc
        ).isoformat(),
    }


@router.post("/{provider_id}/verify-vision")
async def verify_provider_vision(
    provider_id: str,
    current: User = Depends(require_teacher),
    registry: ExpertRegistry = Depends(get_scoped_expert_registry),
):
    """Verify custom vision with one repository synthetic image."""
    stored = (
        get_provider_config(
            current.id,
            provider_id,
            master_key=settings.provider_encryption_key,
        )
        if settings.provider_encryption_key
        else None
    )
    provider = registry.get(provider_id, include_unverified=True)
    if (
        stored is None
        or provider is None
        or registry.uses_shared_pool()
        or provider.provider_type != CUSTOM_PROVIDER_TYPE
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Provider not found")
    if not settings.custom_provider_endpoints_enabled:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={"code": "custom_provider_endpoints_disabled"},
        )
    if stored.verification_status != "verified":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "provider_endpoint_not_verified"},
        )
    if stored.risk_ack_version != CUSTOM_PROVIDER_RISK_ACK_VERSION:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "provider_endpoint_risk_ack_required"},
        )
    _check_custom_probe_limit(current.id, "verify_vision")
    sample = (
        Path(__file__).resolve().parents[2]
        / "SmarTAI_test_case"
        / "ocr_samples"
        / "S003_synthetic_geography_handwriting.png"
    )
    try:
        provider.supports_vision = True
        response = await asyncio.wait_for(
            provider.ainvoke_vision(
                "Return only the sample identifier written at the top-left of this synthetic image.",
                [VisionImage(data=sample.read_bytes(), media_type="image/png")],
            ),
            timeout=max(
                5,
                int(settings.custom_provider_verification_timeout_seconds),
            ),
        )
        if "S003" not in response.content.upper().replace(" ", ""):
            raise ValueError("vision_probe_mismatch")
    except Exception as exc:
        checked_at = time.time()
        error_code = _verification_error_code(exc)
        persisted = set_provider_vision_verification(
            current.id,
            provider_id,
            verification_status="failed",
            checked_at=checked_at,
            error_code=error_code,
            expected_updated_at=stored.updated_at,
        )
        if not persisted:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={"code": "expert_verification_stale"},
            ) from exc
        raise HTTPException(
            _verification_http_status(error_code),
            detail={"code": error_code, "provider_id": provider_id},
        ) from exc
    checked_at = time.time()
    persisted = set_provider_vision_verification(
        current.id,
        provider_id,
        verification_status="verified",
        checked_at=checked_at,
        expected_updated_at=stored.updated_at,
    )
    if not persisted:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "expert_verification_stale"},
        )
    return {
        "status": "success",
        "provider_id": provider_id,
        "vision_verification_status": "verified",
        "vision_last_checked_at": datetime.fromtimestamp(
            checked_at, tz=timezone.utc
        ).isoformat(),
    }


@router.delete("/{provider_id}")
def remove_provider(
    provider_id: str,
    current: User = Depends(require_teacher),
    registry: ExpertRegistry = Depends(get_scoped_expert_registry),
):
    """Remove a provider entirely."""
    existed = delete_provider_config(current.id, provider_id)
    if existed:
        return {"status": "success", "message": f"Provider {provider_id} removed."}
    return {"status": "not_found", "message": f"Provider {provider_id} not found."}


def _verification_error_code(exc: Exception) -> str:
    exception_chain: list[BaseException] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        exception_chain.append(current)
        current = current.__cause__ or current.__context__

    for item in exception_chain:
        if isinstance(item, ProviderEndpointError):
            return item.code
        if isinstance(item, ssl.SSLError):
            return "provider_endpoint_tls_failed"
        if str(item) == "vision_probe_mismatch":
            return "provider_endpoint_protocol_mismatch"

    timeout_types: tuple[type[BaseException], ...] = (
        asyncio.TimeoutError,
        TimeoutError,
    )
    connection_types: tuple[type[BaseException], ...] = (ConnectionError, OSError)
    try:
        import httpx

        timeout_types += (httpx.TimeoutException,)
        connection_types += (httpx.TransportError,)
    except ImportError:  # pragma: no cover - httpx is a runtime dependency
        pass
    try:
        from openai import APIConnectionError, APITimeoutError

        timeout_types += (APITimeoutError,)
        connection_types += (APIConnectionError,)
    except ImportError:  # pragma: no cover - OpenAI adapter is optional
        pass
    try:
        from anthropic import APIConnectionError as AnthropicAPIConnectionError
        from anthropic import APITimeoutError as AnthropicAPITimeoutError

        timeout_types += (AnthropicAPITimeoutError,)
        connection_types += (AnthropicAPIConnectionError,)
    except ImportError:  # pragma: no cover - Anthropic adapter is optional
        pass

    if any(isinstance(item, timeout_types) for item in exception_chain):
        return "expert_verification_timeout"
    status_code = next(
        (
            getattr(item, "status_code", None)
            or getattr(getattr(item, "response", None), "status_code", None)
            for item in exception_chain
            if (
                getattr(item, "status_code", None) is not None
                or getattr(getattr(item, "response", None), "status_code", None)
                is not None
            )
        ),
        None,
    )
    if isinstance(status_code, int) and 300 <= status_code < 400:
        return "provider_endpoint_redirect_blocked"
    if status_code in {401, 403}:
        return "expert_verification_auth_failed"
    if status_code == 404:
        return "expert_verification_model_not_found"
    if status_code == 429:
        return "expert_verification_rate_limited"
    try:
        import httpx

        if any(isinstance(item, httpx.RemoteProtocolError) for item in exception_chain):
            return "provider_endpoint_protocol_mismatch"
    except ImportError:  # pragma: no cover
        pass
    if any(isinstance(item, connection_types) for item in exception_chain):
        return "expert_verification_connection_failed"
    return "expert_verification_provider_error"


def _verification_http_status(error_code: str) -> int:
    if error_code == "expert_verification_rate_limited":
        return status.HTTP_429_TOO_MANY_REQUESTS
    if error_code in {
        "expert_verification_timeout",
        "expert_verification_connection_failed",
        "provider_endpoint_dns_failed",
    }:
        return status.HTTP_503_SERVICE_UNAVAILABLE
    if error_code.startswith("provider_endpoint_"):
        return status.HTTP_422_UNPROCESSABLE_ENTITY
    return status.HTTP_502_BAD_GATEWAY
