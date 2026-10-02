"""Resolve one committed DB snapshot per call; never mutate settings or cache.

Precedence: owner storage override > global override > settings > code default.
The caller's transaction is reused for quota admission and email rate checks.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any
from sqlalchemy import literal, select, union_all
from sqlalchemy.orm import Session
from backend.config import settings
from backend.db.business_config_models import BusinessConfigRecord, UserStorageConfigRecord

DEFAULTS: dict[str, str | int] = {
    "allowed_email_domains": "",
    "email_verification_resend_seconds": 60,
    "email_verification_hourly_email_limit": 5,
    "email_verification_hourly_ip_limit": 20,
    "unfinished_source_quota_bytes": 536870912,
    "knowledge_storage_quota_bytes": 536870912,
}
STORAGE_KEYS = frozenset(("unfinished_source_quota_bytes", "knowledge_storage_quota_bytes"))
BOUNDS = {
    "email_verification_resend_seconds": (1, 3600),
    "email_verification_hourly_email_limit": (1, 100),
    "email_verification_hourly_ip_limit": (1, 1000),
    "unfinished_source_quota_bytes": (0, 1099511627776),
    "knowledge_storage_quota_bytes": (0, 1099511627776),
}


def validate_changes(changes: dict[str, Any], *, user_scope: bool = False) -> dict[str, Any]:
    allowed = STORAGE_KEYS if user_scope else DEFAULTS.keys()
    if not changes or changes.keys() - allowed:
        raise ValueError("Only the listed business settings can be changed")
    result = {}
    for key, value in changes.items():
        if value is None:
            result[key] = None
        elif key == "allowed_email_domains":
            result[key] = validate_domains(value)
        else:
            low, high = BOUNDS[key]
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{key} must be an integer between {low} and {high}")
            result[key] = value
    return result


def validate_domains(value: Any) -> str:
    # Reuse the established IDNA canonicalizer; do not invent another matcher.
    from backend.services.email_registration import _normalize_domain
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError("Registration domains must be text of at most 4096 characters")
    if value in ("", "*"):
        return value
    parts = value.split(",")
    if len(parts) > 64 or any(not part.strip() or "*" in part for part in parts):
        raise ValueError("Use comma-separated root domains, an explicit empty string, or * alone")
    domains = []
    for part in parts:
        try:
            domain = _normalize_domain(part)
            if "." not in domain:
                raise ValueError("A public root domain is required")
            try:
                ipaddress.ip_address(domain)
            except ValueError:
                pass
            else:
                raise ValueError("IP addresses are not email domains")
        except ValueError as exc:
            raise ValueError("Invalid registration domain; enter domains without URL, @, port or wildcard") from exc
        if domain not in domains:
            domains.append(domain)
    return ",".join(domains)


@dataclass(frozen=True)
class BusinessConfiguration:
    values: dict[str, Any]
    sources: dict[str, str]
    global_overrides: dict[str, Any]
    user_overrides: dict[str, Any]
    global_version: int
    user_version: int
    registration_rules_managed: bool


def read_business_config(session: Session, owner_id: str | None = None) -> BusinessConfiguration:
    statement = select(literal("global"), BusinessConfigRecord.overrides,
                       BusinessConfigRecord.version, BusinessConfigRecord.registration_rules_managed).where(
                           BusinessConfigRecord.id == "global")
    if owner_id is not None:
        statement = union_all(statement, select(literal("user"), UserStorageConfigRecord.overrides,
                              UserStorageConfigRecord.version, literal(False)).where(
                                  UserStorageConfigRecord.owner_id == owner_id))
    rows = {row[0]: row for row in session.execute(statement)}
    global_row, user_row = rows.get("global"), rows.get("user")
    global_overrides = dict(global_row[1]) if global_row else {}
    user_overrides = dict(user_row[1]) if user_row else {}
    values, sources = {}, {}
    fields_set = getattr(settings, "model_fields_set", set())
    for key, default in DEFAULTS.items():
        if key in user_overrides and key in STORAGE_KEYS:
            value, source = user_overrides[key], "user_override"
        elif key in global_overrides:
            value, source = global_overrides[key], "global_override"
        else:
            value = getattr(settings, key, default)
            source = "settings" if key in fields_set or value != default else "default"
        # Preserve the repositories' legacy environment handling (negative = 0).
        # DB overrides have strict validation, and zero always blocks new bytes.
        values[key] = max(0, int(value)) if key in STORAGE_KEYS else value
        sources[key] = source
    return BusinessConfiguration(values, sources, global_overrides, user_overrides,
                                 global_row[2] if global_row else 0, user_row[2] if user_row else 0,
                                 bool(global_row[3]) if global_row else False)


def storage_quota_limit(session: Session, owner_id: str, key: str) -> int:
    if key not in STORAGE_KEYS:
        raise ValueError("Unknown storage quota")
    return int(read_business_config(session, owner_id).values[key])
