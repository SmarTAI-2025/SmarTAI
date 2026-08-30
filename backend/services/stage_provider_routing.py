"""One explicit routing contract for task-stage provider records.

Generic LLM configurations remain in ``ExpertRegistry``.  Specialized Baidu
Unlimited-OCR credentials remain in their independent owner-scoped repository.
This module only exposes a discriminated route reference so the existing task
workflow can freeze either record without creating a second provider registry,
task workflow, or credential fallback.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import select

from backend.db.models import OCRProviderCredentialRecord
from backend.db.ocr_provider_repository import (
    BAIDU_UNLIMITED_OCR_PROVIDER_TYPE,
    get_baidu_unlimited_ocr_credential_metadata,
    get_current_baidu_unlimited_ocr_credential_metadata,
)
from backend.db.session import session_scope
from backend.domain.errors import ValidationError
from backend.llm.registry import resolve_owner_default_provider_id
from backend.services.baidu_unlimited_ocr_factory import (
    OCRCredentialEncryptionUnavailableError,
    OCRCredentialNotFoundError,
    build_owner_baidu_unlimited_ocr_skill,
)


BAIDU_OCR_ROUTE_PREFIX = "ocr:baidu_unlimited_ocr:"
BAIDU_OCR_DISPLAY_NAME = "百度文档解析（Unlimited-OCR）"


@dataclass(frozen=True)
class StageProviderRoute:
    route_id: str
    kind: Literal["llm", "ocr"]
    provider: Any | None = None
    credential_id: str | None = None

    @property
    def is_baidu_ocr(self) -> bool:
        return self.kind == "ocr" and self.credential_id is not None


def stage_provider_configuration_fingerprint(
    *,
    owner_id: str,
    route: StageProviderRoute,
    registry,
) -> str:
    """Hash the exact invocation route without persisting plaintext secrets."""

    if route.is_baidu_ocr:
        assert route.credential_id is not None
        with session_scope() as session:
            record = session.scalar(
                select(OCRProviderCredentialRecord).where(
                    OCRProviderCredentialRecord.id == route.credential_id,
                    OCRProviderCredentialRecord.owner_id == owner_id,
                    OCRProviderCredentialRecord.provider_type
                    == BAIDU_UNLIMITED_OCR_PROVIDER_TYPE,
                )
            )
            if record is None:
                raise ValidationError(
                    "The selected OCR credential is unavailable.",
                    code="ocr_credential_not_found",
                )
            row = {
                "route_id": route.route_id,
                "provider_type": record.provider_type,
                "api_key_version": record.api_key_version,
                "secret_key_version": record.secret_key_version,
                "api_key_secret_digest": hashlib.sha256(
                    (
                        f"{record.encrypted_api_key}:"
                        f"{record.api_key_nonce}"
                    ).encode("utf-8")
                ).hexdigest(),
                "secret_key_secret_digest": hashlib.sha256(
                    (
                        f"{record.encrypted_secret_key}:"
                        f"{record.secret_key_nonce}"
                    ).encode("utf-8")
                ).hexdigest(),
                "scope": "owner_ocr",
            }
    else:
        provider = route.provider
        config = getattr(provider, "config", None)
        if config is not None and callable(getattr(config, "model_dump", None)):
            config_row = config.model_dump(mode="json")
            api_key = str(config_row.pop("api_key", ""))
            row = {
                "route_id": route.route_id,
                "provider_type": config_row.get("provider_type"),
                "model": config_row.get("model"),
                "base_url": config_row.get("base_url"),
                "endpoint_identity": config_row.get("endpoint_identity"),
                "wire_protocol": config_row.get("wire_protocol"),
                "enabled": config_row.get("enabled"),
                "max_concurrent": config_row.get("max_concurrent"),
                "rpm": config_row.get("rpm"),
                "secret_digest": hashlib.sha256(
                    api_key.encode("utf-8")
                ).hexdigest(),
                "scope": "llm",
            }
        else:
            # Deterministic injected providers used by tests and local E2E do
            # not carry a ProviderConfig. Their redacted registry row remains
            # sufficient to detect route/model changes without storing data.
            config_rows = [
                item
                for item in registry.list_configs()
                if str(item.get("provider_id")) == route.route_id
                and item.get("enabled")
            ]
            if len(config_rows) != 1 or provider is None:
                raise ValidationError(
                    "The selected recognition provider is not enabled.",
                    code="recognition_provider_not_enabled",
                )
            item = config_rows[0]
            row = {
                key: item.get(key)
                for key in (
                    "provider_id",
                    "provider_type",
                    "model",
                    "base_url",
                    "endpoint_identity",
                    "wire_protocol",
                    "enabled",
                    "max_concurrent",
                    "rpm",
                    "scope",
                    "is_shared",
                    "supports_vision",
                )
            }
    return hashlib.sha256(
        json.dumps(
            row,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def baidu_ocr_route_id(credential_id: str) -> str:
    normalized = str(credential_id or "").strip()
    if not normalized or len(normalized) > 64 or ":" in normalized:
        raise ValueError("invalid OCR credential id")
    return f"{BAIDU_OCR_ROUTE_PREFIX}{normalized}"


def baidu_ocr_credential_id(route_id: str | None) -> str | None:
    value = str(route_id or "").strip()
    if not value.startswith(BAIDU_OCR_ROUTE_PREFIX):
        return None
    credential_id = value.removeprefix(BAIDU_OCR_ROUTE_PREFIX)
    if not credential_id or len(credential_id) > 64 or ":" in credential_id:
        return None
    return credential_id


def list_stage_provider_options(owner_id: str, registry) -> list[dict[str, Any]]:
    """Return non-secret options from the two existing scoped repositories."""
    default_provider_id = resolve_owner_default_provider_id(owner_id, registry)
    options = [
        {
            **item,
            "provider_kind": "llm",
            "is_default": item.get("provider_id") == default_provider_id,
        }
        for item in registry.list_configs()
    ]
    metadata = get_current_baidu_unlimited_ocr_credential_metadata(owner_id)
    if metadata is not None:
        verification_status = {
            "credentials_verified": "verified",
            "failed": "failed",
        }.get(metadata.verification_status, "unverified")
        options.append({
            "provider_id": baidu_ocr_route_id(metadata.id),
            "credential_id": metadata.id,
            "provider_type": BAIDU_UNLIMITED_OCR_PROVIDER_TYPE,
            "provider_kind": "ocr",
            "model": "unlimited-ocr-parser",
            "display_name": BAIDU_OCR_DISPLAY_NAME,
            "configured_display_name": BAIDU_OCR_DISPLAY_NAME,
            "resolved_display_name": BAIDU_OCR_DISPLAY_NAME,
            "base_url": None,
            "enabled": True,
            "scope": "owner",
            "is_shared": False,
            "editable": False,
            "is_default": False,
            "supports_vision": True,
            "max_concurrent": 1,
            "rpm": 0,
            "verification_status": verification_status,
            "last_checked_at": (
                datetime.fromtimestamp(
                    metadata.last_checked_at,
                    tz=timezone.utc,
                ).isoformat()
                if metadata.last_checked_at is not None
                else None
            ),
            "verification_error_code": metadata.verification_error_code,
        })
    return options


def resolve_stage_provider_route(
    *,
    owner_id: str,
    registry,
    requested_route_id: str | None,
) -> StageProviderRoute:
    """Resolve exactly one owner-scoped route; never choose an alternate."""
    route_id = requested_route_id or resolve_owner_default_provider_id(
        owner_id,
        registry,
    )
    if not route_id:
        raise ValidationError(
            "No enabled recognition provider is available.",
            code="no_provider_configured",
        )

    credential_id = baidu_ocr_credential_id(route_id)
    if credential_id is not None:
        metadata = get_baidu_unlimited_ocr_credential_metadata(
            owner_id,
            credential_id,
        )
        if metadata is None:
            raise ValidationError(
                "The selected OCR credential is unavailable.",
                code="ocr_credential_not_found",
            )
        return StageProviderRoute(
            route_id=route_id,
            kind="ocr",
            credential_id=credential_id,
        )

    enabled_ids = {
        str(item.get("provider_id"))
        for item in registry.list_configs()
        if item.get("enabled")
    }
    getter = getattr(registry, "get", None)
    provider = getter(route_id) if callable(getter) else None
    if route_id not in enabled_ids or provider is None:
        raise ValidationError(
            "The selected recognition provider is not enabled.",
            code="recognition_provider_not_enabled",
        )
    return StageProviderRoute(route_id=route_id, kind="llm", provider=provider)


def build_owner_baidu_ocr_skill(owner_id: str, route: StageProviderRoute):
    """Construct the specialized Skill from the explicit owner and record id."""
    if not route.is_baidu_ocr:
        raise ValueError("route is not Baidu Unlimited-OCR")
    assert route.credential_id is not None
    try:
        return build_owner_baidu_unlimited_ocr_skill(owner_id, route.credential_id)
    except OCRCredentialNotFoundError as exc:
        raise ValidationError(
            "The selected OCR credential is unavailable.",
            code="ocr_credential_not_found",
        ) from exc
    except (OCRCredentialEncryptionUnavailableError, ValueError) as exc:
        raise ValidationError(
            "The selected OCR credential cannot be loaded safely.",
            code="provider_credentials_unavailable",
        ) from exc


def assert_grading_routes_supported(setup) -> None:
    if any(
        baidu_ocr_credential_id(provider_id) is not None
        for provider_id in setup.selected_provider_ids
    ):
        raise ValidationError(
            "Baidu Unlimited-OCR does not support grading. Select a grading model.",
            code="ocr_provider_grading_not_supported",
        )
