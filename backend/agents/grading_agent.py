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

from backend.models import Correction, ProblemInfo, StudentAnswerInfo
from backend.llm.registry import ExpertRegistry
from backend.agents.multi_expert import run_multi_expert, AllExpertsFailed

if TYPE_CHECKING:
    from backend.progress.tracker import ProgressReporter

# Import all skills to trigger their @register_skill registrations
from backend.skills import concept, calculation, proof, programming  # noqa: F401

logger = logging.getLogger(__name__)


async def grade_student(
    *,
    student_data: Dict[str, Any],
    problem_store: Dict[str, Dict[str, Any]],
    registry: ExpertRegistry,
    reporter: Optional["ProgressReporter"] = None,
    language: str = "en",
    task_id: Optional[str] = None,
    multi_sample_n: Optional[int] = None,
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
            logger.warning(f"Problem {q_id} not in problem_store, skipping")
            continue

        try:
            problem = ProblemInfo(**problem_raw)
        except Exception as e:
            logger.error(f"Invalid problem data for {q_id}: {e}")
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
            logger.error(f"Invalid answer data for {q_id}: {e}")
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
        ))

    # Run all questions for this student concurrently
    corrections = await asyncio.gather(*tasks, return_exceptions=True)

    final_corrections: List[Correction] = []
    for c in corrections:
        if isinstance(c, Exception):
            logger.error(f"Grading task for {student_id} raised: {c}")
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
        )
        duration = (time.perf_counter() - t0) * 1000
        logger.info(
            f"Graded {student_id}/{problem.q_id} [{problem.type}] "
            f"score={correction.score}/{correction.max_score} "
            f"confidence={correction.confidence:.2f} in {duration:.0f}ms"
        )
        if reporter:
            await reporter.increment_completed()
        return correction
    except AllExpertsFailed as e:
        # Every expert returned a blank/failed result. Produce a Correction
        # with synthesis_method='all_failed' so the frontend can render an
        # explicit error state and the teacher knows to re-run / change
        # provider rather than seeing a 0-score with cryptic mixed text.
        logger.error(f"All experts failed for {student_id}/{problem.q_id}: {e}")
        failure_lines = [
            f"- {er.provider}: {(er.comment or 'unknown error').strip()[:240]}"
            for er in e.failures
        ]
        comment = (
            "⚠ 所有 AI 专家批改失败 — 请检查 BYOK 配额或更换专家后重新批改：\n"
            + "\n".join(failure_lines)
        )
        if reporter:
            await reporter.increment_completed()
        return Correction(
            q_id=problem.q_id,
            type=problem.type,
            score=0.0,
            max_score=10.0,
            confidence=0.0,
            comment=comment,
            steps=[],
            expert_results=e.failures,
            synthesis_method="all_failed",
        )
    except Exception as e:
        logger.exception(f"Error grading {student_id}/{problem.q_id}")
        # Return a zero-score Correction so the batch doesn't silently drop
        return Correction(
            q_id=problem.q_id,
            type=problem.type,
            score=0.0,
            max_score=10.0,
            confidence=0.0,
            comment=f"Grading error: {e}",
            steps=[],
            synthesis_method="all_failed",
        )


async def grade_batch(
    *,
    student_store: Dict[str, Dict[str, Any]],
    problem_store: Dict[str, Dict[str, Any]],
    registry: ExpertRegistry,
    reporter: Optional["ProgressReporter"] = None,
    language: str = "en",
    task_id: Optional[str] = None,
    multi_sample_n: Optional[int] = None,
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
        )
        for sd in student_store.values()
        if sd.get("stu_id")
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)
    final: List[Dict[str, Any]] = []
    for r in results:
        if isinstance(r, Exception):
            logger.error(f"grade_batch task failed: {r}")
            continue
        final.append(r)

    if reporter:
        await reporter.set_phase("done")

    return final
