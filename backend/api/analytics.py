"""Normalized post-grading analytics for the Figma presentation API.

The assignment, frozen grading run, grade results, teacher reviews, answers,
and student identities are loaded from normalized SQL rows. Derived prose is
not cached in process: task deletion must not leave content in another web
worker that cannot observe a local invalidation hook.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from threading import RLock
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from backend.agents import analytics_agent, grounded_ask
from backend.analytics.ask_workspace import AskDataError, load_snapshot
from backend.services.task_history_progress import enrich_history_progress
from backend.domain.errors import DomainError, NotFound
from backend.api.errors import domain_error_response
from backend.auth import require_task_teacher as require_teacher
from backend.db.models import AssignmentRecord, SubmissionRecord, UserRecord
from backend.db.session import session_scope
from backend.db.workflow_repository import AssignmentStudentPresentationRecord, GradingRunSetupRecord
from backend.llm.registry import (
    ExpertRegistry,
    SharedPoolLimitError,
    get_scoped_expert_registry,
    resolve_owner_default_provider,
)
from backend.models import User


router = APIRouter(prefix="/analytics", tags=["analytics"])
logger = logging.getLogger(__name__)

_RATE_WINDOW_SECONDS = 30.0


class QueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    mode: Literal["filter", "summary", "chart"] = "filter"
    history: list[str] = Field(default_factory=list, max_length=4)


class FilterIntentRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    surface: analytics_agent.FilterIntentSurface


_query_last_at: dict[str, float] = {}
_query_rate_lock = RLock()


def _not_found() -> HTTPException:
    return HTTPException(
        status.HTTP_404_NOT_FOUND,
        detail={"code": "analytics_task_not_found"},
    )


def _check_rate_limit(owner_id: str) -> None:
    now = time.monotonic()
    with _query_rate_lock:
        last = _query_last_at.get(owner_id, 0.0)
        elapsed = now - last
        if elapsed < _RATE_WINDOW_SECONDS:
            retry_after = max(1, math.ceil(_RATE_WINDOW_SECONDS - elapsed))
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "code": "analytics_rate_limited",
                    "retry_after_seconds": retry_after,
                },
                headers={"Retry-After": str(retry_after)},
            )
        _query_last_at[owner_id] = now
        stale_before = now - (_RATE_WINDOW_SECONDS * 2)
        for actor_id, timestamp in list(_query_last_at.items()):
            if timestamp < stale_before:
                _query_last_at.pop(actor_id, None)


def _safe_text(value: Any, max_length: int) -> str:
    return str(value or "").replace("\x00", "").strip()[:max_length]


def _provider_error(exc: SharedPoolLimitError) -> HTTPException:
    limited = str(exc) == "shared_pool_daily_limit_reached"
    return HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS if limited else status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": (
                "analytics_quota_exceeded"
                if limited else "analytics_provider_unavailable"
            ),
            "message": "共享模型日额度已用完，请等待 UTC 00:00 重置、联系管理员调整或使用 BYOK。" if limited else "共享模型暂不可用。"
        },
    )


def _load_filter_identifiers(task_id: str, owner_id: str) -> set[str]:
    """Authorize the task and read identities without requiring a grading run."""
    with session_scope() as session:
        owned = session.scalar(select(AssignmentRecord.id).where(
            AssignmentRecord.id == task_id, AssignmentRecord.teacher_id == owner_id,
        ))
        if owned is None:
            raise _not_found()
        students = session.execute(
            select(UserRecord.id, UserRecord.username)
            .join(SubmissionRecord, SubmissionRecord.student_id == UserRecord.id)
            .where(SubmissionRecord.assignment_id == task_id)
        ).all()
        presentations = session.execute(select(
            AssignmentStudentPresentationRecord.student_id,
            AssignmentStudentPresentationRecord.display_student_id,
            AssignmentStudentPresentationRecord.display_name,
        ).where(AssignmentStudentPresentationRecord.assignment_id == task_id)).all()
        identifiers = {str(value).strip() for row in [*students, *presentations] for value in row if value}
        # Results may still show the frozen identity after a presentation edit.
        for rows in session.scalars(select(GradingRunSetupRecord.input_manifest["student_presentations"]).where(
            GradingRunSetupRecord.assignment_id == task_id, GradingRunSetupRecord.owner_id == owner_id,
        )):
            for row in rows if isinstance(rows, list) else []:
                if isinstance(row, dict):
                    identifiers.update(str(row[field]).strip() for field in ("student_id", "display_student_id", "display_name") if row.get(field))
        return identifiers - {""}


def _redact_filter_question(question: str, identifiers: set[str]) -> tuple[str, dict[str, str]]:
    """Remove known student identifiers before sending query text to a provider."""
    if not identifiers:
        return question, {}
    placeholders: dict[str, str] = {}
    originals: dict[str, str] = {}
    def replace(match: re.Match[str]) -> str:
        literal = match.groupdict().get("numeric_id") or match.groupdict().get("trailing_id") or match.group(0)
        identity = literal.casefold()
        placeholder = placeholders.setdefault(identity, f"<student_{len(placeholders) + 1}>")
        originals[placeholder] = literal
        return (match.groupdict().get("numeric_prefix") or "") + placeholder
    patterns = []
    for value in sorted(identifiers, key=len, reverse=True):
        if value.isdigit() and len(value) < 6:
            continue
        escaped = re.escape(value)
        patterns.append(
            rf"(?<![a-z0-9_]){escaped}(?![a-z0-9_])"
            if re.search(r"[a-z0-9]", value, re.IGNORECASE) else escaped
        )
    numeric_ids = "|".join(re.escape(value) for value in sorted(identifiers, key=len, reverse=True) if value.isdigit() and len(value) < 6)
    if numeric_ids:
        # A short numeric ID must not consume a score limit or Q1's number.
        patterns.append(
            rf"(?P<numeric_prefix>(?<![a-z0-9_])(?:学号|学生|同学|student(?:\s*id)?|id|#)\s*[:：#]?\s*)"
            rf"(?P<numeric_id>{numeric_ids})(?![a-z0-9_])"
        )
        patterns.append(rf"(?<![a-z0-9_])(?P<trailing_id>{numeric_ids})(?=\s*(?:同学|学生))")
    return re.sub("|".join(patterns), replace, question, flags=re.IGNORECASE), originals


@router.post("/{task_id}/filter-intent")
async def interpret_filter_intent(
    task_id: str,
    req: FilterIntentRequest,
    current: User = Depends(require_teacher),
    registry: ExpertRegistry = Depends(get_scoped_expert_registry),
):
    known_identifiers = _load_filter_identifiers(task_id, current.id)
    provider = resolve_owner_default_provider(current.id, registry)
    if provider is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "analytics_provider_unavailable"},
        )
    _check_rate_limit(f"{current.id}:filter-intent")
    provider_question, identities = _redact_filter_question(req.question, known_identifiers)

    try:
        output = await analytics_agent.interpret_filter_intent(
            question=provider_question,
            surface=req.surface,
            provider=provider,
        )
        text_terms = []
        for term in output.text_terms:
            normalized = term.strip().casefold()
            if normalized in identities and req.surface in {"student_analysis", "review_overview", "submission_review"}:
                text_terms.append(identities[normalized])
            elif re.search(r"<student(?:_[^>]*)?>", term, re.IGNORECASE):
                output.recognized = False
            else:
                text_terms.append(_safe_text(term, 80))
        if not output.recognized:
            output = analytics_agent.FilterIntentOutput(recognized=False, explanation=output.explanation)
            text_terms = []
        return {
            **output.model_dump(),
            "question_tokens": [_safe_text(value, 40) for value in output.question_tokens],
            "text_terms": text_terms,
            "explanation": _safe_text(output.explanation, 500),
        }
    except SharedPoolLimitError as exc:
        raise _provider_error(exc) from exc
    except Exception as exc:
        logger.warning(
            "Analytics intent interpretation failed; task_id=%s surface=%s exception_type=%s",
            task_id,
            req.surface,
            type(exc).__name__,
        )
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail={"code": "analytics_intent_failed"},
        ) from exc


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    surface: str = Field(default="student_analysis", max_length=64)
    context_student_id: str | None = Field(default=None, max_length=160)
    history: list[str] = Field(default_factory=list, max_length=4)


_ask_bursts: dict[str, list[float]] = {}


def _check_ask_limit(owner_id: str) -> None:
    now = time.monotonic()
    with _query_rate_lock:
        for actor in list(_ask_bursts):
            _ask_bursts[actor] = [t for t in _ask_bursts[actor] if now - t < 60]
            if not _ask_bursts[actor]:
                del _ask_bursts[actor]
        requests = _ask_bursts.setdefault(owner_id, [])
        if len(requests) >= 12:
            raise HTTPException(429, detail={"code": "analytics_rate_limited"}, headers={"Retry-After": "60"})
        requests.append(now)


async def _run_grounded_ask(task_id: str | None, req: AskRequest, current: User, registry: ExpertRegistry):
    try:
        if any(len(turn) > 2000 for turn in req.history):
            raise HTTPException(422, detail={"code": "ask_history_too_long"})
        snapshot = await asyncio.to_thread(load_snapshot, task_id, current.id)
        provider = resolve_owner_default_provider(current.id, registry)
        if provider is None:
            raise HTTPException(503, detail={"code": "analytics_provider_unavailable"})
        _check_ask_limit(current.id)
        result = await grounded_ask.ask(task_id=task_id, owner_id=current.id, question=req.question,
            surface=req.surface, provider=provider, context_student_id=req.context_student_id,
            history=req.history, snapshot=snapshot)
        if req.surface == "history" and result.get("tasks") is not None:
            result["tasks"] = await enrich_history_progress(result["tasks"], owner_id=current.id)
        # Keep query plans and diagnostic context inside the agent. All Ask
        # routes, including /query's nested execution, share this boundary.
        return {key: result[key] for key in (
            "recognized", "kind", "explanation", "data", "selection", "chart", "candidates", "tasks",
        ) if key in result}
    except DomainError as exc:
        if isinstance(exc, NotFound):
            raise _not_found() from exc
        return domain_error_response(exc)
    except AskDataError as exc:
        return {"recognized": False, "kind": "clarification", "explanation": str(exc),
            "data": {"columns": [], "rows": []}, "selection": None, "chart": None, "candidates": []}
    except SharedPoolLimitError as exc:
        raise _provider_error(exc) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Grounded Ask failed; task_id=%s exception_type=%s", task_id, type(exc).__name__)
        raise HTTPException(502, detail={"code": "analytics_generation_failed"}) from exc


@router.post("/ask")
async def ask_teacher(req: AskRequest, current: User = Depends(require_teacher),
                      registry: ExpertRegistry = Depends(get_scoped_expert_registry)):
    return await _run_grounded_ask(None, req, current, registry)


@router.post("/{task_id}/ask")
async def ask_task(task_id: str, req: AskRequest, current: User = Depends(require_teacher),
                   registry: ExpertRegistry = Depends(get_scoped_expert_registry)):
    return await _run_grounded_ask(task_id, req, current, registry)


@router.post("/{task_id}/query")
async def nl_query(task_id: str, req: QueryRequest, current: User = Depends(require_teacher),
                   registry: ExpertRegistry = Depends(get_scoped_expert_registry)):
    # Compatibility route: old clients cannot bypass grounded chart execution.
    executed = await _run_grounded_ask(task_id, AskRequest(question=req.question,
        surface="chart" if req.mode == "chart" else "student_analysis", history=req.history), current, registry)
    if not isinstance(executed, dict):
        return executed
    if executed.get("chart"):
        return {**executed["chart"], "execution": executed}
    if req.mode == "filter" and (executed.get("selection") or {}).get("kind") == "students":
        return {"mode": "filter", "student_ids": executed["selection"]["ids"],
                "explanation": executed["explanation"], "execution": executed}
    return {"mode": "query", "execution": executed}
