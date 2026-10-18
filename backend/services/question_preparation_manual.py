"""Recover verified question artifacts for explicit teacher completion; no model calls."""
from copy import deepcopy
import time

from backend.agents.question_preparation_agent import (
    _issue, _issue_field, _validate_major_question_candidates,
    requested_major_question_materials,
)
from backend.domain.errors import InvalidTransition, ValidationError
from backend.models import QuestionScorePolicy
from backend.services.question_preparation_artifacts import (
    QUESTIONS_EXTRACTED_STAGE, UPLOADED_MATERIALS_ALIGNED_STAGE, read_base_preparation_artifact,
    read_question_candidate_artifact,
)
from backend.services.teacher_score_constraints import teacher_score_requirements


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
    teacher_points = teacher_score_requirements(
        problems, QuestionScorePolicy.model_validate(payload.get("score_policy") or {}))
    artifacts = checkpoint.get("question_artifact_ids") or {}
    completed = set(checkpoint.get("completed_question_ids") or [])
    if set(artifacts) != completed or not completed <= set(problems):
        unavailable()
    missing_questions = []
    for index, (q_id, problem) in enumerate(problems.items()):
        targets = [row for row in requested if row["q_id"] == q_id]
        issues = deepcopy(base.payload.issues.get(q_id, []))
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


def validate_manual_source_questions(operation, questions, completed_sources):
    """Only teacher-authored questions may replace a source with no verified list."""
    from backend.services.question_structure import build_major_question_structure, validate_rubric_points, QuestionRubricValidationError
    from backend.services.workflow_inputs import question_inputs
    saved = question_inputs(task_id=operation.assignment_id, owner_id=operation.owner_id)["input"]
    sources = saved["sources"] if saved and saved["job_id"] == operation.id else []
    if not sources or set(completed_sources) != {source["source_token"] for source in sources}:
        raise ValidationError("Review every source before continuing.", code="question_sources_pending")
    packages = {}
    for index, question in enumerate(questions):
        raw = question.model_dump()
        q_id = raw["q_id"].strip()
        if not q_id or q_id in packages or any(not raw[field].strip() for field in ("stem", "criterion")):
            raise ValidationError("Each question needs a unique ID, stem and rubric.", code="question_review_blocked")
        raw.update(q_id=q_id, review_status="needs_review", max_score_source="teacher_edited")
        structure = build_major_question_structure(raw, major_order=index, structure_source="deterministic", review_status="needs_review")
        try:
            validate_rubric_points(raw["criterion"], raw["max_score"], structure)
        except QuestionRubricValidationError:
            raise ValidationError("Rubric points must match the maximum score.", code="question_review_blocked") from None
        marker = _issue(q_id, "source", "manual_completion_required", "warning", [])
        marker["details"] = {"required_fields": ["stem", "criterion"], "reason": operation.error_code}
        raw["preparation_issues"] = [marker]
        packages[q_id] = raw
    return packages
