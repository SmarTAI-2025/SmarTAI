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


LIMIT_CODES = frozenset({"provider_rate_limited", "provider_daily_quota_exceeded",
                         "provider_zero_quota", "provider_quota_exceeded"})


def structured_quota_code(value) -> str | None:
    """A QuotaFailure is a complete decision, including a non-daily limit.

    Only quotaValue in a violated quota metric is a structured ceiling. If it
    is absent, an explicit ceiling in this response's message may supply it;
    remaining/usage and unrelated limits cannot, nor can any outer wrapper.
    """
    violations = list(_violations(value))
    if not violations:
        return None
    ceilings = [item[field] for _, item in violations for field in ("quotaValue", "quota_value")
                if item.get(field) is not None]
    if any(_is_zero(value) for value in ceilings) or (not ceilings and _has_explicit_zero_ceiling(value)):
        return "provider_zero_quota"
    if any(re.search(_DAILY, metric) for metric, _ in violations):
        return "provider_daily_quota_exceeded"
    return "provider_rate_limited"


def quota_error_code(value) -> str | None:
    """Read SDK causes before turning a generic wrapper into a stable code."""
    chain = list(_quota_error_chain(value))
    for item in chain:
        if code := structured_quota_code(item):
            return code
    for item in chain:
        for code in (getattr(item, "code", None), str(item).strip()):
            if isinstance(code, str) and code in LIMIT_CODES:
                return code
    # Without structured fields, prefer the original response's explicit
    # message over the SDK wrapper's paraphrase of it.
    for item in reversed(chain):
        if code := _unstructured_quota_code(item):
            return code
    return None


def _quota_error_chain(value):
    seen = set()
    while value is not None and id(value) not in seen:
        seen.add(id(value))
        yield value
        # SDKs raise their wrapper *from* the response error. Implicit context
        # can instead be a previous attempt, so it is not current quota evidence.
        value = value.__cause__ if isinstance(value, BaseException) else None


def _unstructured_quota_code(value):
    text = _error_text(value)
    if _has_explicit_zero_ceiling(value):
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


def exhausted_quota_code(value) -> str | None:
    code = quota_error_code(value)
    return code if code != "provider_rate_limited" else None


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


_NUMBER = r"[+-]?\d+(?:\.\d+)?(?:e[+-]?\d+)?"
# A complete field label carries the meaning. Never search for a suffix such as
# 配额 inside 剩余配额, or for an unrelated limit somewhere in the error message.
_CEILING_FIELD = re.compile(
    rf"(?:quota (?:limit|ceiling)|(?:配额|额度)上限|总配额|总额度)"
    rf"\s*(?::|=|为|是)\s*({_NUMBER})\s*[。.]?"
)
# Google relays may preserve this explicit error-line shape without retaining
# the QuotaFailure object. Here `limit` belongs to the named violated metric.
_QUOTA_LIMIT_LINE = re.compile(
    rf"\*?\s*quota exceeded for (?:metric:\s*[a-z0-9_./-]+|(?:requests?|tokens?) per (?:day|minute))"
    rf"\s*[,;]\s*limit:\s*({_NUMBER})(?:\s*,\s*model:\s*[a-z0-9_./-]+)?\s*[.]?"
)


def _zero_ceiling_in_text(text):
    for line in text.splitlines():
        if match := _QUOTA_LIMIT_LINE.fullmatch(line.strip()):
            if _is_zero(match.group(1)):
                return True
        for field in re.split(r"[;,，；]", line):
            if match := _CEILING_FIELD.fullmatch(field.strip()):
                if _is_zero(match.group(1)):
                    return True
    return False


def _has_explicit_zero_ceiling(value):
    return any(_zero_ceiling_in_text(message.lower()) for message in _error_messages(value))


def _error_messages(value):
    """Keep message boundaries intact; concatenating fields changes their meaning."""
    if isinstance(value, str):
        yield value
        try:
            yield from _error_messages(json.loads(value))
        except (ValueError, TypeError):
            pass
    elif isinstance(value, dict):
        message = value.get("message")
        if isinstance(message, str):
            yield message
        for field in ("error", "details"):
            yield from _error_messages(value.get(field))
    elif isinstance(value, list):
        for item in value:
            yield from _error_messages(item)
    elif isinstance(value, BaseException):
        yield str(value)
        for field in ("body", "details", "response_json"):
            yield from _error_messages(getattr(value, field, None))


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
