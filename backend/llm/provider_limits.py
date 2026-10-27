"""Recognize explicit daily limits without guessing from an undifferentiated 429."""
import json
from decimal import Decimal, InvalidOperation
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
    if _has_zero_quota(value):
        return "provider_zero_quota"
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
                            yield metric.lower(), violation
            else:
                yield from _quota_violations(detail)
    elif isinstance(value, list):
        for detail in value:
            yield from _quota_violations(detail)


_EXHAUSTED = r"(?:exceeded|exhausted|reached|耗尽|用完|超限|超出)"
_NOT_EXHAUSTED = (
    rf"\b(?:not|never|hasn't|haven't|isn't|aren't|wasn't|weren't|hasn’t|isn’t)"
    rf"(?:\s+(?:yet|been|fully|completely|already|actually|ever|still))*\s+{_EXHAUSTED}"
    rf"|(?:尚未|还未|并未|未曾|没有|未)(?:完全|实际|被)?{_EXHAUSTED}"
)
_DAILY_LIMIT = (
    rf"(?:(?:{_DAILY}).{{0,50}}(?:quota|limit|额度|限额|配额)"
    r"|(?:requests?|tokens?)\s+per\s+day|日配额|日限额)"
)


def _violations(value):
    for detail in [value, *(getattr(value, field, None) for field in ("body", "details", "response_json"))]:
        yield from _quota_violations(detail)


def _is_zero(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return False
    try:
        return Decimal(str(value)).is_zero()
    except InvalidOperation:
        return False


def _has_zero_quota(value):
    violations = list(_violations(value))
    # Only a violated limit's ceiling is evidence. Usage/remaining=0 and a
    # missing value do not mean the model has a zero allowance.
    ceilings = [item[field] for _, item in violations for field in ("quotaValue", "quota_value")
                if item.get(field) is not None]
    if ceilings:
        return any(_is_zero(ceiling) for ceiling in ceilings)
    text = _error_text(value)
    if "provider_zero_quota" in text:
        return True
    if not re.search(r"\bquota\b|配额|额度", text):
        return False
    # Unstructured Google relays often retain "limit: 0" but lose QuotaFailure.
    values = re.findall(
        r"(?:\blimit|\bquota[ _]?value|(?:配额|额度)(?:上限)?)\s*['\"]?"
        r"\s*(?::|=|为|是)\s*['\"]?([+-]?\d+(?:\.\d+)?(?:e[+-]?\d+)?)(?![\w.])", text,
    )
    return any(_is_zero(limit) for limit in values)


def is_daily_quota_error(value) -> bool:
    violations = [metric for metric, _ in _violations(value)]
    if violations:
        # A minute violation plus prose mentioning daily allowance is not RPD.
        return any(re.search(_DAILY, metric) for metric in violations)
    text = _error_text(value)
    if "provider_daily_quota_exceeded" in text:
        return True
    # Match both "daily quota exhausted" and "quota exceeded for requests per
    # day". Negation belongs to the exhaustion statement, not just "not been".
    for clause in re.split(r"[\n;.!。；，,]", text):
        if re.search(_NOT_EXHAUSTED, clause):
            continue
        if re.search(r"\b(?:remaining|available)\b|尚有|剩余", clause):
            continue
        if re.search(rf"{_DAILY_LIMIT}.{{0,30}}{_EXHAUSTED}|{_EXHAUSTED}.{{0,30}}{_DAILY_LIMIT}", clause):
            return True
    return False
