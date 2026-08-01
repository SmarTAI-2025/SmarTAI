"""Unified Q01 question/material preparation orchestration.

The public workflow is intentionally one job.  Existing extraction, material
alignment and generation helpers remain the bounded skills used underneath;
this agent owns ordering, progress and the single atomic result payload.
"""
from __future__ import annotations

import time
import uuid
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Tuple

from backend.agents.ingest_agent import (
    extract_problems,
    generate_missing_question_materials,
    parse_material_import_to_candidates,
)
from backend.llm.providers import BaseProvider
from backend.models import ProblemSourceDraft, TestCase, is_programming_question_type
from backend.progress.tracker import ProgressReporter


SourceRow = Tuple[ProblemSourceDraft, str]

QUESTION_PREPARATION_WORKFLOW = "question_preparation"
QUESTION_PREPARATION_STAGE_SEQUENCE = (
    "validating_sources",
    "extracting_questions",
    "aligning_uploaded_materials",
    "generating_solutions",
    "aligning_rubrics",
    "preparing_programming_tests",
    "detecting_conflicts",
    "committing_question_packages",
)


async def prepare_question_packages(
    sources: Iterable[SourceRow],
    provider: BaseProvider,
    *,
    provider_id: str,
    reporter: ProgressReporter,
) -> Dict[str, Dict[str, Any]]:
    """Prepare complete per-question packages from all Q01 sources.

    The returned mapping is not persisted here.  The API worker commits it to
    the normalized assignment/question tables only after every substep
    succeeds, preserving the previous good version if a later step fails.
    """

    source_rows = list(sources)
    problem_sources = [row for row in source_rows if row[0].role == "problem"]
    if not problem_sources:
        raise ValueError("At least one problem source is required.")

    await reporter.configure_workflow(
        QUESTION_PREPARATION_WORKFLOW,
        QUESTION_PREPARATION_STAGE_SEQUENCE,
    )
    await reporter.set_phase("parsing")
    await reporter.set_stage_progress(
        "validating_sources",
        total_steps=8,
        completed_steps=1,
        message="Validated question and optional material sources",
    )

    problem_text = _join_sources(problem_sources)
    structure_mode = (
        "extract_from_source"
        if any(draft.structure_mode == "extract_from_source" for draft, _ in problem_sources)
        else "organized"
    )
    extraction_hint = "\n".join(
        draft.extraction_hint.strip()
        for draft, _ in problem_sources
        if draft.extraction_hint.strip()
    )
    confirmed_candidates = [
        candidate
        for draft, _ in problem_sources
        for candidate in draft.candidates
    ]
    problem_data: Dict[str, Dict[str, Any]] = {}
    await reporter.set_stage_progress(
        "extracting_questions",
        total_steps=8,
        completed_steps=1,
        message="Recognizing question structure",
    )
    await extract_problems(
        problem_text,
        provider,
        problem_data,
        reporter=reporter,
        structure_mode=structure_mode,
        extraction_hint=extraction_hint,
        confirmed_candidates=confirmed_candidates,
        manage_progress_lifecycle=False,
    )

    issues: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    selected_candidates: Dict[Tuple[str, str], List[Tuple[Any, ProblemSourceDraft]]] = defaultdict(list)
    target_by_role = {
        "reference_answer": "reference_answer",
        "rubric": "criterion",
        "programming_tests": "test_cases",
    }
    await reporter.set_stage_progress(
        "aligning_uploaded_materials",
        total_steps=8,
        completed_steps=2,
        message="Matching uploaded answers, rubrics and programming tests",
    )
    for draft, text in source_rows:
        target = target_by_role.get(draft.role)
        if target is None:
            continue
        parsed = await parse_material_import_to_candidates(
            text=text,
            problems_data=problem_data,
            targets=[target],
            structure_mode=draft.structure_mode,
            extraction_hint=draft.extraction_hint,
            provider=provider,
            reporter=reporter,
            manage_progress_lifecycle=False,
        )
        for candidate in parsed:
            selected_candidates[(candidate.q_id, target)].append((candidate, draft))

    for (q_id, target), rows in selected_candidates.items():
        if q_id not in problem_data:
            continue
        ranked = sorted(rows, key=lambda row: float(row[0].confidence), reverse=True)
        candidate, draft = ranked[0]
        value: Any = (
            [case.model_dump() for case in (candidate.test_cases or [])]
            if target == "test_cases"
            else (candidate.text_value or "").strip()
        )
        if not value:
            continue
        problem_data[q_id][target] = value
        provenance = dict(problem_data[q_id].get("material_provenance") or {})
        provenance[target] = {
            "import_job_id": reporter.job_id,
            "candidate_id": f"qprep_{uuid.uuid4().hex[:12]}",
            "source_kind": draft.source_kind,
            "source_filename": draft.filename,
            "library_material_id": draft.library_material_id,
            "confidence": float(candidate.confidence),
            "match_status": candidate.match_status,
            "source_excerpt": candidate.source_excerpt[:600],
            "source_location": candidate.source_location[:160],
            "reason": candidate.reason[:300],
            "review_status": "pending",
            "imported_at": time.time(),
            "updated_at": time.time(),
        }
        problem_data[q_id]["material_provenance"] = provenance

        if float(candidate.confidence) < 0.72 or candidate.match_status == "possible":
            issues[q_id].append(_issue(q_id, _issue_field(target), "low_confidence", "warning", [draft.filename]))
        distinct_values = {_candidate_value(row[0], target) for row in ranked if _candidate_value(row[0], target)}
        if len(distinct_values) > 1:
            issues[q_id].append(_issue(q_id, _issue_field(target), "source_conflict", "warning", [row[1].filename for row in ranked]))

    await reporter.set_stage_progress(
        "generating_solutions",
        total_steps=8,
        completed_steps=3,
        message="Generating complete answers for material not supplied by the teacher",
    )
    requested_targets: List[Dict[str, str]] = []
    for q_id, problem in problem_data.items():
        material_provenance = problem.get("material_provenance") or {}
        # A teacher answer may contain only the final result. Ask the same
        # bounded generator to preserve it and expand it into reviewable steps.
        if (
            not str(problem.get("reference_answer") or "").strip()
            or "reference_answer" in material_provenance
        ):
            requested_targets.append(_target(q_id, "reference_answer"))
        if not str(problem.get("criterion") or "").strip():
            requested_targets.append(_target(q_id, "criterion"))
        if is_programming_question_type(problem.get("type")):
            if not str(problem.get("solution_code") or "").strip():
                requested_targets.append(_target(q_id, "solution_code"))
            if not list(problem.get("test_cases") or []):
                requested_targets.append(_target(q_id, "test_cases"))

    if requested_targets:
        generated = await generate_missing_question_materials(
            problems_data=problem_data,
            requested_targets=requested_targets,
            test_case_count=6,
            provider=provider,
            reporter=reporter,
            manage_progress_lifecycle=False,
        )
        now = time.time()
        generated_target_ids: set[str] = set()
        for index, candidate in enumerate(generated, start=1):
            problem = problem_data.get(candidate.q_id)
            if problem is None:
                continue
            generated_target_ids.add(candidate.target_id)
            if candidate.target == "test_cases":
                value = [case.model_dump() for case in (candidate.test_cases or [])]
            else:
                value = (candidate.text_value or "").strip()
            if not value:
                issues[candidate.q_id].append(
                    _issue(candidate.q_id, _issue_field(candidate.target), "generation_failed", "blocking", [])
                )
                continue
            problem[candidate.target] = value
            provenance = dict(problem.get("ai_completion_provenance") or {})
            provenance[candidate.target] = {
                "job_id": reporter.job_id,
                "candidate_id": f"qprep_ai_{index}",
                "source_kind": "ai_generated",
                "provider_id": provider_id,
                "review_status": "pending",
                "generated_at": now,
                "updated_at": now,
            }
            problem["ai_completion_provenance"] = provenance
        for target in requested_targets:
            if target["target_id"] in generated_target_ids:
                continue
            issues[target["q_id"]].append(
                _issue(
                    target["q_id"],
                    _issue_field(target["target"]),
                    "generation_failed",
                    "blocking",
                    [],
                )
            )

    await reporter.set_stage_progress(
        "aligning_rubrics",
        total_steps=8,
        completed_steps=4,
        message="Aligning answers and grading rubrics for review",
    )
    for q_id, problem in problem_data.items():
        if str(problem.get("criterion") or "").strip() and "criterion" not in (problem.get("material_provenance") or {}):
            provenance = dict(problem.get("ai_completion_provenance") or {})
            provenance.setdefault("criterion", {
                "job_id": reporter.job_id,
                "candidate_id": f"qprep_extract_{q_id}",
                "source_kind": "ai_generated",
                "provider_id": provider_id,
                "review_status": "pending",
                "generated_at": time.time(),
                "updated_at": time.time(),
            })
            problem["ai_completion_provenance"] = provenance

    await reporter.set_stage_progress(
        "preparing_programming_tests",
        total_steps=8,
        completed_steps=5,
        message="Normalizing programming examples and hidden tests",
    )
    for problem in problem_data.values():
        if not is_programming_question_type(problem.get("type")):
            problem.pop("test_cases", None)
            problem.pop("solution_code", None)
            continue
        normalized_cases = []
        for index, raw_case in enumerate(problem.get("test_cases") or [], start=1):
            case = TestCase.model_validate(raw_case)
            payload = case.model_dump()
            payload["title"] = case.title or f"样例 {index}"
            payload["io_mode"] = "function" if case.function_name else case.io_mode
            normalized_cases.append(payload)
        problem["test_cases"] = normalized_cases

    await reporter.set_stage_progress(
        "detecting_conflicts",
        total_steps=8,
        completed_steps=6,
        message="Detecting only risks that need teacher attention",
    )
    for q_id, problem in problem_data.items():
        problem["preparation_issues"] = issues.get(q_id, [])

    await reporter.set_stage_progress(
        "committing_question_packages",
        total_steps=8,
        completed_steps=7,
        message="Question packages ready for transactional commit",
    )
    return problem_data


def _join_sources(rows: List[SourceRow]) -> str:
    return "\n\n".join(
        f"[Source: {draft.filename}]\n{text.strip()}"
        for draft, text in rows
        if text.strip()
    )


def _target(q_id: str, target: str) -> Dict[str, str]:
    return {"target_id": f"{q_id}:{target}", "q_id": q_id, "target": target}


def _candidate_value(candidate: Any, target: str) -> str:
    if target == "test_cases":
        return str([case.model_dump() for case in (candidate.test_cases or [])])
    return str(candidate.text_value or "").strip()


def _issue_field(target: str) -> str:
    return {
        "criterion": "rubric",
        "reference_answer": "answer",
        "test_cases": "programming_tests",
        "solution_code": "programming_tests",
    }.get(target, "stem")


def _issue(q_id: str, field: str, code: str, severity: str, source_ids: List[str]) -> Dict[str, Any]:
    return {
        "issue_id": f"issue_{uuid.uuid4().hex[:12]}",
        "q_id": q_id,
        "field": field,
        "code": code,
        "severity": severity,
        "source_ids": list(dict.fromkeys(source_ids)),
        "details": {},
        "status": "open",
    }
