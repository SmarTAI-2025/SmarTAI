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


_DAILY = r"per[ _-]?day|daily|requestsperday|tokensperday|\brpd\b|\btpd\b|每日|每天|日配额|日限额"


def _quota_violations(value):
    """Google QuotaFailure violations identify the limit actually exceeded."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return
    if isinstance(value, dict):
        for key, detail in value.items():
            if key == "violations" and isinstance(detail, list):
                for violation in detail:
                    if isinstance(violation, dict):
                        metric = " ".join(str(violation.get(field, "")) for field in
                                          ("quotaMetric", "quotaId", "quota_metric", "quota_id")).strip()
                        if metric:
                            yield metric.lower()
            else:
                yield from _quota_violations(detail)
    elif isinstance(value, list):
        for detail in value:
            yield from _quota_violations(detail)


def is_daily_quota_error(value) -> bool:
    details = [value, *(getattr(value, field, None) for field in ("body", "details", "response_json"))]
    violations = [metric for detail in details for metric in _quota_violations(detail)]
    if violations:
        # A minute violation plus prose mentioning daily allowance is not RPD.
        return any(re.search(_DAILY, metric) for metric in violations)
    text = _error_text(value)
    if "provider_daily_quota_exceeded" in text:
        return True
    # Unstructured relays must explicitly report exhaustion near the daily
    # limit, not merely mention a daily allowance elsewhere in a 429 message.
    for clause in re.split(r"[\n;.!。；]", text):
        if not re.search(_DAILY, clause):
            continue
        if re.search(r"remaining|available|not\s+(?:been\s+)?(?:exceeded|exhausted|reached)|未耗尽|尚有|剩余", clause):
            continue
        limit = rf"(?:{_DAILY})[^\n;.!。；]{{0,50}}(?:quota|limit|限额|配额)"
        exhausted = r"(?:exceeded|exhausted|reached|耗尽|用完|超限|超出)"
        if re.search(rf"{limit}[^\n;.!。；]{{0,30}}{exhausted}|{exhausted}[^\n;.!。；]{{0,30}}{limit}", clause):
            return True
    return False
