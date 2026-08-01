"""History query interpreter with deterministic-first, bounded LLM fallback.

The deterministic parser handles ordinary filters without any model.  Only
unresolved text is optionally sent through Agent -> Tool -> Provider, and the
result is revalidated against the current owner's real course/tag candidates.
No model output can introduce an arbitrary field or another owner's ID.
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
from datetime import date
from threading import Lock
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from backend.config import settings
from backend.llm.providers import BaseProvider
from backend.models import TaskStatus
from backend.progress.tracker import ProgressReporter
from backend.skills.history_query_skill import HistoryQuerySkill


HistorySort = Literal[
    "updated_desc", "updated_asc", "created_desc", "created_asc",
    "name_asc", "name_desc", "attention_first", "stage_asc", "stage_desc",
]

_STATUSES = {
    "draft", "extracting_problems", "problems_ready", "parsing_submissions",
    "submissions_ready", "grading", "graded", "review_confirmed",
    "generating_analysis", "finalized", "error",
}
_SORTS = {
    "updated_desc", "updated_asc", "created_desc", "created_asc",
    "name_asc", "name_desc", "attention_first", "stage_asc", "stage_desc",
}


class HistoryQueryFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    q: Optional[str] = Field(default=None, max_length=120)
    semester_id: Optional[str] = Field(default=None, max_length=40)
    course_id: Optional[str] = Field(default=None, max_length=80)
    tag_ids: List[str] = Field(default_factory=list, max_length=30)
    statuses: List[TaskStatus] = Field(default_factory=list, max_length=11)
    unfinished: Optional[bool] = None
    needs_attention: Optional[bool] = None


class HistoryQueryAmbiguity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fragment: str = Field(default="", max_length=120)
    message: str = Field(default="", max_length=240)
    candidates: List[str] = Field(default_factory=list, max_length=20)


class HistoryLLMOutput(BaseModel):
    """Strict model output; dynamic owner checks are applied after parsing."""

    model_config = ConfigDict(extra="forbid")

    filters: HistoryQueryFilters = Field(default_factory=HistoryQueryFilters)
    sort: Optional[HistorySort] = None
    explanation: str = Field(default="", max_length=500)
    ambiguities: List[HistoryQueryAmbiguity] = Field(
        default_factory=list, max_length=10,
    )


_STATUS_TERMS: Dict[str, tuple[str, ...]] = {
    "draft": ("草稿", "draft"),
    "extracting_problems": ("题目识别中", "提取题目中", "extracting problems"),
    "problems_ready": ("题目已就绪", "problems ready"),
    "parsing_submissions": ("作答解析中", "解析作答中", "parsing submissions"),
    "submissions_ready": ("待批改", "作答已就绪", "ready to grade"),
    "grading": ("批改中", "grading"),
    "graded": ("已批改", "批改完成", "结果待复核", "结果已生成", "graded"),
    "review_confirmed": ("复核已确认", "已确认复核", "review confirmed"),
    "generating_analysis": ("分析生成中", "生成分析中", "generating analysis"),
    "finalized": ("正式完成", "已完成", "finalized", "finalised", "completed"),
    "error": ("出错", "错误", "失败", "error", "failed"),
}

_GENERIC_TERMS = (
    "请帮我", "帮我", "请筛选", "请过滤", "请查找", "请显示", "请列出",
    "筛选任务", "过滤任务", "查找任务", "显示任务", "列出任务",
    "筛选", "过滤", "查找", "找到", "显示", "列出", "一下", "并且", "以及",
    "please", "show", "find", "filter", "tasks", "task", "all", "and",
)

_last_llm_at: Dict[str, float] = {}
_llm_daily_usage: Dict[tuple[str, str], int] = {}
_llm_limit_lock = Lock()


def _normalise_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip()


def _term_pattern(term: str) -> str:
    """Match Latin/digit terms as tokens and CJK phrases as substrings.

    Word boundaries prevent filler words/codes such as ``and``, ``all`` or
    ``AI`` from matching inside real task keywords like ``standard``, ``small``
    or ``explain``. CJK has no whitespace token boundary, so multi-character
    phrases retain substring semantics.
    """

    escaped = re.escape(term.casefold())
    if re.search(r"[a-z0-9]", term, flags=re.IGNORECASE):
        return rf"(?<![a-z0-9_]){escaped}(?![a-z0-9_])"
    return escaped


def _contains_term(text: str, term: str) -> bool:
    return re.search(_term_pattern(term), text.casefold(), flags=re.IGNORECASE) is not None


def _consume(remaining: str, term: str) -> str:
    return re.sub(_term_pattern(term), " ", remaining, flags=re.IGNORECASE)


def _semester_aliases(semester_id: str) -> List[str]:
    aliases = [semester_id.casefold()]
    match = re.fullmatch(
        r"(20\d{2})-(20\d{2})-(autumn|winter|spring|summer)", semester_id,
        flags=re.IGNORECASE,
    )
    if not match:
        return aliases
    start, end, season = match.groups()
    zh = {"autumn": "秋", "winter": "冬", "spring": "春", "summer": "夏"}[season.casefold()]
    aliases.extend([
        f"{start}-{end}{zh}", f"{start}-{end} {zh}",
        f"{start}-{end}{zh}季学期", f"{start}-{end} {season.casefold()}",
    ])
    return aliases


def _build_conditions(
    filters: HistoryQueryFilters,
    sort: Optional[HistorySort],
    *,
    courses: List[Dict[str, str]],
    tags: List[Dict[str, str]],
) -> List[Dict[str, Any]]:
    course_names = {item["id"]: item.get("name", item["id"]) for item in courses}
    tag_names = {item["id"]: item.get("name", item["id"]) for item in tags}
    conditions: List[Dict[str, Any]] = []
    values = filters.model_dump(exclude_none=True)
    for field, value in values.items():
        if value in ([], ""):
            continue
        label = field
        display_value: Any = value
        if field == "course_id":
            label = "课程"
            display_value = course_names.get(str(value), value)
        elif field == "tag_ids":
            label = "标签"
            display_value = [tag_names.get(str(item), item) for item in value]
        elif field == "semester_id":
            label = "学期"
        elif field == "statuses":
            label = "状态"
        elif field == "unfinished":
            label = "未完成"
        elif field == "needs_attention":
            label = "需要关注"
        elif field == "q":
            label = "关键词"
        conditions.append({"field": field, "label": label, "value": display_value})
    if sort:
        conditions.append({"field": "sort", "label": "排序", "value": sort})
    return conditions


def deterministic_history_query(
    query: str,
    *,
    semesters: List[str],
    courses: List[Dict[str, str]],
    tags: List[Dict[str, str]],
) -> Dict[str, Any]:
    """Parse exact metadata/status/sort phrases without an LLM."""

    text = _normalise_text(query)
    lower = text.casefold()
    remaining = lower
    filters = HistoryQueryFilters()
    matched: List[str] = []
    ambiguities: List[HistoryQueryAmbiguity] = []

    semester_matches: List[tuple[str, str]] = []
    for semester_id in semesters:
        for alias in _semester_aliases(semester_id):
            if alias and _contains_term(lower, alias):
                semester_matches.append((semester_id, alias))
                break
    if len({item[0] for item in semester_matches}) == 1:
        filters.semester_id = semester_matches[0][0]
        remaining = _consume(remaining, semester_matches[0][1])
        matched.append("学期")
    elif semester_matches:
        candidates = sorted({item[0] for item in semester_matches})
        ambiguities.append(HistoryQueryAmbiguity(
            fragment=text,
            message="匹配到多个学期，请选择一个。",
            candidates=candidates,
        ))

    course_matches: List[tuple[Dict[str, str], str]] = []
    for course in courses:
        for candidate in (course.get("name", ""), course.get("code", "")):
            candidate_lower = candidate.strip().casefold()
            if candidate_lower and _contains_term(lower, candidate_lower):
                course_matches.append((course, candidate_lower))
                break
    unique_course_ids = {item[0]["id"] for item in course_matches}
    if len(unique_course_ids) == 1:
        course, term = course_matches[0]
        filters.course_id = course["id"]
        remaining = _consume(remaining, term)
        matched.append("课程")
    elif len(unique_course_ids) > 1:
        ambiguities.append(HistoryQueryAmbiguity(
            fragment=text,
            message="匹配到多个课程，请选择一个。",
            candidates=[item[0].get("name", item[0]["id"]) for item in course_matches],
        ))

    found_tag_ids: List[str] = []
    for tag in tags:
        term = tag.get("name", "").strip().casefold()
        if term and _contains_term(lower, term):
            found_tag_ids.append(tag["id"])
            remaining = _consume(remaining, term)
    if found_tag_ids:
        filters.tag_ids = list(dict.fromkeys(found_tag_ids))
        matched.append("标签")

    if any(_contains_term(lower, term) for term in (
        "未完成", "没完成", "未批改完", "还没批完", "批改未完成",
        "unfinished", "incomplete",
    )):
        filters.unfinished = True
        for term in (
            "未完成", "没完成", "未批改完", "还没批完", "批改未完成",
            "unfinished", "incomplete",
        ):
            remaining = _consume(remaining, term)
        matched.append("未完成")

    if any(_contains_term(lower, term) for term in (
        "需要关注", "需关注", "需要处理", "待复核", "低置信度",
        "needs attention", "review needed", "low confidence",
    )):
        filters.needs_attention = True
        for term in (
            "需要关注", "需关注", "需要处理", "待复核", "低置信度",
            "needs attention", "review needed", "low confidence",
        ):
            remaining = _consume(remaining, term)
        matched.append("需要关注")

    statuses: List[TaskStatus] = []
    for status_name, terms in _STATUS_TERMS.items():
        for term in terms:
            if _contains_term(lower, term):
                statuses.append(status_name)  # type: ignore[arg-type]
                remaining = _consume(remaining, term)
                break
    if statuses:
        filters.statuses = list(dict.fromkeys(statuses))
        matched.append("状态")

    sort: Optional[HistorySort] = None
    sort_terms: List[tuple[HistorySort, tuple[str, ...]]] = [
        ("attention_first", ("优先处理", "关注优先", "attention first")),
        ("updated_asc", ("最早更新", "最久未更新", "oldest updated")),
        ("created_asc", ("最早创建", "最旧", "oldest created")),
        ("name_desc", ("名称倒序", "name descending")),
        ("name_asc", ("名称正序", "按名称", "name ascending")),
        ("stage_desc", ("阶段倒序", "状态倒序", "stage descending")),
        ("stage_asc", ("按阶段", "阶段排序", "状态排序", "stage ascending")),
        ("updated_desc", ("最近", "最新", "刚更新", "most recent", "latest")),
    ]
    for sort_name, terms in sort_terms:
        matched_term = next((term for term in terms if _contains_term(lower, term)), None)
        if matched_term:
            sort = sort_name
            remaining = _consume(remaining, matched_term)
            matched.append("排序")
            break

    for term in _GENERIC_TERMS:
        remaining = _consume(remaining, term)
    unresolved = re.sub(r"[\s,，。.!！?？:：;；、]+", " ", remaining).strip()
    # Once real structural conditions were recognised, discard a residue only
    # when the *entire* residue consists of connector particles and the generic
    # noun “任务”. This handles “还没批完的任务” without ever deleting “和/的”
    # from genuine keywords such as “和尚作业”.
    if matched and re.fullmatch(
        r"(?:(?:的|和|且|并且|以及)\s*)*(?:任务)?"
        r"(?:\s*(?:的|和|且|并且|以及))*",
        unresolved,
    ):
        unresolved = ""

    return {
        "filters": filters,
        "sort": sort,
        "explanation": (
            f"已确定性识别：{'、'.join(dict.fromkeys(matched))}。"
            if matched else "未识别到结构化条件。"
        ),
        "ambiguities": ambiguities,
        "unresolved": unresolved,
    }


_HISTORY_QUERY_SYSTEM = """You interpret a teacher's History-page filter.
Return only JSON matching the supplied schema. You may use only IDs present in
the candidate JSON. Never invent a course, tag, semester, status, or field.
Allowed statuses: draft, extracting_problems, problems_ready,
parsing_submissions, submissions_ready, grading, graded, review_confirmed,
generating_analysis, finalized, error.
Allowed sort values: updated_desc, updated_asc, created_desc, created_asc,
name_asc, name_desc, attention_first, stage_asc, stage_desc.
Use q only for a task-name/free-text term. If meaning is uncertain, leave that
filter unset and add an ambiguity. Keep explanation short and in the query's
language.
"""


async def interpret_history_query(
    query: str,
    *,
    semesters: List[str],
    courses: List[Dict[str, str]],
    tags: List[Dict[str, str]],
    provider: Optional[BaseProvider],
    reporter: ProgressReporter,
    owner_id: str = "anonymous",
) -> Dict[str, Any]:
    """Return a safe interpretation; LLM failure preserves deterministic data."""

    await reporter.set_phase("classifying")
    deterministic = deterministic_history_query(
        query, semesters=semesters, courses=courses, tags=tags,
    )
    await reporter._emit_message("HistoryQueryAgent: deterministic parse complete")

    filters: HistoryQueryFilters = deterministic["filters"]
    sort: Optional[HistorySort] = deterministic["sort"]
    ambiguities: List[HistoryQueryAmbiguity] = list(deterministic["ambiguities"])
    explanation = deterministic["explanation"]
    unresolved = deterministic["unresolved"]
    source: Literal["deterministic", "llm", "hybrid"] = "deterministic"

    llm_rate_limited = False
    llm_disabled = False
    if unresolved and provider is not None:
        if not settings.history_query_llm_enabled:
            provider = None
            llm_disabled = True
            await reporter._emit_message(
                "HistoryQueryAgent: model enhancement kill switch is off",
                "info",
            )
        else:
            now = time.monotonic()
            cooldown = max(0.0, float(settings.history_query_llm_cooldown_seconds))
            day_key = (owner_id, date.today().isoformat())
            daily_limit = max(0, int(settings.history_query_llm_daily_limit))
            with _llm_limit_lock:
                last = _last_llm_at.get(owner_id, 0.0)
                daily_used = _llm_daily_usage.get(day_key, 0)
                allowed = daily_limit > 0 and daily_used < daily_limit
                if allowed and now - last >= cooldown:
                    _llm_daily_usage[day_key] = daily_used + 1
                    _last_llm_at[owner_id] = now
                else:
                    provider = None
                    llm_rate_limited = True
            if llm_rate_limited:
                await reporter._emit_message(
                    "HistoryQueryAgent: per-owner cooldown/daily limit; using deterministic fallback",
                    "warn",
                )

    if unresolved and provider is not None:
        candidates = {
            "semesters": semesters,
            "courses": courses,
            "tags": tags,
        }
        user_prompt = json.dumps({
            "query": query,
            "unresolved_text": unresolved,
            "already_parsed": {
                "filters": filters.model_dump(exclude_none=True),
                "sort": sort,
            },
            "candidates": candidates,
        }, ensure_ascii=False)
        try:
            llm = await HistoryQuerySkill(provider).interpret(
                system_prompt=_HISTORY_QUERY_SYSTEM,
                user_prompt=user_prompt,
                output_model=HistoryLLMOutput,
                reporter=reporter,
            )
            valid_semesters = set(semesters)
            valid_courses = {item["id"] for item in courses}
            valid_tags = {item["id"] for item in tags}

            if filters.q is None and llm.filters.q:
                filters.q = llm.filters.q.strip() or None
            if filters.semester_id is None and llm.filters.semester_id:
                if llm.filters.semester_id in valid_semesters:
                    filters.semester_id = llm.filters.semester_id
                else:
                    ambiguities.append(HistoryQueryAmbiguity(
                        fragment=llm.filters.semester_id,
                        message="模型建议的学期不在可选范围内，已忽略。",
                    ))
            if filters.course_id is None and llm.filters.course_id:
                if llm.filters.course_id in valid_courses:
                    filters.course_id = llm.filters.course_id
                else:
                    ambiguities.append(HistoryQueryAmbiguity(
                        fragment=llm.filters.course_id,
                        message="模型建议的课程不属于当前用户，已忽略。",
                    ))
            invalid_tag_ids = [
                tag_id for tag_id in llm.filters.tag_ids
                if tag_id not in valid_tags
            ]
            filters.tag_ids = list(dict.fromkeys([
                *filters.tag_ids,
                *(tag_id for tag_id in llm.filters.tag_ids if tag_id in valid_tags),
            ]))
            if invalid_tag_ids:
                ambiguities.append(HistoryQueryAmbiguity(
                    fragment=", ".join(invalid_tag_ids)[:120],
                    message="模型建议的标签不属于当前用户，已忽略。",
                ))
            if not filters.statuses:
                filters.statuses = [
                    item for item in llm.filters.statuses if item in _STATUSES
                ]
            if filters.unfinished is None:
                filters.unfinished = llm.filters.unfinished
            if filters.needs_attention is None:
                filters.needs_attention = llm.filters.needs_attention
            if sort is None:
                sort = llm.sort
            allowed_candidates = {*semesters, *_STATUSES, *_SORTS}
            for course in courses:
                allowed_candidates.update(filter(None, (
                    course.get("id"), course.get("name"), course.get("code"),
                )))
            for tag in tags:
                allowed_candidates.update(filter(None, (
                    tag.get("id"), tag.get("name"),
                )))
            ambiguities.extend(
                HistoryQueryAmbiguity(
                    fragment=item.fragment,
                    message=item.message,
                    candidates=[
                        candidate for candidate in item.candidates
                        if candidate in allowed_candidates
                    ],
                )
                for item in llm.ambiguities
            )
            explanation += f" {llm.explanation}" if llm.explanation else ""
            source = "hybrid" if deterministic["explanation"].startswith("已") else "llm"
        except Exception:
            await reporter._emit_message(
                "HistoryQueryAgent: model unavailable; deterministic fallback retained",
                "warn",
            )

    if unresolved and provider is None:
        # Free-text search is always available without a model. Structured
        # conditions already parsed above remain active alongside it.
        filters.q = unresolved[:120]
        if llm_rate_limited:
            explanation += " 模型请求额度或冷却中，剩余文本按普通关键词搜索。"
        elif llm_disabled:
            explanation += " 模型增强当前关闭，剩余文本按普通关键词搜索。"
        else:
            explanation += " 未配置可安全共享的模型，剩余文本按普通关键词搜索。"
    elif unresolved and provider is not None and source == "deterministic" and filters.q is None:
        # Provider failure follows the same non-breaking fallback.
        filters.q = unresolved[:120]
        explanation += " 模型增强失败，剩余文本按普通关键词搜索。"

    await reporter.set_phase("done")
    return {
        "filters": filters.model_dump(exclude_none=True),
        "sort": sort,
        "explanation": explanation.strip(),
        "conditions": _build_conditions(
            filters, sort, courses=courses, tags=tags,
        ),
        "ambiguities": [item.model_dump() for item in ambiguities],
        "source": source,
    }
