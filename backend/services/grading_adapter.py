"""Adapter between the normalized education domain and the existing grading
algorithm (``grade_batch``).

This module only *translates* shapes — it never changes prompts, skills, or
scoring. It builds the ``problem_store`` / ``student_store`` dicts that
``grade_batch`` expects from normalized question and revision rows, supplies
the frozen grading-run knowledge scope through the existing ``task_id``
parameter, and maps each returned
``Correction`` into a ``GradeResultDTO``.

Failure semantics (no publishable real zero):
* ``confidence == 0`` or ``synthesis_method in {all_failed, quota_exhausted}``
  → ``needs_review`` (the teacher must look; it is not a real 0);
* a missing correction for a submitted question → ``needs_review``;
* an exception raised by ``grade_batch`` → the whole run fails, not silent zeros.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.agents.grading_agent import grade_batch
from backend.domain import education
from backend.domain.errors import DomainError
from backend.models import Correction, TaskGradingSetup
from backend.config import settings


# synthesis_method values produced by the algorithm when every expert failed.
_ALL_FAILED_METHODS = frozenset({"all_failed", "quota_exhausted", "degraded_to_single"})


@dataclass
class AdapterOutcome:
    """One per-student batch outcome, ready to persist as GradeResult rows."""
    student_id: str
    results: list[education.GradeResultDTO]


def build_problem_store(questions: list[education.QuestionDTO]) -> dict[str, dict[str, Any]]:
    """Map normalized questions to the ProblemInfo-compatible dict keyed by q_id.

    test_cases / reference_answer are forwarded unchanged; the algorithm decides
    whether to generate missing ones. We do not synthesize or alter them here.
    """
    store: dict[str, dict[str, Any]] = {}
    for q in questions:
        store[q.q_id] = {
            "q_id": q.q_id,
            "number": q.number or str(q.order_index + 1),
            "type": q.type,
            "stem": q.stem,
            "criterion": q.criterion,
            "reference_answer": q.reference_answer,
            "test_cases": q.test_cases,
            "max_score": q.max_score,
        }
    return store


def build_student_store(
    frozen_revisions: list[tuple[education.SubmissionRevisionDTO, str]],
) -> dict[str, dict[str, Any]]:
    """Build the student_store dict from frozen (revision, student_id) pairs.

    Each student's answers are pulled from the *frozen* revision captured at run
    creation, so a regrade of a later revision cannot retroactively change what
    this run grades.
    """
    store: dict[str, dict[str, Any]] = {}
    for revision, student_id in frozen_revisions:
        stu_ans = [
            {"q_id": a.q_id, "number": a.number, "type": a.type,
             "content": a.content, "flag": list(a.flag or [])}
            for a in revision.answers
        ]
        store[student_id] = {
            "stu_id": student_id,
            "stu_name": student_id,
            "stu_ans": stu_ans,
        }
    return store


def correction_to_result(
    *, run_id: str, revision_id: str, question: education.QuestionDTO,
    student_id: str, correction: Correction | None,
) -> education.GradeResultDTO:
    """Map a single Correction (or its absence) to a GradeResultDTO.

    A real graded answer keeps its score. A confidence-0 / all-failed / missing
    correction becomes ``needs_review`` rather than a publishable zero, so the
    teacher review queue — never the released score — receives it.
    """
    q_id = question.q_id
    if correction is None:
        return education.GradeResultDTO(
            id="", grading_run_id=run_id, submission_revision_id=revision_id,
            question_id=question.id, student_id=student_id, q_id=q_id,
            ai_score=None, ai_max_score=question.max_score, ai_comment="",
            ai_steps=[], ai_confidence=None, ai_expert_results=[],
            ai_synthesis_method=None, requires_review=True,
            review_reason="missing_correction", result_status=education.GradeResultStatus.NEEDS_REVIEW.value,
            created_at=0, updated_at=0,
        )

    method = correction.synthesis_method
    failed = (
        correction.confidence == 0
        or method in _ALL_FAILED_METHODS
        or correction.requires_human_review
    )
    result_status = (
        education.GradeResultStatus.NEEDS_REVIEW.value if failed
        else education.GradeResultStatus.GRADED.value
    )
    return education.GradeResultDTO(
        id="", grading_run_id=run_id, submission_revision_id=revision_id,
        question_id=question.id, student_id=student_id, q_id=q_id,
        ai_score=None if failed else correction.score,
        ai_max_score=question.max_score,
        ai_comment=correction.comment or "",
        ai_steps=[s.model_dump() for s in correction.steps],
        ai_confidence=correction.confidence,
        ai_expert_results=[e.model_dump() for e in correction.expert_results],
        ai_synthesis_method=method,
        requires_review=failed,
        review_reason=(
            ",".join(correction.review_reasons or [])
            if correction.requires_human_review and correction.review_reasons
            else ("llm_failed" if failed else None)
        ),
        result_status=result_status,
        created_at=0, updated_at=0,
    )


def normalize_results(
    *, assignment_id: str, run_id: str, student_ids: list[str],
    questions: list[education.QuestionDTO],
    frozen_revisions: dict[str, education.SubmissionRevisionDTO | None],
    corrections: dict[str, list[Correction]],
) -> list[education.GradeResultDTO]:
    """Pure shape mapping from per-student Corrections to GradeResultDTOs.

    This is the testable, LLM-free core of the adapter: given the corrections a
    (real or fake) grading pass produced, build the normalized result rows. The
    live ``run_grading`` wraps ``grade_batch`` and feeds its output here; tests
    call this directly with a fake correction set so the lifecycle is
    deterministic without touching the algorithm.
    """
    q_by_id = {q.q_id: q for q in questions}
    out: list[education.GradeResultDTO] = []
    for student_id in student_ids:
        revision = frozen_revisions.get(student_id)
        if revision is None:
            continue
        student_corrections = {c.q_id: c for c in corrections.get(student_id, [])}
        for q in questions:
            out.append(correction_to_result(
                run_id=run_id, revision_id=revision.id, question=q,
                student_id=student_id, correction=student_corrections.get(q.q_id),
            ))
    return out


async def run_grading(
    *, run_id: str, assignment_id: str, teacher_id: str,
    questions: list[education.QuestionDTO],
    frozen_revisions: list[tuple[education.SubmissionRevisionDTO, str]],
    registry, language: str = "en", reporter=None,
    grading_setup: TaskGradingSetup | None = None,
) -> list[AdapterOutcome]:
    """Run the unchanged algorithm over normalized inputs and return per-student
    outcomes. A façade run reads only the knowledge ids frozen at run creation;
    a normalized caller without setup keeps the assignment scope.

    Raises on a batch-level failure (the caller marks the run failed); per-
    question failures are mapped to ``needs_review`` results, not dropped.
    """
    problem_store = build_problem_store(questions)
    student_store = build_student_store(frozen_revisions)
    if not student_store or not problem_store:
        return []
    knowledge_task_id = (
        None
        if grading_setup is not None and grading_setup.knowledge_scope == "none"
        else (
            f"grading-run:{run_id}"
            if grading_setup is not None
            else assignment_id
        )
    )
    if settings.e2e_fake_provider:
        from backend.testing.fake_provider import FakeProvider, fake_grade_batch

        raw_results = await fake_grade_batch(
            student_store=student_store,
            problem_store=problem_store,
            provider=FakeProvider(fail_qids={settings.e2e_fail_qid} if settings.e2e_fail_qid else set()),
            language=language,
            task_id=knowledge_task_id,
        )
    else:
        raw_results = await grade_batch(
            student_store=student_store, problem_store=problem_store, registry=registry,
            reporter=reporter, language=language, task_id=knowledge_task_id,
            multi_sample_n=grading_setup.multi_sample_n if grading_setup else None,
            aggregation_method=grading_setup.aggregation_method if grading_setup else None,
            grading_setup=grading_setup,
        )
    revision_by_student = {sid: rev for rev, sid in frozen_revisions}
    raw_by_student = {raw.get("student_id", ""): raw for raw in raw_results}
    outcomes: list[AdapterOutcome] = []
    for student_id, revision in revision_by_student.items():
        raw = raw_by_student.get(student_id)
        corrections = {c.q_id: c for c in (raw or {}).get("corrections", [])}
        results = []
        for q in questions:
            normalized = correction_to_result(
                run_id=run_id, revision_id=revision.id, question=q,
                student_id=student_id, correction=corrections.get(q.q_id),
            )
            if raw is None:
                normalized.requires_review = True
                normalized.review_reason = "missing_student_result"
                normalized.result_status = education.GradeResultStatus.NEEDS_REVIEW.value
                normalized.ai_score = None
            results.append(normalized)
        outcomes.append(AdapterOutcome(student_id=student_id, results=results))
    return outcomes
