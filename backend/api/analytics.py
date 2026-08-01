"""Normalized post-grading analytics for the Figma presentation API.

The assignment, frozen grading run, grade results, teacher reviews, answers,
and student identities are loaded from normalized SQL rows.  The bounded
in-process cache below contains only derived common-mistake prose; it is never
used as a source of grading facts.
"""
from __future__ import annotations

import hashlib
import logging
import math
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from threading import RLock
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, select

from backend.agents import analytics_agent
from backend.auth import require_teacher
from backend.db.models import (
    AssignmentQuestionRecord,
    AssignmentRecord,
    GradeResultRecord,
    GradingRunRecord,
    SubmissionAnswerRecord,
    TeacherReviewRecord,
    UserRecord,
)
from backend.db.session import session_scope
from backend.llm.registry import (
    ExpertRegistry,
    SharedPoolLimitError,
    get_scoped_expert_registry,
)
from backend.models import User


router = APIRouter(prefix="/analytics", tags=["analytics"])
logger = logging.getLogger(__name__)

_USABLE_RUN_STATUSES = frozenset({"completed", "partial_failed"})
_RATE_WINDOW_SECONDS = 30.0
_CACHE_TTL_SECONDS = 2 * 60 * 60
_CACHE_MAX_ENTRIES = 1000


class QueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    mode: Literal["filter", "summary", "chart"] = "filter"


@dataclass(frozen=True)
class _QuestionFact:
    id: str
    q_id: str
    number: str
    type: str
    stem: str
    criterion: str
    max_score: float
    version: int

    def prompt_value(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "q_id": self.q_id,
            "number": self.number,
            "type": self.type,
            "stem": self.stem,
            "criterion": self.criterion,
            "max_score": self.max_score,
            "version": self.version,
        }


@dataclass(frozen=True)
class _ResultFact:
    id: str
    submission_revision_id: str
    question_id: str
    student_id: str
    q_id: str
    score: float | None
    max_score: float
    comment: str
    confidence: float | None
    requires_review: bool
    review_reason: str | None
    result_status: str
    updated_at: float
    review_id: str | None
    review_created_at: float | None

    @property
    def is_scored(self) -> bool:
        return self.result_status == "graded" and self.score is not None


@dataclass(frozen=True)
class _AnalyticsFacts:
    assignment_id: str
    run_id: str
    cache_version: str
    questions_by_id: dict[str, _QuestionFact]
    questions_by_q_id: dict[str, _QuestionFact]
    results: tuple[_ResultFact, ...]
    answers: dict[tuple[str, str], str]
    student_names: dict[str, str]
    results_payload: dict[str, Any]
    per_student_stats: list[dict[str, Any]]

    @property
    def problem_data(self) -> dict[str, dict[str, Any]]:
        return {
            q_id: question.prompt_value()
            for q_id, question in self.questions_by_q_id.items()
        }

    @property
    def student_ids(self) -> set[str]:
        return {result.student_id for result in self.results}


@dataclass(frozen=True)
class _CacheEntry:
    markdown: str
    created_at: float


_cache: "OrderedDict[tuple[str, str, str, str], _CacheEntry]" = OrderedDict()
_cache_lock = RLock()
_query_last_at: dict[str, float] = {}
_query_rate_lock = RLock()


def _not_found() -> HTTPException:
    return HTTPException(
        status.HTTP_404_NOT_FOUND,
        detail={"code": "analytics_task_not_found"},
    )


def _not_ready() -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT,
        detail={"code": "analytics_not_ready"},
    )


def _question_not_found() -> HTTPException:
    return HTTPException(
        status.HTTP_404_NOT_FOUND,
        detail={"code": "analytics_question_not_found"},
    )


def _question(record: AssignmentQuestionRecord) -> _QuestionFact:
    return _QuestionFact(
        id=record.id,
        q_id=record.q_id,
        number=record.number or "",
        type=record.type,
        stem=record.stem or "",
        criterion=record.criterion or "",
        max_score=float(record.max_score),
        version=int(record.version),
    )


def _load_facts(task_id: str, owner_id: str) -> _AnalyticsFacts:
    """Read one internally consistent, owner-scoped analytics snapshot."""
    with session_scope() as session:
        assignment = session.scalar(
            select(AssignmentRecord).where(
                AssignmentRecord.id == task_id,
                AssignmentRecord.teacher_id == owner_id,
            )
        )
        if assignment is None:
            raise _not_found()

        run = session.scalar(
            select(GradingRunRecord)
            .where(
                GradingRunRecord.assignment_id == task_id,
                GradingRunRecord.teacher_id == owner_id,
            )
            .order_by(GradingRunRecord.created_at.desc(), GradingRunRecord.id.desc())
            .limit(1)
        )
        if run is None or run.status not in _USABLE_RUN_STATUSES:
            raise _not_ready()

        question_records = list(session.scalars(
            select(AssignmentQuestionRecord)
            .join(
                AssignmentRecord,
                AssignmentRecord.id == AssignmentQuestionRecord.assignment_id,
            )
            .where(
                AssignmentQuestionRecord.assignment_id == task_id,
                AssignmentRecord.teacher_id == owner_id,
            )
            .order_by(
                AssignmentQuestionRecord.order_index,
                AssignmentQuestionRecord.version,
                AssignmentQuestionRecord.created_at,
            )
        ))
        questions_by_id = {record.id: _question(record) for record in question_records}
        # AssignmentQuestion keeps historical versions. The current presentation
        # view uses the greatest version for an unqualified q_id; exact result
        # rows still point at their immutable question_id below.
        questions_by_q_id: dict[str, _QuestionFact] = {}
        for record in question_records:
            candidate = questions_by_id[record.id]
            previous = questions_by_q_id.get(record.q_id)
            if previous is None or candidate.version >= previous.version:
                questions_by_q_id[record.q_id] = candidate

        result_records = list(session.scalars(
            select(GradeResultRecord)
            .join(
                GradingRunRecord,
                GradingRunRecord.id == GradeResultRecord.grading_run_id,
            )
            .where(
                GradeResultRecord.grading_run_id == run.id,
                GradingRunRecord.assignment_id == task_id,
                GradingRunRecord.teacher_id == owner_id,
            )
            .order_by(GradeResultRecord.student_id, GradeResultRecord.q_id)
        ))
        result_ids = [record.id for record in result_records]
        review_records = list(session.scalars(
            select(TeacherReviewRecord)
            .join(
                GradeResultRecord,
                GradeResultRecord.id == TeacherReviewRecord.grade_result_id,
            )
            .join(
                GradingRunRecord,
                GradingRunRecord.id == GradeResultRecord.grading_run_id,
            )
            .where(
                TeacherReviewRecord.grade_result_id.in_(result_ids),
                TeacherReviewRecord.teacher_id == owner_id,
                GradingRunRecord.id == run.id,
                GradingRunRecord.teacher_id == owner_id,
            )
            .order_by(TeacherReviewRecord.created_at, TeacherReviewRecord.id)
        )) if result_ids else []
        latest_reviews = {
            review.grade_result_id: review for review in review_records
        }

        results: list[_ResultFact] = []
        for record in result_records:
            review = latest_reviews.get(record.id)
            results.append(_ResultFact(
                id=record.id,
                submission_revision_id=record.submission_revision_id,
                question_id=record.question_id,
                student_id=record.student_id,
                q_id=record.q_id,
                score=(
                    float(review.new_score)
                    if review is not None
                    else (float(record.ai_score) if record.ai_score is not None else None)
                ),
                max_score=float(record.ai_max_score),
                comment=(
                    review.new_comment if review is not None else (record.ai_comment or "")
                ),
                confidence=(
                    float(record.ai_confidence)
                    if record.ai_confidence is not None else None
                ),
                requires_review=bool(record.requires_review),
                review_reason=record.review_reason,
                result_status=record.result_status,
                updated_at=record.updated_at,
                review_id=review.id if review is not None else None,
                review_created_at=review.created_at if review is not None else None,
            ))
            exact_question = questions_by_id.get(record.question_id)
            if exact_question is not None:
                questions_by_q_id[record.q_id] = exact_question

        answer_rows = list(session.scalars(
            select(SubmissionAnswerRecord)
            .join(
                GradeResultRecord,
                and_(
                    GradeResultRecord.submission_revision_id
                    == SubmissionAnswerRecord.revision_id,
                    GradeResultRecord.question_id == SubmissionAnswerRecord.question_id,
                ),
            )
            .join(
                GradingRunRecord,
                GradingRunRecord.id == GradeResultRecord.grading_run_id,
            )
            .where(
                GradingRunRecord.id == run.id,
                GradingRunRecord.assignment_id == task_id,
                GradingRunRecord.teacher_id == owner_id,
            )
        ))
        answers = {
            (answer.revision_id, answer.question_id): answer.content or ""
            for answer in answer_rows
        }

        student_names = dict(
            session.execute(
                select(UserRecord.id, UserRecord.username)
                .join(GradeResultRecord, GradeResultRecord.student_id == UserRecord.id)
                .join(
                    GradingRunRecord,
                    GradingRunRecord.id == GradeResultRecord.grading_run_id,
                )
                .where(
                    GradingRunRecord.id == run.id,
                    GradingRunRecord.assignment_id == task_id,
                    GradingRunRecord.teacher_id == owner_id,
                )
                .distinct()
            ).all()
        )

        version_parts = [run.id, str(run.completed_at or run.created_at)]
        version_parts.extend(
            f"{result.id}:{result.updated_at}:{result.review_id or ''}:"
            f"{result.review_created_at or ''}"
            for result in results
        )
        cache_version = hashlib.sha256(
            "|".join(version_parts).encode("utf-8")
        ).hexdigest()[:20]

    payload, stats = _presentation_payload(results, answers, student_names)
    return _AnalyticsFacts(
        assignment_id=task_id,
        run_id=run.id,
        cache_version=cache_version,
        questions_by_id=questions_by_id,
        questions_by_q_id=questions_by_q_id,
        results=tuple(results),
        answers=answers,
        student_names=student_names,
        results_payload=payload,
        per_student_stats=stats,
    )


def _presentation_payload(
    results: list[_ResultFact],
    answers: dict[tuple[str, str], str],
    student_names: dict[str, str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    grouped: dict[str, list[_ResultFact]] = defaultdict(list)
    for result in results:
        grouped[result.student_id].append(result)

    students: list[dict[str, Any]] = []
    stats: list[dict[str, Any]] = []
    for student_id in sorted(grouped):
        student_results = grouped[student_id]
        scored = [result for result in student_results if result.is_scored]
        corrections = [
            {
                "q_id": result.q_id,
                "score": result.score,
                "max_score": result.max_score,
                "comment": result.comment,
                "confidence": result.confidence,
                "requires_human_review": result.requires_review,
                "review_reasons": (
                    [result.review_reason] if result.review_reason else []
                ),
            }
            for result in scored
        ]
        student_answers = [
            {
                "q_id": result.q_id,
                "content": answers.get(
                    (result.submission_revision_id, result.question_id), ""
                ),
            }
            for result in student_results
        ]
        total = sum(float(result.score or 0.0) for result in scored)
        maximum = sum(result.max_score for result in scored)
        percentage = round(total / maximum * 100, 1) if maximum > 0 else None
        name = student_names.get(student_id, student_id)
        students.append({
            "student_id": student_id,
            "student_name": name,
            "corrections": corrections,
            "student_answers": student_answers,
        })
        stats.append({
            "id": student_id,
            "name": name,
            "total": round(total, 2),
            "max": round(maximum, 2),
            "pct": percentage,
            "graded_items": len(scored),
            "unresolved_items": len(student_results) - len(scored),
            "per_q": [
                {
                    "q_id": result.q_id,
                    "score": result.score,
                    "max_score": result.max_score,
                }
                for result in scored
            ],
        })
    return {"results": students}, stats


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


def _safe_chart(output: analytics_agent.ChartOutput) -> dict[str, Any]:
    """Return only the Plotly fields/types the Figma renderer understands."""
    validated = analytics_agent.ChartOutput.model_validate(output.model_dump())
    traces: list[dict[str, Any]] = []
    if not 1 <= len(validated.traces) <= 4:
        raise ValueError("invalid_trace_count")
    for trace in validated.traces:
        if trace.type not in analytics_agent.ALLOWED_TRACE_TYPES:
            raise ValueError("unsupported_trace_type")
        rendered: dict[str, Any] = {"type": trace.type}
        if trace.name is not None:
            rendered["name"] = _safe_text(trace.name, 160)
        for field_name in ("x", "y"):
            values = getattr(trace, field_name)
            if values is not None:
                rendered[field_name] = _safe_axis_values(values)
        if trace.labels is not None:
            if len(trace.labels) > 50:
                raise ValueError("too_many_chart_points")
            rendered["labels"] = [_safe_text(label, 160) for label in trace.labels]
        if trace.values is not None:
            if len(trace.values) > 50 or not all(
                _finite_number(value) for value in trace.values
            ):
                raise ValueError("invalid_chart_values")
            rendered["values"] = [float(value) for value in trace.values]
        if "x" in rendered and "y" in rendered and len(rendered["x"]) != len(rendered["y"]):
            raise ValueError("chart_axis_length_mismatch")
        if "labels" in rendered and "values" in rendered and (
            len(rendered["labels"]) != len(rendered["values"])
        ):
            raise ValueError("chart_pie_length_mismatch")
        traces.append(rendered)

    layout = validated.layout
    if not 240 <= layout.height <= 800:
        raise ValueError("invalid_chart_height")
    if layout.barmode not in {None, "group", "stack", "relative"}:
        raise ValueError("invalid_chart_barmode")
    rendered_layout: dict[str, Any] = {"height": layout.height}
    for field_name in ("title", "xaxis_title", "yaxis_title"):
        value = getattr(layout, field_name)
        if value is not None:
            rendered_layout[field_name] = _safe_text(value, 200)
    if layout.barmode is not None:
        rendered_layout["barmode"] = layout.barmode
    return {
        "mode": "chart",
        "title": _safe_text(validated.title, 200) or "Chart",
        "rationale": _safe_text(validated.rationale, 1000),
        "traces": traces,
        "layout": rendered_layout,
    }


def _safe_axis_values(values: list[Any]) -> list[str | float]:
    if len(values) > 50:
        raise ValueError("too_many_chart_points")
    rendered: list[str | float] = []
    for value in values:
        if isinstance(value, str):
            rendered.append(_safe_text(value, 160))
        elif _finite_number(value):
            rendered.append(float(value))
        else:
            raise ValueError("invalid_chart_axis_value")
    return rendered


def _finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _provider_error(exc: SharedPoolLimitError) -> HTTPException:
    limited = str(exc) == "shared_pool_daily_limit_reached"
    return HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS if limited else status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": (
                "analytics_quota_exceeded"
                if limited else "analytics_provider_unavailable"
            )
        },
    )


@router.post("/{task_id}/query")
async def nl_query(
    task_id: str,
    req: QueryRequest,
    current: User = Depends(require_teacher),
    registry: ExpertRegistry = Depends(get_scoped_expert_registry),
):
    facts = _load_facts(task_id, current.id)
    if not facts.results:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "analytics_no_results"},
        )
    provider = registry.pick_default()
    if provider is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "analytics_provider_unavailable"},
        )
    _check_rate_limit(current.id)

    try:
        if req.mode == "filter":
            output = await analytics_agent.filter_students(
                question=req.question,
                results_payload=facts.results_payload,
                problem_data=facts.problem_data,
                per_student_stats=facts.per_student_stats,
                provider=provider,
            )
            student_ids = [
                student_id
                for student_id in dict.fromkeys(output.student_ids)
                if student_id in facts.student_ids
            ]
            return {
                "mode": "filter",
                "student_ids": student_ids,
                "explanation": _safe_text(output.explanation, 1000),
            }
        if req.mode == "summary":
            output = await analytics_agent.summarize(
                question=req.question,
                results_payload=facts.results_payload,
                problem_data=facts.problem_data,
                per_student_stats=facts.per_student_stats,
                provider=provider,
            )
            return {
                "mode": "summary",
                "markdown": _safe_text(output.markdown, 4000),
            }
        output = await analytics_agent.make_chart(
            question=req.question,
            results_payload=facts.results_payload,
            problem_data=facts.problem_data,
            per_student_stats=facts.per_student_stats,
            provider=provider,
        )
        return _safe_chart(output)
    except HTTPException:
        raise
    except SharedPoolLimitError as exc:
        raise _provider_error(exc) from exc
    except Exception as exc:
        # Never return provider, parser, prompt, student-answer, or credential
        # details. Exception type is sufficient for server-side diagnosis.
        logger.warning(
            "Analytics generation failed; task_id=%s mode=%s exception_type=%s",
            task_id,
            req.mode,
            type(exc).__name__,
        )
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail={"code": "analytics_generation_failed"},
        ) from exc


def _resolve_question(facts: _AnalyticsFacts, identifier: str) -> _QuestionFact:
    question = facts.questions_by_id.get(identifier) or facts.questions_by_q_id.get(identifier)
    if question is None:
        raise _question_not_found()
    return question


def _question_breakdown(
    facts: _AnalyticsFacts, question: _QuestionFact
) -> dict[str, Any]:
    matching = [
        result for result in facts.results
        if result.question_id == question.id or result.q_id == question.q_id
    ]
    scored = [result for result in matching if result.is_scored]
    rows = [
        {
            "student_id": result.student_id,
            "student_name": facts.student_names.get(
                result.student_id, result.student_id
            ),
            "answer": facts.answers.get(
                (result.submission_revision_id, result.question_id), ""
            ),
            "score": float(result.score or 0.0),
            "max_score": result.max_score,
            "comment": result.comment,
            "confidence": result.confidence,
            "requires_human_review": result.requires_review,
            "review_reasons": [result.review_reason] if result.review_reason else [],
        }
        for result in scored
    ]
    scores = [float(result.score or 0.0) for result in scored]
    maxima = [result.max_score for result in scored]
    average = sum(scores) / len(scores) if scores else 0.0
    average_max = sum(maxima) / len(maxima) if maxima else question.max_score
    pass_count = sum(
        1 for score, maximum in zip(scores, maxima)
        if maximum > 0 and score / maximum >= 0.6
    )
    stats = {
        "n": len(scored),
        "total_results": len(matching),
        "unavailable": len(matching) - len(scored),
        "avg": round(average, 2),
        "max_score": round(average_max, 2),
        "pct_avg": round(average / average_max * 100, 1) if average_max > 0 else 0.0,
        "pass_rate": round(pass_count / len(scored) * 100, 1) if scored else 0.0,
        "min": min(scores) if scores else 0.0,
        "max": max(scores) if scores else 0.0,
    }
    problem = question.prompt_value()
    return {
        "q_id": question.q_id,
        "question": question.number or question.q_id,
        "stem": question.stem,
        "max_score": question.max_score,
        "avg_score": round(average, 2),
        "problem": problem,
        "stats": stats,
        "rows": rows,
    }


def _cache_get(key: tuple[str, str, str, str]) -> str | None:
    now = time.monotonic()
    with _cache_lock:
        _prune_cache(now)
        entry = _cache.get(key)
        if entry is None:
            return None
        _cache.move_to_end(key)
        return entry.markdown


def _cache_put(key: tuple[str, str, str, str], markdown: str) -> None:
    with _cache_lock:
        _cache[key] = _CacheEntry(markdown=markdown, created_at=time.monotonic())
        _cache.move_to_end(key)
        _prune_cache()


def _prune_cache(now: float | None = None) -> None:
    current = time.monotonic() if now is None else now
    for key, entry in list(_cache.items()):
        if current - entry.created_at > _CACHE_TTL_SECONDS:
            _cache.pop(key, None)
    while len(_cache) > _CACHE_MAX_ENTRIES:
        _cache.popitem(last=False)


def _clear_cache(owner_id: str, task_id: str, question_id: str | None = None) -> None:
    with _cache_lock:
        for key in list(_cache):
            key_owner, key_task, key_question, _version = key
            if (
                key_owner == owner_id
                and key_task == task_id
                and (question_id is None or key_question == question_id)
            ):
                _cache.pop(key, None)


@router.get("/{task_id}/per_question/{question_id}")
async def per_question(
    task_id: str,
    question_id: str,
    current: User = Depends(require_teacher),
    registry: ExpertRegistry = Depends(get_scoped_expert_registry),
):
    facts = _load_facts(task_id, current.id)
    question = _resolve_question(facts, question_id)
    breakdown = _question_breakdown(facts, question)
    cache_key = (current.id, task_id, question.q_id, facts.cache_version)
    common_mistakes_md = _cache_get(cache_key)
    if common_mistakes_md is None and breakdown["rows"]:
        provider = registry.pick_default()
        if provider is not None:
            try:
                output = await analytics_agent.question_common_mistakes(
                    q_id=question.q_id,
                    breakdown=breakdown,
                    provider=provider,
                )
                common_mistakes_md = _safe_text(output.common_mistakes_md, 4000)
                _cache_put(cache_key, common_mistakes_md)
            except SharedPoolLimitError:
                common_mistakes_md = ""
            except Exception as exc:
                logger.warning(
                    "Common-mistake generation failed; task_id=%s exception_type=%s",
                    task_id,
                    type(exc).__name__,
                )
                common_mistakes_md = ""
    if common_mistakes_md is None:
        common_mistakes_md = ""
    return {**breakdown, "common_mistakes_md": common_mistakes_md}


def _authorize_cache_clear(task_id: str, owner_id: str) -> None:
    with session_scope() as session:
        owned = session.scalar(
            select(AssignmentRecord.id).where(
                AssignmentRecord.id == task_id,
                AssignmentRecord.teacher_id == owner_id,
            )
        )
    if owned is None:
        raise _not_found()


@router.delete("/{task_id}/cache")
def reset_task_cache(
    task_id: str,
    current: User = Depends(require_teacher),
):
    _authorize_cache_clear(task_id, current.id)
    _clear_cache(current.id, task_id)
    return {"status": "cleared"}


# Current Figma client compatibility. The canonical task-level endpoint above
# clears all derived entries and is preferred by new callers.
@router.delete("/{task_id}/per_question/{question_id}/cache")
def reset_per_question_cache(
    task_id: str,
    question_id: str,
    current: User = Depends(require_teacher),
):
    _authorize_cache_clear(task_id, current.id)
    _clear_cache(current.id, task_id, question_id)
    return {"status": "cleared"}
