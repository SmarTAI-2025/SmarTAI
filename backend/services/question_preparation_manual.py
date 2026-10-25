"""Read verified structured drafts into the existing review matrix; no AI recovery."""
from copy import deepcopy

from backend.agents.question_preparation_agent import _issue
from backend.domain.errors import InvalidTransition
from backend.services.question_preparation_artifacts import (
    QUESTIONS_EXTRACTED_STAGE, read_base_preparation_artifact,
)


def recover_manual_question_packages(operation):
    checkpoint, payload = operation.checkpoint or {}, operation.payload or {}
    if "manual_saved_sources" in payload or payload.get("question_preparation_version") != 2:
        raise InvalidTransition("Saved-text AI recovery has been retired.", code="saved_text_recovery_retired")
    if (checkpoint.get("incremental_saving_units") or checkpoint.get("incremental_source_invalidations")
            or checkpoint.get("incremental_view_from_units")):
        problems = _project_interrupted_save(operation)
        return _mark_manual_rows(problems, operation.error_code)
    artifact_id = checkpoint.get("questions_extracted_artifact_id")
    attempt = (checkpoint.get("artifact_attempts") or {}).get(artifact_id, operation.attempt)
    if (not artifact_id or artifact_id not in (operation.artifact_refs or [])
            or type(attempt) is not int or not 1 <= attempt <= operation.attempt):
        unavailable()
    saved = read_base_preparation_artifact(
        artifact_id, stage=QUESTIONS_EXTRACTED_STAGE, owner_id=operation.owner_id,
        task_id=operation.assignment_id, operation_id=operation.id, attempt=attempt,
        input_hash=operation.input_hash, provider_record_id=payload.get("recognition_provider_id"),
    )
    if saved is None:
        unavailable()
    problems = deepcopy(saved.payload.problem_data)
    if list(problems) != list(checkpoint.get("question_ids") or []):
        unavailable()
    return _mark_manual_rows(problems, operation.error_code)


def _mark_manual_rows(problems, error_code):
    missing = []
    for q_id, row in problems.items():
        fields = [name for name in ("number", "type", "stem", "criterion") if not str(row.get(name) or "").strip()]
        if fields or row.get("preparation_issues"):
            missing.append(q_id)
            marker = _issue(q_id, "source", "manual_completion_required", "warning", [])
            marker["details"] = {"required_fields": fields, "reason": error_code}
            row.setdefault("preparation_issues", []).append(marker)
        row["review_status"] = "needs_review"
    return problems, missing


def _project_interrupted_save(operation):
    """Recover a planned, verified object write without historical scans or AI."""
    from types import SimpleNamespace
    from backend.agents.incremental_question_preparation import IncrementalQuestionPreparation
    from backend.db import workflow_repository
    from backend.models import QuestionScorePolicy
    from backend.services.question_preparation_artifacts import read_incremental_preparation_units
    from backend.services.question_preparation_identity import source_fingerprint, question_preparation_input_hash
    import hashlib
    sources = []
    frozen_inputs = []
    for ref in operation.payload.get("source_refs", []):
        source = workflow_repository.get_operation(ref["source_operation_id"], owner_id=operation.owner_id)
        if (source.assignment_id != operation.assignment_id or source.operation_type != "problem_source"
                or source.attempt != ref["source_attempt"]
                or source_fingerprint(source.payload) != source.input_hash
                or source.payload.get("sha256") != operation.payload.get("source_content_hashes", {}).get(source.id)
                or hashlib.sha256(str(source.payload.get("text") or "").encode()).hexdigest()
                    != operation.payload.get("source_text_hashes", {}).get(source.id)
                or any((source.payload.get("source_ref") or {}).get(key) != ref.get(key)
                       for key in ("source_id", "stored_file_id"))
                or source.payload.get("role") != ref.get("role")):
            unavailable()
        frozen_inputs.append(dict(source_token=source.id, source_fingerprint=source.input_hash, role=ref["role"]))
        draft = SimpleNamespace(source_token=source.id, role=ref["role"],
            extraction_hint=source.payload.get("extraction_hint", ""), filename=source.payload.get("filename", ""))
        sources.append((draft, "", source.payload))
    payload = operation.payload
    expected_hash = question_preparation_input_hash(ordered_source_inputs=frozen_inputs,
        logical_input_revision=payload["requested_workflow_revision"], replace_confirmed=payload["replace_confirmed"],
        generation_policy=payload["generation_policy"], score_policy=payload["score_policy"],
        recognition_provider_id=payload["recognition_provider_id"],
        provider_configuration_fingerprint=payload["provider_configuration_fingerprint"],
        generation_language=payload.get("generation_language"),
        acknowledged_restart_from=payload.get("acknowledged_restart_from"))
    if expected_hash != operation.input_hash:
        unavailable()
    context = SimpleNamespace(operation_id=operation.id, owner_id=operation.owner_id,
        assignment_id=operation.assignment_id, attempt=operation.attempt,
        input_hash=operation.input_hash, checkpoint_data=operation.checkpoint, artifact_refs=operation.artifact_refs)
    pipeline = IncrementalQuestionPreparation(operation=context, sources=sources, provider=None,
        provider_id=operation.payload["recognition_provider_id"], reporter=None, read_source=None,
        score_policy=QuestionScorePolicy.model_validate(operation.payload["score_policy"]), ocr_only=True)
    pipeline.units, _, _ = read_incremental_preparation_units(checkpoint=operation.checkpoint,
        artifact_refs=operation.artifact_refs, **pipeline.context)
    return pipeline.project()


def unavailable():
    raise InvalidTransition("Verified question drafts are unavailable for manual completion.",
                            code="question_preparation_manual_unavailable")


def initial_manual_question_packages(operation):
    """Project explicit frozen targets even if the worker never started."""
    problems = _project_interrupted_save(operation)
    return _mark_manual_rows(problems, operation.error_code)[0]


def blank_manual_question_packages(operation, count):
    """Create editable matrix rows only; no invented recognized content."""
    from backend.models import QuestionScorePolicy
    from backend.services.teacher_score_constraints import explicit_teacher_scores
    policy = QuestionScorePolicy.model_validate((operation.payload or {}).get("score_policy") or {})
    outline = explicit_teacher_scores(policy.per_question_text or "") if policy.mode == "per_question" else {}
    packages = {}
    for index in range(count):
        q_id = f"manual-{index + 1}"
        marker = _issue(q_id, "source", "manual_completion_required", "warning", [])
        marker["details"] = {"required_fields": ["stem", "criterion"], "reason": operation.error_code}
        allocation = outline.get(str(index + 1))
        maximum, source, review = 10, "default_10", "needs_review"
        if policy.mode == "uniform":
            maximum, source, review = policy.uniform_max_score, "uniform", "confirmed"
        elif allocation is not None and allocation.maximum is not None:
            maximum, source = float(allocation.maximum), "per_question_text"
        packages[q_id] = dict(q_id=q_id, number=str(index + 1), type="", stem="",
            criterion="", reference_answer="", max_score=maximum, review_status="needs_review",
            max_score_source=source, max_score_review_status=review,
            preparation_issues=[marker])
    return packages
