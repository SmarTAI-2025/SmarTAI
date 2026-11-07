"""Small, explicit upstream error vocabulary; never export provider prose.

HTTP status alone cannot distinguish a content refusal from an account fault.
Only documented structured identifiers override it. Numeric identifiers are
vendor-scoped so an arbitrary compatible relay cannot redefine another API.
"""
from __future__ import annotations

CONFIGURATION_FAILURES = frozenset({
    "provider_auth_failed", "provider_permission_denied", "provider_region_unsupported",
    "provider_model_not_found", "provider_model_or_endpoint_not_found",
    "provider_model_access_denied",
})
CONTENT_FAILURES = frozenset({"provider_content_blocked", "provider_recitation_blocked"})

_CONTENT_CODES = {
    "recitation": "provider_recitation_blocked",
    **dict.fromkeys(("content_policy_violation", "content_filter", "content_filter_error", "safety",
                    "content_blocked", "data_inspection_failed", "datainspectionfailed"),
                   "provider_content_blocked"),
}
_VENDOR_CODES = {
    "zhipu": {
        "1113": "provider_quota_exceeded",
        "1211": "provider_model_not_found",
        "1220": "provider_permission_denied",
        "1221": "provider_model_or_endpoint_not_found",
        "1222": "provider_model_or_endpoint_not_found",
        "1301": "provider_content_blocked",
        "1309": "provider_quota_exceeded",
        "1310": "provider_quota_exceeded",
        "1311": "provider_permission_denied",
        "1314": "provider_quota_exceeded",
        "1315": "provider_permission_denied",
        **dict.fromkeys(map(str, range(1316, 1322)), "provider_quota_exceeded"),
    },
    "qwen": {"arrearage": "provider_quota_exceeded"},
    "doubao": {"operationdenied.serviceoverdue": "provider_quota_exceeded"},
    "tencent": {
        **dict.fromkeys(("401007", "401008", "403004"), "provider_quota_exceeded"),
        "429004": "provider_daily_quota_exceeded",
        "451001": "provider_content_blocked",
    },
}


def structured_error_code(body, provider_type: str) -> str | None:
    if not isinstance(body, dict):
        return None
    error = body.get("error", body)
    if not isinstance(error, dict):
        return None
    identifiers = [str(error.get(name, "")).lower() for name in ("code", "type", "status")]
    # Content refusal takes precedence even if a relay wraps it as permission_error.
    for code in identifiers:
        if code in _CONTENT_CODES:
            return _CONTENT_CODES[code]
    for code in identifiers:
        if code in _VENDOR_CODES.get(provider_type, {}):
            return _VENDOR_CODES[provider_type][code]
    return None


def google_configuration_error(details) -> str | None:
    """Recognize the Google SDK's specific HTTP 400 configuration diagnostics."""
    if not isinstance(details, dict):
        return None
    error = details.get("error", details)
    if not isinstance(error, dict):
        return None
    if "user location is not supported" in str(error.get("message", "")).lower():
        return "provider_region_unsupported"
    reasons = error.get("details", [])
    if isinstance(reasons, list) and any(isinstance(item, dict) and item.get("reason") in {
        "API_KEY_INVALID", "API_KEY_EXPIRED", "API_KEY_SERVICE_BLOCKED",
    } for item in reasons):
        return "provider_auth_failed"
    return None


def normalized_error_code(exc: BaseException, provider_type: str) -> str | None:
    """Find authoritative SDK fields through explicit causes, never old context."""
    from backend.domain.errors import DomainError
    from fastapi import HTTPException
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, (DomainError, HTTPException)):
            return None  # Application permissions/fencing are not vendor evidence.
        for name in ("body", "details", "response_json"):
            if code := structured_error_code(getattr(exc, name, None), provider_type):
                return code
        if provider_type == "gemini" and upstream_http_status(exc) == 400:
            if code := google_configuration_error(getattr(exc, "details", None)):
                return code
        if provider_type == "deepseek" and upstream_http_status(exc) == 402:
            return "provider_quota_exceeded"
        exc = exc.__cause__
    return None


def upstream_http_status(exc: BaseException) -> int | None:
    from backend.domain.errors import DomainError
    from fastapi import HTTPException
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, (DomainError, HTTPException)):
            return None
        for status in (getattr(exc, "status_code", None),
                       getattr(getattr(exc, "response", None), "status_code", None),
                       getattr(exc, "code", None)):
            if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
                return status
        exc = exc.__cause__
    return None


def configuration_failure_code(exc: BaseException) -> str | None:
    from backend.domain.errors import DomainError
    from fastapi import HTTPException
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, (DomainError, HTTPException)):
            return None
        code = getattr(exc, "code", None)
        if isinstance(code, str) and code.startswith("provider_"):
            # A normalized content/quota/payload verdict wins over its HTTP cause.
            if code == "provider_model_access_denied":
                return "provider_permission_denied"  # Existing workflow-safe code.
            return code if code in CONFIGURATION_FAILURES else None
        status = upstream_http_status(exc)
        if status is not None:
            return {401: "provider_auth_failed", 403: "provider_permission_denied",
                    404: "provider_model_not_found"}.get(status)
        exc = exc.__cause__
    return None
