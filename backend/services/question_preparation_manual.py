"""Recover completed packages or structure saved text for the original matrix."""
from copy import deepcopy
import time

from backend.agents.question_preparation_agent import (
    _issue, _issue_field, _validate_major_question_candidates,
    requested_major_question_materials,
)
from backend.domain.errors import InvalidTransition
from backend.models import QuestionScorePolicy
from backend.services.question_preparation_artifacts import (
    QUESTIONS_EXTRACTED_STAGE, UPLOADED_MATERIALS_ALIGNED_STAGE, read_base_preparation_artifact,
    read_question_candidate_artifact,
)
from backend.services.teacher_score_constraints import teacher_score_requirements


def saved_question_sources(operation):
    from backend.services.recognition_recovery import saved_recognition
    from backend.config import settings
    from backend.db import workflow_repository
    from backend.services.question_sources import question_recognition_options
    if operation.payload.get("manual_saved_sources"):
        return None  # Failed organization is retried explicitly, never by handoff.
    before = operation.completed_at or operation.updated_at
    artifacts = {}
    for ref in operation.payload.get("source_refs", []):
        if not ref.get("stored_file_id"):
            continue
        source = workflow_repository.get_operation(ref["source_operation_id"], owner_id=operation.owner_id)
        scope = question_recognition_options(source.payload.get("recognition_options"), extraction_hint=source.payload.get("extraction_hint", ""))
        saved = saved_recognition(owner_id=operation.owner_id, task_id=operation.assignment_id,
            stored_file_id=ref["stored_file_id"], purpose=_source_purpose(ref["role"]), before=before,
            selected_pages=scope.pages)
        if saved.text:
            artifacts[ref["source_operation_id"]] = saved.artifact_ids
    return {"before": before, "artifacts": artifacts, "chunk_chars": settings.source_chunk_chars,
            "chunk_overlap": max(0, settings.source_chunk_overlap_chars)} if artifacts else None


def restore_saved_question_sources(operation, sources):
    from backend.services.recognition_recovery import saved_recognition
    from backend.services.question_sources import question_recognition_options
    frozen = operation.payload["manual_saved_sources"]
    refs = {ref["source_operation_id"]: ref for ref in operation.payload["source_refs"]}
    restored = []
    for draft, text, payload in sources:
        ids = frozen["artifacts"].get(draft.source_token)
        if ids:
            saved = saved_recognition(owner_id=operation.owner_id, task_id=operation.assignment_id,
                stored_file_id=refs[draft.source_token]["stored_file_id"], purpose=_source_purpose(draft.role),
                before=frozen["before"], artifact_ids=ids,
                selected_pages=question_recognition_options(payload.get("recognition_options"), extraction_hint=draft.extraction_hint).pages)
            text = saved.text
        restored.append((draft, text, payload))
    return restored


def _source_purpose(role):
    return {"problem": "problems", "reference_answer": "reference", "rubric": "rubric",
            "programming_tests": "test_cases"}[role]


async def prepare_saved_question_packages(sources, *, provider, reporter, score_policy,
                                          ocr_only, recovered, on_started, on_extracted, on_failed,
                                          completed_chunks=0, chunk_settings=None):
    from backend.agents.ingest_agent import _extract_problems_call, _chunk_problem_text, extract_problems_from_ocr_markdown
    from backend.config import settings
    from backend.services.question_sources import question_recognition_options
    from backend.services.question_structure import annotate_major_question_structures
    from backend.services.teacher_score_constraints import explicit_teacher_scores, normalize_question_number
    from backend.tools.problem_dedup import dedupe_extracted_problems
    from backend.agents.question_preparation_agent import _SubmissionSafeProvider
    problem_sources = [(draft, text) for draft, text, _ in sources if draft.role == "problem" and text.strip()]
    scopes = [question_recognition_options(payload.get("recognition_options"), extraction_hint=draft.extraction_hint)
              for draft, _text, payload in sources if draft.role == "problem"]
    wanted = list(dict.fromkeys(number for scope in scopes for number in scope.targets))
    packages = deepcopy(recovered) if recovered is not None else {}
    text = "\n\n".join(f"[Saved problem source]\n{text}" for _, text in problem_sources)
    text += "\n\n" + "\n\n".join(f"[Saved {draft.role}; supporting material, NOT question text]\n{text}"
        for draft, text, _ in sources if draft.role != "problem" and text.strip())
    hint = "\n".join(draft.extraction_hint for draft, _ in problem_sources)
    if wanted:
        hint += "\nQuestion numbers: " + ", ".join(wanted)
    outline = explicit_teacher_scores(score_policy.per_question_text or "") if score_policy.mode == "per_question" else {}
    chunk_size = (chunk_settings or {}).get("chunk_chars", settings.source_chunk_chars)
    overlap = (chunk_settings or {}).get("chunk_overlap", max(0, settings.source_chunk_overlap_chars))
    chunks = _chunk_problem_text(text, chunk_size, overlap) if chunk_size > 0 else [text]
    if recovered is not None and not completed_chunks:
        return packages  # An already complete pre-chunk-format artifact.
    for index, chunk in enumerate(chunks):
        if index < completed_chunks:
            continue
        await reporter.set_current_step("extracting_questions", message=f"Organizing saved text {index + 1}/{len(chunks)}")
        await on_started()
        try:
            incoming = {}
            if ocr_only:
                await extract_problems_from_ocr_markdown(chunk, incoming, reporter=reporter)
            else:
                incoming = await _extract_problems_call(chunk, _SubmissionSafeProvider(provider, reporter=reporter), reporter=reporter,
                    structure_mode="extract_from_source", extraction_hint=hint,
                    partial_source=True, manage_progress_lifecycle=False, allow_empty=True)
            incoming = _saved_package_issues(incoming, score_policy, outline)
            wanted_numbers = {normalize_question_number(number) for number in wanted}
            for problem in incoming.values():
                number = normalize_question_number(problem.get("number"))
                if wanted_numbers and number and number not in wanted_numbers:
                    continue
                q_id = f"q{len(packages) + 1}"
                packages[q_id] = {**problem, "q_id": q_id}
            packages = annotate_major_question_structures(dedupe_extracted_problems(packages))
            packages = _saved_package_issues(packages, score_policy, outline)
            await on_extracted(packages, completed_chunks=index + 1)
        except Exception as exc:
            await on_failed(QUESTIONS_EXTRACTED_STAGE, exc)
            raise
    # Explicitly requested, missing questions get editable rows, never fabricated stems.
    wanted += [number for number in outline if number not in wanted]
    actual = {normalize_question_number(row.get("number")) for row in packages.values()}
    for number in wanted:
        if normalize_question_number(number) not in actual:
            q_id = f"q{len(packages) + 1}"
            packages[q_id] = dict(q_id=q_id, number=number, type="other", stem="", criterion="", max_score=10)
            actual.add(normalize_question_number(number))
    return _saved_package_issues(packages, score_policy, outline)


def _saved_package_issues(packages, score_policy, outline):
    from backend.services.teacher_score_constraints import normalize_question_number
    for q_id, problem in packages.items():
        issues = [issue for issue in problem.get("preparation_issues") or []
                  if issue.get("code") not in {"manual_completion_required", "generation_failed"}]
        if problem.pop("source_incomplete", False):
            issues.append(_issue(q_id, "stem", "recognition_partial", "blocking", []))
        maximum = outline.get(normalize_question_number(problem.get("number")))
        problem["max_score"] = (float(score_policy.uniform_max_score) if score_policy.mode == "uniform"
                                else float(maximum.maximum) if maximum and maximum.maximum is not None else 10)
        problem["max_score_source"] = score_policy.mode
        problem["max_score_review_status"] = "confirmed" if score_policy.mode != "per_question" or maximum else "needs_review"
        required = []
        for field in ("stem", "criterion", "reference_answer"):
            if not str(problem.get(field) or "").strip():
                issues.append(_issue(q_id, _issue_field(field), "generation_failed",
                                     "warning" if field == "reference_answer" else "blocking", []))
                if field != "reference_answer":
                    required.append(field)
        marker = _issue(q_id, "source", "manual_completion_required", "warning", [])
        marker["details"] = {"required_fields": required}
        issues.append(marker)
        problem.update(preparation_issues=issues, review_status="needs_review")
    return packages


def recover_manual_question_packages(operation):
    checkpoint, payload = operation.checkpoint or {}, operation.payload or {}
    attempts = checkpoint.get("artifact_attempts") or {}

    def context(artifact_id):
        attempt = attempts.get(artifact_id, operation.attempt)
        if (artifact_id not in (operation.artifact_refs or [])
                or type(attempt) is not int or not 1 <= attempt <= operation.attempt):
            unavailable()
        return dict(owner_id=operation.owner_id, task_id=operation.assignment_id,
                    operation_id=operation.id, attempt=attempt, input_hash=operation.input_hash,
                    provider_record_id=payload.get("recognition_provider_id"))

    aligned = bool(checkpoint.get("aligned_base_artifact_id"))
    base_id = checkpoint.get("aligned_base_artifact_id") or checkpoint.get("questions_extracted_artifact_id")
    if not base_id:
        unavailable()
    base = read_base_preparation_artifact(
        base_id, stage=UPLOADED_MATERIALS_ALIGNED_STAGE if aligned else QUESTIONS_EXTRACTED_STAGE, **context(base_id))
    if base is None:
        unavailable()
    problems = deepcopy(base.payload.problem_data)
    if not problems or (checkpoint.get("question_ids") and list(problems) != list(checkpoint["question_ids"])):
        unavailable()
    requested = requested_major_question_materials(problems)
    teacher_points = ({} if payload.get("manual_saved_sources") else teacher_score_requirements(
        problems, QuestionScorePolicy.model_validate(payload.get("score_policy") or {})))
    artifacts = checkpoint.get("question_artifact_ids") or {}
    completed = set(checkpoint.get("completed_question_ids") or [])
    if set(artifacts) != completed or not completed <= set(problems):
        unavailable()
    missing_questions = []
    for index, (q_id, problem) in enumerate(problems.items()):
        targets = [row for row in requested if row["q_id"] == q_id]
        issues = deepcopy(base.payload.issues.get(q_id, problem.get("preparation_issues", [])))
        if q_id in completed:
            artifact_id = artifacts[q_id]
            saved = read_question_candidate_artifact(
                artifact_id, q_id=q_id, question_order=index, **context(artifact_id))
            if saved is None:
                unavailable()
            candidates = _validate_major_question_candidates(q_id, targets, saved.payload.candidates,
                {**problem, "teacher_subpart_points": teacher_points.get(q_id, {})})
            for candidate in candidates:
                problem[candidate.target] = ([case.model_dump() for case in candidate.test_cases]
                    if candidate.target == "test_cases" else candidate.text_value)
                problem.setdefault("ai_completion_provenance", {})[candidate.target] = {
                    "job_id": operation.id, "candidate_id": candidate.target_id,
                    "source_kind": "ai_generated", "provider_id": payload.get("recognition_provider_id"),
                    "review_status": "pending", "generated_at": time.time(), "updated_at": time.time(),
                }
        elif targets:
            missing_questions.append(q_id)
            required_fields = []
            for target in targets:
                field = target["target"]
                # Keep supplied teacher material, including a short reference answer.
                # Unverified generated material is not promoted from partial responses.
                if field in {"stem", "criterion"}:
                    required_fields.append(field)
                if not problem.get(field):
                    issue = _issue(q_id, _issue_field(field), "generation_failed",
                                   "blocking" if field in {"stem", "criterion"} else "warning", [])
                    issue["details"] = {"target": field}
                    issues.append(issue)
            marker = _issue(q_id, "source", "manual_completion_required", "warning", [])
            marker["details"] = {"required_fields": required_fields, "reason": operation.error_code}
            issues.append(marker)
            problem["review_status"] = "needs_review"
        problem["preparation_issues"] = issues
    return problems, missing_questions


def unavailable():
    raise InvalidTransition("Verified question drafts are unavailable for manual completion.",
                            code="question_preparation_manual_unavailable")


def blank_manual_question_packages(operation, count):
    """Create editable matrix rows only; no invented recognized content."""
    packages = {}
    for index in range(count):
        q_id = f"manual-{index + 1}"
        marker = _issue(q_id, "source", "manual_completion_required", "warning", [])
        marker["details"] = {"required_fields": ["stem", "criterion"], "reason": operation.error_code}
        packages[q_id] = dict(q_id=q_id, number=str(index + 1), type="short", stem="",
            criterion="", reference_answer="", max_score=10, review_status="needs_review",
            preparation_issues=[marker])
    return packages
