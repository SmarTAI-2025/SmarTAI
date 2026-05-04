"""
Analytics API — natural-language post-grading queries + per-question breakdown.

Endpoints:
  POST /analytics/{task_id}/query
    body: {question: str, mode: "filter"|"summary"|"chart"}
    returns: mode-specific structured output

  GET  /analytics/{task_id}/per_question/{q_id}
    returns: deterministic stats + (cached) LLM-summarized common mistakes

Rate limiting:
  Per user, max 1 NL query per 30s. The `chart`/`summary`/`filter` LLM calls
  are gated; `per_question` is mostly deterministic (only the common-mistakes
  bullet uses LLM and is cached per (task_id, q_id)).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.auth import require_teacher
from backend.models import User, Task
from backend.state import (
    JobStore, TaskStore,
    get_job_store, get_task_store,
)
from backend.llm.registry import ExpertRegistry, get_expert_registry
from backend.agents import analytics_agent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analytics", tags=["analytics"])

# ─── Rate limiting (in-memory) ───────────────────────────────────────────────

_RATE_WINDOW_SEC = 30.0
_last_query_at: Dict[str, float] = {}  # owner_id → ts of last NL query

# Per-question common-mistakes cache: (task_id, q_id) → markdown
_cm_cache: Dict[str, str] = {}


def _check_rate_limit(owner_id: str) -> None:
    last = _last_query_at.get(owner_id, 0.0)
    now = time.time()
    elapsed = now - last
    if elapsed < _RATE_WINDOW_SEC:
        wait = round(_RATE_WINDOW_SEC - elapsed, 1)
        raise HTTPException(429, detail=f"Rate limited. Retry in {wait}s.")
    _last_query_at[owner_id] = now


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _check_owner(task: Task, user: User) -> None:
    if user.role == "admin":
        return
    if task.owner_id != user.id:
        raise HTTPException(403, detail="Not your task")


def _get_results_payload(task: Task, job_store: JobStore) -> Dict[str, Any]:
    if task.status != "graded" or not task.grading_job_id:
        raise HTTPException(409, detail=f"Task not graded yet (status={task.status})")
    job = job_store.get(task.grading_job_id)
    if job is None or job.results is None:
        raise HTTPException(404, detail="Grading result not found")
    return dict(job.results)


# ─── Schemas ─────────────────────────────────────────────────────────────────

class QueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    mode: Literal["filter", "summary", "chart"] = "filter"


# ─── Endpoints ───────────────────────────────────────────────────────────────

@router.post("/{task_id}/query")
async def nl_query(
    task_id: str,
    req: QueryRequest,
    current: User = Depends(require_teacher),
    task_store: TaskStore = Depends(get_task_store),
    job_store: JobStore = Depends(get_job_store),
    registry: ExpertRegistry = Depends(get_expert_registry),
):
    task = task_store.get(task_id)
    if task is None:
        raise HTTPException(404, detail="Task not found")
    _check_owner(task, current)

    # Rate limit (per owner)
    _check_rate_limit(current.id)

    payload = _get_results_payload(task, job_store)
    provider = registry.pick_default()
    if provider is None:
        raise HTTPException(503, detail="No LLM provider configured.")

    try:
        if req.mode == "filter":
            out = await analytics_agent.filter_students(
                question=req.question,
                results_payload=payload,
                problem_data=task.problem_data,
                provider=provider,
            )
            return {"mode": "filter", **out.model_dump()}

        elif req.mode == "summary":
            out = await analytics_agent.summarize(
                question=req.question,
                results_payload=payload,
                problem_data=task.problem_data,
                provider=provider,
            )
            return {"mode": "summary", **out.model_dump()}

        elif req.mode == "chart":
            out = await analytics_agent.make_chart(
                question=req.question,
                results_payload=payload,
                problem_data=task.problem_data,
                provider=provider,
            )
            return {"mode": "chart", **out.model_dump()}

        else:
            raise HTTPException(400, detail=f"Unknown mode: {req.mode}")

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"NL query failed for task={task_id} mode={req.mode}")
        raise HTTPException(500, detail=f"Analytics error: {e}")


@router.get("/{task_id}/per_question/{q_id}")
async def per_question(
    task_id: str,
    q_id: str,
    current: User = Depends(require_teacher),
    task_store: TaskStore = Depends(get_task_store),
    job_store: JobStore = Depends(get_job_store),
    registry: ExpertRegistry = Depends(get_expert_registry),
):
    task = task_store.get(task_id)
    if task is None:
        raise HTTPException(404, detail="Task not found")
    _check_owner(task, current)

    payload = _get_results_payload(task, job_store)
    breakdown = analytics_agent.per_question_breakdown(q_id, payload, task.problem_data)

    cache_key = f"{task_id}::{q_id}"
    common_mistakes_md = _cm_cache.get(cache_key)
    if common_mistakes_md is None:
        provider = registry.pick_default()
        if provider is not None and breakdown["rows"]:
            try:
                out = await analytics_agent.question_common_mistakes(
                    q_id=q_id,
                    breakdown=breakdown,
                    provider=provider,
                )
                common_mistakes_md = out.common_mistakes_md
                _cm_cache[cache_key] = common_mistakes_md
            except Exception as e:
                logger.warning(f"common-mistakes summary failed: {e}")
                common_mistakes_md = ""
        else:
            common_mistakes_md = ""

    return {
        **breakdown,
        "common_mistakes_md": common_mistakes_md,
    }


@router.delete("/{task_id}/per_question/{q_id}/cache")
def reset_per_question_cache(
    task_id: str,
    q_id: str,
    current: User = Depends(require_teacher),
    task_store: TaskStore = Depends(get_task_store),
):
    task = task_store.get(task_id)
    if task is None:
        raise HTTPException(404, detail="Task not found")
    _check_owner(task, current)
    _cm_cache.pop(f"{task_id}::{q_id}", None)
    return {"status": "cleared"}
