"""
Analytics Agent — natural-language post-grading queries.

Three modes (single-turn, no follow-up):
  - filter: returns subset of student_ids matching the teacher's ask
  - summary: returns markdown text summarizing patterns / common mistakes
  - chart: returns plotly figure JSON (whitelisted shape) for arbitrary visualization

All modes consume:
  - The grading result (per-student corrections)
  - The problem data (q_id, type, stem, criterion)
  - Optional per_student_stats (avg/max/pct cached up-front)

Design constraints:
  - Strict input cap (<= 1000 chars in question, <= 50 students sampled in chart)
  - Plotly chart output validated against a small schema; unknown trace types rejected
  - LLM is forced to return JSON; we parse via the same `extract_and_parse_json` used elsewhere
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Literal, Optional

from langchain_core.messages import SystemMessage, HumanMessage
from pydantic import BaseModel, Field, ValidationError

from backend.llm.providers import BaseProvider
from backend.tools.structured_llm import extract_and_parse_json

logger = logging.getLogger(__name__)


# ─── Output schemas ──────────────────────────────────────────────────────────

class FilterOutput(BaseModel):
    student_ids: List[str] = Field(description="Subset of student IDs matching the teacher's ask")
    explanation: str = Field("", description="One-sentence rationale for the filter")


class SummaryOutput(BaseModel):
    markdown: str = Field(description="Markdown text summarizing the answer to the teacher's ask")


# ── Plotly: tightly bounded subset ──────────────────────────────────────────

ALLOWED_TRACE_TYPES = {"bar", "scatter", "pie", "histogram", "box"}


class ChartTrace(BaseModel):
    type: Literal["bar", "scatter", "pie", "histogram", "box"]
    name: Optional[str] = None
    x: Optional[List[Any]] = None
    y: Optional[List[Any]] = None
    labels: Optional[List[str]] = None
    values: Optional[List[float]] = None
    mode: Optional[str] = None
    marker: Optional[Dict[str, Any]] = None


class ChartLayout(BaseModel):
    title: Optional[str] = None
    xaxis_title: Optional[str] = Field(None, alias="xaxis_title")
    yaxis_title: Optional[str] = Field(None, alias="yaxis_title")
    height: int = 360
    barmode: Optional[str] = None


class ChartOutput(BaseModel):
    """Restricted Plotly figure spec. Not full Plotly — tightly enumerated."""
    title: str = Field("Chart", description="Short caption (also used as layout.title)")
    rationale: str = Field("", description="One-sentence rationale for the chosen chart type")
    traces: List[ChartTrace] = Field(min_length=1, max_length=4)
    layout: ChartLayout = Field(default_factory=ChartLayout)


# ─── Per-question summary output ─────────────────────────────────────────────

class QuestionSummaryOutput(BaseModel):
    common_mistakes_md: str = Field(description="Markdown summary of common mistakes / 易错点")


# ─── Prompts ─────────────────────────────────────────────────────────────────

FILTER_SYS = """You are SmarTAI's analytics assistant. The teacher will ask a question
that should resolve to a subset of students from a graded class.

Inputs you receive:
  - The grading result: per-student total score, percentage, grade letter, per-question scores.
  - The problem set (q_id → type, number, stem fragment).
  - The teacher's question.

Return JSON: {"student_ids": [...], "explanation": "one sentence rationale"}.
- Only return student IDs that exist in the input.
- If the question is ambiguous, pick the most reasonable interpretation.
- Output must start with { and end with }.
"""

SUMMARY_SYS = """You are SmarTAI's analytics assistant. The teacher asks a question that
should be answered with a short summary in Chinese or English (matching the question).

Return JSON: {"markdown": "..."}.
- Use markdown lists/headings as appropriate.
- Stay under 800 characters.
- Output must start with { and end with }.
"""

CHART_SYS = """You are SmarTAI's analytics assistant. The teacher asks for a chart.

Return JSON for a restricted Plotly figure. The schema is:
{
  "title": "Chart title",
  "rationale": "Why this chart type",
  "traces": [
    {"type": "bar"|"scatter"|"pie"|"histogram"|"box",
     "name": "...", "x":[...], "y":[...],
     "labels":[...] (pie only), "values":[...] (pie only),
     "mode": "lines"|"markers" (scatter only),
     "marker": {"color": "..."}}
  ],
  "layout": {"title":"...", "xaxis_title":"...", "yaxis_title":"...", "height": 360, "barmode": "group|stack"}
}

Hard rules:
- Only the trace types above. No 3D, no maps, no scattergeo, etc.
- Maximum 4 traces. Maximum 50 data points per trace.
- All values must be valid JSON. No JS function bodies.
- Output must start with { and end with }.
"""

QUESTION_SUMMARY_SYS = """You are SmarTAI's analytics assistant. Given a problem and many
students' answers + scores + comments for that problem, summarize common mistakes (易错点)
and patterns. Be concrete: cite quantities like "47% wrote ...", "common error is X".

Return JSON: {"common_mistakes_md": "..."} where the markdown is under 800 chars.
Output must start with { and end with }.
"""


# ─── Helpers: build prompt body from grading data ────────────────────────────

def _build_grading_context(
    results_payload: Dict[str, Any],
    problem_data: Dict[str, Dict[str, Any]],
    *,
    per_student_stats: Optional[List[Dict[str, Any]]] = None,
    max_students: int = 50,
) -> str:
    """Build a compact context for the LLM. Caps at ~50 students to control tokens."""
    students = results_payload.get("results", [])
    if isinstance(students, dict):
        students = [students]

    # Compute lite stats if not provided
    if per_student_stats is None:
        per_student_stats = []
        for s in students[:max_students]:
            corrections = s.get("corrections", []) or []
            total = sum(float(c.get("score", 0) or 0) for c in corrections)
            mx = sum(float(c.get("max_score", 0) or 0) for c in corrections)
            pct = (total / mx * 100) if mx > 0 else 0.0
            per_student_stats.append({
                "id": s.get("student_id", ""),
                "name": s.get("student_name", ""),
                "total": round(total, 2),
                "max": round(mx, 2),
                "pct": round(pct, 1),
                "per_q": [
                    {
                        "q_id": c.get("q_id", ""),
                        "score": c.get("score", 0),
                        "max_score": c.get("max_score", 0),
                    }
                    for c in corrections
                ],
            })

    problem_lite = [
        {
            "q_id": p.get("q_id", ""),
            "number": p.get("number", ""),
            "type": p.get("type", ""),
            "stem_preview": (p.get("stem", "") or "")[:120],
        }
        for p in (problem_data or {}).values()
    ]

    return json.dumps({
        "problems": problem_lite,
        "students": per_student_stats[:max_students],
    }, ensure_ascii=False)


# ─── The three modes ─────────────────────────────────────────────────────────

async def filter_students(
    *,
    question: str,
    results_payload: Dict[str, Any],
    problem_data: Dict[str, Dict[str, Any]],
    provider: BaseProvider,
    per_student_stats: Optional[List[Dict[str, Any]]] = None,
) -> FilterOutput:
    ctx = _build_grading_context(results_payload, problem_data, per_student_stats=per_student_stats)
    user_msg = f"**[Class Data (JSON)]**:\n{ctx}\n\n**[Teacher Question]**: {question}"
    response = await provider.ainvoke([
        SystemMessage(content=FILTER_SYS),
        HumanMessage(content=user_msg),
    ])
    return extract_and_parse_json(response.content, FilterOutput)


async def summarize(
    *,
    question: str,
    results_payload: Dict[str, Any],
    problem_data: Dict[str, Dict[str, Any]],
    provider: BaseProvider,
    per_student_stats: Optional[List[Dict[str, Any]]] = None,
) -> SummaryOutput:
    ctx = _build_grading_context(results_payload, problem_data, per_student_stats=per_student_stats)
    user_msg = f"**[Class Data (JSON)]**:\n{ctx}\n\n**[Teacher Question]**: {question}"
    response = await provider.ainvoke([
        SystemMessage(content=SUMMARY_SYS),
        HumanMessage(content=user_msg),
    ])
    return extract_and_parse_json(response.content, SummaryOutput)


async def make_chart(
    *,
    question: str,
    results_payload: Dict[str, Any],
    problem_data: Dict[str, Dict[str, Any]],
    provider: BaseProvider,
    per_student_stats: Optional[List[Dict[str, Any]]] = None,
) -> ChartOutput:
    ctx = _build_grading_context(results_payload, problem_data, per_student_stats=per_student_stats)
    user_msg = f"**[Class Data (JSON)]**:\n{ctx}\n\n**[Teacher Asks for Chart]**: {question}"
    response = await provider.ainvoke([
        SystemMessage(content=CHART_SYS),
        HumanMessage(content=user_msg),
    ])
    return extract_and_parse_json(response.content, ChartOutput)


# ─── Per-question detail (deterministic stats + LLM common-mistakes) ─────────

def per_question_breakdown(
    q_id: str,
    results_payload: Dict[str, Any],
    problem_data: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """Aggregate a single question's stats + collect every student's answer for it."""
    problem = (problem_data or {}).get(q_id, {})

    students = results_payload.get("results", [])
    if isinstance(students, dict):
        students = [students]

    rows: List[Dict[str, Any]] = []
    scores: List[float] = []
    max_scores: List[float] = []

    for s in students:
        sid = s.get("student_id", "")
        sname = s.get("student_name", "")
        # Match correction
        correction = next(
            (c for c in (s.get("corrections", []) or []) if c.get("q_id") == q_id),
            None,
        )
        # Match answer
        answer = next(
            (a for a in (s.get("student_answers", []) or []) if a.get("q_id") == q_id),
            None,
        )
        if correction is None:
            continue
        score = float(correction.get("score", 0) or 0)
        mx = float(correction.get("max_score", 0) or 0)
        scores.append(score)
        max_scores.append(mx)
        rows.append({
            "student_id": sid,
            "student_name": sname,
            "score": score,
            "max_score": mx,
            "pct": round(score / mx * 100, 1) if mx else 0.0,
            "comment": correction.get("comment", ""),
            "confidence": correction.get("confidence", 0),
            "answer": (answer or {}).get("content", "") if answer else "",
        })

    avg = sum(scores) / len(scores) if scores else 0.0
    mx_avg = sum(max_scores) / len(max_scores) if max_scores else 0.0
    pct_avg = (avg / mx_avg * 100) if mx_avg else 0.0
    pass_count = sum(1 for s, m in zip(scores, max_scores) if m and s / m >= 0.6)
    pass_rate = (pass_count / len(scores) * 100) if scores else 0.0

    return {
        "q_id": q_id,
        "problem": problem,
        "stats": {
            "n": len(scores),
            "avg": round(avg, 2),
            "max_score": round(mx_avg, 2),
            "pct_avg": round(pct_avg, 1),
            "pass_rate": round(pass_rate, 1),
            "min": min(scores) if scores else 0,
            "max": max(scores) if scores else 0,
        },
        "rows": rows,
    }


async def question_common_mistakes(
    *,
    q_id: str,
    breakdown: Dict[str, Any],
    provider: BaseProvider,
) -> QuestionSummaryOutput:
    """Generate Chinese-or-English markdown summary of common mistakes for a question."""
    problem = breakdown.get("problem", {})
    rows = breakdown.get("rows", [])
    # Cap rows to control tokens
    capped_rows = [
        {
            "student_id": r["student_id"],
            "score": r["score"],
            "max_score": r["max_score"],
            "answer": (r.get("answer", "") or "")[:200],
            "comment": (r.get("comment", "") or "")[:200],
        }
        for r in rows[:50]
    ]
    user_msg = (
        f"**[Problem]**:\n"
        f"  number: {problem.get('number','')}\n"
        f"  type: {problem.get('type','')}\n"
        f"  stem: {problem.get('stem','')[:300]}\n"
        f"  criterion: {problem.get('criterion','')[:200]}\n\n"
        f"**[Student Responses (JSON)]**:\n{json.dumps(capped_rows, ensure_ascii=False)}"
    )
    response = await provider.ainvoke([
        SystemMessage(content=QUESTION_SUMMARY_SYS),
        HumanMessage(content=user_msg),
    ])
    return extract_and_parse_json(response.content, QuestionSummaryOutput)
