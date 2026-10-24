"""Question-scoped grading plans, frozen before student/expert fan-out.

Only private run/checkpoint data carries these plans. Public correction DTOs
retain their existing shape. There is deliberately no process-wide plan cache.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

from pydantic import ValidationError

from backend.models import ProblemInfo

if TYPE_CHECKING:
    from backend.services.math_rubric_plan import MathRubricPlan

MATH_GRADING_ENGINE_VERSION = "math_evidence_v2"
ENGINE_EVENT = "grading_math_engine"
PLAN_EVENT = "grading_math_rubric_plan"
_current_plans: ContextVar[dict[str, "MathRubricPlan"] | None] = ContextVar(
    "math_grading_plans", default=None,
)


async def report_math_progress(operation, *args, **kwargs):
    """Keep a reporter/storage failure outside the model-fallback boundary."""
    from backend.services.grading_checkpoints import CheckpointWriteError
    from backend.domain.errors import DomainError
    from sqlalchemy.exc import SQLAlchemyError
    try:
        return await operation(*args, **kwargs)
    except (CheckpointWriteError, DomainError, SQLAlchemyError):
        raise
    except Exception as exc:
        raise CheckpointWriteError("math_progress_write_failed") from exc


def uses_math_grading(problem_type: str) -> bool:
    """Prepare only the resources needed by the saved type's grading strategy."""
    from backend.skills.base import get_skill_for_type
    from backend.skills.math_grading import MathGradingSkill

    try:
        return issubclass(get_skill_for_type(problem_type), MathGradingSkill)
    except ValueError:
        return False  # Missing/unsupported types are blocked by question review.


def is_math_problem(problem_or_type) -> bool:
    return uses_math_grading(
        problem_or_type if isinstance(problem_or_type, str) else problem_or_type.type
    )


def problem_from_question(question) -> ProblemInfo:
    """Use precisely the same content as the grading adapter."""
    problem = ProblemInfo(
        q_id=question.q_id, number=question.number or str(question.order_index + 1),
        type=question.type, stem=question.stem, criterion=question.criterion,
        reference_answer=question.reference_answer, test_cases=question.test_cases,
        max_score=question.max_score,
    )
    problem._math_rubric_source = question.source or {}
    return problem


@contextmanager
def math_plans_context(plans: dict[str, "MathRubricPlan"]):
    token = _current_plans.set({qid: plan.model_copy(deep=True) for qid, plan in plans.items()})
    try:
        yield
    finally:
        _current_plans.reset(token)


def get_math_plan(problem: ProblemInfo) -> "MathRubricPlan | None":
    from backend.services.math_rubric_plan import math_rubric_fingerprint

    plan = (_current_plans.get() or {}).get(problem.q_id)
    if (plan is None or plan.input_fingerprint != math_rubric_fingerprint(problem)
            or plan.max_score != problem.max_score):
        return None
    # A skill cannot silently change the rules seen by the next expert.
    return plan.model_copy(deep=True)


def frozen_engine_version(manifest: dict, events: list[dict]) -> str | None:
    version = manifest.get("math_grading_engine_version")
    for event in events:
        if event.get("message") == ENGINE_EVENT:
            version = (event.get("payload") or {}).get("version")
    return version


def plans_from_snapshots(manifest: dict, events: list[dict]) -> dict[str, "MathRubricPlan"]:
    """Restore actual plans; an old engine never supplies a new engine's rules."""
    from backend.services.math_rubric_plan import MathRubricPlan

    if frozen_engine_version(manifest, events) != MATH_GRADING_ENGINE_VERSION:
        return {}
    raw_plans = dict(manifest.get("math_rubric_plans") or {})
    for event in events:
        payload = event.get("payload") or {}
        if event.get("message") == PLAN_EVENT and payload.get("engine_version") == MATH_GRADING_ENGINE_VERSION:
            raw_plans[payload.get("q_id")] = payload.get("plan")
    plans = {}
    for qid, raw in raw_plans.items():
        try:
            plan = MathRubricPlan.model_validate(raw)
        except (ValidationError, TypeError):
            continue
        if isinstance(qid, str) and plan.schema_version == "math_rubric_v2":
            plans[qid] = plan
    return plans


def matching_math_plans(questions, plans: dict[str, "MathRubricPlan"]) -> dict[str, "MathRubricPlan"]:
    from backend.services.math_rubric_plan import math_rubric_fingerprint

    return {
        question.q_id: plans[question.q_id]
        for question in questions
        if uses_math_grading(question.type)
        and question.q_id in plans
        and plans[question.q_id].max_score == question.max_score
        and plans[question.q_id].input_fingerprint == math_rubric_fingerprint(problem_from_question(question))
    }


def expert_matches_plan(item: dict, math_qids: set[str], plans: dict[str, "MathRubricPlan"]) -> bool:
    if item.get("q_id") not in math_qids:
        return True
    plan = plans.get(item["q_id"])
    return bool(
        plan is not None
        and item.get("math_grading_engine_version") == MATH_GRADING_ENGINE_VERSION
        and item.get("math_plan_id") == plan.plan_id
    )


def _has_critical_plan_failure(error: BaseException, critical_types: tuple[type, ...]) -> bool:
    """Provider exception classification must not conceal a local write failure."""
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, critical_types):
            return True
        pending.extend(
            cause for cause in (current.__cause__, current.__context__)
            if isinstance(cause, BaseException)
        )
    return False


async def prepare_math_plans(problem_store, registry, *, grading_setup=None, reporter=None):
    """Prepare each question once, without receiving any student's answer.

    The adapter and direct batch entry point may both call this helper: an
    already frozen context is reused without a model call or checkpoint write.
    """
    from contextlib import nullcontext
    from backend.services.grading_checkpoints import current_checkpoints, CheckpointWriteError
    from backend.domain.errors import DomainError
    from sqlalchemy.exc import SQLAlchemyError
    from backend.services.math_rubric_plan import (
        fallback_math_rubric_plan, math_rubric_fingerprint,
    )
    from backend.progress.grading_activity import observe_auxiliary_request
    from backend.services.rubric_drafts import saved_draft, bind_draft, compile_draft, remember_draft

    checkpoints = current_checkpoints()
    plans = {}
    provider = None
    provider_selected = False
    for qid, raw in problem_store.items():
        async with checkpoints.math_plan_lock(qid) if checkpoints is not None else nullcontext():
            try:
                problem = raw if isinstance(raw, ProblemInfo) else ProblemInfo.model_validate(raw)
                if isinstance(raw, dict):
                    problem._math_rubric_source = raw.get("_math_rubric_source") or {}
                    if raw.get("rubric_draft"):
                        problem._math_rubric_source = {**problem._math_rubric_source, "presentation": {
                            **(problem._math_rubric_source.get("presentation") or {}),
                            "rubric_draft": raw["rubric_draft"],
                            "ai_completion_provenance": raw.get("ai_completion_provenance") or {},
                        }}
            except ValidationError:
                # Invalid question rows keep the existing grading input failure path.
                continue
            if not uses_math_grading(problem.type):
                continue
            plan = get_math_plan(problem)
            if plan is None and checkpoints is not None:
                plan = checkpoints.get_math_plan(problem)
            if plan is None:
                draft = saved_draft(problem, grading_setup=grading_setup)
                if draft is not None:
                    plan = bind_draft(draft, problem, grading_setup=grading_setup)
                    if checkpoints is not None:
                        checkpoints.save_math_plan(problem, plan)
            if plan is None:
                if not provider_selected:
                    getter = getattr(registry, "get", None)
                    picker = getattr(registry, "pick_default", None)
                    if grading_setup is not None and callable(getter):
                        provider = getter(grading_setup.primary_provider_id)
                    elif callable(picker):
                        provider = picker()
                    else:
                        available = getattr(registry, "list_available", lambda: [])()
                        provider = available[0] if available else None
                    provider_selected = True
                if reporter is not None:
                    from backend.models import ActiveUnit
                    await report_math_progress(reporter.substep,
                        ActiveUnit(student_id="", q_id=qid, skill="MathRubricPlan", expert="", step="starting"),
                        "prepare_grading_criteria",
                    )
                if provider is None or not callable(getattr(provider, "ainvoke", None)):
                    plan = fallback_math_rubric_plan(problem, grading_setup=grading_setup)
                else:
                    try:
                        async with observe_auxiliary_request(reporter, provider):
                            draft = await compile_draft(
                                problem, provider, grading_setup=grading_setup, reporter=reporter,
                            )
                        if draft is not None:
                            remember_draft(problem, draft,
                                question_id=raw.get("_math_question_id") if isinstance(raw, dict) else None,
                                expected_version=raw.get("_math_question_version") if isinstance(raw, dict) else None,
                                checkpoints=checkpoints)
                            if isinstance(raw, dict):
                                raw["_math_rubric_source"] = problem._math_rubric_source
                            plan = bind_draft(draft, problem, grading_setup=grading_setup)
                        else:
                            plan = fallback_math_rubric_plan(problem, grading_setup=grading_setup)
                    except (CheckpointWriteError, DomainError, SQLAlchemyError):
                        raise
                    except Exception as exc:
                        # Cancellation derives from BaseException and is not caught.
                        if _has_critical_plan_failure(
                            exc, (CheckpointWriteError, DomainError, SQLAlchemyError),
                        ):
                            raise
                        # A failed interpretation retains the original rubric.
                        plan = fallback_math_rubric_plan(problem, grading_setup=grading_setup)
                if (plan.input_fingerprint != math_rubric_fingerprint(problem)
                        or plan.max_score != problem.max_score):
                    plan = fallback_math_rubric_plan(problem, grading_setup=grading_setup)
                if checkpoints is not None:
                    checkpoints.save_math_plan(problem, plan)
            plans[qid] = plan
    return plans


def ensure_run_math_version(*, run_id, worker_id, questions, frozen_setup):
    """Fence old paid work before a worker imports or skips any result rows.

    An untouched queued run can adopt the new engine. An older run with
    mathematical results must use the existing new-run retry path because AI
    result rows are immutable; overwriting them would destroy its history.
    """
    from backend.db import grading_repository as repository
    from backend.domain.errors import VersionConflict

    math_qids = {q.q_id for q in questions if uses_math_grading(q.type)}
    if not math_qids:
        return
    manifest = (frozen_setup.input_manifest if frozen_setup else {}) or {}
    events = repository.list_events(run_id)
    version = frozen_engine_version(manifest, events)
    plans = matching_math_plans(questions, plans_from_snapshots(manifest, events))
    completed = [
        row for row in repository.list_results_for_run(run_id) if row.q_id in math_qids
    ]
    inherited = [row for row in manifest.get("reused_results", []) if row.get("q_id") in math_qids]
    if version == MATH_GRADING_ENGINE_VERSION:
        if any(row.q_id not in plans for row in completed) or any(row["q_id"] not in plans for row in inherited):
            raise VersionConflict("Frozen grading plan is missing; start a new run.", code="grading_inputs_changed")
        return
    old_votes = [
        item for item in manifest.get("reused_experts", []) if item.get("q_id") in math_qids
    ]
    old_votes.extend(
        event.get("payload") or {} for event in events
        if event.get("message") == "grading_expert_checkpoint"
        and (event.get("payload") or {}).get("q_id") in math_qids
    )
    if completed or inherited or old_votes:
        raise VersionConflict("Grading engine changed; start a new run.", code="grading_inputs_changed")
    repository.record_event(
        run_id, worker_id=worker_id, level="info", message=ENGINE_EVENT,
        payload={"version": MATH_GRADING_ENGINE_VERSION},
    )
