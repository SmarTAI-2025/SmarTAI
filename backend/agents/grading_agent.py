"""
GradingAgent: the top-level entry point for grading a student's submission.

Responsibilities:
  - Iterate over a student's answers
  - Dispatch each answer to the right skill (via MultiExpertAgent)
  - Run concurrently across questions
  - Report progress through ProgressReporter

This replaces the giant if/elif dispatch in backend/routers/ai_grading.py.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, Any, List, Optional, TYPE_CHECKING

from backend.models import Correction, ProblemInfo, StudentAnswerInfo, TaskGradingSetup
from backend.llm.registry import ExpertRegistry
from backend.agents.multi_expert import run_multi_expert, AllExpertsFailed
from backend.skills.base import format_deterministic_feedback

if TYPE_CHECKING:
    from backend.progress.tracker import ProgressReporter

# Import all skills to trigger their @register_skill registrations
from backend.skills import concept, calculation, proof, programming  # noqa: F401

logger = logging.getLogger(__name__)


def _apply_low_confidence_policy(
    correction: Correction,
    grading_setup: Optional[TaskGradingSetup],
) -> Correction:
    """Apply the teacher's deterministic review threshold after model work."""

    if (
        grading_setup is not None
        and correction.confidence < grading_setup.low_confidence_threshold
    ):
        correction.requires_human_review = True
        if "low_confidence" not in correction.review_reasons:
            correction.review_reasons.append("low_confidence")
    return correction


def _grading_failure_feedback(
    kind: str,
    grading_setup: Optional[TaskGradingSetup],
) -> str:
    """Return a stable, C-01-aware message for a deterministic failure path."""

    messages = {
        "quota_exhausted": {
            "zh_message": "该题因所有 AI 专家达到 API 调用配额而暂未批改。",
            "en_message": "This item was not graded because all AI experts reached their API quota.",
            "zh_detail": "当前模型的每分钟调用额度已耗尽。",
            "en_detail": "The active models exhausted their per-minute request allowance.",
            "zh_suggestion": "请稍候重试，或在模型设置中检查该专家的 RPM 与并发限制。",
            "en_suggestion": "Retry later, or check this expert's RPM and concurrency limits in model settings.",
            "legacy_message": (
                "⏳ 该题暂未批改完成 — 所有 AI 专家都遇到了 API 每分钟调用配额上限。\n"
                "请稍候片刻后在「批改」页重试，或在 BYOK 设置里把该专家的 "
                "RPM / max_concurrent 调高（免费档常见为 15 RPM）。"
            ),
        },
        "transient_llm": {
            "zh_message": "该题因所有 AI 专家均发生网络或超时错误而暂未批改。",
            "en_message": "This item was not graded because every AI expert encountered a network or timeout error.",
            "zh_detail": "本次失败未产生有效评分。",
            "en_detail": "This attempt did not produce a valid score.",
            "zh_suggestion": "请稍后重试；如反复出现，请检查代理或网络配置。",
            "en_suggestion": "Retry later; if the issue persists, check the proxy and network configuration.",
            "legacy_message": (
                "🌐 该题暂未批改完成 — 所有 AI 专家都出现了网络或超时错误。\n"
                "请稍后重试；如反复出现，请检查代理 / 网络配置。"
            ),
        },
        "parse_failed": {
            "zh_message": "该题因所有 AI 专家的返回内容均无法解析而暂未批改。",
            "en_message": "This item was not graded because none of the AI expert responses could be parsed.",
            "zh_detail": "本次失败未产生有效评分。",
            "en_detail": "This attempt did not produce a valid score.",
            "zh_suggestion": "请稍后重试，或在模型设置中更换更稳定的模型。",
            "en_suggestion": "Retry later, or choose a more reliable model in model settings.",
            "legacy_message": (
                "⚠ 该题暂未批改完成 — 所有 AI 专家返回的内容均无法解析。\n"
                "请稍后重试，或在 BYOK 设置里更换一个更稳定的模型。"
            ),
        },
        "general": {
            "zh_message": "所有 AI 专家均未能完成该题批改。",
            "en_message": "None of the AI experts could complete grading for this item.",
            "zh_detail": "本次失败未产生有效评分。",
            "en_detail": "This attempt did not produce a valid score.",
            "zh_suggestion": "请检查模型配置后重新批改。",
            "en_suggestion": "Check the model configuration and grade this item again.",
            "legacy_message": "⚠ 所有 AI 专家批改失败 — 请检查 BYOK 配置后重新批改。",
        },
        "unknown": {
            "zh_message": "该题批改时发生未知错误。",
            "en_message": "An unexpected error occurred while grading this item.",
            "zh_detail": "本次失败未产生有效评分。",
            "en_detail": "This attempt did not produce a valid score.",
            "zh_suggestion": "请稍后重试；如反复出现，请检查模型配置。",
            "en_suggestion": "Retry later; if the issue persists, check the model configuration.",
            "legacy_message": "⚠ 该题批改时发生未知错误，请稍后重试。",
        },
    }
    return format_deterministic_feedback(
        grading_setup,
        **messages.get(kind, messages["unknown"]),
    )


async def grade_student(
    *,
    student_data: Dict[str, Any],
    problem_store: Dict[str, Dict[str, Any]],
    registry: ExpertRegistry,
    reporter: Optional["ProgressReporter"] = None,
    language: str = "en",
    task_id: Optional[str] = None,
    multi_sample_n: Optional[int] = None,
    aggregation_method: Optional[str] = None,
    grading_setup: Optional[TaskGradingSetup] = None,
) -> Dict[str, Any]:
    """
    Grade all answers from a single student submission.

    Args:
        student_data: {"stu_id": ..., "stu_name": ..., "stu_ans": [...]}
        problem_store: Keyed by q_id, values are ProblemInfo-compatible dicts.
        registry: ExpertRegistry with at least one provider.
        reporter: Optional progress reporter.
        task_id: Optional task scope for KB retrieval. Threaded down to skills.
        multi_sample_n: Per-call override for `settings.multi_sample_n`. None
            (default) → use the global setting. Threaded down to
            `run_multi_expert` so a teacher can opt into multi-sampling on a
            single important task without changing the global default. Ignored
            when ≥ 2 providers are configured (variance comes from the experts
            themselves).

    Returns:
        {
            "student_id": str,
            "student_name": str,
            "corrections": List[Correction],
        }
    """
    student_id = student_data.get("stu_id", "")
    student_name = student_data.get("stu_name", f"Student {student_id}")
    answers_raw = student_data.get("stu_ans", [])

    if not student_id:
        raise ValueError("student_data missing stu_id")

    logger.info(f"grade_student: {student_id} ({student_name}), {len(answers_raw)} answers")

    # Build problem/answer objects
    tasks = []
    for ans_raw in answers_raw:
        q_id = ans_raw.get("q_id")
        problem_raw = problem_store.get(q_id)
        if problem_raw is None:
            logger.warning("Question missing from grading input; q_id=%s", q_id)
            continue

        try:
            problem = ProblemInfo(**problem_raw)
        except Exception as e:
            logger.error(
                "Invalid problem data for q_id=%s; exception_type=%s",
                q_id,
                type(e).__name__,
            )
            continue

        try:
            answer = StudentAnswerInfo(
                q_id=ans_raw.get("q_id", ""),
                number=ans_raw.get("number", ""),
                type=ans_raw.get("type", problem.type),
                content=ans_raw.get("content", ""),
                flag=ans_raw.get("flag", []),
            )
        except Exception as e:
            logger.error(
                "Invalid answer data for q_id=%s; exception_type=%s",
                q_id,
                type(e).__name__,
            )
            continue

        tasks.append(_grade_single_answer(
            problem=problem,
            answer=answer,
            student_id=student_id,
            registry=registry,
            reporter=reporter,
            language=language,
            task_id=task_id,
            multi_sample_n=multi_sample_n,
            aggregation_method=aggregation_method,
            grading_setup=grading_setup,
        ))

    # Run all questions for this student concurrently
    corrections = await asyncio.gather(*tasks, return_exceptions=True)

    final_corrections: List[Correction] = []
    for c in corrections:
        if isinstance(c, Exception):
            logger.error(
                "Per-question grading task failed; exception_type=%s",
                type(c).__name__,
            )
            continue
        final_corrections.append(c)

    return {
        "student_id": student_id,
        "student_name": student_name,
        "corrections": final_corrections,
        "student_answers": answers_raw,
    }


async def _grade_single_answer(
    *,
    problem: ProblemInfo,
    answer: StudentAnswerInfo,
    student_id: str,
    registry: ExpertRegistry,
    reporter: Optional["ProgressReporter"] = None,
    language: str = "en",
    task_id: Optional[str] = None,
    multi_sample_n: Optional[int] = None,
    aggregation_method: Optional[str] = None,
    grading_setup: Optional[TaskGradingSetup] = None,
) -> Correction:
    """Grade a single (problem, answer) pair. Wraps MultiExpertAgent."""
    try:
        t0 = time.perf_counter()
        correction = await run_multi_expert(
            problem=problem,
            answer=answer,
            student_id=student_id,
            registry=registry,
            reporter=reporter,
            language=language,
            task_id=task_id,
            multi_sample_n=multi_sample_n,
            aggregation_method=aggregation_method,
            grading_setup=grading_setup,
        )
        duration = (time.perf_counter() - t0) * 1000
        logger.info(
            f"Graded {student_id}/{problem.q_id} [{problem.type}] "
            f"score={correction.score}/{correction.max_score} "
            f"confidence={correction.confidence:.2f} in {duration:.0f}ms"
        )
        if reporter:
            await reporter.increment_completed()
        return _apply_low_confidence_policy(correction, grading_setup)
    except AllExpertsFailed as e:
        # Every expert returned a blank/failed result. Produce a Correction
        # with policy-aware fallback feedback + synthesis_method that the frontend
        # can render distinctly. We deliberately do NOT splice the raw English
        # error text (e.g. "Quota exceeded for metric: …") into the comment —
        # teachers should see actionable guidance, not stack traces. The raw
        # per-expert reasons remain in `expert_results` for ops triage.
        logger.error(
            "All experts failed for grading item; dominant_kind=%s",
            e.dominant_kind,
        )
        if e.dominant_kind == "quota_exhausted":
            synthesis_method = "quota_exhausted"
        elif e.dominant_kind == "transient_llm":
            synthesis_method = "all_failed"
        elif e.dominant_kind == "parse_failed":
            synthesis_method = "all_failed"
        else:
            synthesis_method = "all_failed"
        comment = _grading_failure_feedback(e.dominant_kind, grading_setup)
        if reporter:
            await reporter.increment_completed()
        return _apply_low_confidence_policy(Correction(
            q_id=problem.q_id,
            type=problem.type,
            score=0.0,
            max_score=problem.max_score,
            confidence=0.0,
            comment=comment,
            steps=[],
            expert_results=e.failures,
            synthesis_method=synthesis_method,
        ), grading_setup)
    except Exception as e:
        logger.error(
            "Error grading student/q_id; exception_type=%s",
            type(e).__name__,
        )
        # Return a zero-score Correction so the batch doesn't silently drop.
        # Keep the comment friendly — raw stack traces don't belong in a batch.
        return _apply_low_confidence_policy(Correction(
            q_id=problem.q_id,
            type=problem.type,
            score=0.0,
            max_score=problem.max_score,
            confidence=0.0,
            comment=_grading_failure_feedback("unknown", grading_setup),
            steps=[],
            synthesis_method="all_failed",
        ), grading_setup)


async def grade_batch(
    *,
    student_store: Dict[str, Dict[str, Any]],
    problem_store: Dict[str, Dict[str, Any]],
    registry: ExpertRegistry,
    reporter: Optional["ProgressReporter"] = None,
    language: str = "en",
    task_id: Optional[str] = None,
    multi_sample_n: Optional[int] = None,
    aggregation_method: Optional[str] = None,
    grading_setup: Optional[TaskGradingSetup] = None,
) -> List[Dict[str, Any]]:
    """
    Grade all students in student_store concurrently.
    Returns a list of per-student results.

    `task_id` flows down to each skill so KB retrieval can scope to the
    current task. Pass None for ad-hoc / legacy calls.

    `multi_sample_n` is a per-call override for `settings.multi_sample_n` (the
    number of independent samples drawn in single-provider mode). Threaded
    through grade_student → _grade_single_answer → run_multi_expert. None means
    "use the global setting". Ignored when ≥ 2 providers are configured.
    """
    if reporter:
        await reporter.set_phase("grading")
        await reporter.set_totals(
            students=len(student_store),
            questions=len(problem_store),
        )

    tasks = [
        grade_student(
            student_data=sd,
            problem_store=problem_store,
            registry=registry,
            reporter=reporter,
            language=language,
            task_id=task_id,
            multi_sample_n=multi_sample_n,
            aggregation_method=aggregation_method,
            grading_setup=grading_setup,
        )
        for sd in student_store.values()
        if sd.get("stu_id")
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)
    final: List[Dict[str, Any]] = []
    for r in results:
        if isinstance(r, Exception):
            logger.error(
                "Student grading task failed; exception_type=%s",
                type(r).__name__,
            )
            continue
        final.append(r)

    if reporter:
        await reporter.set_phase("done")

    return final
