"""Unified Q01 question/material preparation orchestration.

The public workflow is intentionally one job.  Existing extraction, material
alignment and generation helpers remain the bounded skills used underneath;
this agent owns ordering, progress and the single atomic result payload.
"""
from __future__ import annotations

import asyncio
import time
import unicodedata
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from typing import Any, Dict, Iterable, List, Tuple

from backend.agents.ingest_agent import (
    AICompletionCandidateOutput,
    extract_problems,
    extract_problems_from_ocr_markdown,
    generate_missing_question_materials,
    parse_material_import_to_candidates,
    split_ocr_markdown_sections,
)
from backend.config import settings
from backend.llm.endpoint_policy import ProviderEndpointError
from backend.llm.providers import BaseProvider
from backend.domain.errors import ValidationError
from backend.models import (
    ProblemSourceDraft,
    QuestionScorePolicy,
    TestCase,
    is_programming_question_type,
)
from backend.progress.tracker import ProgressReporter
from backend.services.background_errors import classify_background_error
from backend.services.question_structure import (
    MajorQuestionStructureV1,
    QuestionRubricValidationError,
    validate_rubric_points,
)
from backend.skills.question_score import resolve_question_score_policy


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


class _ProviderSubmissionUncertainError(RuntimeError):
    retryable = False
    submission_may_exist = True


class _ProviderSubmissionRejectedError(RuntimeError):
    """A provider call that is known not to have started billable work."""

    submission_may_exist = False

    def __init__(self, exc: Exception) -> None:
        super().__init__(str(exc))
        status_code = getattr(exc, "status_code", None)
        if status_code is None:
            status_code = getattr(
                getattr(exc, "response", None), "status_code", None
            )
        self.status_code = status_code
        self.retry_after = getattr(exc, "retry_after", None)
        explicit_retryable = getattr(exc, "retryable", None)
        if isinstance(explicit_retryable, bool):
            self.retryable = explicit_retryable
        else:
            normalized = str(exc).casefold()
            self.retryable = status_code == 429 or any(
                marker in normalized
                for marker in (
                    "rate limit",
                    "rate_limit",
                    "quota",
                    "resource_exhausted",
                    "resourceexhausted",
                )
            )


def _provider_submission_may_exist(exc: Exception) -> bool:
    explicit = getattr(exc, "submission_may_exist", None)
    if isinstance(explicit, bool):
        return explicit
    if isinstance(exc, ProviderEndpointError):
        return False
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
    if status_code in {400, 401, 403, 404, 409, 422, 429}:
        return False
    code = str(getattr(exc, "code", "") or "").casefold()
    if code in {
        "provider_response_invalid",
        "provider_image_payload_invalid",
        "provider_message_payload_not_supported",
    }:
        return False
    text = f"{code} {exc}".casefold()
    if any(marker in text for marker in (
        "rate_limit",
        "rate limit",
        "rate_limited",
        "quota",
        "resource_exhausted",
        "resourceexhausted",
        "shared_pool_daily_limit_reached",
        "shared_pool_disabled",
    )):
        return False
    return True


def provider_submission_is_uncertain(exc: BaseException) -> bool:
    """Return true only when a guarded call has an unknown provider outcome.

    A successfully returned but invalid response has no uncertainty marker and
    is therefore safe for an explicit teacher retry. The guarded provider adds
    a positive marker before the structured-LLM retry layer can otherwise hide
    the original transport exception in its cause chain.
    """

    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if getattr(current, "submission_may_exist", None) is True:
            return True
        current = current.__cause__ or current.__context__
    return False


class _SubmissionSafeProvider:
    """Disable in-worker replay when a provider may have received the call."""

    def __init__(self, provider: BaseProvider) -> None:
        self._provider = provider

    def __getattr__(self, name: str):
        return getattr(self._provider, name)

    async def ainvoke(self, messages):
        try:
            return await self._provider.ainvoke(messages)
        except Exception as exc:
            if _provider_submission_may_exist(exc):
                raise _ProviderSubmissionUncertainError(
                    "provider_submit_uncertain"
                ) from exc
            raise _ProviderSubmissionRejectedError(exc) from exc


async def _run_base_provider_stage(
    call: Awaitable[Any],
    *,
    stage: str,
    on_failed: Callable[[str, Exception], Awaitable[None]] | None,
):
    try:
        return await call
    except Exception as exc:
        if on_failed is not None:
            await on_failed(stage, exc)
        raise


async def generate_major_question_materials(
    *,
    problems_data: Dict[str, Dict[str, Any]],
    requested_targets: List[Dict[str, str]],
    test_case_count: int,
    provider: BaseProvider,
    reporter: ProgressReporter,
    recovered_candidates_by_question: Mapping[
        str, Sequence[AICompletionCandidateOutput]
    ] | None = None,
    completed_question_ids: Sequence[str] | None = None,
    on_question_started: Callable[[str], Awaitable[None]] | None = None,
    on_question_completed: Callable[
        [str, list[AICompletionCandidateOutput]], Awaitable[None]
    ] | None = None,
    on_question_failed: Callable[[str, Exception], Awaitable[None]] | None = None,
) -> list[AICompletionCandidateOutput]:
    """Generate one bounded provider request per scored major question.

    All requested fields for a question stay in the same call, including every
    internal subpart. Results are returned in source-question and target order,
    regardless of completion order. Recovered artifacts pass the same strict
    validation as new provider output and seed factual progress without another
    provider call. Lifecycle hooks let 03C checkpoint the transition to a real
    provider submission, each completed artifact, and the original exception
    before it is reduced to a safe progress error code.
    """

    targets_by_question: dict[str, list[dict[str, str]]] = defaultdict(list)
    seen_target_ids: set[str] = set()
    for target in requested_targets:
        q_id = str(target.get("q_id") or "")
        target_name = str(target.get("target") or "")
        target_id = str(target.get("target_id") or "")
        if (
            q_id not in problems_data
            or target_name not in {
                "criterion", "reference_answer", "solution_code", "test_cases"
            }
            or target_id != f"{q_id}:{target_name}"
            or target_id in seen_target_ids
        ):
            raise ValidationError(
                "A major-question generation target was invalid or duplicated.",
                code="provider_response_invalid",
            )
        seen_target_ids.add(target_id)
        targets_by_question[q_id].append(target)
    question_ids = [q_id for q_id in problems_data if targets_by_question[q_id]]
    if not question_ids:
        raise ValidationError(
            "No valid major-question generation targets were supplied.",
            code="provider_response_invalid",
        )

    limit = _major_question_generation_concurrency(provider)
    recovered_input = dict(recovered_candidates_by_question or {})
    unknown_recovered_ids = set(recovered_input) - set(question_ids)
    if unknown_recovered_ids:
        raise ValidationError(
            "Recovered materials referenced an unknown major question.",
            code="provider_response_invalid",
        )

    if completed_question_ids is None:
        completed_ids = [
            q_id for q_id in question_ids if q_id in recovered_input
        ]
    else:
        completed_ids = [str(q_id).strip() for q_id in completed_question_ids]
        if (
            any(not q_id for q_id in completed_ids)
            or len(completed_ids) != len(set(completed_ids))
            or not set(completed_ids) <= set(question_ids)
            or set(completed_ids) != set(recovered_input)
        ):
            raise ValidationError(
                "Recovered materials did not match the completed major questions.",
                code="provider_response_invalid",
            )
        completed_ids.sort(key=question_ids.index)

    semaphore = asyncio.Semaphore(limit)
    results: dict[str, list[AICompletionCandidateOutput]] = {}
    failures: dict[str, Exception] = {}
    for q_id in completed_ids:
        try:
            recovered_candidates = [
                AICompletionCandidateOutput.model_validate(candidate)
                for candidate in recovered_input[q_id]
            ]
            results[q_id] = _validate_major_question_candidates(
                q_id,
                targets_by_question[q_id],
                recovered_candidates,
                problems_data[q_id],
            )
        except ValidationError:
            raise
        except Exception as exc:
            raise ValidationError(
                "Recovered major-question materials were invalid.",
                code="provider_response_invalid",
            ) from exc

    await reporter.configure_question_generation(
        question_ids,
        completed_question_ids=completed_ids,
        question_labels={
            q_id: str(problems_data[q_id].get("number") or q_id)
            for q_id in question_ids
        },
    )

    async def run_unit(q_id: str) -> None:
        started = False
        try:
            async with semaphore:
                await reporter.mark_question_generation_started(q_id)
                started = True
                if on_question_started is not None:
                    await on_question_started(q_id)
                candidates = await generate_missing_question_materials(
                    problems_data={q_id: problems_data[q_id]},
                    requested_targets=targets_by_question[q_id],
                    test_case_count=test_case_count,
                    provider=provider,
                    reporter=None,
                    manage_progress_lifecycle=False,
                )
                validated = _validate_major_question_candidates(
                    q_id,
                    targets_by_question[q_id],
                    candidates,
                    problems_data[q_id],
                )
                if on_question_completed is not None:
                    await on_question_completed(q_id, validated)
                results[q_id] = validated
                await reporter.mark_question_generation_finished(
                    q_id, succeeded=True
                )
        except asyncio.CancelledError:
            if started:
                await reporter.mark_question_generation_cancelled(q_id)
            raise
        except Exception as exc:
            failure_to_raise = exc
            if on_question_failed is not None:
                try:
                    await on_question_failed(q_id, exc)
                except asyncio.CancelledError:
                    if started:
                        await reporter.mark_question_generation_cancelled(q_id)
                    raise
                except Exception as checkpoint_exc:
                    failure_to_raise = checkpoint_exc
            failures[q_id] = failure_to_raise
            if started:
                await reporter.mark_question_generation_finished(
                    q_id,
                    succeeded=False,
                    error_code=classify_background_error(
                        exc, "ai_completion_failed"
                    ),
                )

    tasks = [
        asyncio.create_task(run_unit(q_id))
        for q_id in question_ids
        if q_id not in results
    ]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    if failures:
        first_failed = next(q_id for q_id in question_ids if q_id in failures)
        raise failures[first_failed]
    return [candidate for q_id in question_ids for candidate in results[q_id]]


def _major_question_generation_concurrency(provider: BaseProvider) -> int:
    """Use the same BYOK concurrency contract as grading.

    This outer gate bounds active major-question units for truthful progress.
    Actual calls remain protected by ``BaseProvider``'s RPM limiter,
    per-provider semaphore, and process-wide endpoint semaphore. There is no
    separate question-generation concurrency setting.
    """

    config = getattr(provider, "config", None)
    configured = getattr(config, "max_concurrent", None)
    if isinstance(configured, bool):
        configured = None
    try:
        provider_limit = int(configured) if configured is not None else 0
    except (TypeError, ValueError):
        provider_limit = 0
    if provider_limit <= 0:
        provider_limit = max(
            1, int(settings.max_concurrent_llm_per_provider)
        )

    endpoint_limit = max(1, int(settings.max_concurrent_llm_per_endpoint))
    return min(provider_limit, endpoint_limit)


def _validate_major_question_candidates(
    q_id: str,
    targets: list[dict[str, str]],
    candidates: list[AICompletionCandidateOutput],
    problem: dict[str, Any],
) -> list[AICompletionCandidateOutput]:
    expected = {target["target_id"]: target for target in targets}
    accepted: dict[str, AICompletionCandidateOutput] = {}
    for candidate in candidates:
        target = expected.get(candidate.target_id)
        value_present = (
            bool(candidate.test_cases)
            if candidate.target == "test_cases"
            else bool((candidate.text_value or "").strip())
        )
        if (
            target is None
            or candidate.target_id in accepted
            or candidate.q_id != q_id
            or target["q_id"] != candidate.q_id
            or target["target"] != candidate.target
            or not value_present
        ):
            raise ValidationError(
                "The provider returned an invalid major-question material candidate.",
                code="provider_response_invalid",
            )
        structure = MajorQuestionStructureV1.model_validate(
            problem.get("question_structure")
        )
        if candidate.target == "criterion" and structure.subparts:
            try:
                rubric_summary = validate_rubric_points(
                    candidate.text_value or "",
                    problem.get("max_score", 10),
                    structure,
                )
            except QuestionRubricValidationError as exc:
                raise ValidationError(
                    "The generated rubric did not preserve the major-question score.",
                    code="provider_response_invalid",
                ) from exc
            if not rubric_summary.has_explicit_subpart_points:
                raise ValidationError(
                    "The generated rubric omitted explicit subpart allocations.",
                    code="provider_response_invalid",
                )
        if candidate.target == "reference_answer" and structure.subparts:
            normalized_answer = unicodedata.normalize(
                "NFKC", candidate.text_value or ""
            ).casefold()
            if any(
                unicodedata.normalize("NFKC", part.label).casefold()
                not in normalized_answer
                for part in structure.subparts
            ):
                raise ValidationError(
                    "The generated answer omitted a labelled subpart.",
                    code="provider_response_invalid",
                )
        accepted[candidate.target_id] = candidate
    if set(accepted) != set(expected):
        raise ValidationError(
            "The provider omitted a required major-question material candidate.",
            code="provider_response_invalid",
        )
    return [accepted[target["target_id"]] for target in targets]


def requested_major_question_materials(
    problem_data: Dict[str, Dict[str, Any]],
) -> list[dict[str, str]]:
    """Return missing targets in stable major-question/field order."""

    requested: list[dict[str, str]] = []
    for q_id, problem in problem_data.items():
        material_provenance = problem.get("material_provenance") or {}
        # A teacher answer may contain only the final result. Ask the bounded
        # generator to preserve it and expand it into reviewable steps.
        if (
            not str(problem.get("reference_answer") or "").strip()
            or "reference_answer" in material_provenance
        ):
            requested.append(_target(q_id, "reference_answer"))
        structure = MajorQuestionStructureV1.model_validate(
            problem.get("question_structure")
        )
        if (
            not str(problem.get("criterion") or "").strip()
            or (
                structure.subparts
                and "criterion" not in material_provenance
            )
        ):
            requested.append(_target(q_id, "criterion"))
        if is_programming_question_type(problem.get("type")):
            if not str(problem.get("solution_code") or "").strip():
                requested.append(_target(q_id, "solution_code"))
            if not list(problem.get("test_cases") or []):
                requested.append(_target(q_id, "test_cases"))
    return requested


async def prepare_question_packages(
    sources: Iterable[SourceRow],
    provider: BaseProvider,
    *,
    provider_id: str,
    reporter: ProgressReporter,
    score_policy: QuestionScorePolicy,
    recovered_extracted_problem_data: Mapping[
        str, Mapping[str, Any]
    ] | None = None,
    on_extraction_started: Callable[[], Awaitable[None]] | None = None,
    on_questions_extracted: Callable[
        [Dict[str, Dict[str, Any]]], Awaitable[None]
    ] | None = None,
    on_base_alignment_started: Callable[[], Awaitable[None]] | None = None,
    on_base_failed: Callable[
        [str, Exception], Awaitable[None]
    ] | None = None,
    recovered_base_problem_data: Mapping[str, Mapping[str, Any]] | None = None,
    recovered_base_issues: Mapping[
        str, Sequence[Mapping[str, Any]]
    ] | None = None,
    on_base_prepared: Callable[
        [Dict[str, Dict[str, Any]], Dict[str, List[Dict[str, Any]]]],
        Awaitable[None],
    ] | None = None,
    recovered_candidates_by_question: Mapping[
        str, Sequence[AICompletionCandidateOutput]
    ] | None = None,
    completed_question_ids: Sequence[str] | None = None,
    on_question_started: Callable[[str], Awaitable[None]] | None = None,
    on_question_completed: Callable[
        [str, list[AICompletionCandidateOutput]], Awaitable[None]
    ] | None = None,
    on_question_failed: Callable[[str, Exception], Awaitable[None]] | None = None,
    provider_submission_safe: bool = False,
) -> Dict[str, Dict[str, Any]]:
    """Prepare complete per-question packages from all Q01 sources.

    The returned mapping is not persisted here.  The API worker commits it to
    the normalized assignment/question tables only after every substep
    succeeds, preserving the previous good version if a later step fails.
    """

    source_rows = list(sources)
    if provider_submission_safe:
        provider = _SubmissionSafeProvider(provider)
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

    if recovered_base_problem_data is not None:
        try:
            problem_data = {
                str(q_id): deepcopy(dict(problem))
                for q_id, problem in recovered_base_problem_data.items()
                if str(q_id).strip()
            }
            if not problem_data or len(problem_data) != len(recovered_base_problem_data):
                raise ValueError("invalid recovered major questions")
            recovered_issue_rows = recovered_base_issues or {}
            if not set(recovered_issue_rows) <= set(problem_data):
                raise ValueError("recovered issues reference unknown questions")
            issues = defaultdict(
                list,
                {
                    str(q_id): [deepcopy(dict(issue)) for issue in rows]
                    for q_id, rows in recovered_issue_rows.items()
                },
            )
        except Exception as exc:
            raise ValidationError(
                "Recovered question-preparation base data was invalid.",
                code="provider_response_invalid",
            ) from exc
        await reporter.set_stage_progress(
            "extracting_questions",
            total_steps=8,
            completed_steps=1,
            message="Recovered recognized major-question structure",
        )
        await reporter.set_stage_progress(
            "aligning_uploaded_materials",
            total_steps=8,
            completed_steps=2,
            message="Recovered aligned teacher materials and score policy",
        )
    else:
        if recovered_extracted_problem_data is not None:
            try:
                problem_data = {
                    str(q_id): deepcopy(dict(problem))
                    for q_id, problem in recovered_extracted_problem_data.items()
                    if str(q_id).strip()
                }
                if (
                    not problem_data
                    or len(problem_data) != len(recovered_extracted_problem_data)
                ):
                    raise ValueError("invalid recovered extracted questions")
            except Exception as exc:
                raise ValidationError(
                    "Recovered extracted major questions were invalid.",
                    code="provider_response_invalid",
                ) from exc
            await reporter.set_stage_progress(
                "extracting_questions",
                total_steps=8,
                completed_steps=1,
                message="Recovered recognized major-question structure",
            )
        else:
            problem_text = _join_sources(problem_sources)
            structure_mode = (
                "extract_from_source"
                if any(
                    draft.structure_mode == "extract_from_source"
                    for draft, _ in problem_sources
                )
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
            problem_data = {}
            await reporter.set_stage_progress(
                "extracting_questions",
                total_steps=8,
                completed_steps=1,
                message="Recognizing question structure",
            )
            if on_extraction_started is not None:
                await on_extraction_started()
            await _run_base_provider_stage(
                extract_problems(
                    problem_text,
                    provider,
                    problem_data,
                    reporter=reporter,
                    structure_mode=structure_mode,
                    extraction_hint=extraction_hint,
                    confirmed_candidates=confirmed_candidates,
                    manage_progress_lifecycle=False,
                ),
                stage="questions_extracted",
                on_failed=on_base_failed,
            )
            if on_questions_extracted is not None:
                await on_questions_extracted(problem_data)

        issues = defaultdict(list)
        has_provider_alignment_work = bool(
            score_policy.mode == "per_question"
            or any(
                draft.role in {"reference_answer", "rubric", "programming_tests"}
                for draft, _text in source_rows
            )
        )
        if has_provider_alignment_work and on_base_alignment_started is not None:
            await on_base_alignment_started()
        resolved_scores = await _run_base_provider_stage(
            resolve_question_score_policy(
                problem_data,
                score_policy,
                provider,
                reporter=reporter,
            ),
            stage="uploaded_materials_aligned",
            on_failed=(on_base_failed if has_provider_alignment_work else None),
        )
        for q_id, resolved in resolved_scores.items():
            problem_data[q_id]["max_score"] = resolved.max_score
            problem_data[q_id]["max_score_source"] = resolved.source
            problem_data[q_id]["max_score_review_status"] = resolved.review_status
            if resolved.issue_code:
                issues[q_id].append(
                    _issue(q_id, "max_score", resolved.issue_code, "warning", [])
                )

        selected_candidates: Dict[
            Tuple[str, str], List[Tuple[Any, ProblemSourceDraft]]
        ] = defaultdict(list)
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
            parsed = await _run_base_provider_stage(
                parse_material_import_to_candidates(
                    text=text,
                    problems_data=problem_data,
                    targets=[target],
                    structure_mode=draft.structure_mode,
                    extraction_hint=draft.extraction_hint,
                    provider=provider,
                    reporter=reporter,
                    manage_progress_lifecycle=False,
                ),
                stage="uploaded_materials_aligned",
                on_failed=on_base_failed,
            )
            for candidate in parsed:
                selected_candidates[(candidate.q_id, target)].append(
                    (candidate, draft)
                )

        for (q_id, target), rows in selected_candidates.items():
            if q_id not in problem_data:
                continue
            ranked = sorted(
                rows, key=lambda row: float(row[0].confidence), reverse=True
            )
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

            if (
                float(candidate.confidence) < 0.72
                or candidate.match_status == "possible"
            ):
                issues[q_id].append(
                    _issue(
                        q_id,
                        _issue_field(target),
                        "low_confidence",
                        "warning",
                        [draft.filename],
                    )
                )
            distinct_values = {
                _candidate_value(row[0], target)
                for row in ranked
                if _candidate_value(row[0], target)
            }
            if len(distinct_values) > 1:
                issues[q_id].append(
                    _issue(
                        q_id,
                        _issue_field(target),
                        "source_conflict",
                        "warning",
                        [row[1].filename for row in ranked],
                    )
                )
        if on_base_prepared is not None:
            await on_base_prepared(problem_data, dict(issues))

    await reporter.set_stage_progress(
        "generating_solutions",
        total_steps=8,
        completed_steps=3,
        message="Generating complete answers for material not supplied by the teacher",
    )
    requested_targets = requested_major_question_materials(problem_data)

    if requested_targets:
        generated = await generate_major_question_materials(
            problems_data=problem_data,
            requested_targets=requested_targets,
            test_case_count=6,
            provider=provider,
            reporter=reporter,
            recovered_candidates_by_question=recovered_candidates_by_question,
            completed_question_ids=completed_question_ids,
            on_question_started=on_question_started,
            on_question_completed=on_question_completed,
            on_question_failed=on_question_failed,
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
        try:
            structure = MajorQuestionStructureV1.model_validate(
                problem.get("question_structure")
            )
            rubric_summary = validate_rubric_points(
                str(problem.get("criterion") or ""),
                problem.get("max_score", 10),
                structure,
            )
        except QuestionRubricValidationError as exc:
            raise ValidationError(
                "Explicit subpart rubric points must add up to the major-question maximum.",
                code=exc.summary.issue_code or "rubric_subpart_points_mismatch",
            ) from exc
        except Exception as exc:
            raise ValidationError(
                "Question structure could not be reduced to one scored row per major question.",
                code="question_structure_mismatch",
            ) from exc
        problem["rubric_point_summary"] = rubric_summary.model_dump()
        problem["preparation_issues"] = issues.get(q_id, [])

    await reporter.set_stage_progress(
        "committing_question_packages",
        total_steps=8,
        completed_steps=7,
        message="Question packages ready for transactional commit",
    )
    return problem_data


async def prepare_ocr_question_packages(
    sources: Iterable[SourceRow],
    *,
    provider_id: str,
    reporter: ProgressReporter,
    score_policy: QuestionScorePolicy,
) -> Dict[str, Dict[str, Any]]:
    """Continue Baidu OCR Markdown through the existing question workflow.

    Unlimited-OCR is not an LLM. This path therefore performs only exact,
    deterministic structure matching and leaves generated answers, rubrics,
    and ambiguous score mappings for teacher review.
    """
    del provider_id
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
        "validating_sources", total_steps=8, completed_steps=1,
        message="Validated OCR question and material sources",
    )
    problem_data: Dict[str, Dict[str, Any]] = {}
    await reporter.set_stage_progress(
        "extracting_questions", total_steps=8, completed_steps=1,
        message="Mapping OCR Markdown question sections",
    )
    await extract_problems_from_ocr_markdown(
        _join_sources(problem_sources),
        problem_data,
        reporter=reporter,
    )

    for q_id, problem in problem_data.items():
        issues = list(problem.get("preparation_issues") or [])
        if score_policy.mode == "uniform":
            assert score_policy.uniform_max_score is not None
            problem["max_score"] = float(score_policy.uniform_max_score)
            problem["max_score_source"] = "uniform"
            problem["max_score_review_status"] = "confirmed"
        else:
            problem["max_score"] = 10.0
            problem["max_score_source"] = "default_10"
            problem["max_score_review_status"] = "needs_review"
            issues.append(_issue(
                q_id,
                "max_score",
                (
                    "max_score_not_found"
                    if score_policy.mode == "per_question"
                    else "default_max_score_requires_review"
                ),
                "warning",
                [],
            ))
        problem["preparation_issues"] = issues

        try:
            structure = MajorQuestionStructureV1.model_validate(
                problem.get("question_structure")
            )
            problem["rubric_point_summary"] = validate_rubric_points(
                str(problem.get("criterion") or ""),
                problem.get("max_score", 10),
                structure,
            ).model_dump()
        except QuestionRubricValidationError as exc:
            raise ValidationError(
                "Explicit subpart rubric points must add up to the major-question maximum.",
                code=exc.summary.issue_code or "rubric_subpart_points_mismatch",
            ) from exc
        except Exception as exc:
            raise ValidationError(
                "Question structure could not be reduced to one scored row per major question.",
                code="question_structure_mismatch",
            ) from exc

    by_number = {
        str(problem.get("number") or "").strip(): q_id
        for q_id, problem in problem_data.items()
    }
    await reporter.set_stage_progress(
        "aligning_uploaded_materials", total_steps=8, completed_steps=2,
        message="Matching exact OCR material headings",
    )
    for draft, text in source_rows:
        target = {
            "reference_answer": "reference_answer",
        }.get(draft.role)
        if target is None:
            continue
        for number, value in split_ocr_markdown_sections(text):
            q_id = by_number.get(number.strip())
            if q_id is None or not value.strip():
                continue
            problem_data[q_id][target] = value.strip()
            provenance = dict(problem_data[q_id].get("material_provenance") or {})
            provenance[target] = {
                "import_job_id": reporter.job_id,
                "source_kind": draft.source_kind,
                "source_filename": draft.filename,
                "library_material_id": draft.library_material_id,
                "confidence": 1.0,
                "match_status": "exact",
                "source_excerpt": value.strip()[:600],
                "source_location": f"question {number}",
                "reason": "Exact OCR question-number heading",
                "review_status": "pending",
                "imported_at": time.time(),
                "updated_at": time.time(),
            }
            problem_data[q_id]["material_provenance"] = provenance

    await reporter.set_stage_progress(
        "generating_solutions", total_steps=8, completed_steps=3,
        message="OCR-only route does not generate missing solutions",
    )
    await reporter.set_stage_progress(
        "aligning_rubrics", total_steps=8, completed_steps=4,
        message="Rubrics remain for teacher review",
    )
    await reporter.set_stage_progress(
        "preparing_programming_tests", total_steps=8, completed_steps=5,
        message="Programming tests remain for teacher review",
    )
    await reporter.set_stage_progress(
        "detecting_conflicts", total_steps=8, completed_steps=6,
        message="Recorded OCR-only review requirements",
    )
    await reporter.set_stage_progress(
        "committing_question_packages", total_steps=8, completed_steps=7,
        message="OCR question packages ready for transactional commit",
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
