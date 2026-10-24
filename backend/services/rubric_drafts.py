"""Persist semantic rubric work once; bind numerical preferences without an LLM.

Draft identity deliberately excludes display settings, experts and question IDs.
Question IDs can change when incremental packages are assembled; the frozen plan
still uses the final question identity. Teacher text remains the source of truth.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.services.math_rubric_plan import (
    MathRubricPlan, MathRubricRoute, _Explanation, _build_plan, _content_hash,
    _mapping, _question_input, _validated_explanation, _with_preferences,
)


class RubricDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    routes: tuple[MathRubricRoute, ...] = Field(min_length=1, max_length=16)
    follow_through: str = Field(min_length=1, max_length=20000)
    numerical_policy: str = Field(min_length=1, max_length=20000)
    advisories: tuple[str, ...] = Field(default=(), max_length=20)


class RubricDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["rubric_draft_v1"] = "rubric_draft_v1"
    content_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    teacher_notes: str = ""
    # Unbound means no strictness/partial-credit prose has been appended.
    plan: MathRubricPlan


def draft_fingerprint(problem) -> str:
    context = _question_input(problem)
    # teacher_subpart_points is a generation-only hint; the accepted allocations
    # are already captured in the criterion and question maximum.
    return _content_hash({k: v for k, v in context.items()
                          if k not in {"q_id", "number", "teacher_subpart_points"}})


def render_rubric(definition: RubricDefinition, *, language="zh") -> str:
    """One deterministic view of the same scoring data used by the grader."""
    zh = language == "zh"
    lines = []
    for route in definition.routes:
        lines.append(f"{route.description} — {route.applies_when}")
        for item in route.items:
            amount = f"{item.points:g}"
            points = (f"扣{amount}分" if route.mode == "deductions" else f"{amount}分") if zh else (
                f"deduct {amount} points" if route.mode == "deductions" else f"{amount} points")
            limit = ""
            if route.mode == "deductions":
                limit = ("；不限次数" if item.max_occurrences is None else f"；最多{item.max_occurrences}次") if zh else (
                    "; unlimited occurrences" if item.max_occurrences is None else f"; at most {item.max_occurrences} times")
            adjustment = ""
            if item.default_adjustments:
                adjustment = "；未指定政策按批改严格度调整" if zh else "; unspecified policies follow grading strictness"
            lines.append(f"- {item.description}：{points}{limit}{adjustment}")
        if route.minimum_score:
            condition = ("非空作答" if route.minimum_condition == "nonblank" else "") if zh else (
                "For nonblank answers, " if route.minimum_condition == "nonblank" else "")
            lines.append(f"{condition}最低{route.minimum_score:g}分" if zh else f"{condition}minimum {route.minimum_score:g} points")
        lines.extend(rule.description for rule in route.exemptions)
        if route.error_policy_source_quote:
            lines.append(route.error_policy_source_quote)
    lines.extend((definition.follow_through, definition.numerical_policy))
    return "\n".join(lines)


def generated_rubric(problem: dict, definition: RubricDefinition, *, language="zh"):
    """Validate against original teacher rules BEFORE rendering AI additions."""
    original = str(problem.get("criterion") or "")
    explanation = _validated_explanation(_question_input(problem), _Explanation(
        **definition.model_dump(), effective_criterion=(
            "按以下结构化评分项执行；页面标准与此底稿一致。" if language == "zh" else
            "Apply these structured routes; the displayed rubric uses the same definition."
        ),
    ))
    # Existing teacher material is adopted, never replaced with a generated view.
    canonical_definition = RubricDefinition.model_validate(explanation.model_dump(exclude={"effective_criterion"}))
    criterion = original or render_rubric(canonical_definition, language=language)
    completed = {**problem, "criterion": criterion}
    if not original:
        completed["ai_completion_provenance"] = {
            **(problem.get("ai_completion_provenance") or {}), "criterion": {"source_kind": "ai_generated"},
        }
    holistic = all(route.mode == "holistic" for route in explanation.routes)
    plan = _build_plan(_question_input(completed), explanation,
        status="holistic" if holistic else "compiled",
        reason="model_selected_holistic" if holistic else "")
    return criterion, RubricDraft(content_fingerprint=draft_fingerprint(completed), plan=plan)


def retarget_draft(draft: RubricDraft, problem) -> RubricDraft:
    """Seal a generated candidate after sibling fields in its response are known."""
    payload = draft.plan.model_dump(mode="json", exclude={"plan_id"})
    payload["input_fingerprint"] = _content_hash(_question_input(problem))
    plan = MathRubricPlan(plan_id=_content_hash(payload), **payload)
    return draft.model_copy(update={"content_fingerprint": draft_fingerprint(problem), "plan": plan})


def saved_draft(problem, *, grading_setup=None) -> RubricDraft | None:
    row = _mapping(problem)
    source = row.get("source") or getattr(problem, "_math_rubric_source", {}) or {}
    presentation = source.get("presentation") or {} if isinstance(source, Mapping) else {}
    if not isinstance(presentation, Mapping):
        return None
    value = row.get("rubric_draft") or presentation.get("rubric_draft")
    if not value:
        return None
    try:
        draft = RubricDraft.model_validate(value)
    except (ValueError, TypeError):
        return None
    setup = _mapping(grading_setup) if grading_setup is not None else {}
    context = _question_input(problem)
    if (draft.content_fingerprint != draft_fingerprint(problem)
            or draft.teacher_notes != str(setup.get("teacher_notes") or "")
            or draft.plan.original_criterion != context["criterion"]
            or draft.plan.max_score != context["max_score"]):
        return None
    return draft


def bind_draft(draft: RubricDraft, problem, *, grading_setup=None) -> MathRubricPlan:
    if draft.content_fingerprint != draft_fingerprint(problem):
        raise ValueError("rubric_draft_content_changed")
    setup = _mapping(grading_setup) if grading_setup is not None else {}
    if draft.teacher_notes != str(setup.get("teacher_notes") or ""):
        raise ValueError("rubric_draft_teacher_policy_changed")
    plan = retarget_draft(draft, problem).plan
    return _with_preferences(plan, grading_setup)


async def compile_draft(problem, provider, *, grading_setup=None, reporter=None):
    """Only missing/stale semantic content reaches the bounded compiler."""
    from backend.services.math_rubric_plan import prepare_math_rubric_plan

    setup = dict(_mapping(grading_setup)) if grading_setup is not None else {}
    # Core obligations and weights must not depend on a numerical slider.
    semantic_setup = {**setup, "strictness": 50, "allow_partial_credit": True}
    plan = await prepare_math_rubric_plan(problem, provider, grading_setup=semantic_setup,
                                         reporter=reporter, bind_preferences=False)
    return RubricDraft(content_fingerprint=draft_fingerprint(problem),
                       teacher_notes=str(setup.get("teacher_notes") or ""), plan=plan)


def remember_draft(problem, draft: RubricDraft, *, question_id=None, expected_version=None, checkpoints=None):
    """Cache metadata without changing teacher content or its version.

    Persist only if the actual question still has the content that was compiled.
    A stale operation cannot overwrite a later teacher edit or another question.
    """
    from copy import deepcopy

    from backend.services.math_rubric_plan import _UNREPRESENTABLE_RULES

    if (draft.plan.compilation_status == "holistic"
            and draft.plan.compilation_reason not in {*_UNREPRESENTABLE_RULES, "model_selected_holistic"}):
        # Freeze a safe fallback for this run, but a transient malformed answer
        # must not become a permanent cache hit on the next explicit AI action.
        return
    value = draft.model_dump(mode="json")
    if question_id is not None and checkpoints is not None:
        import time
        from sqlalchemy import select, update
        from backend.db import grading_repository
        from backend.db.models import AssignmentQuestionRecord, GradingRunRecord
        from backend.db.session import session_scope
        from backend.domain.errors import LeaseLost
        from backend.services.math_grading_context import problem_from_question

        with session_scope() as session:
            run = session.scalar(select(GradingRunRecord).where(
                GradingRunRecord.id == checkpoints.run_id,
            ).with_for_update())
            if (run is None or run.status != "running" or run.lease_owner != checkpoints.worker_id
                    or (run.lease_expiry or 0) <= time.time() or run.released_at is not None
                    or grading_repository._active_assignment_for_run(session, run.id,
                        actor_id=run.teacher_id, lock=True) is None):
                raise LeaseLost("rubric_draft_write_lease_lost")
            record = session.scalar(select(AssignmentQuestionRecord).where(
                AssignmentQuestionRecord.id == question_id,
                AssignmentQuestionRecord.assignment_id == run.assignment_id,
            ).with_for_update())
            if (record is None or record.version != expected_version
                    or draft_fingerprint(problem_from_question(record)) != draft.content_fingerprint):
                return
            source = deepcopy(record.source or {})
            source.setdefault("presentation", {})["rubric_draft"] = value
            # ORM onupdate timestamps represent teacher content edits. Cache
            # metadata must preserve that time as well as the content version.
            session.execute(update(AssignmentQuestionRecord).where(
                AssignmentQuestionRecord.id == record.id,
            ).values(source=source, updated_at=record.updated_at))
    source = deepcopy(problem._math_rubric_source)
    source.setdefault("presentation", {})["rubric_draft"] = value
    problem._math_rubric_source = source
