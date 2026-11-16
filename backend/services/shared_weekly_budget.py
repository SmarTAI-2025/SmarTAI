"""Atomic weekly USD reservations, settled only from official OpenAI usage.

Prices verified 2026-11-16: developers.openai.com/api/docs/models/{model}.
Standard/global processing, 30-minute cache writes; no hosted tools. Reasoning
is already included in output tokens. Integer nanodollars avoid money rounding.
Legacy daily admission is intentionally independent of this service.
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, select, text

from backend.db.model_quota_models import SharedModelChargeRecord as Charge
from backend.db.models import UserRecord
from backend.db.session import get_session
from backend.services.business_config import read_business_config
from backend.services.model_quota import ModelQuotaError

NANO_USD = 1_000_000_000
PRICE_VERSION = "openai-standard-20261116"
SGT = ZoneInfo("Asia/Singapore")
# Nanodollars per token: ordinary input, cached input, cache write, output.
PRICES = {
    "gpt-6-luna": (100, 10, 125, 500),
    "gpt-6.1-terra": (1000, 50, 1250, 5000),
    "gpt-6.1-sol": (2000, 100, 2500, 10000),
}


def week_window(now=None):
    now = (now or datetime.now(timezone.utc)).astimezone(SGT)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=now.weekday())
    return start.date().isoformat(), (start + timedelta(days=7)).isoformat()


def _number(value):
    if type(value) is not int or value < 0:
        raise ModelQuotaError("shared_pool_usage_unavailable")
    return value


def rates(model, input_tokens):
    if model not in PRICES:
        raise ModelQuotaError("shared_pool_billing_unavailable")
    price = PRICES[model]
    # The long-context rate applies to the entire request, not the excess.
    return tuple(p * 2 for p in price[:3]) + (price[3] * 3 // 2,) if input_tokens > 272000 else price


def actual_charge(model, usage):
    """No tokenizer/character fallback; missing or inconsistent usage is unknown."""
    if not isinstance(usage, dict) or not isinstance(usage.get("input_tokens_details"), dict):
        raise ModelQuotaError("shared_pool_usage_unavailable")
    inputs, outputs, total = (_number(usage.get(k)) for k in ("input_tokens", "output_tokens", "total_tokens"))
    details = usage["input_tokens_details"]
    cached, written = (_number(details.get(k)) for k in ("cached_tokens", "cache_write_tokens"))
    if total != inputs + outputs or cached + written > inputs:
        raise ModelQuotaError("shared_pool_usage_unavailable")
    split = details.get("cache_write_tokens_details")
    if split is not None and (not isinstance(split, dict) or
            _number(split.get("ttl_30m_tokens")) != written or _number(split.get("ttl_12h_tokens")) != 0):
        raise ModelQuotaError("shared_pool_usage_unavailable")
    output_details = usage.get("output_tokens_details")
    reasoning = _number(output_details.get("reasoning_tokens")) if isinstance(output_details, dict) else None
    if reasoning is not None and reasoning > outputs:
        raise ModelQuotaError("shared_pool_usage_unavailable")
    ordinary_rate, cached_rate, write_rate, output_rate = rates(model, inputs)
    cost = (inputs - cached - written) * ordinary_rate + cached * cached_rate + written * write_rate + outputs * output_rate
    # Persist counters only, never arbitrary upstream metadata or content.
    facts = {"input_tokens": inputs, "output_tokens": outputs, "total_tokens": total,
             "cached_tokens": cached, "cache_write_tokens": written, "reasoning_tokens": reasoning}
    return cost, facts


def _lock_owner(session, owner, *, require_active=True):
    if session.bind.dialect.name == "sqlite":
        session.execute(text("BEGIN IMMEDIATE"))
    row = session.scalar(select(UserRecord).where(UserRecord.id == owner).with_for_update())
    if row is None or (require_active and not row.is_active):
        raise ModelQuotaError("model_quota_owner_unavailable")


def _totals(session, owner, week):
    spent, pending, requests, unknown = session.execute(select(
        func.coalesce(func.sum(case((Charge.state == "settled", Charge.cost_nano_usd), else_=0)), 0),
        func.coalesce(func.sum(case((Charge.state.in_(("reserved", "unknown")), Charge.reserved_nano_usd), else_=0)), 0),
        func.count(Charge.id),
        func.coalesce(func.sum(case((Charge.state == "unknown", 1), else_=0)), 0),
    ).where(Charge.owner_id == owner, Charge.week == week)).one()
    return int(spent), int(pending), int(requests), int(unknown)


def _limit(config):
    return int(config.values["shared_pool_weekly_limit_cents"]) * (NANO_USD // 100)


def reserve(owner, model, input_tokens, *, max_output_tokens=128000, now=None):
    """Reserve the worst priced input and bounded output before sending."""
    inputs = _number(input_tokens)
    maximum = _number(max_output_tokens)
    if inputs > 922000 or not 16 <= maximum <= 128000:
        raise ModelQuotaError("shared_pool_request_too_large")
    rate = rates(model, inputs)
    input_hold = inputs * max(rate[:3])
    week, _ = week_window(now)
    with get_session() as session:
        try:
            _lock_owner(session, owner)
            limit = _limit(read_business_config(session, owner))
            spent, pending, _, unknown = _totals(session, owner, week)
            remaining = limit - spent - pending
            maximum = min(maximum, (remaining - input_hold) // rate[3])
            if maximum < 16:
                if pending and limit - spent >= input_hold + 16 * rate[3]:
                    raise ModelQuotaError("shared_pool_usage_unavailable" if unknown else "shared_pool_budget_pending")
                raise ModelQuotaError("shared_pool_weekly_limit_reached")
            charge_id = uuid4().hex
            session.add(Charge(id=charge_id, owner_id=owner, week=week, model=model,
                state="reserved", reserved_nano_usd=input_hold + maximum * rate[3],
                price_version=PRICE_VERSION, created_at=(now or datetime.now(timezone.utc)).timestamp()))
            session.commit()
            return charge_id, maximum
        except BaseException:
            session.rollback()
            raise


def finish(owner, charge_id, *, usage=None, response_id=None, rejected=False, now=None):
    """Idempotent settlement; interruptions keep a durable hold across restarts."""
    with get_session() as session:
        try:
            # Disabling an account blocks new admissions, not settlement of
            # requests already sent while the account was active.
            _lock_owner(session, owner, require_active=False)
            row = session.scalar(select(Charge).where(Charge.id == charge_id, Charge.owner_id == owner))
            if row is None:
                raise ModelQuotaError("shared_pool_usage_unavailable")
            if row.state in ("settled", "released"):
                return
            if isinstance(response_id, str):
                row.response_id = response_id[:128]
            row.settled_at = (now or datetime.now(timezone.utc)).timestamp()
            error = None
            if rejected:
                row.state = "released"
            elif usage is None:
                row.state = "unknown"
            else:
                try:
                    cost, facts = actual_charge(row.model, usage)
                    row.cost_nano_usd, row.usage, row.state = cost, facts, "settled"
                    if cost > row.reserved_nano_usd:
                        error = ModelQuotaError("shared_pool_usage_unavailable")
                except ModelQuotaError as exc:
                    row.state, error = "unknown", exc
            session.commit()
            if error:
                raise error
        except BaseException:
            session.rollback()
            raise


def weekly_usage(session, owner, *, config=None, now=None):
    week, resets = week_window(now)
    limit = _limit(config or read_business_config(session, owner))
    spent, pending, requests, unknown = _totals(session, owner, week)
    return {"week": week, "timezone": "Asia/Singapore", "resets_at": resets,
            "limit_usd": limit / NANO_USD, "spent_usd": spent / NANO_USD,
            "reserved_usd": pending / NANO_USD, "remaining_usd": max(0, limit-spent-pending) / NANO_USD,
            "requests": requests, "unknown_requests": unknown}
