"""Restart-safe adapter for durable problem-extraction operations."""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from backend.db import file_repository, source_outcome_repository
from backend.domain.errors import NotFound, ValidationError
from backend.skills.ocr_ingest import LLMVisionOCRSkill
from backend.storage import get_storage


def artifact_name(
    operation_id: str, attempt: int, stage: str, extension: str
) -> str:
    return f"{operation_id}-attempt-{attempt}-{stage}.{extension}"


def _stage_progress(stage: str, completed_steps: int) -> dict[str, Any]:
    return {
        "workflow": "problem_recognition",
        "stage": stage,
        "completed_steps": completed_steps,
        "total_steps": 4,
    }


async def run_problem_extraction(
    operation,
    *,
    registry_factory: Callable[[str], Any],
    extract_text: Callable[..., Awaitable[str]],
    extract_questions: Callable[..., Awaitable[Any]],
    commit: Callable[..., int],
) -> None:
    payload = dict(operation.payload or {})
    source_id = payload.get("source_id")
    if not isinstance(source_id, str) or not source_id:
        raise NotFound("workflow_source")
    source = source_outcome_repository.get_source(
        source_id, owner_id=operation.owner_id
    )
    if (
        source.assignment_id != operation.assignment_id
        or source.operation_id != operation.operation_id
        or source.attempt != operation.attempt
    ):
        raise NotFound("workflow_source")

    checkpoint = dict(operation.checkpoint_data or {})
    artifact_refs = list(operation.artifact_refs or [])
    text_artifact_id = checkpoint.get("text_artifact_id")
    if not text_artifact_id:
        recovered = _find_stage_artifact(
            operation,
            stage="source_text",
            extension="txt",
            kind="problem_extraction_text",
        )
        text_artifact_id = recovered.id if recovered is not None else None
    text = _read_text_artifact(
        text_artifact_id,
        owner_id=operation.owner_id,
        assignment_id=operation.assignment_id,
    )
    registry = None
    provider = None
    if text is None:
        await operation.update_progress(_stage_progress("reading_source", 0))
        registry = registry_factory(operation.owner_id)
        provider = registry.pick_default()
        if provider is None:
            raise ValidationError(
                "No enabled provider is available.",
                code="no_provider_configured",
            )
        vision = registry.pick_vision(provider)
        with get_storage().open(source.storage_key) as stream:
            content = stream.read()
        text = await extract_text(
            content,
            source.original_name,
            ocr_skill=LLMVisionOCRSkill(vision) if vision is not None else None,
            purpose="problems",
        )
        artifact = file_repository.save_file(
            storage=get_storage(),
            owner_id=operation.owner_id,
            kind="problem_extraction_text",
            original_name=artifact_name(
                operation.operation_id, operation.attempt, "source_text", "txt"
            ),
            content=text.encode("utf-8"),
            content_type="text/plain",
            assignment_id=operation.assignment_id,
        )
        text_artifact_id = artifact.id
        artifact_refs = [artifact.id]
        checkpoint = {"text_artifact_id": artifact.id}
        await operation.checkpoint(
            stage="source_text_extracted",
            checkpoint=checkpoint,
            artifact_refs=artifact_refs,
        )
    await operation.update_progress(_stage_progress("recognizing_structure", 1))

    structured_artifact_id = checkpoint.get("structured_artifact_id")
    if not structured_artifact_id:
        recovered = _find_stage_artifact(
            operation,
            stage="structured_problems",
            extension="json",
            kind="problem_extraction_structure",
        )
        structured_artifact_id = recovered.id if recovered is not None else None
    problem_data = _read_json_artifact(
        structured_artifact_id,
        owner_id=operation.owner_id,
        assignment_id=operation.assignment_id,
    )
    if problem_data is None:
        if registry is None:
            registry = registry_factory(operation.owner_id)
        if provider is None:
            provider = registry.pick_default()
        if provider is None:
            raise ValidationError(
                "No enabled provider is available.",
                code="no_provider_configured",
            )
        problem_data = {}
        options = dict(payload.get("extraction_options") or {})
        await extract_questions(
            text,
            provider,
            problem_data,
            structure_mode=str(options.get("structure_mode") or "organized"),
            extraction_hint=str(options.get("extraction_hint") or ""),
            confirmed_candidates=list(options.get("confirmed_candidates") or []),
            manage_progress_lifecycle=False,
        )
        body = json.dumps(
            problem_data,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        artifact = file_repository.save_file(
            storage=get_storage(),
            owner_id=operation.owner_id,
            kind="problem_extraction_structure",
            original_name=artifact_name(
                operation.operation_id,
                operation.attempt,
                "structured_problems",
                "json",
            ),
            content=body,
            content_type="application/json",
            assignment_id=operation.assignment_id,
        )
        structured_artifact_id = artifact.id
        artifact_refs = [str(text_artifact_id), artifact.id]
        checkpoint = {
            "text_artifact_id": text_artifact_id,
            "structured_artifact_id": artifact.id,
        }
        await operation.checkpoint(
            stage="problems_structured",
            checkpoint=checkpoint,
            artifact_refs=artifact_refs,
        )
    await operation.update_progress(_stage_progress("validating_questions", 3))

    commit(
        operation.assignment_id,
        operation.owner_id,
        problem_data,
        source.original_name,
        expected_workflow_revision=(
            int(payload.get("base_workflow_revision") or 0) + 1
        ),
        replace_confirmed=bool(payload.get("replace_confirmed")),
        operation_id=operation.operation_id,
        expected_operation_attempt=operation.attempt,
        expected_lease_token=operation.lease_token,
        operation_progress=_stage_progress("completed", 4),
        operation_checkpoint=checkpoint,
        operation_artifact_refs=artifact_refs,
    )


def _find_stage_artifact(operation, *, stage: str, extension: str, kind: str):
    expected_name = artifact_name(
        operation.operation_id, operation.attempt, stage, extension
    )
    matches = [
        item
        for item in file_repository.list_files(
            owner_id=operation.owner_id,
            assignment_id=operation.assignment_id,
        )
        if item.kind == kind and item.original_name == expected_name
    ]
    return max(matches, key=lambda item: item.created_at) if matches else None


def _owned_artifact(file_id: object, *, owner_id: str, assignment_id: str):
    if not isinstance(file_id, str) or not file_id:
        return None
    artifact = file_repository.get_file(file_id=file_id, owner_id=owner_id)
    if artifact is None or artifact.assignment_id != assignment_id:
        raise NotFound("stored_file")
    return artifact


def _read_text_artifact(
    file_id: object, *, owner_id: str, assignment_id: str
) -> str | None:
    artifact = _owned_artifact(
        file_id, owner_id=owner_id, assignment_id=assignment_id
    )
    if artifact is None:
        return None
    with get_storage().open(artifact.storage_key) as stream:
        return stream.read().decode("utf-8")


def _read_json_artifact(
    file_id: object, *, owner_id: str, assignment_id: str
) -> dict[str, dict] | None:
    artifact = _owned_artifact(
        file_id, owner_id=owner_id, assignment_id=assignment_id
    )
    if artifact is None:
        return None
    with get_storage().open(artifact.storage_key) as stream:
        value = json.loads(stream.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("problem extraction artifact must be an object")
    return value
