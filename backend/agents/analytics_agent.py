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
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from backend.llm.providers import BaseProvider
from backend.tools.structured_llm import extract_and_parse_json

logger = logging.getLogger(__name__)


# ─── Output schemas ──────────────────────────────────────────────────────────

class FilterOutput(BaseModel):
    student_ids: List[str] = Field(description="Subset of student IDs matching the teacher's ask")
    explanation: str = Field("", description="One-sentence rationale for the filter")


FilterIntentSurface = Literal[
    "student_analysis", "review_overview", "question_analysis",
    "question_preparation", "submission_review", "student_answer_review",
]


class FilterIntentOutput(BaseModel):
    """A data-free translation from natural language to local filter controls."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    recognized: bool = True
    min_score_percent: Optional[float] = Field(None, ge=0, le=100)
    max_score_percent: Optional[float] = Field(None, ge=0, le=100)
    pass_status: Optional[Literal["pass", "fail", "unscored"]] = None
    low_confidence: bool = False
    review_status: Optional[Literal["pending", "confirmed", "none"]] = None
    disagreement: bool = False
    annotated: bool = False
    sort: Optional[Literal[
        "score_asc", "score_desc", "confidence_asc", "review_desc",
        "name_asc", "name_desc", "question", "confidence_desc", "review_asc",
        "id_asc", "id_desc", "question_desc", "max_score_asc", "max_score_desc",
        "type_asc", "type_desc", "coverage_asc", "coverage_desc",
    ]] = None
    question_tokens: List[str] = Field(default_factory=list, max_length=4)
    question_types: List[Literal[
        "calculation", "programming", "proof", "concept", "choice", "fill_blank", "short_answer",
    ]] = Field(default_factory=list, max_length=4)
    max_average_confidence: Optional[float] = Field(None, ge=0, le=1)
    missing_knowledge: bool = False
    min_max_score: Optional[float] = Field(None, ge=0)
    max_max_score: Optional[float] = Field(None, ge=0)
    preparation_status: Optional[Literal[
        "attention", "low_confidence", "source_conflict", "parse_anomaly", "ready",
    ]] = None
    material_field: Optional[Literal["stem", "answer", "rubric", "tests"]] = None
    material_status: Optional[Literal["missing", "ready", "generated", "recognized"]] = None
    submission_status: Optional[Literal[
        "review", "missing", "identity", "recognized", "reviewed",
    ]] = None
    text_terms: List[str] = Field(default_factory=list, max_length=4)
    explanation: str = Field("", max_length=500)

    @model_validator(mode="after")
    def fail_closed_when_unrecognized_or_empty(self) -> "FilterIntentOutput":
        """An unsupported compound request must not execute a supported subset."""
        controls = self.model_dump(exclude={"recognized", "explanation"})
        actionable = any(
            value is not None and value is not False and value != []
            for value in controls.values()
        )
        if self.recognized and actionable:
            return self
        self.recognized = False
        for name in controls:
            setattr(self, name, type(self).model_fields[name].get_default(call_default_factory=True))
        return self


_STUDENT_FILTER_FIELDS = {
    "min_score_percent", "max_score_percent", "pass_status", "low_confidence",
    "review_status", "disagreement", "text_terms",
}
_STUDENT_SORTS = {
    "score_asc", "score_desc", "confidence_asc", "confidence_desc",
    "review_asc", "review_desc", "name_asc", "name_desc", "id_asc", "id_desc",
}
_QUESTION_SORTS = {
    "question", "question_desc", "max_score_asc", "max_score_desc",
    "type_asc", "type_desc", "review_asc", "review_desc",
}
_FILTER_CAPABILITIES = {
    "student_analysis": (_STUDENT_FILTER_FIELDS, _STUDENT_SORTS),
    "review_overview": (
        _STUDENT_FILTER_FIELDS | {"question_tokens", "annotated"}, _STUDENT_SORTS,
    ),
    "question_analysis": (
        {"min_score_percent", "max_score_percent", "low_confidence", "review_status",
         "question_tokens", "question_types", "max_average_confidence",
         "missing_knowledge", "text_terms"},
        _QUESTION_SORTS | {"score_asc", "score_desc", "confidence_asc", "confidence_desc"},
    ),
    "question_preparation": (
        {"question_tokens", "question_types", "text_terms", "min_max_score",
         "max_max_score", "preparation_status", "material_field", "material_status",
         "low_confidence"},
        _QUESTION_SORTS,
    ),
    "submission_review": (
        {"question_tokens", "text_terms", "submission_status"},
        {"name_asc", "name_desc", "id_asc", "id_desc", "coverage_asc",
         "coverage_desc", "review_asc", "review_desc"},
    ),
    "student_answer_review": (
        {"question_tokens", "text_terms", "submission_status"},
        {"question", "question_desc"},
    ),
}


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

FILTER_INTENT_SYS = """You translate a teacher's natural-language filter or sort request
into a fixed set of local UI controls. You receive ONLY the teacher's query and the UI
surface name. You never receive student records, scores, answers, or class analytics.

Return JSON with exactly these fields:
{
  "recognized": true,
  "min_score_percent": null,
  "max_score_percent": null,
  "pass_status": null,
  "low_confidence": false,
  "review_status": null,
  "disagreement": false,
  "annotated": false,
  "sort": null,
  "question_tokens": [],
  "question_types": [],
  "max_average_confidence": null,
  "missing_knowledge": false,
  "min_max_score": null,
  "max_max_score": null,
  "preparation_status": null,
  "material_field": null,
  "material_status": null,
  "submission_status": null,
  "text_terms": [],
  "explanation": "short explanation in the query language"
}

Allowed values:
- pass_status: "pass", "fail", "unscored", or null.
- review_status: "pending", "confirmed", "none", or null.
- sort: "score_asc", "score_desc", "confidence_asc", "confidence_desc",
  "review_asc", "review_desc", "name_asc", "name_desc", "id_asc", "id_desc",
  "question", "question_desc", "max_score_asc", "max_score_desc",
  "type_asc", "type_desc", "coverage_asc", "coverage_desc", or null.
  Name and ID order are distinct. question is ascending question-number order.
  review sorts count items needing attention; coverage sorts recognized answer count.
- On graded surfaces, score limits are percentages from 0 to 100. Phrases such
  as "90分以下" mean max_score_percent=90. Bare "从高到低" means sort="score_desc".
- Use question_tokens only for explicit question references such as Q2 or 第3题.
- On question_analysis, question_types can contain calculation, programming,
  proof, concept, choice, fill_blank, or short_answer. Use max_average_confidence
  for an explicit mean-confidence limit (e.g. below 80% -> 0.8), and
  missing_knowledge for questions without a knowledge-point label.
- On student_analysis, only score limits, pass_status, low_confidence,
  review_status, disagreement, student sort, and text_terms are supported.
- On review_overview, the same controls plus question_tokens and annotated
  are supported.
- On question_analysis, only score limits, low_confidence, review_status,
  question_tokens, question_types, max_average_confidence, missing_knowledge,
  text_terms, and score/confidence/review/question/max_score/type sort are supported.
- On question_preparation, grading has not happened. Only question_tokens,
  question_types, text_terms, low_confidence, min_max_score/max_max_score,
  preparation_status, material_field/material_status, and question/max_score/type/
  review sort are supported. Full marks are absolute points (e.g. 满分至少10分 ->
  min_max_score=10); 按满分升序 -> sort="max_score_asc", not a text search.
  preparation_status is attention, low_confidence, source_conflict, parse_anomaly,
  or ready. material_field is stem, answer, rubric, or tests; material_status is
  missing, ready, generated, or recognized. Supply both material fields together
  (e.g. 缺少标答 -> material_field="answer", material_status="missing").
- On submission_review, grading has not happened. Only question_tokens, text_terms,
  submission_status, and name/id/coverage/review sort are supported.
  submission_status is review (answers needing attention), missing (missing answers),
  identity (identity needs checking), recognized, or reviewed (all answers reviewed).
- The exact field and sort allowlists for this surface are appended below.
- If ANY requested condition is unsupported on the surface, return
  recognized=false. Do not silently apply only part of the request.
- Use text_terms only for literal words that should still be matched locally.
- Numbered <student_1> placeholders are private student identities. When filtering
  that student on student_analysis/review_overview/submission_review, return the EXACT placeholder
  as a separate text_terms item; never replace it with a guessed name. Each term
  is an AND condition. Excluding identities or an OR between identities is not
  supported; return recognized=false for those instructions.
- If the request cannot map to these controls, set recognized=false and explain why.
- Do not invent names, IDs, score thresholds, or question numbers.
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
- Use only the supplied facts. pct is a score percentage (0-100);
  avg_confidence and per_q.confidence are normalized (0-1). Null means unknown,
  never zero. low_confidence_count counts confidence below 0.65.
- review_signal_count counts items with a review signal; pending_review_count
  counts those without a teacher review. Group these facts when asked to compare
  questions or reviewed/unreviewed students. Never invent a missing metric.
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


async def interpret_filter_intent(
    *,
    question: str,
    surface: FilterIntentSurface,
    provider: BaseProvider,
) -> FilterIntentOutput:
    """Interpret only the query text; no grading or student payload is accepted."""
    user_msg = json.dumps(
        {"surface": surface, "teacher_query": question},
        ensure_ascii=False,
    )
    allowed_fields, allowed_sorts = _FILTER_CAPABILITIES[surface]
    controls = json.dumps({
        "allowed_filter_fields": sorted(allowed_fields),
        "allowed_sorts": sorted(allowed_sorts),
    })
    response = await provider.ainvoke([
        SystemMessage(content=f"{FILTER_INTENT_SYS}\nSurface capabilities: {controls}"),
        HumanMessage(content=user_msg),
    ])
    try:
        output = extract_and_parse_json(response.content, FilterIntentOutput)
    except ValueError:
        # Unknown fields/enums must not be dropped while executing the remainder.
        return FilterIntentOutput(recognized=False)
    unsupported = set(FilterIntentOutput.model_fields) - allowed_fields - {
        "recognized", "sort", "explanation",
    }
    if (output.sort is not None and output.sort not in allowed_sorts) or any(
        getattr(output, field) is not None
        and getattr(output, field) is not False
        and getattr(output, field) != []
        for field in unsupported
    ) or bool(output.material_field) != bool(output.material_status):
        output.recognized = False
    for lower, upper in (
        (output.min_score_percent, output.max_score_percent),
        (output.min_max_score, output.max_max_score),
    ):
        if lower is not None and upper is not None and lower > upper:
            output.recognized = False
    return FilterIntentOutput.model_validate(output.model_dump())


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
