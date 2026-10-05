"""Recognize explicit daily limits without guessing from an undifferentiated 429."""
import json
import re


def _error_text(value) -> str:
    """Inspect SDK structured details locally; never expose them to clients/logs."""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str).lower()
    parts = [str(value)]
    for field in ("body", "details", "code", "type"):
        detail = getattr(value, field, None)
        if detail is not None:
            parts.append(json.dumps(detail, ensure_ascii=False, default=str))
    return " ".join(parts).lower()


def exhausted_quota_code(value) -> str | None:
    """Only explicit evidence distinguishes daily caps from billing and RPM/TPM."""
    text = _error_text(value)
    if any(code in text for code in (
        "insufficient_quota", "credit_balance_exhausted", "billing_hard_limit_reached",
        "organization_spend_limit_exceeded", "project_spend_limit_exceeded",
        "organization_usage_limit_exceeded", "insufficient balance", "余额不足",
    )):
        return "provider_quota_exceeded"
    if is_daily_quota_error(value):
        return "provider_daily_quota_exceeded"
    return None


def is_daily_quota_error(value) -> bool:
    text = _error_text(value)
    # Provider metric names (Gemini), natural-language errors and relay codes.
    daily = re.search(r"per[ _-]?day|daily|requestsperday|tokensperday|\brpd\b|\btpd\b|每日|每天|日配额|日限额", text)
    exhausted = re.search(r"quota|limit|exceed|exhaust|reached|resource.?exhausted|限额|配额|超限|耗尽", text)
    return bool(daily and exhausted)
