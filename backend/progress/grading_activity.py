"""Factual grading telemetry. Never serialize prompts, steps or provider errors.

The observer is scoped to one expert assessment, not one HTTP request. Retries
and auxiliary requests cannot inflate completed counts or change grading policy.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar

logger = logging.getLogger(__name__)
_current = ContextVar("grading_activity", default=None)
STATES = {"processing", "queued", "responding", "retrying", "succeeded", "failed", "cancelled"}
COUNTS = (*sorted(STATES), "total")
REASONS = {"rate_limit", "timeout", "unavailable"}


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def public_models(value, *, terminal=False):
    """Explicit output allowlist, also applied to persisted/legacy payloads."""
    result = []
    for row in value if isinstance(value, list) else []:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            continue
        if not row["id"].startswith("model-") or not row["id"][6:].isdigit():
            continue
        label = row.get("label")
        item = {"id": row["id"], "label": label[:100] if isinstance(label, str) and label else row["id"]}
        item.update({key: int(row[key]) if _number(row.get(key)) else 0 for key in COUNTS})
        times = row.get("completion_times", [])
        item["completion_times"] = [v for v in times if _number(v)][-8:] if isinstance(times, list) else []
        if terminal:
            for key in ("processing", "queued", "responding", "retrying"):
                item[key] = 0
        result.append(item)
    return result


def public_event(row):
    payload = row.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    event = payload.get("grading_activity")
    if not isinstance(event, dict):
        code = {"grading_started": "started", "run_completed": "completed",
                "operation_cancelled": "stopped", "run_cancelled": "stopped",
                "run_failed": "stopped"}.get(row.get("message"))
        event = {"code": code}
    code = event.get("code")
    if not isinstance(code, str) or code not in STATES | {"started", "completed", "stopped"}:
        return None
    result = {"sequence": row["sequence"], "ts": row["created_at"], "code": code}
    model_id = event.get("model_id", "")
    if isinstance(model_id, str) and model_id.startswith("model-") and model_id[6:].isdigit():
        result["model_id"] = model_id
    for key in ("attempt", "max_attempts", "retry_at"):
        if _number(event.get(key)):
            result[key] = event[key]
    if isinstance(event.get("reason"), str) and event["reason"] in REASONS:
        result["reason"] = event["reason"]
    return result


def public_waits(value):
    return [{"model": row["model"][:100], "reason": "provider_rate_limited",
             **{key: row[key] for key in ("attempt", "max_attempts", "retry_at")}}
            for row in value if isinstance(row, dict) and isinstance(row.get("model"), str)
            and row.get("reason") == "provider_rate_limited"
            and all(_number(row.get(key)) for key in ("attempt", "max_attempts", "retry_at"))] if isinstance(value, list) else []


def model_eta_seconds(models, *, now):
    """Estimate the slow unfinished model from recent assessment completions.

    Withhold estimates during retry or when samples are sparse/stale. Fast
    completed models must not make the remaining slow model look faster.
    """
    estimates = []
    for model in models:
        remaining = model["total"] - model["succeeded"] - model["failed"]
        if remaining <= 0:
            continue
        times = model["completion_times"]
        if model["retrying"] or len(times) < 3:
            return None
        span = times[-1] - times[0]
        interval = span / (len(times) - 1)
        if span < 5 or now - times[-1] > max(60, interval * 3):
            return None
        estimates.append(remaining * interval)
    # No remaining assessments can still mean aggregation/persistence is active.
    return math.ceil(max(estimates)) if estimates else None


class GradingActivity:
    def __init__(self, reporter, total):
        self.reporter = reporter
        self.total = total
        self.models = {}

    def configure(self, samples):
        from backend.services.execution_control import provider_key
        for provider, _ in samples:
            key = provider_key(provider)
            if key not in self.models:
                config = getattr(provider, "config", None)
                self.models[key] = {
                    "id": f"model-{len(self.models) + 1}",
                    # Only the configured public name/model; never provider_id
                    # (which can incorporate endpoint or credential identity).
                    "label": (getattr(config, "display_name", None) or getattr(config, "model", None) or f"Model {len(self.models) + 1}")[:100],
                    **{name: 0 for name in COUNTS}, "completion_times": [],
                }
                self.models[key]["total"] = self.total * sum(provider_key(p) == key for p, _ in samples)

    async def transition(self, unit, state, **details):
        try:
            previous = unit["state"]
            if previous == state and state != "retrying":
                return
            model = self.models[unit["key"]]
            if previous:
                model[previous] -= 1
            model[state] += 1
            unit["state"] = state
            if state in {"succeeded", "failed"}:
                model["completion_times"] = (model["completion_times"] + [time.time()])[-8:]
            await self.reporter.publish_grading_activity({
                "model_progress": public_models(list(self.models.values())),
                "grading_activity": {"model_id": model["id"], "code": state, **details},
            })
        except Exception:
            # An observability failure must never turn a valid score into failure.
            logger.warning("Grading activity update failed", exc_info=False)


@asynccontextmanager
async def observe_expert(reporter, provider, samples):
    observer = getattr(reporter, "grading_activity", None)
    unit = None
    if isinstance(observer, GradingActivity):
        try:
            from backend.services.execution_control import provider_key
            observer.configure(samples)
            unit = {"observer": observer, "key": provider_key(provider), "state": None, "successful": False}
            await observer.transition(unit, "processing")
        except Exception:
            unit = None
    token = _current.set(unit)
    try:
        yield unit
    except asyncio.CancelledError:
        if unit:
            await observer.transition(unit, "cancelled")
        raise
    except Exception:
        if unit:
            await observer.transition(unit, "failed")
        raise
    else:
        if unit:
            await observer.transition(unit, "succeeded" if unit["successful"] else "failed")
    finally:
        _current.reset(token)


async def report_provider_state(provider, state, **details):
    unit = _current.get()
    if unit is None:
        return
    try:
        from backend.services.execution_control import provider_key
        if provider_key(provider) == unit["key"]:
            await unit["observer"].transition(unit, state, **details)
    except Exception:
        logger.warning("Grading provider activity update failed", exc_info=False)
