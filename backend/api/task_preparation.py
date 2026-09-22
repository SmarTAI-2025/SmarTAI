"""Question-preparation and material-completion task façade endpoints.

Source drafts and candidate plans are durable, TTL-bounded workflow-operation
rows.  Confirmed question fields are committed only to normalized assignment
question records.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, field_validator

from backend.agents.ingest_agent import (
    AICompletionCandidateOutput,
    MaterialImportCandidateOutput,
    generate_missing_question_materials,
    parse_material_import_to_candidates,
    parse_reference_to_per_question,
    parse_test_cases_to_per_question,
)
from backend.agents.question_preparation_agent import (
    QUESTION_PREPARATION_STAGE_SEQUENCE,
    prepare_ocr_question_packages,
    prepare_question_packages,
    provider_submission_is_uncertain,
    requested_major_question_materials,
)
from backend.api.errors import domain_error_response
from backend.auth import require_teacher
from backend.db import (
    assignment_repository,
    file_repository,
    source_outcome_repository,
    workflow_repository,
)
from backend.domain.errors import (
    DomainError,
    InvalidTransition,
    LeaseLost,
    NotFound,
    ValidationError,
    VersionConflict,
)
from backend.llm.registry import (
    ExpertRegistry,
    get_scoped_expert_registry,
)
from backend.knowledge.service import ingest_document
from backend.models import (
    ProblemSourceDraft,
    QuestionScorePolicy,
    User,
    is_programming_question_type,
)
from backend.progress.tracker import get_or_create_reporter, get_reporter, remove_reporter
from backend.services import source_files as source_file_service
from backend.services import task_facade
from backend.services.stage_provider_routing import (
    StageProviderRoute,
    build_owner_baidu_ocr_skill,
    list_stage_provider_options,
    resolve_stage_provider_route,
    stage_provider_configuration_fingerprint,
)
from backend.storage import StorageObjectNotFound, get_storage
from backend.services.background_errors import (
    classify_background_error,
    safe_background_error_code,
)
from backend.services.question_preparation_artifacts import (
    QUESTIONS_EXTRACTED_STAGE,
    UPLOADED_MATERIALS_ALIGNED_STAGE,
    find_base_preparation_artifact,
    find_final_question_packages_artifact,
    find_question_candidate_artifact,
    read_base_preparation_artifact,
    read_final_question_packages_artifact,
    read_question_candidate_artifact,
    save_base_preparation_artifact,
    save_final_question_packages_artifact,
    save_question_candidate_artifact,
)
from backend.skills.ocr_ingest import LLMVisionOCRSkill, OCRPurpose
from backend.tools.file_processing import (
    IMAGE_MEDIA_TYPES,
    extract_text_from_upload,
    inspect_upload_content,
    inspect_baidu_ocr_upload,
)


router = APIRouter(prefix="/tasks", tags=["task-preparation"])
logger = logging.getLogger(__name__)

MAX_SOURCE_BYTES = 5 * 1024 * 1024
MAX_SOURCE_CHARACTERS = 400_000
SOURCE_TTL_SECONDS = 2 * 60 * 60

_DOCUMENT_SOURCE_EXTENSIONS = (".pdf", ".txt", ".md", ".markdown")
_IMAGE_SOURCE_EXTENSIONS = tuple(IMAGE_MEDIA_TYPES)
_VISION_SOURCE_ROLES = frozenset({"problem", "reference_answer", "rubric"})
_SOURCE_ROLE_EXTENSIONS = {
    "problem": _DOCUMENT_SOURCE_EXTENSIONS,
    "reference_answer": _DOCUMENT_SOURCE_EXTENSIONS,
    "rubric": _DOCUMENT_SOURCE_EXTENSIONS,
    "programming_tests": (*_DOCUMENT_SOURCE_EXTENSIONS, ".json"),
}
_SOURCE_ROLE_OCR_PURPOSE: dict[str, OCRPurpose] = {
    "problem": "problems",
    "reference_answer": "reference",
    "rubric": "problems",
    "programming_tests": "test_cases",
}


def _question_preparation_failure_code(exc: Exception) -> str:
    """Return a stable, non-sensitive code for a background preparation failure."""
    return classify_background_error(exc, "problem_extraction_failed")


def _failed_question_preparation_replay_response(operation):
    """Project an exact failed replay without republishing the provider work."""

    checkpoint = dict(operation.checkpoint or {})
    submission_uncertain = (
        operation.error_code == "provider_submit_uncertain"
        or bool(checkpoint.get("base_provider_inflight_stage"))
        or bool(checkpoint.get("provider_inflight_question_ids"))
    )
    code = (
        "provider_submit_uncertain"
        if submission_uncertain
        else safe_background_error_code(
            operation.error_code,
            "problem_extraction_failed",
        )
    )
    message = (
        "The previous provider submission state is uncertain and was not replayed."
        if submission_uncertain
        else "The previous question preparation failed; use the explicit retry action."
    )
    return domain_error_response(InvalidTransition(message, code=code))


def _provider_http_status_for_code(code: str) -> int:
    if code in {"provider_rate_limited", "media_inspection_busy"}:
        return status.HTTP_429_TOO_MANY_REQUESTS
    if code in {
        "provider_timeout",
        "provider_unreachable",
        "provider_unavailable",
        "provider_submit_uncertain",
        "media_inspection_unavailable",
        "media_inspection_timeout",
    }:
        return status.HTTP_503_SERVICE_UNAVAILABLE
    if code in {
        "provider_vision_not_supported",
        "provider_model_not_found",
        "provider_request_rejected",
        "provider_auth_failed",
        "provider_permission_denied",
        "provider_quota_exceeded",
        "ocr_input_invalid",
        "ocr_unsupported_file",
        "ocr_file_too_large",
        "ocr_image_dimension_limit_exceeded",
        "pdf_page_limit_exceeded",
    }:
        return status.HTTP_422_UNPROCESSABLE_ENTITY
    return status.HTTP_502_BAD_GATEWAY


_SOURCE_MIME_TYPES = {
    ".pdf": frozenset({"application/pdf", "application/x-pdf"}),
    ".txt": frozenset({"text/plain"}),
    ".md": frozenset({"text/markdown", "text/plain"}),
    ".markdown": frozenset({"text/markdown", "text/plain"}),
    ".json": frozenset({"application/json", "text/json", "text/plain"}),
    ".jpg": frozenset({"image/jpeg"}),
    ".jpeg": frozenset({"image/jpeg"}),
    ".png": frozenset({"image/png"}),
    ".bmp": frozenset({"image/bmp"}),
    ".tif": frozenset({"image/tiff"}),
    ".tiff": frozenset({"image/tiff"}),
    ".webp": frozenset({"image/webp"}),
}


def _accepted_source_extensions(role: str, *, has_vision: bool) -> list[str]:
    base = _SOURCE_ROLE_EXTENSIONS.get(role)
    if base is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_source_role", "role": role},
        )
    accepted = list(base)
    # Image inputs stay selectable even when the current model is text-only so
    # the backend can return the precise, recoverable model-capability error.
    if role in _VISION_SOURCE_ROLES:
        accepted.extend(_IMAGE_SOURCE_EXTENSIONS)
    return accepted


def _material_import_source_role(targets: list[str]) -> str:
    """Choose the strictest upload policy for a combined material import."""

    if "test_cases" in targets:
        return "programming_tests"
    if targets == ["reference_answer"]:
        return "reference_answer"
    if targets == ["criterion"]:
        return "rubric"
    return "problem"


def _source_role_ocr_purpose(role: str) -> OCRPurpose:
    """Keep OCR instructions aligned with the uploaded material's role."""

    try:
        return _SOURCE_ROLE_OCR_PURPOSE[role]
    except KeyError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_source_role", "role": role},
        ) from exc


def _validate_source_upload(
    file: UploadFile,
    *,
    role: str,
    has_vision: bool,
    vision_error_code: str = "vision_provider_required",
    enforce_vision: bool = True,
) -> str:
    """Validate role, extension, and declared MIME before reading upload bytes."""

    filename = Path(file.filename or "source").name
    extension = Path(filename.lower()).suffix
    accepted = _accepted_source_extensions(role, has_vision=has_vision)
    if (
        extension in _IMAGE_SOURCE_EXTENSIONS
        and role in _VISION_SOURCE_ROLES
        and not has_vision
        and enforce_vision
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": vision_error_code,
                "role": role,
                "filename": filename,
                "recovery": "configure_vision_provider",
            },
        )
    if extension not in accepted:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail={
                "code": "source_type_not_allowed",
                "role": role,
                "filename": filename,
                "accepted_extensions": accepted,
            },
        )

    declared_mime = (file.content_type or "").split(";", 1)[0].strip().lower()
    allowed_mimes = _SOURCE_MIME_TYPES[extension]
    if (
        declared_mime
        and declared_mime != "application/octet-stream"
        and declared_mime not in allowed_mimes
    ):
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail={
                "code": "source_mime_type_not_allowed",
                "role": role,
                "filename": filename,
                "content_type": declared_mime,
            },
        )
    return extension


def _stable_vision_error(
    exc: HTTPException,
    *,
    role: str,
    filename: str,
    vision_error_code: str,
    stored_file_id: str | None = None,
) -> None:
    detail = exc.detail
    structured_code = detail.get("code") if isinstance(detail, dict) else None
    if structured_code in {
        "vision_provider_required",
        "provider_vision_not_supported",
    } or (
        exc.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        and isinstance(detail, str)
        and "requires OCR" in detail
    ):
        public_detail = {
            "code": vision_error_code,
            "role": role,
            "filename": filename,
            "recovery": (
                "choose_another_provider"
                if vision_error_code == "provider_vision_not_supported"
                else "configure_vision_provider"
            ),
        }
        if stored_file_id is not None:
            public_detail["stored_file_id"] = stored_file_id
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=public_detail,
        ) from exc
    raise exc


class StartQuestionPreparationRequest(BaseModel):
    source_tokens: list[str] = Field(min_length=1, max_length=20)
    expected_workflow_revision: int = Field(ge=0)
    replace_confirmed: bool = False
    generation_policy: Literal["complete_required_materials"] = "complete_required_materials"
    score_policy: QuestionScorePolicy = Field(default_factory=QuestionScorePolicy)
    recognition_provider_id: str | None = Field(default=None, max_length=240)

    @field_validator("source_tokens")
    @classmethod
    def _require_unique_source_tokens(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("source_tokens must be unique")
        return value


class RetryQuestionPreparationRequest(BaseModel):
    recognition_provider_id: str | None = Field(default=None, max_length=240)
    expected_workflow_revision: int = Field(ge=0)


def _question_preparation_input_hash(
    *,
    ordered_source_inputs: Sequence[Mapping[str, Any]],
    logical_input_revision: int,
    replace_confirmed: bool,
    generation_policy: str,
    score_policy: Mapping[str, Any],
    recognition_provider_id: str,
    provider_configuration_fingerprint: str,
) -> str:
    """Hash the ordered logical input independently of retry claim revisions."""

    return hashlib.sha256(
        json.dumps(
            {
                "sources": list(ordered_source_inputs),
                "base_revision": logical_input_revision,
                "replace_confirmed": replace_confirmed,
                "generation_policy": generation_policy,
                "score_policy": dict(score_policy),
                "recognition_provider_id": recognition_provider_id,
                "provider_configuration_fingerprint": (
                    provider_configuration_fingerprint
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _resolve_recognition_provider(
    *,
    owner_id: str,
    registry: ExpertRegistry,
    requested_provider_id: str | None,
) -> tuple[str, Any]:
    route = resolve_stage_provider_route(
        owner_id=owner_id,
        registry=registry,
        requested_route_id=requested_provider_id,
    )
    return route.route_id, route


class StartMaterialImportRequest(BaseModel):
    source_token: str = Field(min_length=1, max_length=128)


class ApplyMaterialImportRequest(BaseModel):
    accepted_candidate_ids: list[str] = Field(default_factory=list, max_length=200)
    overwrite_candidate_ids: list[str] = Field(default_factory=list, max_length=200)
    expected_workflow_revision: int = Field(ge=0)


class ConfirmAICompletionRequest(BaseModel):
    target_ids: list[str] = Field(min_length=1, max_length=200)
    expected_workflow_revision: int = Field(ge=0)
    test_case_count: int = Field(default=6, ge=1, le=12)


@router.get("/{task_id}/question-preparation/capabilities")
def question_preparation_capabilities(
    task_id: str,
    current: User = Depends(require_teacher),
    registry: ExpertRegistry = Depends(get_scoped_expert_registry),
):
    try:
        assignment_repository.get_assignment(task_id, actor_id=current.id)
    except DomainError as exc:
        return domain_error_response(exc)
    has_vision = registry.pick_vision() is not None or any(
        item.get("provider_kind") == "ocr"
        for item in list_stage_provider_options(current.id, registry)
    )
    common = {
        "course_library": True,
        "inline_text": True,
    }
    return {
        "contract_version": 2,
        "operation": "question_preparation",
        "stage_sequence": list(QUESTION_PREPARATION_STAGE_SEQUENCE),
        "source_roles": {
            "problem": {
                **common,
                "accepted_extensions": _accepted_source_extensions(
                    "problem", has_vision=has_vision
                ),
            },
            "reference_answer": {
                **common,
                "accepted_extensions": _accepted_source_extensions(
                    "reference_answer", has_vision=has_vision
                ),
            },
            "rubric": {
                **common,
                "accepted_extensions": _accepted_source_extensions(
                    "rubric", has_vision=has_vision
                ),
            },
            "programming_tests": {
                **common,
                "accepted_extensions": _accepted_source_extensions(
                    "programming_tests", has_vision=has_vision
                ),
            },
        },
        "reader": {
            "selectable_text_pdf": True,
            "plain_text": True,
            "markdown": True,
            "json_programming_tests": True,
            "ocr": has_vision,
            "vision": has_vision,
            "scanned_pdf": has_vision,
            "images": has_vision,
            "docx": False,
        },
        "limits": {
            "max_file_bytes": MAX_SOURCE_BYTES,
            "max_text_characters": MAX_SOURCE_CHARACTERS,
            "max_inline_rubric_characters": 12_000,
        },
        "score_policy": {
            "supported_modes": ["default_10", "uniform", "per_question"],
            "default_mode": "default_10",
            "default_max_score": 10,
            "maximum_max_score": 10_000,
            "per_question_text_max_characters": 12_000,
            "rubric_weight_format": "percentage",
        },
    }


@router.get("/{task_id}/problem-sources/library")
def problem_source_library(
    task_id: str,
    scope: Literal["course", "all"] = "course",
    q: str | None = None,
    current: User = Depends(require_teacher),
):
    try:
        task = task_facade.get_task(task_id=task_id, owner_id=current.id, full=False)
        from backend.db import course_library_repository

        materials = course_library_repository.list_materials(
            owner_id=current.id,
            course_id=task.get("course_id") if scope == "course" else None,
            query=q or None,
        )
    except ImportError:
        materials = []
    except DomainError as exc:
        return domain_error_response(exc)
    items = [
        {
            "material_id": material.material_id,
            "filename": material.filename,
            "course_id": material.course_id,
            "content_type": material.content_type,
            "size_bytes": material.size_bytes,
            "created_at": material.created_at,
        }
        for material in materials
    ]
    return {"items": items, "total": len(items), "scope": scope}


@router.post("/{task_id}/problem-sources/preflight")
@router.post("/{task_id}/question-preparation/sources/preflight")
async def preflight_problem_source(
    task_id: str,
    file: UploadFile | None = File(default=None),
    library_material_id: str | None = Form(default=None),
    stored_file_id: str | None = Form(default=None),
    inline_text: str | None = Form(default=None),
    structure_mode: str = Form(default="organized"),
    role: str = Form(default="problem"),
    extraction_hint: str = Form(default=""),
    save_to_library: bool = Form(default=False),
    recognition_provider_id: str | None = Form(default=None),
    current: User = Depends(require_teacher),
    registry: ExpertRegistry = Depends(get_scoped_expert_registry),
):
    try:
        stored_file_id = (
            stored_file_id.strip()
            if isinstance(stored_file_id, str) and stored_file_id.strip()
            else None
        )
        recognition_provider_id = (
            recognition_provider_id.strip()
            if isinstance(recognition_provider_id, str)
            and recognition_provider_id.strip()
            else None
        )
        workflow = workflow_repository.get_workflow(task_id, owner_id=current.id)
        resolved_provider_id, route = _resolve_recognition_provider(
            owner_id=current.id,
            registry=registry,
            requested_provider_id=recognition_provider_id,
        )
        descriptor = await _select_source(
            file=file,
            library_material_id=library_material_id,
            stored_file_id=stored_file_id,
            inline_text=inline_text,
            owner_id=current.id,
            registry=registry,
            role=role,
            provider=route.provider,
            document_ocr_available=route.is_baidu_ocr,
            task_id=task_id,
        )
        provisional_payload = {
            "filename": descriptor["filename"],
            "content_type": descriptor.get("content_type"),
            "size_bytes": descriptor["size_bytes"],
            "sha256": descriptor["sha256"],
            "source_kind": descriptor["kind"],
            "library_material_id": descriptor.get("library_material_id"),
            "structure_mode": structure_mode,
            "role": role,
            "extraction_hint": extraction_hint,
            "save_to_library": save_to_library,
            "base_workflow_revision": workflow.workflow_revision,
            "recognition_provider_id": resolved_provider_id,
        }
        input_hash = _source_fingerprint(provisional_payload)
        replay = task_facade.find_task_operation(
            task_id=task_id,
            owner_id=current.id,
            operation_type="problem_source",
            input_hash=input_hash,
        )
        if replay is not None and not task_facade._operation_is_retryable(replay):
            if replay.status == "ready" and replay.payload.get("text"):
                return _problem_source_preflight_response(
                    operation=replay,
                    workflow_revision=workflow.workflow_revision,
                )
            if replay.status == "error":
                code = str(replay.error_code or "problem_source_parse_failed")
                detail: dict[str, Any] = {"code": code}
                replay_ref = dict((replay.payload or {}).get("source_ref") or {})
                if not replay_ref:
                    checkpoint_refs = list(
                        (replay.checkpoint or {}).get("source_refs") or []
                    )
                    if checkpoint_refs and isinstance(checkpoint_refs[0], dict):
                        replay_ref = dict(checkpoint_refs[0])
                if isinstance(replay_ref.get("stored_file_id"), str):
                    detail["stored_file_id"] = replay_ref["stored_file_id"]
                raise HTTPException(_provider_http_status_for_code(code), detail=detail)
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={"code": "problem_source_preflight_processing"},
            )
        operation, created = workflow_repository.create_operation(
            assignment_id=task_id,
            owner_id=current.id,
            operation_type="problem_source",
            input_hash=input_hash,
            payload=provisional_payload,
            expires_at=time.time() + SOURCE_TTL_SECONDS,
            initial_status="preparing",
        )
        if not created:
            if operation.status == "ready" and operation.payload.get("text"):
                return _problem_source_preflight_response(
                    operation=operation,
                    workflow_revision=workflow.workflow_revision,
                )
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={"code": "problem_source_preflight_processing"},
            )

        source_ref = {
            "role": role,
            "source_kind": descriptor["kind"],
            "display_name": descriptor["filename"],
            "source_id": None,
            "stored_file_id": descriptor.get("stored_file_id"),
            "source_operation_id": operation.id,
            "source_attempt": operation.attempt,
            "library_material_id": descriptor.get("library_material_id"),
            "knowledge_document_id": descriptor.get("knowledge_document_id"),
        }
        artifact_refs: list[str] = []
        stored_created = False
        stored = descriptor.get("_stored")
        if descriptor["kind"] == "upload":
            try:
                if stored is None:
                    stored, stored_created = source_file_service.persist_problem_source(
                        storage=get_storage(),
                        owner_id=current.id,
                        task_id=task_id,
                        original_name=descriptor["filename"],
                        content=descriptor["_body"],
                        content_type=descriptor["content_type"],
                    )
                source, _ = source_outcome_repository.register_source(
                    owner_id=current.id,
                    assignment_id=task_id,
                    operation_id=operation.id,
                    expected_attempt=operation.attempt,
                    order_index=0,
                    stored_file_id=stored.id,
                )
                source_ref.update(
                    source_id=source.id,
                    stored_file_id=stored.id,
                )
                artifact_refs.append(stored.id)
            except Exception as exc:
                if stored_created and stored is not None:
                    file_repository.delete_unlinked_file(
                        storage=get_storage(),
                        file_id=stored.id,
                        owner_id=current.id,
                        assignment_id=task_id,
                    )
                _mark_problem_source_failed(
                    operation=operation,
                    owner_id=current.id,
                    error_code="source_persistence_failed",
                )
                logger.warning(
                    "Problem source persistence failed; operation_id=%s exception_type=%s",
                    operation.id,
                    type(exc).__name__,
                )
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={"code": "source_persistence_failed"},
                ) from None

        try:
            operation = workflow_repository.save_operation_checkpoint(
                operation.id,
                owner_id=current.id,
                expected_attempt=operation.attempt,
                expected_checkpoint_revision=operation.checkpoint_revision,
                stage="problem_source_saved",
                checkpoint={"source_refs": [source_ref]},
                artifact_refs=artifact_refs,
            )
            operation = workflow_repository.update_operation(
                operation.id,
                owner_id=current.id,
                expected_attempt=operation.attempt,
                payload={**provisional_payload, "source_ref": source_ref},
            )
            if route.is_baidu_ocr and descriptor["kind"] == "upload":
                if stored is None:
                    raise RuntimeError("source_persistence_failed")
                text, operation = await _run_durable_question_source_ocr(
                    operation=operation,
                    body=descriptor["_body"],
                    filename=descriptor["filename"],
                    content_type=descriptor.get("content_type"),
                    stored_source_id=stored.id,
                    task_id=task_id,
                    owner_id=current.id,
                    route=route,
                    role=role,
                )
            else:
                text = await _extract_selected_source(
                    descriptor=descriptor,
                    registry=registry,
                    role=role,
                    provider=route.provider,
                    vision_error_code=(
                        "provider_vision_not_supported"
                        if route.provider is not None
                        else "vision_provider_required"
                    ),
                    stored_file_id=stored.id if stored is not None else None,
                )
        except HTTPException as exc:
            detail = dict(exc.detail) if isinstance(exc.detail, dict) else {}
            if stored is not None and "stored_file_id" not in detail:
                detail["stored_file_id"] = stored.id
            _mark_problem_source_failed(
                operation=operation,
                owner_id=current.id,
                error_code=str(detail.get("code") or "problem_source_parse_failed"),
            )
            if detail != exc.detail:
                raise HTTPException(exc.status_code, detail=detail) from exc
            raise
        except DomainError as exc:
            _mark_problem_source_failed(
                operation=operation,
                owner_id=current.id,
                error_code=task_facade._detail_error(
                    exc, "problem_source_parse_failed"
                ),
            )
            raise
        except Exception as exc:
            error_code = _question_preparation_failure_code(exc)
            _mark_problem_source_failed(
                operation=operation,
                owner_id=current.id,
                error_code=error_code,
            )
            logger.warning(
                "Problem source extraction failed; operation_id=%s error_code=%s exception_type=%s",
                operation.id,
                error_code,
                type(exc).__name__,
            )
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": error_code,
                    **(
                        {"stored_file_id": stored.id}
                        if stored is not None
                        else {}
                    ),
                },
            ) from None

        try:
            saved_material = await _save_source_to_library(
                save=save_to_library,
                descriptor=descriptor,
                owner_id=current.id,
                task_id=task_id,
                role=role,
                existing_material_id=library_material_id,
            )
        except DomainError as exc:
            _mark_problem_source_failed(
                operation=operation,
                owner_id=current.id,
                error_code=task_facade._detail_error(
                    exc, "problem_source_library_save_failed"
                ),
            )
            raise
        except Exception as exc:
            _mark_problem_source_failed(
                operation=operation,
                owner_id=current.id,
                error_code="problem_source_library_save_failed",
            )
            logger.warning(
                "Problem source library save failed; operation_id=%s exception_type=%s",
                operation.id,
                type(exc).__name__,
            )
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "problem_source_library_save_failed"},
            ) from None
        effective_material_id = (
            library_material_id
            or (saved_material or {}).get("material_id")
        )
        candidates = _detect_candidates(text)
        payload = {
            **provisional_payload,
            "text": text,
            "library_material_id": effective_material_id,
            "candidates": candidates,
            "source_ref": source_ref,
            "saved_material": saved_material,
        }
        operation = workflow_repository.update_operation(
            operation.id,
            owner_id=current.id,
            expected_attempt=operation.attempt,
            status="ready",
            payload=payload,
        )
        return _problem_source_preflight_response(
            operation=operation,
            workflow_revision=workflow.workflow_revision,
        )
    except DomainError as exc:
        return domain_error_response(exc)


def _mark_problem_source_failed(*, operation, owner_id: str, error_code: str) -> None:
    try:
        workflow_repository.update_operation(
            operation.id,
            owner_id=owner_id,
            expected_attempt=operation.attempt,
            status="error",
            error_code=error_code[:128],
            completed_at=time.time(),
        )
    except Exception:
        return


def _problem_source_preflight_response(*, operation, workflow_revision: int) -> dict:
    payload = dict(operation.payload or {})
    source_ref = dict(payload.get("source_ref") or {})
    candidates = list(payload.get("candidates") or [])
    structure_mode = str(payload.get("structure_mode") or "organized")
    return {
        "status": "ready",
        "source_token": operation.id,
        "source": {
            "kind": payload.get("source_kind"),
            "filename": payload.get("filename"),
            "size_bytes": payload.get("size_bytes"),
            "sha256": payload.get("sha256"),
            "library_material_id": payload.get("library_material_id"),
            "stored_file_id": source_ref.get("stored_file_id"),
        },
        "role": payload.get("role", "problem"),
        "structure_mode": structure_mode,
        "requires_confirmation": (
            structure_mode == "extract_from_source" and bool(candidates)
        ),
        "candidate_summary": {
            "matched": candidates if structure_mode == "organized" else [],
            "possible_matches": candidates if structure_mode != "organized" else [],
            "not_found": [],
            "semantic_match_performed": False,
            "notice": None,
        },
        "base_workflow_revision": payload.get("base_workflow_revision", 0),
        "workflow_revision": workflow_revision,
        "recognition_provider_id": payload.get("recognition_provider_id"),
        "saved_material": payload.get("saved_material"),
    }


async def _select_source(
    *, file: UploadFile | None, library_material_id: str | None,
    stored_file_id: str | None = None,
    inline_text: str | None, owner_id: str, registry: ExpertRegistry,
    role: str,
    provider=None,
    document_ocr_available: bool = False,
    task_id: str | None = None,
) -> dict:
    _accepted_source_extensions(
        role,
        has_vision=(
            document_ocr_available
            or (provider is not None and registry.pick_vision(provider) is not None)
        ),
    )
    selected = (
        int(file is not None)
        + int(bool(stored_file_id))
        + int(bool(library_material_id))
        + int(bool((inline_text or "").strip()))
    )
    if selected != 1:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "exactly_one_source_required"})
    if file is not None or stored_file_id:
        if provider is None and not document_ocr_available:
            provider = registry.pick_default()
        vision = registry.pick_vision(provider) if provider is not None else None
        vision_error_code = (
            "provider_vision_not_supported"
            if provider is not None
            else "vision_provider_required"
        )
        stored = None
        source_upload = file
        if stored_file_id:
            if task_id is None:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={"code": "stored_source_task_required"},
                )
            stored = await run_in_threadpool(
                file_repository.get_file,
                file_id=stored_file_id,
                owner_id=owner_id,
            )
            if (
                stored is None
                or stored.assignment_id != task_id
                or stored.kind != "problem_source"
            ):
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND,
                    detail={"code": "stored_source_not_found"},
                )

            class _StoredUpload:
                filename = stored.original_name
                content_type = stored.content_type

            source_upload = _StoredUpload()
        assert source_upload is not None
        _validate_source_upload(
            source_upload,
            role=role,
            has_vision=vision is not None or document_ocr_available,
            vision_error_code=vision_error_code,
            enforce_vision=False,
        )
        if stored is not None:
            storage = get_storage()

            def _read_stored_source() -> bytes:
                with storage.open(stored.storage_key) as stream:
                    return stream.read(MAX_SOURCE_BYTES + 1)

            try:
                body = await run_in_threadpool(_read_stored_source)
            except StorageObjectNotFound:
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND,
                    detail={"code": "stored_source_not_found"},
                ) from None
            except Exception:
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={"code": "source_preview_storage_unavailable"},
                ) from None
        else:
            assert file is not None
            body = await file.read(MAX_SOURCE_BYTES + 1)
        if not body:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail={"code": "source_empty"})
        if len(body) > MAX_SOURCE_BYTES:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail={"code": "source_too_large", "max_bytes": MAX_SOURCE_BYTES},
            )
        filename = source_file_service.safe_display_name(
            source_upload.filename or "source"
        )
        extension = Path(filename.lower()).suffix
        detected = (
            source_file_service.detected_preview_mime(body[:4096], filename)
            if extension in {".pdf", *_IMAGE_SOURCE_EXTENSIONS}
            else inspect_upload_content(
                body,
                filename,
                source_upload.content_type,
            ).content_type
        )
        if detected not in _SOURCE_MIME_TYPES[extension]:
            raise HTTPException(
                status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail={
                    "code": "source_content_type_not_allowed",
                    "role": role,
                    "filename": filename,
                },
            )
        descriptor = {
            "kind": "upload", "filename": filename,
            "size_bytes": len(body), "content_type": detected,
            "sha256": hashlib.sha256(body).hexdigest(),
            "stored_file_id": stored.id if stored is not None else None,
            "_body": body,
            "_vision": vision,
            "_vision_error_code": vision_error_code,
            "_stored": stored,
        }
    elif library_material_id:
        from backend.db import course_library_repository

        material = course_library_repository.get_material(
            material_id=library_material_id, owner_id=owner_id
        )
        if material is None:
            raise NotFound("course_material")
        descriptor = {
            "kind": "library", "filename": material.filename,
            "size_bytes": material.size_bytes,
            "content_type": material.content_type,
            "sha256": material.sha256,
            "library_material_id": material.material_id,
            "knowledge_document_id": material.document_id,
            "stored_file_id": material.stored_file_id,
        }
    else:
        text = (inline_text or "").strip()
        descriptor = {
            "kind": "inline_text", "filename": "inline-text.txt",
            "size_bytes": len(text.encode("utf-8")), "content_type": "text/plain",
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "_body": text.encode("utf-8"),
            "_text": text,
        }
    return descriptor


async def _extract_selected_source(
    *,
    descriptor: dict,
    registry: ExpertRegistry,
    role: str,
    provider=None,
    vision_error_code: str | None = None,
    stored_file_id: str | None = None,
) -> str:
    if descriptor["kind"] == "upload":
        vision = descriptor.get("_vision")
        resolved_vision_error = (
            vision_error_code
            or descriptor.get("_vision_error_code")
            or (
                "provider_vision_not_supported"
                if provider is not None
                else "vision_provider_required"
            )
        )
        filename = descriptor["filename"]
        if (
            Path(filename.lower()).suffix in _IMAGE_SOURCE_EXTENSIONS
            and role in _VISION_SOURCE_ROLES
            and vision is None
        ):
            detail: dict[str, Any] = {
                "code": resolved_vision_error,
                "role": role,
                "filename": filename,
                "recovery": (
                    "choose_another_provider"
                    if resolved_vision_error == "provider_vision_not_supported"
                    else "configure_vision_provider"
                ),
            }
            if stored_file_id is not None:
                detail["stored_file_id"] = stored_file_id
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)
        ocr_skill = LLMVisionOCRSkill(vision) if vision is not None else None
        try:
            text = await extract_text_from_upload(
                descriptor["_body"],
                filename,
                ocr_skill=ocr_skill,
                purpose=_source_role_ocr_purpose(role),
                reporter=None,
            )
        except HTTPException as exc:
            _stable_vision_error(
                exc,
                role=role,
                filename=filename,
                vision_error_code=resolved_vision_error,
                stored_file_id=stored_file_id,
            )
    elif descriptor["kind"] == "library":
        from backend.db.knowledge_repository import list_chunks

        chunks = list_chunks([descriptor["knowledge_document_id"]])
        text = "\n\n".join(chunk.content for chunk in chunks)
    else:
        text = str(descriptor.get("_text") or "")
    text = text.strip()
    if not text:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail={"code": "source_empty"})
    if len(text) > MAX_SOURCE_CHARACTERS:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail={"code": "source_text_too_large"})
    return text


async def _read_source(
    *, file: UploadFile | None, library_material_id: str | None,
    stored_file_id: str | None = None,
    inline_text: str | None, owner_id: str, registry: ExpertRegistry,
    role: str, provider=None, task_id: str | None = None,
) -> tuple[str, dict]:
    """Compatibility reader for auxiliary material paths.

    The formal problem-source preflight uses ``_select_source`` directly so it
    can persist upload bytes and their source relation before this extraction.
    """

    descriptor = await _select_source(
        file=file,
        library_material_id=library_material_id,
        stored_file_id=stored_file_id,
        inline_text=inline_text,
        owner_id=owner_id,
        registry=registry,
        role=role,
        provider=provider,
        task_id=task_id,
    )
    text = await _extract_selected_source(
        descriptor=descriptor,
        registry=registry,
        role=role,
        provider=provider,
        vision_error_code=(
            "provider_vision_not_supported"
            if provider is not None
            else "vision_provider_required"
        ),
        stored_file_id=descriptor.get("stored_file_id"),
    )
    return text, descriptor


async def _save_source_to_library(
    *, save: bool, descriptor: dict, owner_id: str, task_id: str,
    role: str, existing_material_id: str | None,
) -> dict | None:
    if not save:
        return None
    from backend.db import course_library_repository

    if existing_material_id:
        material = course_library_repository.get_material(
            existing_material_id, owner_id
        )
        if material is None:
            raise NotFound("course_material")
        return {**material.public(), "created": False}

    body = descriptor.get("_body")
    if not isinstance(body, bytes) or not body:
        raise InvalidTransition("library_source_bytes_unavailable")
    filename = Path(str(descriptor.get("filename") or "source.txt")).name
    document = await ingest_document(
        owner_id=owner_id,
        original_name=filename,
        content=body,
        content_type=descriptor.get("content_type"),
        title=Path(filename).stem,
    )
    if document.status != "ready":
        raise InvalidTransition("knowledge_document_not_ready")
    task = task_facade.get_task(task_id=task_id, owner_id=owner_id, full=False)
    category = {
        "reference_answer": "answer",
        "rubric": "rubric",
    }.get(role, "other")
    material, created = course_library_repository.create_material(
        owner_id=owner_id,
        document_id=document.id,
        filename=filename,
        category=category,
        labels=[],
        course_id=task.get("course_id"),
        group_id=None,
    )
    return {**material.public(), "created": created}


def _detect_candidates(text: str) -> list[dict]:
    pattern = re.compile(
        r"(?im)^\s*(?:question\s+|q)?(?P<number>\d+(?:\.\d+)*)\s*[.、):：]\s*(?P<title>[^\n]{0,180})"
    )
    candidates = []
    for index, match in enumerate(pattern.finditer(text[:MAX_SOURCE_CHARACTERS])):
        candidates.append({
            "candidate_id": f"source_candidate_{index + 1}",
            "question_number": match.group("number"),
            "preview": match.group("title").strip()[:180],
            "line_number": text.count("\n", 0, match.start()) + 1,
            "match_kind": "heading", "reason": "Explicit question heading",
        })
    return candidates[:200]


def _source_fingerprint(payload: dict) -> str:
    selected = {
        key: payload.get(key)
        for key in (
            "sha256", "filename", "content_type", "source_kind",
            "library_material_id", "structure_mode", "role",
            "extraction_hint", "save_to_library", "targets",
            "base_workflow_revision", "recognition_provider_id",
        )
    }
    return hashlib.sha256(json.dumps(selected, sort_keys=True).encode()).hexdigest()


def _validated_question_source_ref(
    *, operation, payload: dict, task_id: str, owner_id: str
) -> dict:
    if operation.status != "ready":
        raise InvalidTransition(
            "Problem source is not ready.", code="problem_source_not_ready"
        )
    source_kind = str(payload.get("source_kind") or "upload")
    raw_ref = payload.get("source_ref")
    if not isinstance(raw_ref, dict):
        if source_kind == "upload":
            raise InvalidTransition(
                "Problem source bytes were not persisted.",
                code="problem_source_not_persisted",
            )
        raw_ref = {}
    ref = {
        "role": str(payload.get("role") or "problem"),
        "source_kind": source_kind,
        "display_name": source_file_service.safe_display_name(
            payload.get("filename") or "source"
        ),
        "source_id": raw_ref.get("source_id"),
        "stored_file_id": raw_ref.get("stored_file_id"),
        "source_operation_id": operation.id,
        "source_attempt": operation.attempt,
        "library_material_id": payload.get("library_material_id"),
        "knowledge_document_id": raw_ref.get("knowledge_document_id"),
    }
    if source_kind == "upload":
        source_id = ref["source_id"]
        file_id = ref["stored_file_id"]
        if not isinstance(source_id, str) or not isinstance(file_id, str):
            raise InvalidTransition(
                "Problem source bytes were not persisted.",
                code="problem_source_not_persisted",
            )
        source = source_outcome_repository.get_source(
            source_id, owner_id=owner_id
        )
        stored = file_repository.get_file(file_id=file_id, owner_id=owner_id)
        if (
            source.assignment_id != task_id
            or source.operation_id != operation.id
            or source.attempt != operation.attempt
            or source.stored_file_id != file_id
            or stored is None
            or stored.assignment_id != task_id
            or stored.kind != "problem_source"
        ):
            raise NotFound("problem_source")
    elif source_kind == "library":
        from backend.db import course_library_repository

        material_id = ref["library_material_id"]
        if not isinstance(material_id, str):
            raise NotFound("problem_source")
        material = course_library_repository.get_material(material_id, owner_id)
        if material is None:
            raise NotFound("problem_source")
        ref.update(
            stored_file_id=material.stored_file_id,
            knowledge_document_id=material.document_id,
        )
        if material.stored_file_id is not None:
            stored = file_repository.get_file(
                file_id=material.stored_file_id, owner_id=owner_id
            )
            if (
                stored is None
                or stored.knowledge_document_id != material.document_id
                or stored.assignment_id is not None
            ):
                raise NotFound("problem_source")
    else:
        ref.update(source_id=None, stored_file_id=None)
    return ref


def _question_ocr_artifact_name(operation_id: str, attempt: int) -> str:
    return f"{operation_id}-attempt-{attempt}-ocr.md"


def _load_question_ocr_artifact(
    *,
    owner_id: str,
    task_id: str,
    operation_id: str,
    attempt: int,
) -> tuple[str, str] | None:
    expected_name = _question_ocr_artifact_name(operation_id, attempt)
    artifact = next((
        item
        for item in file_repository.list_files(
            owner_id=owner_id,
            assignment_id=task_id,
        )
        if item.kind == "question_ocr_text"
        and item.original_name == expected_name
    ), None)
    if artifact is None:
        return None
    with get_storage().open(artifact.storage_key) as stream:
        body = stream.read(10 * 1024 * 1024 + 1)
    if not body or len(body) > 10 * 1024 * 1024:
        return None
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return (text, artifact.id) if text.strip() else None


async def _run_durable_question_source_ocr(
    *,
    operation,
    body: bytes,
    filename: str,
    content_type: str | None,
    stored_source_id: str,
    task_id: str,
    owner_id: str,
    route: StageProviderRoute,
    role: str,
):
    """Submit one question source once and durably reuse its Markdown."""
    if not route.is_baidu_ocr:
        raise ValueError("route is not Baidu Unlimited-OCR")
    recovered = _load_question_ocr_artifact(
        owner_id=owner_id,
        task_id=task_id,
        operation_id=operation.id,
        attempt=operation.attempt,
    )
    if recovered is not None:
        return recovered[0], operation
    if (operation.checkpoint or {}).get("ocr_inflight_source_id"):
        raise RuntimeError("provider_submit_uncertain")

    # Every untrusted PDF/image check completes in the killable media worker
    # before the provider checkpoint or network call.
    await inspect_baidu_ocr_upload(
        body,
        filename,
        content_type=content_type,
    )
    try:
        operation = workflow_repository.save_operation_checkpoint(
            operation.id,
            owner_id=owner_id,
            expected_attempt=operation.attempt,
            expected_checkpoint_revision=operation.checkpoint_revision,
            stage="question_ocr_submitting",
            checkpoint={
                **dict(operation.checkpoint or {}),
                "ocr_inflight_source_id": stored_source_id,
            },
            artifact_refs=list(dict.fromkeys([
                *operation.artifact_refs,
                stored_source_id,
            ])),
        )
    except VersionConflict as exc:
        current = workflow_repository.get_operation(operation.id, owner_id=owner_id)
        recovered = _load_question_ocr_artifact(
            owner_id=owner_id,
            task_id=task_id,
            operation_id=current.id,
            attempt=current.attempt,
        )
        if recovered is not None:
            return recovered[0], current
        raise RuntimeError("provider_submit_uncertain") from exc

    skill = None
    try:
        skill = build_owner_baidu_ocr_skill(owner_id, route)
        result = await skill.recognize_document(
            body,
            filename,
            _source_role_ocr_purpose(role),
        )
        text = result.text.strip()
        if not text:
            raise RuntimeError("ocr_empty_result")
        try:
            artifact = await run_in_threadpool(
                file_repository.save_file,
                storage=get_storage(),
                owner_id=owner_id,
                kind="question_ocr_text",
                original_name=_question_ocr_artifact_name(
                    operation.id,
                    operation.attempt,
                ),
                content=text.encode("utf-8"),
                content_type="text/markdown",
                assignment_id=task_id,
            )
        except Exception as exc:
            raise RuntimeError("provider_submit_uncertain") from exc
        try:
            checkpoint = dict(operation.checkpoint or {})
            checkpoint.pop("ocr_inflight_source_id", None)
            checkpoint["ocr_completed_source_id"] = stored_source_id
            operation = workflow_repository.save_operation_checkpoint(
                operation.id,
                owner_id=owner_id,
                expected_attempt=operation.attempt,
                expected_checkpoint_revision=operation.checkpoint_revision,
                stage="question_ocr_saved",
                checkpoint=checkpoint,
                artifact_refs=list(dict.fromkeys([
                    *operation.artifact_refs,
                    artifact.id,
                ])),
            )
        except VersionConflict:
            operation = workflow_repository.get_operation(
                operation.id,
                owner_id=owner_id,
            )
        return text, operation
    except Exception as exc:
        code = classify_background_error(exc, "problem_extraction_failed")
        submission_may_exist = bool(
            getattr(exc, "submission_may_exist", True)
        )
        projected_code = (
            "provider_submit_uncertain"
            if submission_may_exist
            and code in {
                "provider_timeout",
                "provider_unreachable",
                "provider_rate_limited",
                "provider_unavailable",
            }
            else code
        )
        if not submission_may_exist:
            try:
                current = workflow_repository.get_operation(
                    operation.id,
                    owner_id=owner_id,
                )
                checkpoint = dict(current.checkpoint or {})
                checkpoint.pop("ocr_inflight_source_id", None)
                operation = workflow_repository.save_operation_checkpoint(
                    current.id,
                    owner_id=owner_id,
                    expected_attempt=current.attempt,
                    expected_checkpoint_revision=current.checkpoint_revision,
                    stage="question_ocr_failed_before_submit",
                    checkpoint=checkpoint,
                    artifact_refs=current.artifact_refs,
                )
            except Exception as checkpoint_exc:
                logger.warning(
                    "Question OCR checkpoint cleanup failed; exception_type=%s",
                    type(checkpoint_exc).__name__,
                )
        try:
            workflow_repository.update_operation(
                operation.id,
                owner_id=owner_id,
                expected_attempt=operation.attempt,
                status="error",
                error_code=projected_code,
                completed_at=time.time(),
            )
        except Exception as persistence_exc:
            logger.warning(
                "Question OCR failure state persistence failed; exception_type=%s",
                type(persistence_exc).__name__,
            )
        if projected_code != code:
            raise RuntimeError(projected_code) from exc
        raise
    finally:
        if skill is not None:
            close = getattr(skill.client, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception as close_exc:
                    logger.warning(
                        "Question OCR client close failed; exception_type=%s",
                        type(close_exc).__name__,
                    )


@router.post("/{task_id}/question-preparation/jobs")
async def start_question_preparation(
    task_id: str,
    request: StartQuestionPreparationRequest,
    background_tasks: BackgroundTasks,
    current: User = Depends(require_teacher),
    registry: ExpertRegistry = Depends(get_scoped_expert_registry),
):
    return await _start_question_preparation(
        task_id=task_id,
        request=request,
        background_tasks=background_tasks,
        current=current,
        registry=registry,
        allow_prepared_source_reuse=False,
    )


async def _start_question_preparation(
    *,
    task_id: str,
    request: StartQuestionPreparationRequest,
    background_tasks: BackgroundTasks,
    current: User,
    registry: ExpertRegistry,
    allow_prepared_source_reuse: bool,
    input_workflow_revision: int | None = None,
    retry_source_contract: Mapping[str, Any] | None = None,
):
    # Kept in the endpoint signature for API compatibility. Question
    # preparation is published only to the durable workflow worker below.
    del background_tasks
    try:
        workflow = workflow_repository.get_workflow(task_id, owner_id=current.id)
        recognition_provider_id, route = _resolve_recognition_provider(
            owner_id=current.id,
            registry=registry,
            requested_provider_id=request.recognition_provider_id,
        )
        provider_configuration_fingerprint = (
            stage_provider_configuration_fingerprint(
                owner_id=current.id,
                route=route,
                registry=registry,
            )
        )
        sources = []
        source_fingerprints = []
        job_source_refs: list[dict] = []
        job_artifact_refs: list[str] = []
        for token in request.source_tokens:
            operation = workflow_repository.get_operation(token, owner_id=current.id)
            if operation.assignment_id != task_id or operation.operation_type != "problem_source":
                raise NotFound("problem_source")
            payload = dict(operation.payload or {})
            if (
                not allow_prepared_source_reuse
                and operation.expires_at
                and operation.expires_at < time.time()
            ):
                raise InvalidTransition("Problem source expired.", code="stale_revision")
            source_ref = _validated_question_source_ref(
                operation=operation,
                payload=payload,
                task_id=task_id,
                owner_id=current.id,
            )
            draft = ProblemSourceDraft(
                source_token=operation.id, task_id=task_id, owner_id=current.id,
                role=payload.get("role", "problem"),
                source_kind=payload.get("source_kind", "upload"),
                structure_mode=payload.get("structure_mode", "organized"),
                extraction_hint=payload.get("extraction_hint", ""),
                filename=payload.get("filename", "source.txt"),
                content_type=payload.get("content_type") or "text/plain",
                size_bytes=int(payload.get("size_bytes") or 0),
                content_sha256=payload.get("sha256", operation.input_hash),
                library_material_id=payload.get("library_material_id"),
                base_workflow_revision=int(payload.get("base_workflow_revision") or 0),
                resident_bytes=len(str(payload.get("text") or "").encode("utf-8")),
                candidates=list(payload.get("candidates") or []),
                expires_at=operation.expires_at or time.time() + SOURCE_TTL_SECONDS,
            )
            source_ref["content_sha256"] = draft.content_sha256
            source_ref["prepared_text_sha256"] = hashlib.sha256(
                str(payload.get("text") or "").encode("utf-8")
            ).hexdigest()
            if (
                not allow_prepared_source_reuse
                and draft.base_workflow_revision != request.expected_workflow_revision
            ):
                raise VersionConflict(
                    "A selected problem source was prepared from an older task version.",
                    code="stale_revision",
                )
            sources.append((draft, str(payload.get("text") or ""), payload))
            source_fingerprints.append(operation.input_hash)
            job_source_refs.append(source_ref)
            if (
                source_ref["source_kind"] == "upload"
                and isinstance(source_ref.get("stored_file_id"), str)
            ):
                job_artifact_refs.append(source_ref["stored_file_id"])
        ordered_source_inputs = [
            {
                "source_token": draft.source_token,
                "source_fingerprint": fingerprint,
                "role": draft.role,
            }
            for (draft, _text, _payload), fingerprint in zip(
                sources,
                source_fingerprints,
                strict=True,
            )
        ]
        source_content_hashes = {
            draft.source_token: draft.content_sha256
            for draft, _text, _payload in sources
        }
        source_text_hashes = {
            draft.source_token: hashlib.sha256(text.encode("utf-8")).hexdigest()
            for draft, text, _payload in sources
        }
        prepared_source_provider_ids = sorted({
            str(payload.get("recognition_provider_id"))
            for _draft, _text, payload in sources
            if payload.get("recognition_provider_id")
        })
        if allow_prepared_source_reuse:
            frozen = dict(retry_source_contract or {})
            if (
                frozen.get("source_tokens") != request.source_tokens
                or frozen.get("source_refs") != job_source_refs
                or frozen.get("source_content_hashes") != source_content_hashes
                or frozen.get("source_text_hashes") != source_text_hashes
                or frozen.get("prepared_source_provider_ids")
                != prepared_source_provider_ids
            ):
                raise InvalidTransition(
                    "Prepared question sources changed before retry.",
                    code="question_preparation_retry_source_unavailable",
                )
            if (
                frozen.get("recognition_provider_id")
                != recognition_provider_id
                or frozen.get("provider_configuration_fingerprint")
                != provider_configuration_fingerprint
                or frozen.get("provider_capability")
                != ("ocr" if route.is_baidu_ocr else "text")
            ):
                raise InvalidTransition(
                    "The original question-preparation provider changed.",
                    code=(
                        "question_preparation_provider_configuration_changed"
                    ),
                )
        logical_input_revision = (
            request.expected_workflow_revision
            if input_workflow_revision is None
            else input_workflow_revision
        )
        if logical_input_revision < 0:
            raise ValidationError(
                "The question-preparation input revision is invalid.",
                code="stale_revision",
            )
        operation_hash = _question_preparation_input_hash(
            ordered_source_inputs=ordered_source_inputs,
            logical_input_revision=logical_input_revision,
            replace_confirmed=request.replace_confirmed,
            generation_policy=request.generation_policy,
            score_policy=request.score_policy.model_dump(mode="json"),
            recognition_provider_id=recognition_provider_id,
            provider_configuration_fingerprint=(
                provider_configuration_fingerprint
            ),
        )
        replay = task_facade.find_task_operation(
            task_id=task_id, owner_id=current.id,
            operation_type="question_preparation", input_hash=operation_hash,
        )
        if replay is not None:
            # A repeated start request is only an idempotency lookup. Failed
            # work may advance an attempt solely through the explicit retry
            # endpoint, which first validates its frozen prepared sources.
            if replay.status == "error" and not allow_prepared_source_reuse:
                return _failed_question_preparation_replay_response(replay)
            if not task_facade._operation_is_retryable(replay):
                if replay.status == "error":
                    return _failed_question_preparation_replay_response(replay)
                return {
                    "status": task_facade._operation_state(replay),
                    "task_id": task_id, "job_id": replay.id,
                    "workflow_revision": workflow.workflow_revision,
                }
        claim_base_revision = task_facade.retryable_operation_claim_revision(
            workflow=workflow, replay=replay,
            requested_revision=request.expected_workflow_revision,
        )
        task = task_facade.get_task(task_id=task_id, owner_id=current.id, full=False)
        if task.get("problem_count") and not request.replace_confirmed:
            task_facade._raise_replacement_confirmation_required()
        if not allow_prepared_source_reuse and any(
            payload.get("recognition_provider_id") != recognition_provider_id
            for _draft, _text, payload in sources
        ):
            raise ValidationError(
                "Problem sources were prepared with a different model.",
                code="recognition_provider_changed",
            )
        workflow, active = task_facade._ensure_no_other_active_operation(
            task_id=task_id, owner_id=current.id,
            operation_type="question_preparation", input_hash=operation_hash,
            allow_supersede=request.replace_confirmed,
        )
        if active is not None:
            return {
                "status": "already_running", "task_id": task_id,
                "job_id": active.id,
                "workflow_revision": workflow.workflow_revision,
            }
        operation_payload = {
            "contract_version": 1,
            "owner_id": current.id,
            "task_id": task_id,
            "operation_type": "question_preparation",
            "input_hash": operation_hash,
            "source_tokens": request.source_tokens,
            "source_refs": job_source_refs,
            "source_content_hashes": source_content_hashes,
            "source_text_hashes": source_text_hashes,
            "requested_workflow_revision": logical_input_revision,
            "base_workflow_revision": claim_base_revision,
            "claimed_workflow_revision": claim_base_revision + 1,
            "replace_confirmed": request.replace_confirmed,
            "generation_policy": request.generation_policy,
            "score_policy": request.score_policy.model_dump(mode="json"),
            "recognition_provider_id": recognition_provider_id,
            "provider_configuration_fingerprint": (
                provider_configuration_fingerprint
            ),
            "provider_capability": "ocr" if route.is_baidu_ocr else "text",
            "prepared_source_provider_ids": prepared_source_provider_ids,
        }
        job, published, claimed_revision = (
            task_facade.publish_checkpointed_operation_atomic(
                task_id=task_id,
                owner_id=current.id,
                operation_type="question_preparation",
                input_hash=operation_hash,
                expected_workflow_revision=claim_base_revision,
                operation_payload=operation_payload,
                initial_checkpoint_stage="sources_validated",
                initial_checkpoint={
                    "contract_version": 1,
                    "stage": "sources_validated",
                    "base_workflow_revision": claim_base_revision,
                    "claimed_workflow_revision": claim_base_revision + 1,
                    "provider_record_id": recognition_provider_id,
                    "source_content_hashes": operation_payload[
                        "source_content_hashes"
                    ],
                    "source_text_hashes": operation_payload[
                        "source_text_hashes"
                    ],
                    "source_refs": job_source_refs,
                    "source_ids": [
                        ref["source_id"]
                        for ref in job_source_refs
                        if isinstance(ref.get("source_id"), str)
                    ],
                    "stored_file_ids": [
                        ref["stored_file_id"]
                        for ref in job_source_refs
                        if isinstance(ref.get("stored_file_id"), str)
                    ],
                    "question_ids": [],
                    "completed_question_ids": [],
                    "failed_question_ids": [],
                    "provider_inflight_question_ids": [],
                    "base_provider_inflight_stage": None,
                    "question_artifact_ids": {},
                },
                artifact_refs=list(dict.fromkeys(job_artifact_refs)),
                workflow_changes={
                    "presentation_status": "extracting_problems",
                    "active_operation": "question_preparation",
                    "error_code": None,
                    "last_failed_job_id": None,
                    "question_recognition_provider_id": (
                        recognition_provider_id
                    ),
                },
                workflow_job_id_fields=("active_job_id", "extract_job_id"),
                retry_observed_operation_id=(
                    replay.id if replay is not None else None
                ),
                retry_observed_attempt=(
                    replay.attempt if replay is not None else None
                ),
            )
        )
        if not published:
            return {
                "status": task_facade._operation_state(job),
                "task_id": task_id,
                "job_id": job.id,
                "workflow_revision": claimed_revision,
            }
        remove_reporter(job.id)
        return {
            "status": "started", "task_id": task_id, "job_id": job.id,
            "source_count": len(sources), "operation": "question_preparation",
            "progress_contract_version": 1,
            "recognition_provider_id": recognition_provider_id,
            "workflow_revision": claimed_revision,
        }
    except DomainError as exc:
        return domain_error_response(exc)


@router.post("/{task_id}/question-preparation/{job_id}/retry")
async def retry_question_preparation(
    task_id: str,
    job_id: str,
    request: RetryQuestionPreparationRequest,
    background_tasks: BackgroundTasks,
    current: User = Depends(require_teacher),
    registry: ExpertRegistry = Depends(get_scoped_expert_registry),
):
    """Retry the failed generation stage from its durable prepared sources."""
    try:
        workflow = workflow_repository.get_workflow(task_id, owner_id=current.id)
        failed = workflow_repository.get_operation(job_id, owner_id=current.id)
        if (
            failed.assignment_id != task_id
            or failed.operation_type != "question_preparation"
        ):
            raise NotFound("question_preparation")
        if failed.status != "error" or workflow.last_failed_job_id != failed.id:
            raise InvalidTransition(
                "Only the task's latest failed question preparation can be retried.",
                code="question_preparation_retry_not_available",
            )
        if (
            failed.error_code == "provider_submit_uncertain"
            or (failed.checkpoint or {}).get("base_provider_inflight_stage")
            or list(
                (failed.checkpoint or {}).get(
                    "provider_inflight_question_ids"
                )
                or []
            )
        ):
            raise InvalidTransition(
                "The provider submission state must be verified before retry.",
                code="provider_submit_uncertain",
            )
        if workflow.workflow_revision != request.expected_workflow_revision:
            raise VersionConflict(
                "The task changed before question preparation retry.",
                code="stale_revision",
            )
        payload = dict(failed.payload or {})
        frozen_provider_id = payload.get("recognition_provider_id")
        if not isinstance(frozen_provider_id, str) or not frozen_provider_id:
            raise InvalidTransition(
                "The failed question-preparation provider is unavailable.",
                code="question_preparation_retry_source_unavailable",
            )
        if (
            request.recognition_provider_id is not None
            and request.recognition_provider_id != frozen_provider_id
        ):
            raise InvalidTransition(
                "Question preparation retry must use the original provider.",
                code=(
                    "question_preparation_provider_configuration_changed"
                ),
            )
        previous_base_revision = payload.get("base_workflow_revision")
        if (
            isinstance(previous_base_revision, bool)
            or not isinstance(previous_base_revision, int)
            or workflow.workflow_revision != previous_base_revision + 1
        ):
            raise VersionConflict(
                "The task changed after the failed question preparation.",
                code="stale_revision",
            )
        source_tokens = [
            str(token)
            for token in payload.get("source_tokens") or []
            if isinstance(token, str) and token
        ]
        if not source_tokens:
            raise InvalidTransition(
                "Prepared question sources are unavailable for retry.",
                code="question_preparation_retry_source_unavailable",
            )
        retry_request = StartQuestionPreparationRequest(
            source_tokens=source_tokens,
            expected_workflow_revision=workflow.workflow_revision,
            replace_confirmed=bool(payload.get("replace_confirmed")),
            score_policy=QuestionScorePolicy.model_validate(
                payload.get("score_policy") or {}
            ),
            recognition_provider_id=frozen_provider_id,
        )
        original_input_revision = payload.get("requested_workflow_revision")
        if (
            isinstance(original_input_revision, bool)
            or not isinstance(original_input_revision, int)
            or original_input_revision < 0
        ):
            raise InvalidTransition(
                "The failed question-preparation input is unavailable.",
                code="question_preparation_retry_source_unavailable",
            )
        response = await _start_question_preparation(
            task_id=task_id,
            request=retry_request,
            background_tasks=background_tasks,
            current=current,
            registry=registry,
            allow_prepared_source_reuse=True,
            input_workflow_revision=original_input_revision,
            retry_source_contract=payload,
        )
        if isinstance(response, dict):
            return {**response, "reused_prepared_sources": True}
        return response
    except DomainError as exc:
        return domain_error_response(exc)


def _question_preparation_recovery_error(
    message: str,
    *,
    code: str = "question_preparation_contract_invalid",
) -> ValidationError:
    return ValidationError(message, code=code)


def _merge_question_preparation_checkpoint_progress(
    snapshot: Mapping[str, Any],
    checkpoint: Mapping[str, Any],
) -> dict[str, Any]:
    """Keep persisted per-major counters authoritative across worker restarts."""

    merged = dict(snapshot)
    if "generation_question_ids" not in checkpoint:
        return merged
    generation_ids = list(checkpoint.get("generation_question_ids") or [])
    completed_ids = list(checkpoint.get("completed_question_ids") or [])
    failed_ids = list(checkpoint.get("failed_question_ids") or [])
    inflight_ids = list(
        checkpoint.get("provider_inflight_question_ids") or []
    )
    merged.update({
        "total_questions": len(generation_ids),
        "completed_question_ids": completed_ids,
        "failed_question_ids": failed_ids,
        "active_question_ids": inflight_ids,
        "question_error_codes": dict(
            checkpoint.get("question_error_codes") or {}
        ),
    })
    if checkpoint.get("updated_at") is not None:
        merged["last_activity_at"] = checkpoint["updated_at"]
    stage_metrics = dict(merged.get("stage_metrics") or {})
    stage_metrics.update({
        "solution_total_questions": len(generation_ids),
        "solution_completed_questions": len(completed_ids),
        "solution_failed_questions": len(failed_ids),
    })
    merged["stage_metrics"] = stage_metrics
    return merged


def _rehydrate_question_preparation_inputs(operation):
    """Rebuild request-independent inputs from one frozen durable operation."""

    payload = dict(operation.payload or {})
    if (
        payload.get("contract_version") != 1
        or payload.get("owner_id") != operation.owner_id
        or payload.get("task_id") != operation.assignment_id
        or payload.get("operation_type") != "question_preparation"
        or payload.get("input_hash") != operation.input_hash
    ):
        raise _question_preparation_recovery_error(
            "The question-preparation operation contract is invalid."
        )

    source_tokens = payload.get("source_tokens")
    source_refs = payload.get("source_refs")
    source_hashes = payload.get("source_content_hashes")
    source_text_hashes = payload.get("source_text_hashes")
    generation_policy = payload.get("generation_policy")
    replace_confirmed = payload.get("replace_confirmed")
    if (
        not isinstance(source_tokens, list)
        or not 1 <= len(source_tokens) <= 20
        or any(not isinstance(token, str) or not token for token in source_tokens)
        or len(source_tokens) != len(set(source_tokens))
        or not isinstance(source_refs, list)
        or len(source_refs) != len(source_tokens)
        or not isinstance(source_hashes, dict)
        or not isinstance(source_text_hashes, dict)
        or generation_policy != "complete_required_materials"
        or not isinstance(replace_confirmed, bool)
    ):
        raise _question_preparation_recovery_error(
            "The frozen question sources are invalid."
        )

    recognition_provider_id = payload.get("recognition_provider_id")
    if not isinstance(recognition_provider_id, str) or not recognition_provider_id:
        raise _question_preparation_recovery_error(
            "The frozen recognition provider is invalid."
        )
    registry = task_facade._registry_for_owner(operation.owner_id)
    route = resolve_stage_provider_route(
        owner_id=operation.owner_id,
        registry=registry,
        requested_route_id=recognition_provider_id,
    )
    expected_capability = "ocr" if route.is_baidu_ocr else "text"
    frozen_provider_fingerprint = payload.get(
        "provider_configuration_fingerprint"
    )
    if (
        route.route_id != recognition_provider_id
        or payload.get("provider_capability") != expected_capability
    ):
        raise _question_preparation_recovery_error(
            "The frozen recognition provider capability changed.",
            code="recognition_provider_not_enabled",
        )
    current_provider_fingerprint = stage_provider_configuration_fingerprint(
        owner_id=operation.owner_id,
        route=route,
        registry=registry,
    )
    if (
        not isinstance(frozen_provider_fingerprint, str)
        or not re.fullmatch(r"[0-9a-f]{64}", frozen_provider_fingerprint)
        or current_provider_fingerprint != frozen_provider_fingerprint
    ):
        raise _question_preparation_recovery_error(
            "The recognition provider configuration changed after publication.",
            code="question_preparation_provider_configuration_changed",
        )

    sources: list[tuple[ProblemSourceDraft, str, dict[str, Any]]] = []
    source_fingerprints: list[str] = []
    for token, frozen_ref in zip(source_tokens, source_refs, strict=True):
        if not isinstance(frozen_ref, dict):
            raise _question_preparation_recovery_error(
                "A frozen question source reference is invalid."
            )
        try:
            source_operation = workflow_repository.get_operation(
                token,
                owner_id=operation.owner_id,
            )
            if (
                source_operation.assignment_id != operation.assignment_id
                or source_operation.operation_type != "problem_source"
            ):
                raise NotFound("problem_source")
            source_payload = dict(source_operation.payload or {})
            if _source_fingerprint(source_payload) != source_operation.input_hash:
                raise NotFound("problem_source")
            current_ref = _validated_question_source_ref(
                operation=source_operation,
                payload=source_payload,
                task_id=operation.assignment_id,
                owner_id=operation.owner_id,
            )
        except DomainError as exc:
            raise _question_preparation_recovery_error(
                "A frozen question source is no longer available.",
                code="question_preparation_source_unavailable",
            ) from exc

        frozen_sha = source_hashes.get(token)
        frozen_text_sha = source_text_hashes.get(token)
        actual_sha = source_payload.get("sha256", source_operation.input_hash)
        if (
            frozen_ref.get("source_operation_id") != source_operation.id
            or frozen_ref.get("source_attempt") != source_operation.attempt
            or frozen_ref.get("role") != current_ref.get("role")
            or frozen_ref.get("source_kind") != current_ref.get("source_kind")
            or frozen_ref.get("source_id") != current_ref.get("source_id")
            or frozen_ref.get("stored_file_id")
            != current_ref.get("stored_file_id")
            or frozen_ref.get("library_material_id")
            != current_ref.get("library_material_id")
            or not isinstance(frozen_sha, str)
            or not frozen_sha
            or frozen_ref.get("content_sha256") != frozen_sha
            or actual_sha != frozen_sha
            or not isinstance(frozen_text_sha, str)
            or not frozen_text_sha
            or frozen_ref.get("prepared_text_sha256") != frozen_text_sha
        ):
            raise _question_preparation_recovery_error(
                "A frozen question source changed after publication.",
                code="question_preparation_source_unavailable",
            )
        text = source_payload.get("text")
        if not isinstance(text, str) or not text.strip():
            raise _question_preparation_recovery_error(
                "A frozen question source has no recoverable text.",
                code="question_preparation_source_unavailable",
            )
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != frozen_text_sha:
            raise _question_preparation_recovery_error(
                "A frozen question source's prepared text changed.",
                code="question_preparation_source_unavailable",
            )
        draft = ProblemSourceDraft(
            source_token=source_operation.id,
            task_id=operation.assignment_id,
            owner_id=operation.owner_id,
            role=source_payload.get("role", "problem"),
            source_kind=source_payload.get("source_kind", "upload"),
            structure_mode=source_payload.get("structure_mode", "organized"),
            extraction_hint=source_payload.get("extraction_hint", ""),
            filename=source_payload.get("filename", "source.txt"),
            content_type=source_payload.get("content_type") or "text/plain",
            size_bytes=int(source_payload.get("size_bytes") or 0),
            content_sha256=frozen_sha,
            library_material_id=source_payload.get("library_material_id"),
            base_workflow_revision=int(
                source_payload.get("base_workflow_revision") or 0
            ),
            resident_bytes=len(text.encode("utf-8")),
            candidates=list(source_payload.get("candidates") or []),
            expires_at=(
                source_operation.expires_at
                or time.time() + SOURCE_TTL_SECONDS
            ),
        )
        sources.append((draft, text, source_payload))
        source_fingerprints.append(source_operation.input_hash)

    prepared_provider_ids = payload.get("prepared_source_provider_ids")
    actual_prepared_provider_ids = sorted({
        str(source_payload.get("recognition_provider_id"))
        for _draft, _text, source_payload in sources
        if source_payload.get("recognition_provider_id")
    })
    if (
        not isinstance(prepared_provider_ids, list)
        or prepared_provider_ids != actual_prepared_provider_ids
    ):
        raise _question_preparation_recovery_error(
            "A prepared source's recognition provider changed.",
            code="question_preparation_source_unavailable",
        )

    base_revision = payload.get("base_workflow_revision")
    claimed_revision = payload.get("claimed_workflow_revision")
    requested_revision = payload.get("requested_workflow_revision")
    if (
        isinstance(requested_revision, bool)
        or not isinstance(requested_revision, int)
        or requested_revision < 0
        or isinstance(base_revision, bool)
        or not isinstance(base_revision, int)
        or isinstance(claimed_revision, bool)
        or not isinstance(claimed_revision, int)
        or claimed_revision != base_revision + 1
    ):
        raise _question_preparation_recovery_error(
            "The frozen workflow revision is invalid."
        )
    try:
        score_policy = QuestionScorePolicy.model_validate(
            payload.get("score_policy") or {}
        )
    except Exception as exc:
        raise _question_preparation_recovery_error(
            "The frozen question score policy is invalid."
        ) from exc
    ordered_source_inputs = [
        {
            "source_token": draft.source_token,
            "source_fingerprint": fingerprint,
            "role": draft.role,
        }
        for (draft, _text, _payload), fingerprint in zip(
            sources,
            source_fingerprints,
            strict=True,
        )
    ]
    expected_input_hash = _question_preparation_input_hash(
        ordered_source_inputs=ordered_source_inputs,
        logical_input_revision=requested_revision,
        replace_confirmed=replace_confirmed,
        generation_policy=generation_policy,
        score_policy=score_policy.model_dump(mode="json"),
        recognition_provider_id=recognition_provider_id,
        provider_configuration_fingerprint=frozen_provider_fingerprint,
    )
    if expected_input_hash != operation.input_hash:
        raise _question_preparation_recovery_error(
            "The frozen question-preparation input hash is invalid."
        )

    return {
        "sources": sources,
        "route": route,
        "recognition_provider_id": recognition_provider_id,
        "claimed_workflow_revision": claimed_revision,
        "replace_confirmed": replace_confirmed,
        "score_policy": score_policy,
    }


async def run_durable_question_preparation(operation) -> None:
    """Resume one fully published question-preparation operation under lease."""

    remove_reporter(operation.operation_id)
    reporter = get_or_create_reporter(operation.operation_id)
    try:
        inputs = _rehydrate_question_preparation_inputs(operation)
        sources = inputs["sources"]
        route = inputs["route"]
        recognition_provider_id = inputs["recognition_provider_id"]
        await reporter.configure_workflow(
            "question_preparation",
            QUESTION_PREPARATION_STAGE_SEQUENCE,
        )
        await reporter.set_phase("parsing")
        await operation.update_progress(
            (await reporter.snapshot()).model_dump(mode="json")
        )

        payload = dict(operation.payload or {})
        artifact_context = {
            "owner_id": operation.owner_id,
            "task_id": operation.assignment_id,
            "operation_id": operation.operation_id,
            "attempt": operation.attempt,
            "input_hash": operation.input_hash,
            "provider_record_id": recognition_provider_id,
        }
        retry_contract = operation.checkpoint_data.get("retry_frozen_contract")
        raw_artifact_attempts = operation.checkpoint_data.get(
            "artifact_attempts"
        ) or {}
        if not isinstance(raw_artifact_attempts, dict):
            raise _question_preparation_recovery_error(
                "The question-preparation artifact lineage is invalid."
            )
        artifact_attempts: dict[str, int] = {}
        for artifact_id, attempt in raw_artifact_attempts.items():
            if (
                not isinstance(artifact_id, str)
                or artifact_id not in operation.artifact_refs
                or isinstance(attempt, bool)
                or not isinstance(attempt, int)
                or not 1 <= attempt <= operation.attempt
            ):
                raise _question_preparation_recovery_error(
                    "The question-preparation artifact lineage is invalid."
                )
            artifact_attempts[artifact_id] = attempt
        if operation.attempt > 1:
            expected_retry_contract = {
                "contract_version": 1,
                "operation_id": operation.operation_id,
                "from_attempt": operation.attempt - 1,
                "to_attempt": operation.attempt,
                "input_hash": operation.input_hash,
                "provider_record_id": recognition_provider_id,
                "source_content_hashes": payload["source_content_hashes"],
                "source_text_hashes": payload["source_text_hashes"],
            }
            if retry_contract != expected_retry_contract:
                raise _question_preparation_recovery_error(
                    "The question-preparation retry artifact contract is invalid."
                )
        elif retry_contract is not None:
            raise _question_preparation_recovery_error(
                "The question-preparation retry artifact contract is invalid."
            )

        def _artifact_context_for(artifact_id: object) -> dict[str, Any]:
            stable_id = str(artifact_id or "")
            return {
                **artifact_context,
                "attempt": artifact_attempts.get(
                    stable_id,
                    operation.attempt,
                ),
            }

        def _record_artifact_attempt(
            *artifact_ids: str,
        ) -> dict[str, int]:
            updated = dict(artifact_attempts)
            for artifact_id in artifact_ids:
                updated[artifact_id] = operation.attempt
            artifact_attempts.clear()
            artifact_attempts.update(updated)
            return updated

        checkpoint_lock = asyncio.Lock()

        def _checkpoint_payload(stage: str, **changes: Any) -> dict[str, Any]:
            checkpoint = dict(operation.checkpoint_data or {})
            checkpoint.update({
                "contract_version": 1,
                "operation_id": operation.operation_id,
                "attempt": operation.attempt,
                "stage": stage,
                "base_workflow_revision": payload["base_workflow_revision"],
                "claimed_workflow_revision": payload[
                    "claimed_workflow_revision"
                ],
                "provider_record_id": recognition_provider_id,
                "source_content_hashes": payload["source_content_hashes"],
                "source_text_hashes": payload["source_text_hashes"],
                "updated_at": time.time(),
            })
            checkpoint.update(changes)
            return checkpoint

        async def _write_checkpoint(
            stage: str,
            *,
            artifact_refs: Sequence[str] | None = None,
            **changes: Any,
        ) -> dict[str, Any]:
            checkpoint = _checkpoint_payload(stage, **changes)
            refs = list(dict.fromkeys(
                list(operation.artifact_refs)
                + list(artifact_refs or [])
            ))
            await operation.checkpoint(
                stage=stage,
                checkpoint=checkpoint,
                artifact_refs=refs,
            )
            snapshot = (await reporter.snapshot()).model_dump(mode="json")
            snapshot = _merge_question_preparation_checkpoint_progress(
                snapshot,
                checkpoint,
            )
            await operation.update_progress(snapshot)
            return checkpoint

        def _read_or_find_base(stage: str, checkpoint_field: str):
            artifact_id = operation.checkpoint_data.get(checkpoint_field)
            if artifact_id is not None:
                envelope = read_base_preparation_artifact(
                    artifact_id,
                    stage=stage,
                    **_artifact_context_for(artifact_id),
                )
                if envelope is None:
                    raise _question_preparation_recovery_error(
                        "A required base artifact is unavailable.",
                        code="question_preparation_artifact_invalid",
                    )
                return str(artifact_id), envelope
            stored = find_base_preparation_artifact(
                stage=stage,
                **artifact_context,
            )
            if stored is None:
                return None, None
            envelope = read_base_preparation_artifact(
                stored.id,
                stage=stage,
                **artifact_context,
            )
            return stored.id, envelope

        final_artifact_id = operation.checkpoint_data.get("final_artifact_id")
        final_envelope = None
        if final_artifact_id is not None:
            final_envelope = read_final_question_packages_artifact(
                final_artifact_id,
                **_artifact_context_for(final_artifact_id),
            )
        else:
            final_artifact = find_final_question_packages_artifact(
                **artifact_context
            )
            if final_artifact is not None:
                final_artifact_id = final_artifact.id
                final_envelope = read_final_question_packages_artifact(
                    final_artifact.id,
                    **artifact_context,
                )
        if final_artifact_id is not None and final_envelope is None:
            raise _question_preparation_recovery_error(
                "The final question-package artifact is invalid.",
                code="question_preparation_artifact_invalid",
            )
        if final_envelope is not None:
            packages = final_envelope.payload.problem_data
            async with checkpoint_lock:
                await _write_checkpoint(
                    "question_packages_prepared",
                    artifact_refs=[str(final_artifact_id)],
                    artifact_attempts=artifact_attempts,
                    question_ids=list(packages),
                    completed_question_ids=list(
                        operation.checkpoint_data.get(
                            "completed_question_ids"
                        )
                        or []
                    ),
                    failed_question_ids=[],
                    provider_inflight_question_ids=[],
                    base_provider_inflight_stage=None,
                    final_artifact_id=final_artifact_id,
                )
            await _run_question_preparation(
                task_id=operation.assignment_id,
                owner_id=operation.owner_id,
                job_id=operation.operation_id,
                job_attempt=operation.attempt,
                sources=sources,
                provider=route.provider,
                claimed_workflow_revision=inputs[
                    "claimed_workflow_revision"
                ],
                replace_confirmed=inputs["replace_confirmed"],
                score_policy=inputs["score_policy"],
                recognition_provider_id=recognition_provider_id,
                ocr_only=route.is_baidu_ocr,
                durable_operation=operation,
                prebuilt_packages=packages,
            )
            return

        extracted_id, extracted_envelope = _read_or_find_base(
            QUESTIONS_EXTRACTED_STAGE,
            "questions_extracted_artifact_id",
        )
        aligned_id, aligned_envelope = _read_or_find_base(
            UPLOADED_MATERIALS_ALIGNED_STAGE,
            "aligned_base_artifact_id",
        )
        inflight_base_stage = operation.checkpoint_data.get(
            "base_provider_inflight_stage"
        )
        recovered_base_stages = {
            QUESTIONS_EXTRACTED_STAGE: extracted_envelope is not None,
            UPLOADED_MATERIALS_ALIGNED_STAGE: aligned_envelope is not None,
        }
        if (
            inflight_base_stage
            and not recovered_base_stages.get(str(inflight_base_stage), False)
        ):
            raise RuntimeError("provider_submit_uncertain")

        recovered_candidates: dict[
            str, list[AICompletionCandidateOutput]
        ] = {}
        question_artifact_ids = dict(
            operation.checkpoint_data.get("question_artifact_ids") or {}
        )
        completed_question_ids: list[str] = []
        question_ids: list[str] = []
        generation_question_ids: list[str] = []
        if aligned_envelope is not None:
            aligned_problem_data = aligned_envelope.payload.problem_data
            question_ids = list(aligned_problem_data)
            requested_targets = requested_major_question_materials(
                aligned_problem_data
            )
            target_question_ids = {
                str(target["q_id"]) for target in requested_targets
            }
            generation_question_ids = [
                q_id for q_id in question_ids if q_id in target_question_ids
            ]
            unknown_artifact_ids = (
                set(question_artifact_ids) - set(generation_question_ids)
            )
            if unknown_artifact_ids:
                raise _question_preparation_recovery_error(
                    "A question artifact references an unknown generation unit.",
                    code="question_preparation_artifact_invalid",
                )
            for question_order, q_id in enumerate(question_ids):
                if q_id not in target_question_ids:
                    continue
                artifact_id = question_artifact_ids.get(q_id)
                if artifact_id is not None:
                    envelope = read_question_candidate_artifact(
                        artifact_id,
                        q_id=q_id,
                        question_order=question_order,
                        **_artifact_context_for(artifact_id),
                    )
                    if envelope is None:
                        raise _question_preparation_recovery_error(
                            "A completed question artifact is unavailable.",
                            code="question_preparation_artifact_invalid",
                        )
                else:
                    stored = find_question_candidate_artifact(
                        q_id=q_id,
                        question_order=question_order,
                        **artifact_context,
                    )
                    if stored is None:
                        continue
                    artifact_id = stored.id
                    envelope = read_question_candidate_artifact(
                        stored.id,
                        q_id=q_id,
                        question_order=question_order,
                        **artifact_context,
                    )
                if envelope is None:
                    raise _question_preparation_recovery_error(
                        "A question artifact is invalid.",
                        code="question_preparation_artifact_invalid",
                    )
                question_artifact_ids[q_id] = str(artifact_id)
                completed_question_ids.append(q_id)
                recovered_candidates[q_id] = [
                    AICompletionCandidateOutput.model_validate(
                        candidate.model_dump(mode="json")
                    )
                    for candidate in envelope.payload.candidates
                ]

            checkpoint_completed = set(
                operation.checkpoint_data.get("completed_question_ids") or []
            )
            if checkpoint_completed - set(completed_question_ids):
                raise _question_preparation_recovery_error(
                    "A completed question has no verified artifact.",
                    code="question_preparation_artifact_invalid",
                )
            checkpoint_failed = set(
                operation.checkpoint_data.get("failed_question_ids") or []
            )
            if checkpoint_failed - set(generation_question_ids):
                raise _question_preparation_recovery_error(
                    "A failed question references an unknown generation unit."
                )
            if checkpoint_failed:
                raise RuntimeError("ai_completion_failed")
            checkpoint_inflight = set(
                operation.checkpoint_data.get(
                    "provider_inflight_question_ids"
                )
                or []
            )
            if checkpoint_inflight - set(generation_question_ids):
                raise _question_preparation_recovery_error(
                    "An in-flight question references an unknown generation unit."
                )
            if checkpoint_inflight - set(completed_question_ids):
                raise RuntimeError("provider_submit_uncertain")

        discovered_refs = [
            str(artifact_id)
            for artifact_id in (
                extracted_id,
                aligned_id,
                *question_artifact_ids.values(),
            )
            if artifact_id
        ]
        async with checkpoint_lock:
            await _write_checkpoint(
                (
                    "solution_units_generated"
                    if completed_question_ids
                    else (
                        UPLOADED_MATERIALS_ALIGNED_STAGE
                        if aligned_envelope is not None
                        else (
                            QUESTIONS_EXTRACTED_STAGE
                            if extracted_envelope is not None
                            else "sources_validated"
                        )
                    )
                ),
                artifact_refs=discovered_refs,
                question_ids=question_ids,
                generation_question_ids=generation_question_ids,
                completed_question_ids=completed_question_ids,
                failed_question_ids=[],
                provider_inflight_question_ids=[],
                question_error_codes={},
                base_provider_inflight_stage=None,
                questions_extracted_artifact_id=extracted_id,
                aligned_base_artifact_id=aligned_id,
                question_artifact_ids=question_artifact_ids,
            )

        question_order_by_id: dict[str, int] = {
            q_id: index for index, q_id in enumerate(question_ids)
        }

        async def _on_extraction_started() -> None:
            async with checkpoint_lock:
                await _write_checkpoint(
                    "questions_extracting",
                    base_provider_inflight_stage=QUESTIONS_EXTRACTED_STAGE,
                )

        async def _on_questions_extracted(
            problem_data: dict[str, dict[str, Any]],
        ) -> None:
            await operation.heartbeat()
            artifact = await run_in_threadpool(
                save_base_preparation_artifact,
                stage=QUESTIONS_EXTRACTED_STAGE,
                problem_data=problem_data,
                issues={},
                operation_lease_token=operation.lease_token,
                **artifact_context,
            )
            envelope = read_base_preparation_artifact(
                artifact.id,
                stage=QUESTIONS_EXTRACTED_STAGE,
                **artifact_context,
            )
            if envelope is None:
                raise _question_preparation_recovery_error(
                    "The extracted-question artifact could not be verified.",
                    code="question_preparation_artifact_invalid",
                )
            async with checkpoint_lock:
                await _write_checkpoint(
                    QUESTIONS_EXTRACTED_STAGE,
                    artifact_refs=[artifact.id],
                    artifact_attempts=_record_artifact_attempt(artifact.id),
                    question_ids=list(envelope.payload.problem_data),
                    base_provider_inflight_stage=None,
                    questions_extracted_artifact_id=artifact.id,
                )

        async def _on_base_alignment_started() -> None:
            async with checkpoint_lock:
                await _write_checkpoint(
                    "uploaded_materials_aligning",
                    base_provider_inflight_stage=(
                        UPLOADED_MATERIALS_ALIGNED_STAGE
                    ),
                )

        async def _on_base_failed(stage: str, exc: Exception) -> None:
            uncertain = provider_submission_is_uncertain(exc)
            async with checkpoint_lock:
                changes: dict[str, Any] = {
                    "base_error_code": (
                        "provider_submit_uncertain"
                        if uncertain
                        else _question_preparation_failure_code(exc)
                    )
                }
                if not uncertain:
                    changes["base_provider_inflight_stage"] = None
                await _write_checkpoint(f"{stage}_failed", **changes)

        async def _on_base_prepared(
            problem_data: dict[str, dict[str, Any]],
            issues: dict[str, list[dict[str, Any]]],
        ) -> None:
            await operation.heartbeat()
            artifact = await run_in_threadpool(
                save_base_preparation_artifact,
                stage=UPLOADED_MATERIALS_ALIGNED_STAGE,
                problem_data=problem_data,
                issues=issues,
                operation_lease_token=operation.lease_token,
                **artifact_context,
            )
            envelope = read_base_preparation_artifact(
                artifact.id,
                stage=UPLOADED_MATERIALS_ALIGNED_STAGE,
                **artifact_context,
            )
            if envelope is None:
                raise _question_preparation_recovery_error(
                    "The aligned-question artifact could not be verified.",
                    code="question_preparation_artifact_invalid",
                )
            aligned_questions = envelope.payload.problem_data
            question_ids[:] = list(aligned_questions)
            question_order_by_id.clear()
            question_order_by_id.update({
                q_id: index for index, q_id in enumerate(question_ids)
            })
            requested = requested_major_question_materials(aligned_questions)
            target_ids = {str(target["q_id"]) for target in requested}
            generation_question_ids[:] = [
                q_id for q_id in question_ids if q_id in target_ids
            ]
            async with checkpoint_lock:
                await _write_checkpoint(
                    UPLOADED_MATERIALS_ALIGNED_STAGE,
                    artifact_refs=[artifact.id],
                    artifact_attempts=_record_artifact_attempt(artifact.id),
                    question_ids=question_ids,
                    generation_question_ids=generation_question_ids,
                    completed_question_ids=[],
                    failed_question_ids=[],
                    provider_inflight_question_ids=[],
                    question_error_codes={},
                    base_provider_inflight_stage=None,
                    aligned_base_artifact_id=artifact.id,
                )

        async def _on_question_started(q_id: str) -> None:
            async with checkpoint_lock:
                inflight = list(
                    operation.checkpoint_data.get(
                        "provider_inflight_question_ids"
                    )
                    or []
                )
                if q_id not in inflight:
                    inflight.append(q_id)
                inflight.sort(key=generation_question_ids.index)
                await _write_checkpoint(
                    "solution_unit_submitting",
                    provider_inflight_question_ids=inflight,
                )

        async def _on_question_completed(
            q_id: str,
            candidates: list[AICompletionCandidateOutput],
        ) -> None:
            await operation.heartbeat()
            question_order = question_order_by_id[q_id]
            artifact = await run_in_threadpool(
                save_question_candidate_artifact,
                q_id=q_id,
                question_order=question_order,
                candidates=candidates,
                operation_lease_token=operation.lease_token,
                **artifact_context,
            )
            envelope = read_question_candidate_artifact(
                artifact.id,
                q_id=q_id,
                question_order=question_order,
                **artifact_context,
            )
            if envelope is None:
                raise _question_preparation_recovery_error(
                    "The major-question artifact could not be verified.",
                    code="question_preparation_artifact_invalid",
                )
            async with checkpoint_lock:
                inflight = [
                    item
                    for item in (
                        operation.checkpoint_data.get(
                            "provider_inflight_question_ids"
                        )
                        or []
                    )
                    if item != q_id
                ]
                completed = list(
                    operation.checkpoint_data.get("completed_question_ids")
                    or []
                )
                if q_id not in completed:
                    completed.append(q_id)
                completed.sort(key=generation_question_ids.index)
                artifact_ids = dict(
                    operation.checkpoint_data.get("question_artifact_ids")
                    or {}
                )
                artifact_ids[q_id] = artifact.id
                error_codes = dict(
                    operation.checkpoint_data.get("question_error_codes")
                    or {}
                )
                error_codes.pop(q_id, None)
                await _write_checkpoint(
                    "solution_units_generated",
                    artifact_refs=[artifact.id],
                    artifact_attempts=_record_artifact_attempt(artifact.id),
                    provider_inflight_question_ids=inflight,
                    completed_question_ids=completed,
                    question_artifact_ids=artifact_ids,
                    question_error_codes=error_codes,
                )

        async def _on_question_failed(q_id: str, exc: Exception) -> None:
            code = _question_preparation_failure_code(exc)
            uncertain = provider_submission_is_uncertain(exc)
            async with checkpoint_lock:
                changes: dict[str, Any] = {}
                if not uncertain:
                    changes["provider_inflight_question_ids"] = [
                        item
                        for item in (
                            operation.checkpoint_data.get(
                                "provider_inflight_question_ids"
                            )
                            or []
                        )
                        if item != q_id
                    ]
                    failed = list(
                        operation.checkpoint_data.get("failed_question_ids")
                        or []
                    )
                    if q_id not in failed:
                        failed.append(q_id)
                    failed.sort(key=generation_question_ids.index)
                    changes["failed_question_ids"] = failed
                error_codes = dict(
                    operation.checkpoint_data.get("question_error_codes")
                    or {}
                )
                error_codes[q_id] = (
                    "provider_submit_uncertain" if uncertain else code
                )
                changes["question_error_codes"] = error_codes
                await _write_checkpoint("solution_unit_failed", **changes)

        async def _on_packages_prepared(
            packages: dict[str, dict[str, Any]],
        ) -> dict[str, dict[str, Any]]:
            await operation.heartbeat()
            artifact = await run_in_threadpool(
                save_final_question_packages_artifact,
                problem_data=packages,
                operation_lease_token=operation.lease_token,
                **artifact_context,
            )
            envelope = read_final_question_packages_artifact(
                artifact.id,
                **artifact_context,
            )
            if envelope is None:
                raise _question_preparation_recovery_error(
                    "The final question-package artifact could not be verified.",
                    code="question_preparation_artifact_invalid",
                )
            async with checkpoint_lock:
                await _write_checkpoint(
                    "question_packages_prepared",
                    artifact_refs=[artifact.id],
                    artifact_attempts=_record_artifact_attempt(artifact.id),
                    question_ids=list(envelope.payload.problem_data),
                    provider_inflight_question_ids=[],
                    base_provider_inflight_stage=None,
                    final_artifact_id=artifact.id,
                )
            return envelope.payload.problem_data

        await _run_question_preparation(
            task_id=operation.assignment_id,
            owner_id=operation.owner_id,
            job_id=operation.operation_id,
            job_attempt=operation.attempt,
            sources=sources,
            provider=route.provider,
            claimed_workflow_revision=inputs["claimed_workflow_revision"],
            replace_confirmed=inputs["replace_confirmed"],
            score_policy=inputs["score_policy"],
            recognition_provider_id=recognition_provider_id,
            ocr_only=route.is_baidu_ocr,
            durable_operation=operation,
            recovered_extracted_problem_data=(
                extracted_envelope.payload.problem_data
                if extracted_envelope is not None
                else None
            ),
            on_extraction_started=_on_extraction_started,
            on_questions_extracted=_on_questions_extracted,
            on_base_alignment_started=_on_base_alignment_started,
            on_base_failed=_on_base_failed,
            recovered_base_problem_data=(
                aligned_envelope.payload.problem_data
                if aligned_envelope is not None
                else None
            ),
            recovered_base_issues=(
                aligned_envelope.payload.issues
                if aligned_envelope is not None
                else None
            ),
            on_base_prepared=_on_base_prepared,
            recovered_candidates_by_question=recovered_candidates,
            completed_question_ids=completed_question_ids,
            on_question_started=_on_question_started,
            on_question_completed=_on_question_completed,
            on_question_failed=_on_question_failed,
            on_packages_prepared=_on_packages_prepared,
        )
    except LeaseLost:
        raise
    except Exception as exc:
        error_code = _question_preparation_failure_code(exc)
        if (
            operation.checkpoint_data.get("base_provider_inflight_stage")
            or operation.checkpoint_data.get(
                "provider_inflight_question_ids"
            )
        ):
            error_code = "provider_submit_uncertain"
        logger.warning(
            "Durable question preparation failed; job_id=%s error_code=%s "
            "exception_type=%s",
            operation.operation_id,
            error_code,
            type(exc).__name__,
        )
        await reporter.set_error(error_code)
        snapshot = (await reporter.snapshot()).model_dump(mode="json")
        snapshot = _merge_question_preparation_checkpoint_progress(
            snapshot,
            operation.checkpoint_data,
        )
        task_facade._fail_operation(
            operation.assignment_id,
            operation.owner_id,
            operation.operation_id,
            operation.attempt,
            error_code,
            expected_lease_token=operation.lease_token,
            operation_progress=snapshot,
        )


async def _run_question_preparation(
    *, task_id: str, owner_id: str, job_id: str, job_attempt: int,
    sources: list[tuple[ProblemSourceDraft, str, dict[str, Any]]],
    provider, claimed_workflow_revision: int, replace_confirmed: bool,
    score_policy: QuestionScorePolicy,
    recognition_provider_id: str,
    ocr_only: bool = False,
    source_checkpoint: dict | None = None,
    source_artifact_refs: list[str] | None = None,
    durable_operation=None,
    prebuilt_packages: Mapping[str, Mapping[str, Any]] | None = None,
    recovered_extracted_problem_data: Mapping[
        str, Mapping[str, Any]
    ] | None = None,
    on_extraction_started: Callable[[], Awaitable[None]] | None = None,
    on_questions_extracted: Callable[
        [dict[str, dict[str, Any]]], Awaitable[None]
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
        [dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]],
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
    on_packages_prepared: Callable[
        [dict[str, dict[str, Any]]], Awaitable[dict[str, dict[str, Any]]]
    ] | None = None,
) -> None:
    reporter = get_or_create_reporter(job_id)
    try:
        if prebuilt_packages is not None:
            packages = {
                str(q_id): dict(problem)
                for q_id, problem in prebuilt_packages.items()
            }
        elif ocr_only:
            packages = await prepare_ocr_question_packages(
                [(draft, text) for draft, text, _payload in sources],
                provider_id=recognition_provider_id,
                reporter=reporter,
                score_policy=score_policy,
            )
        else:
            packages = await prepare_question_packages(
                [(draft, text) for draft, text, _payload in sources],
                provider,
                provider_id=recognition_provider_id,
                reporter=reporter,
                score_policy=score_policy,
                recovered_extracted_problem_data=(
                    recovered_extracted_problem_data
                ),
                on_extraction_started=on_extraction_started,
                on_questions_extracted=on_questions_extracted,
                on_base_alignment_started=on_base_alignment_started,
                on_base_failed=on_base_failed,
                recovered_base_problem_data=recovered_base_problem_data,
                recovered_base_issues=recovered_base_issues,
                on_base_prepared=on_base_prepared,
                recovered_candidates_by_question=(
                    recovered_candidates_by_question
                ),
                completed_question_ids=completed_question_ids,
                on_question_started=on_question_started,
                on_question_completed=on_question_completed,
                on_question_failed=on_question_failed,
                provider_submission_safe=(durable_operation is not None),
            )
        if on_packages_prepared is not None:
            packages = await on_packages_prepared(packages)
        snapshot = (await reporter.snapshot()).model_dump(mode="json")
        if durable_operation is not None:
            snapshot = _merge_question_preparation_checkpoint_progress(
                snapshot,
                durable_operation.checkpoint_data,
            )
        terminal_snapshot = {
            **snapshot,
            "phase": "done",
            "current_step": "committing_question_packages",
            "total_steps": 8,
            "completed_steps": 8,
        }
        operation_checkpoint = dict(
            durable_operation.checkpoint_data
            if durable_operation is not None
            else source_checkpoint or {}
        )
        if durable_operation is not None:
            operation_checkpoint.update({
                "stage": "question_packages_committed",
                "updated_at": time.time(),
            })
        operation_artifact_refs = (
            durable_operation.artifact_refs
            if durable_operation is not None
            else source_artifact_refs or []
        )
        task_facade._replace_draft_questions(
            task_id, owner_id, packages,
            ", ".join(draft.filename for draft, _text, _payload in sources),
            expected_workflow_revision=claimed_workflow_revision,
            replace_confirmed=replace_confirmed,
            operation_id=job_id,
            expected_operation_attempt=job_attempt,
            expected_lease_token=(
                durable_operation.lease_token
                if durable_operation is not None
                else None
            ),
            expected_checkpoint_revision=(
                durable_operation.checkpoint_revision
                if durable_operation is not None
                else None
            ),
            expected_active_operation=(
                "question_preparation"
                if durable_operation is not None
                else None
            ),
            operation_progress=terminal_snapshot,
            operation_checkpoint=operation_checkpoint,
            operation_artifact_refs=operation_artifact_refs,
            operation_checkpoint_stage="question_packages_committed",
            recognition_provider_id=recognition_provider_id,
        )
        await reporter.set_stage_progress(
            "committing_question_packages",
            total_steps=8,
            completed_steps=8,
            message="Question packages committed.",
        )
        await reporter.set_phase("done")
    except LeaseLost:
        raise
    except DomainError as exc:
        error_code = task_facade._detail_error(exc, "problem_extraction_failed")
        if durable_operation is not None and (
            durable_operation.checkpoint_data.get("base_provider_inflight_stage")
            or durable_operation.checkpoint_data.get(
                "provider_inflight_question_ids"
            )
        ):
            error_code = "provider_submit_uncertain"
        await reporter.set_error(error_code)
        snapshot = (await reporter.snapshot()).model_dump(mode="json")
        if durable_operation is not None:
            snapshot = _merge_question_preparation_checkpoint_progress(
                snapshot,
                durable_operation.checkpoint_data,
            )
        task_facade._fail_operation(
            task_id, owner_id, job_id, job_attempt,
            error_code,
            expected_lease_token=(
                durable_operation.lease_token
                if durable_operation is not None
                else None
            ),
            operation_progress=snapshot,
        )
    except Exception as exc:
        error_code = _question_preparation_failure_code(exc)
        if durable_operation is not None and (
            durable_operation.checkpoint_data.get("base_provider_inflight_stage")
            or durable_operation.checkpoint_data.get(
                "provider_inflight_question_ids"
            )
        ):
            error_code = "provider_submit_uncertain"
        logger.warning(
            "Background question preparation failed; job_id=%s error_code=%s exception_type=%s",
            job_id,
            error_code,
            type(exc).__name__,
        )
        await reporter.set_error(error_code)
        snapshot = (await reporter.snapshot()).model_dump(mode="json")
        if durable_operation is not None:
            snapshot = _merge_question_preparation_checkpoint_progress(
                snapshot,
                durable_operation.checkpoint_data,
            )
        task_facade._fail_operation(
            task_id, owner_id, job_id, job_attempt, error_code,
            expected_lease_token=(
                durable_operation.lease_token
                if durable_operation is not None
                else None
            ),
            operation_progress=snapshot,
        )


# ─── Q08 material import ─────────────────────────────────────────────────────


@router.post("/{task_id}/material-imports/preflight")
async def preflight_material_import(
    task_id: str,
    file: UploadFile | None = File(default=None),
    library_material_id: str | None = Form(default=None),
    targets: str = Form(...),
    structure_mode: str = Form(default="organized"),
    extraction_hint: str = Form(default=""),
    save_to_library: bool = Form(default=False),
    current: User = Depends(require_teacher),
    registry: ExpertRegistry = Depends(get_scoped_expert_registry),
):
    try:
        requested_targets = json.loads(targets)
        if not isinstance(requested_targets, list):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "invalid_targets"})
    try:
        workflow = workflow_repository.get_workflow(task_id, owner_id=current.id)
        _provider_id, route = _resolve_recognition_provider(
            owner_id=current.id,
            registry=registry,
            requested_provider_id=None,
        )
        text, descriptor = await _read_source(
            file=file, library_material_id=library_material_id,
            inline_text=None, owner_id=current.id, registry=registry,
            role=_material_import_source_role(requested_targets),
            provider=route.provider,
        )
        target_role = (
            "rubric" if requested_targets == ["criterion"]
            else "reference_answer" if requested_targets == ["reference_answer"]
            else "material_import"
        )
        saved_material = await _save_source_to_library(
            save=save_to_library,
            descriptor=descriptor,
            owner_id=current.id,
            task_id=task_id,
            role=target_role,
            existing_material_id=library_material_id,
        )
        effective_material_id = (
            library_material_id
            or (saved_material or {}).get("material_id")
        )
        text_artifact = file_repository.save_file(
            storage=get_storage(), owner_id=current.id,
            kind="material_import_text", original_name="material-source.txt",
            content=text.encode("utf-8"), content_type="text/plain",
            assignment_id=task_id,
        )
        payload = {
            "text_artifact_id": text_artifact.id, "filename": descriptor["filename"],
            "source_kind": descriptor["kind"], "size_bytes": descriptor["size_bytes"],
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "library_material_id": effective_material_id,
            "targets": requested_targets, "structure_mode": structure_mode,
            "extraction_hint": extraction_hint,
            "base_workflow_revision": workflow.workflow_revision,
        }
        draft, _ = workflow_repository.create_operation(
            assignment_id=task_id, owner_id=current.id,
            operation_type="material_source", input_hash=_source_fingerprint({
                **payload, "role": "material_import",
            }), payload=payload, expires_at=time.time() + SOURCE_TTL_SECONDS,
        )
        return {
            "status": "ready", "source_token": draft.id,
            "source": {
                "kind": descriptor["kind"], "filename": descriptor["filename"],
                "size_bytes": descriptor["size_bytes"],
                "sha256": payload["sha256"], "library_material_id": effective_material_id,
            },
            "targets": requested_targets, "structure_mode": structure_mode,
            "extraction_hint": extraction_hint,
            "candidate_summary": {
                "matched": [], "possible_matches": [], "not_found": [],
                "semantic_match_performed": False,
                "notice": "Candidates are generated after confirmation.",
            },
            "base_workflow_revision": workflow.workflow_revision,
            "workflow_revision": workflow.workflow_revision,
            "expires_at": draft.expires_at,
            "saved_material": saved_material,
        }
    except DomainError as exc:
        return domain_error_response(exc)


@router.post("/{task_id}/material-imports")
async def start_material_import(
    task_id: str,
    request: StartMaterialImportRequest,
    background_tasks: BackgroundTasks,
    current: User = Depends(require_teacher),
    registry: ExpertRegistry = Depends(get_scoped_expert_registry),
):
    try:
        source = workflow_repository.get_operation(request.source_token, owner_id=current.id)
        if source.assignment_id != task_id or source.operation_type != "material_source":
            raise NotFound("material_source")
        if source.expires_at is not None and source.expires_at <= time.time():
            raise InvalidTransition("Material source expired.", code="stale_revision")
        payload = dict(source.payload or {})
        base_revision = int(payload.get("base_workflow_revision") or 0)
        workflow = workflow_repository.get_workflow(task_id, owner_id=current.id)
        provider_id, route = _resolve_recognition_provider(
            owner_id=current.id,
            registry=registry,
            requested_provider_id=None,
        )
        operation_hash = hashlib.sha256(json.dumps({
            "source": source.input_hash,
            "base_revision": base_revision,
            "provider_id": provider_id,
        }, sort_keys=True).encode()).hexdigest()
        replay = task_facade.find_task_operation(
            task_id=task_id, owner_id=current.id,
            operation_type="material_import", input_hash=operation_hash,
        )
        if replay is not None and not task_facade._operation_is_retryable(replay):
            state = (
                "already_running" if replay.status in {"pending", "running"}
                else "plan_ready" if replay.status == "ready"
                else "already_done"
            )
            return {
                "status": state, "job_id": replay.id, "task_id": task_id,
                "request_fingerprint": operation_hash,
                "workflow_revision": workflow.workflow_revision,
            }
        claim_base_revision = task_facade.retryable_operation_claim_revision(
            workflow=workflow, replay=replay, requested_revision=base_revision,
        )
        task = task_facade.get_task(task_id=task_id, owner_id=current.id, full=True)
        workflow, active = task_facade._ensure_no_other_active_operation(
            task_id=task_id, owner_id=current.id,
            operation_type="material_import", input_hash=operation_hash,
        )
        if active is not None:
            return {
                "status": "already_running", "job_id": active.id,
                "task_id": task_id,
                "request_fingerprint": operation_hash,
                "workflow_revision": workflow.workflow_revision,
            }
        job, created = workflow_repository.create_operation(
            assignment_id=task_id, owner_id=current.id,
            operation_type="material_import", input_hash=operation_hash,
            payload={
                "source_token": source.id,
                "base_workflow_revision": claim_base_revision,
                "provider_id": provider_id,
            }, expires_at=time.time() + task_facade._OPERATION_PUBLICATION_TTL_SECONDS,
            initial_status="preparing",
        )
        if not created:
            state = (
                "already_running" if job.status in {"pending", "running"}
                else "plan_ready" if job.status == "ready"
                else "already_done"
            )
            return {"status": state, "job_id": job.id, "task_id": task_id,
                    "request_fingerprint": operation_hash,
                    "workflow_revision": workflow.workflow_revision}
        remove_reporter(job.id)
        try:
            claimed_revision = task_facade.activate_workflow_operation_atomic(
                task_id=task_id, owner_id=current.id, operation_id=job.id,
                expected_operation_attempt=job.attempt,
                expected_workflow_revision=claim_base_revision,
                operation_payload=dict(job.payload or {}),
                workflow_changes={
                    "active_operation": "material_import",
                    "active_job_id": job.id, "error_code": None,
                },
            )
        except VersionConflict:
            workflow_repository.update_operation(
                job.id, owner_id=current.id, expected_attempt=job.attempt,
                status="error",
                error_code="stale_revision", completed_at=time.time(),
            )
            task_facade._raise_stale_revision()
        return {
            "status": "started", "job_id": job.id, "task_id": task_id,
            "request_fingerprint": operation_hash,
            "workflow_revision": claimed_revision,
        }
    except DomainError as exc:
        return domain_error_response(exc)


async def _run_material_import(
    *, task_id: str, owner_id: str, job_id: str, job_attempt: int,
    source_id: str,
    source_payload: dict[str, Any], problems_data: dict[str, dict], provider,
    claimed_workflow_revision: int,
    expected_lease_token: str | None = None,
    durable_operation=None,
    recovered_candidates: list[MaterialImportCandidateOutput] | None = None,
    result_artifact=None,
) -> None:
    try:
        reporter = get_or_create_reporter(job_id)
        await reporter.configure_workflow(
            "material_import", ("matching_questions", "validating_matches", "plan_ready")
        )
        candidates = recovered_candidates
        if candidates is None:
            candidates = await parse_material_import_to_candidates(
                text=str(source_payload.get("text") or ""),
                problems_data=problems_data,
                targets=list(source_payload.get("targets") or []),
                structure_mode=str(source_payload.get("structure_mode") or "organized"),
                extraction_hint=str(source_payload.get("extraction_hint") or ""),
                provider=provider, reporter=reporter,
            )
            if durable_operation is not None:
                result_artifact = _save_auxiliary_result_artifact(
                    operation=durable_operation,
                    kind="material_import_result",
                    stage="material-candidates",
                    payload=[item.model_dump(mode="json") for item in candidates],
                )
        if durable_operation is not None and result_artifact is not None and (
            durable_operation.checkpoint_data.get("result_artifact_id")
            != result_artifact.id
        ):
            await durable_operation.checkpoint(
                stage="material_candidates_generated",
                checkpoint={"result_artifact_id": result_artifact.id},
                artifact_refs=[result_artifact.id],
            )
        serialized = []
        for index, candidate in enumerate(candidates, start=1):
            problem = problems_data.get(candidate.q_id, {})
            existing = problem.get(candidate.target)
            serialized.append({
                "candidate_id": f"material_{index}_{hashlib.sha256((candidate.q_id + candidate.target).encode()).hexdigest()[:8]}",
                "q_id": candidate.q_id, "target": candidate.target,
                "match_status": candidate.match_status,
                "text_value": candidate.text_value,
                "test_cases": [case.model_dump(mode="json") for case in (candidate.test_cases or [])] or None,
                "confidence": candidate.confidence,
                "source_excerpt": candidate.source_excerpt[:600],
                "source_location": candidate.source_location[:160],
                "reason": candidate.reason[:300],
                "would_overwrite": bool(existing),
            })
        snapshot = (await reporter.snapshot()).model_dump(mode="json")
        completed_at = time.time()
        persisted_source_payload = {
            key: value for key, value in source_payload.items() if key != "text"
        }
        task_facade.complete_planning_operation_atomic(
            task_id=task_id, owner_id=owner_id,
            expected_workflow_revision=claimed_workflow_revision,
            operation_id=job_id,
            expected_operation_attempt=job_attempt,
            expected_lease_token=expected_lease_token,
            progress=snapshot,
            payload={
                **persisted_source_payload,
                "source_token": source_id, "candidates": serialized,
                "applied_candidate_ids": [], "completed_at": completed_at,
                "base_workflow_revision": claimed_workflow_revision,
            },
        )
    except DomainError as exc:
        task_facade._fail_operation(
            task_id, owner_id, job_id, job_attempt,
            task_facade._detail_error(exc, "material_import_failed"),
            expected_lease_token=expected_lease_token,
        )
    except Exception as exc:
        logger.warning("Background material import failed; job_id=%s", job_id)
        task_facade._fail_operation(
            task_id, owner_id, job_id, job_attempt,
            classify_background_error(exc, "material_import_failed"),
            expected_lease_token=expected_lease_token,
        )


async def run_durable_material_import(operation) -> None:
    source_id = str((operation.payload or {}).get("source_token") or "")
    source = workflow_repository.get_operation(source_id, owner_id=operation.owner_id)
    if source.assignment_id != operation.assignment_id or source.operation_type != "material_source":
        raise NotFound("material_source")
    source_payload = dict(source.payload or {})
    artifact = file_repository.get_file(
        file_id=str(source_payload.get("text_artifact_id") or ""),
        owner_id=operation.owner_id,
    )
    if artifact is None or artifact.assignment_id != operation.assignment_id:
        raise NotFound("stored_file")
    with get_storage().open(artifact.storage_key) as stream:
        source_payload["text"] = stream.read().decode("utf-8")
    result_artifact = _find_auxiliary_result_artifact(
        operation=operation, kind="material_import_result",
        stage="material-candidates",
    )
    recovered_candidates = (
        _read_auxiliary_candidates(result_artifact, MaterialImportCandidateOutput)
        if result_artifact is not None else None
    )
    registry = task_facade._registry_for_owner(operation.owner_id)
    provider = registry.pick_default()
    if provider is None and recovered_candidates is None:
        raise ValidationError("No enabled provider is available.", code="no_provider_configured")
    task = task_facade.get_task(task_id=operation.assignment_id, owner_id=operation.owner_id, full=True)
    await _run_material_import(
        task_id=operation.assignment_id, owner_id=operation.owner_id,
        job_id=operation.operation_id, job_attempt=operation.attempt,
        source_id=source.id, source_payload=source_payload,
        problems_data=task["problem_data"], provider=provider,
        claimed_workflow_revision=int((operation.payload or {}).get("base_workflow_revision") or 0) + 1,
        expected_lease_token=operation.lease_token,
        durable_operation=operation,
        recovered_candidates=recovered_candidates,
        result_artifact=result_artifact,
    )


@router.get("/{task_id}/material-imports/{job_id}")
async def get_material_import(
    task_id: str, job_id: str, current: User = Depends(require_teacher)
):
    try:
        job = workflow_repository.get_operation(job_id, owner_id=current.id)
        if job.assignment_id != task_id or job.operation_type != "material_import":
            raise NotFound("material_import")
        payload = dict(job.payload or {})
        workflow = workflow_repository.get_workflow(task_id, owner_id=current.id)
        candidates = list(payload.get("candidates") or [])
        progress = job.progress or None
        if job.status == "running" and (reporter := get_reporter(job.id)) is not None:
            progress = (await reporter.snapshot()).model_dump(mode="json")
        return {
            "job_id": job.id, "task_id": task_id,
            "status": job.status if job.status in {"running", "ready", "applied", "error"} else "running",
            "request_fingerprint": job.input_hash,
            "source": {
                "kind": payload.get("source_kind", "upload"),
                "filename": payload.get("filename", "source"),
                "size_bytes": payload.get("size_bytes"), "sha256": payload.get("sha256"),
                "library_material_id": payload.get("library_material_id"),
            },
            "targets": payload.get("targets", []),
            "structure_mode": payload.get("structure_mode", "organized"),
            "extraction_hint": payload.get("extraction_hint", ""),
            "overwrite_policy": "missing_only", "candidates": candidates,
            "summary": _material_summary(candidates, payload.get("applied_candidate_ids", [])),
            "progress": progress, "error": job.error_code,
            "applied_candidate_ids": payload.get("applied_candidate_ids", []),
            "workflow_revision": workflow.workflow_revision,
            "created_at": job.created_at, "completed_at": job.completed_at,
            "expires_at": job.expires_at or job.created_at + SOURCE_TTL_SECONDS,
            "storage": "database",
        }
    except DomainError as exc:
        return domain_error_response(exc)


def _material_summary(candidates: list[dict], applied: list[str]) -> dict:
    return {
        "candidate_count": len(candidates),
        "conflict_count": sum(bool(item.get("would_overwrite")) for item in candidates),
        "low_confidence_count": sum(float(item.get("confidence") or 0) < 0.72 for item in candidates),
        "exact_match_count": sum(item.get("match_status") == "exact" for item in candidates),
        "possible_match_count": sum(item.get("match_status") == "possible" for item in candidates),
        "by_target": {
            target: sum(item.get("target") == target for item in candidates)
            for target in ("criterion", "reference_answer", "test_cases")
        },
        "applied_candidate_ids": applied,
    }


@router.post("/{task_id}/material-imports/{job_id}/apply")
def apply_material_import(
    task_id: str, job_id: str, request: ApplyMaterialImportRequest,
    current: User = Depends(require_teacher),
):
    job = None
    try:
        job = workflow_repository.get_operation(job_id, owner_id=current.id)
        if job.assignment_id != task_id or job.operation_type != "material_import":
            raise NotFound("material_import")
        payload = dict(job.payload or {})
        if job.status == "applied":
            workflow = workflow_repository.get_workflow(task_id, owner_id=current.id)
            return {
                "status": "already_done", "job_id": job_id, "task_id": task_id,
                "summary": _material_summary(payload.get("candidates", []), payload.get("applied_candidate_ids", [])),
                "workflow_revision": workflow.workflow_revision,
            }
        if job.status != "ready":
            raise InvalidTransition("Material import is not ready.", code="workflow_busy")
        if job.expires_at is not None and job.expires_at <= time.time():
            raise InvalidTransition("Material import expired.", code="stale_revision")
        accepted = set(request.accepted_candidate_ids)
        overwrite = set(request.overwrite_candidate_ids)
        if not overwrite.issubset(accepted):
            raise ValidationError("Overwrite candidates must also be accepted.")
        candidates = list(payload.get("candidates") or [])
        candidate_map = {
            str(candidate.get("candidate_id")): candidate
            for candidate in candidates if candidate.get("candidate_id")
        }
        if not accepted.issubset(candidate_map):
            raise ValidationError(
                "The material plan changed.", code="stale_revision"
            )
        task = task_facade.get_task(task_id=task_id, owner_id=current.id, full=True)
        applied = []
        patch_map: dict[str, dict[str, Any]] = {}
        for candidate_id in request.accepted_candidate_ids:
            candidate = candidate_map[candidate_id]
            if candidate_id not in accepted:
                continue
            if candidate.get("would_overwrite") and candidate_id not in overwrite:
                continue
            target = candidate.get("target")
            if target not in {"criterion", "reference_answer", "test_cases"}:
                raise ValidationError("Unsupported material target.")
            value = candidate.get("test_cases") if target == "test_cases" else candidate.get("text_value")
            if value in (None, "", []):
                raise ValidationError("Material candidate is empty.")
            problem = task["problem_data"].get(str(candidate.get("q_id") or ""))
            if problem is None:
                raise VersionConflict("Question no longer exists.", code="stale_revision")
            if target == "test_cases" and not is_programming_question_type(problem.get("type")):
                raise ValidationError("Test cases require a programming question.")
            provenance = {
                "import_job_id": job_id, "candidate_id": candidate_id,
                "source_kind": payload.get("source_kind", "upload"),
                "source_filename": payload.get("filename", "source"),
                "library_material_id": payload.get("library_material_id"),
                "confidence": candidate.get("confidence", 0),
                "match_status": candidate.get("match_status", "possible"),
                "source_excerpt": candidate.get("source_excerpt", ""),
                "source_location": candidate.get("source_location", ""),
                "reason": candidate.get("reason", ""),
                "review_status": "pending", "imported_at": time.time(), "updated_at": time.time(),
            }
            q_id = str(candidate["q_id"])
            patch = patch_map.setdefault(q_id, {
                "q_id": q_id, "fields": {},
                "presentation": {"material_provenance": {}},
            })
            patch["fields"][target] = value
            patch["presentation"]["material_provenance"][target] = provenance
            applied.append(candidate_id)
        payload["applied_candidate_ids"] = applied
        revised_revision = task_facade.apply_question_patches_atomic(
            task_id=task_id, owner_id=current.id,
            expected_workflow_revision=request.expected_workflow_revision,
            patches=list(patch_map.values()), operation_id=job_id,
            expected_operation_attempt=job.attempt,
            required_operation_status="ready", final_operation_status="applied",
            operation_payload=payload, operation_progress=dict(job.progress or {}),
        )
        return {
            "status": "applied", "job_id": job_id, "task_id": task_id,
            "summary": _material_summary(payload.get("candidates", []), applied),
            "workflow_revision": revised_revision,
        }
    except DomainError as exc:
        if job is not None and job.status not in {"applied", "error"}:
            task_facade._fail_operation(
                task_id, current.id, job.id, job.attempt,
                task_facade._detail_error(exc, "material_import_failed"),
            )
        return domain_error_response(exc)


# ─── Q09 AI completion ───────────────────────────────────────────────────────


def _missing_targets(task: dict) -> list[dict]:
    output = []
    labels = {
        "criterion": "Rubric", "reference_answer": "Reference answer",
        "solution_code": "Reference solution", "test_cases": "Test cases",
    }
    for q_id, problem in task["problem_data"].items():
        targets = ["criterion", "reference_answer"]
        if is_programming_question_type(problem.get("type")):
            targets.extend(["solution_code", "test_cases"])
        for target in targets:
            if problem.get(target):
                continue
            output.append({
                "target_id": f"{q_id}:{target}", "q_id": q_id,
                "question_number": problem.get("number", ""),
                "question_type": problem.get("type", ""),
                "target": target, "label": labels[target],
            })
    return output


@router.get("/{task_id}/ai-completions/preflight")
def ai_completion_preflight(task_id: str, current: User = Depends(require_teacher)):
    try:
        task = task_facade.get_task(task_id=task_id, owner_id=current.id, full=True)
    except DomainError as exc:
        return domain_error_response(exc)
    missing = _missing_targets(task)
    by_target = {target: sum(item["target"] == target for item in missing) for target in (
        "criterion", "reference_answer", "solution_code", "test_cases"
    )}
    return {
        "status": "ready", "task_id": task_id, "overwrite_policy": "missing_only",
        "missing_targets": missing,
        "summary": {"question_count": task["problem_count"], "missing_count": len(missing), "by_target": by_target},
        "workflow_revision": task["workflow_revision"],
        "provider_call_performed": False, "storage": "database",
    }


@router.post("/{task_id}/ai-completions/confirm")
async def confirm_ai_completion(
    task_id: str, request: ConfirmAICompletionRequest,
    background_tasks: BackgroundTasks,
    current: User = Depends(require_teacher),
    registry: ExpertRegistry = Depends(get_scoped_expert_registry),
):
    try:
        task = task_facade.get_task(task_id=task_id, owner_id=current.id, full=True)
        requested_ids = list(dict.fromkeys(request.target_ids))
        provider_id, route = _resolve_recognition_provider(
            owner_id=current.id,
            registry=registry,
            requested_provider_id=None,
        )
        input_hash = hashlib.sha256(json.dumps({
            "target_ids": sorted(requested_ids),
            "test_case_count": request.test_case_count,
            "base_revision": request.expected_workflow_revision,
            "provider_id": provider_id,
        }, sort_keys=True).encode()).hexdigest()
        replay = task_facade.find_task_operation(
            task_id=task_id, owner_id=current.id,
            operation_type="ai_completion", input_hash=input_hash,
        )
        if replay is not None and not task_facade._operation_is_retryable(replay):
            return {
                "status": task_facade._operation_state(replay),
                "job_id": replay.id, "task_id": task_id,
                "request_fingerprint": input_hash,
                "workflow_revision": task["workflow_revision"],
            }
        workflow = workflow_repository.get_workflow(task_id, owner_id=current.id)
        claim_base_revision = task_facade.retryable_operation_claim_revision(
            workflow=workflow, replay=replay,
            requested_revision=request.expected_workflow_revision,
        )
        allowed = {item["target_id"]: item for item in _missing_targets(task)}
        if len(requested_ids) != len(request.target_ids) or any(
            target_id not in allowed for target_id in requested_ids
        ):
            raise ValidationError(
                "A requested AI completion target is unknown or no longer missing.",
                code="unknown_ai_completion_target",
            )
        selected = [allowed[target_id] for target_id in requested_ids]
        workflow, active = task_facade._ensure_no_other_active_operation(
            task_id=task_id, owner_id=current.id,
            operation_type="ai_completion", input_hash=input_hash,
        )
        if active is not None:
            return {
                "status": "already_running", "job_id": active.id,
                "task_id": task_id, "request_fingerprint": input_hash,
                "workflow_revision": workflow.workflow_revision,
            }
        job, created = workflow_repository.create_operation(
            assignment_id=task_id, owner_id=current.id,
            operation_type="ai_completion", input_hash=input_hash,
            payload={
                "target_ids": requested_ids,
                "test_case_count": request.test_case_count,
                "base_workflow_revision": claim_base_revision,
                "provider_id": provider_id,
            }, expires_at=time.time() + task_facade._OPERATION_PUBLICATION_TTL_SECONDS,
            initial_status="preparing",
        )
        if not created:
            state = "already_running" if job.status in {"pending", "running"} else "already_done"
            return {"status": state, "job_id": job.id, "task_id": task_id,
                    "request_fingerprint": input_hash, "workflow_revision": task["workflow_revision"]}
        remove_reporter(job.id)
        try:
            claimed_revision = task_facade.activate_workflow_operation_atomic(
                task_id=task_id, owner_id=current.id, operation_id=job.id,
                expected_operation_attempt=job.attempt,
                expected_workflow_revision=claim_base_revision,
                operation_payload=dict(job.payload or {}),
                workflow_changes={
                    "active_operation": "ai_completion",
                    "active_job_id": job.id, "error_code": None,
                },
            )
        except VersionConflict:
            workflow_repository.update_operation(
                job.id, owner_id=current.id, expected_attempt=job.attempt,
                status="error",
                error_code="stale_revision", completed_at=time.time(),
            )
            task_facade._raise_stale_revision()
        return {
            "status": "started", "job_id": job.id, "task_id": task_id,
            "request_fingerprint": input_hash,
            "workflow_revision": claimed_revision,
        }
    except DomainError as exc:
        return domain_error_response(exc)


async def _run_ai_completion(
    *, task_id: str, owner_id: str, job_id: str, job_attempt: int,
    problems_data: dict[str, dict], selected: list[dict],
    requested_ids: list[str], test_case_count: int, provider,
    claimed_workflow_revision: int,
    expected_lease_token: str | None = None,
    durable_operation=None,
    recovered_candidates: list[AICompletionCandidateOutput] | None = None,
    result_artifact=None,
    result_provider_id: str | None = None,
) -> None:
    try:
        reporter = get_or_create_reporter(job_id)
        await reporter.configure_workflow(
            "ai_completion",
            ("generating_missing_materials", "validating_generated_materials", "applying_generated_materials"),
        )
        candidates = recovered_candidates
        if candidates is None:
            candidates = await generate_missing_question_materials(
                problems_data=problems_data, requested_targets=selected,
                test_case_count=test_case_count, provider=provider,
                reporter=reporter,
            )
            result_provider_id = provider.provider_id
            if durable_operation is not None:
                result_artifact = _save_auxiliary_result_artifact(
                    operation=durable_operation,
                    kind="ai_completion_result",
                    stage="ai-candidates",
                    payload={
                        "provider_id": result_provider_id,
                        "candidates": [
                            item.model_dump(mode="json") for item in candidates
                        ],
                    },
                )
        if durable_operation is not None and result_artifact is not None and (
            durable_operation.checkpoint_data.get("result_artifact_id")
            != result_artifact.id
        ):
            await durable_operation.checkpoint(
                stage="ai_candidates_generated",
                checkpoint={"result_artifact_id": result_artifact.id},
                artifact_refs=[result_artifact.id],
            )
        effective_provider_id = result_provider_id or provider.provider_id
        applied = []
        serialized = []
        generated_at = time.time()
        requested = {item["target_id"]: item for item in selected}
        patch_map: dict[str, dict[str, Any]] = {}
        seen_target_ids: set[str] = set()
        for index, candidate in enumerate(candidates, start=1):
            expected = requested.get(candidate.target_id)
            if (
                expected is None
                or candidate.target_id in seen_target_ids
                or expected["q_id"] != candidate.q_id
                or expected["target"] != candidate.target
            ):
                raise ValidationError(
                    "The AI returned an unknown completion target.",
                    code="unknown_ai_completion_target",
                )
            seen_target_ids.add(candidate.target_id)
            value = [case.model_dump(mode="json") for case in (candidate.test_cases or [])] if candidate.target == "test_cases" else candidate.text_value
            if not value:
                continue
            candidate_id = f"ai_{job_id}_{index}"
            provenance = {
                "job_id": job_id, "candidate_id": candidate_id,
                "source_kind": "ai_generated", "provider_id": effective_provider_id,
                "review_status": "pending", "generated_at": generated_at,
                "updated_at": generated_at,
            }
            patch = patch_map.setdefault(candidate.q_id, {
                "q_id": candidate.q_id, "fields": {},
                "presentation": {"ai_completion_provenance": {}},
            })
            if candidate.target == "solution_code":
                patch["presentation"]["solution_code"] = value
            else:
                patch["fields"][candidate.target] = value
            patch["presentation"]["ai_completion_provenance"][candidate.target] = provenance
            applied.append(candidate.target_id)
            serialized.append({
                "candidate_id": candidate_id, "target_id": candidate.target_id,
                "q_id": candidate.q_id, "target": candidate.target,
            })
        await reporter.set_stage_progress(
            "applying_generated_materials", total_steps=3, completed_steps=3,
            message="Generated materials applied.",
        )
        await reporter.set_phase("done")
        snapshot = (await reporter.snapshot()).model_dump(mode="json")
        task_facade.apply_question_patches_atomic(
            task_id=task_id, owner_id=owner_id,
            expected_workflow_revision=claimed_workflow_revision,
            patches=list(patch_map.values()), operation_id=job_id,
            expected_operation_attempt=job_attempt,
            expected_lease_token=expected_lease_token,
            required_operation_status="running", final_operation_status="done",
            operation_progress=snapshot, require_missing=True,
            operation_payload={
                "target_ids": requested_ids, "test_case_count": test_case_count,
                "base_workflow_revision": claimed_workflow_revision,
                "candidates": serialized,
                "applied_target_ids": applied,
                "skipped_target_ids": [item for item in requested_ids if item not in applied],
            },
        )
    except DomainError as exc:
        task_facade._fail_operation(
            task_id, owner_id, job_id, job_attempt,
            task_facade._detail_error(exc, "ai_completion_failed"),
            expected_lease_token=expected_lease_token,
        )
    except Exception as exc:
        logger.warning("Background AI completion failed; job_id=%s", job_id)
        task_facade._fail_operation(
            task_id, owner_id, job_id, job_attempt,
            classify_background_error(exc, "ai_completion_failed"),
            expected_lease_token=expected_lease_token,
        )


async def run_durable_ai_completion(operation) -> None:
    task = task_facade.get_task(task_id=operation.assignment_id, owner_id=operation.owner_id, full=True)
    requested_ids = list((operation.payload or {}).get("target_ids") or [])
    allowed = {item["target_id"]: item for item in _missing_targets(task)}
    selected = [allowed[item] for item in requested_ids if item in allowed]
    if len(selected) != len(requested_ids):
        raise ValidationError(
            "A requested AI completion target is unknown or no longer missing.",
            code="unknown_ai_completion_target",
        )
    result_artifact = _find_auxiliary_result_artifact(
        operation=operation, kind="ai_completion_result", stage="ai-candidates",
    )
    recovered_candidates = None
    result_provider_id = None
    if result_artifact is not None:
        raw_result = _read_auxiliary_result(result_artifact)
        if isinstance(raw_result, dict):
            result_provider_id = str(raw_result.get("provider_id") or "") or None
            raw_candidates = raw_result.get("candidates")
        else:
            raw_candidates = raw_result
        recovered_candidates = [
            AICompletionCandidateOutput.model_validate(item)
            for item in raw_candidates
        ]
    registry = task_facade._registry_for_owner(operation.owner_id)
    provider = registry.pick_default()
    if provider is None and recovered_candidates is None:
        raise ValidationError("No enabled provider is available.", code="no_provider_configured")
    if provider is None and result_provider_id is None:
        raise ValidationError("The durable AI result has no provider identity.", code="no_provider_configured")
    await _run_ai_completion(
        task_id=operation.assignment_id, owner_id=operation.owner_id,
        job_id=operation.operation_id, job_attempt=operation.attempt,
        problems_data=task["problem_data"], selected=selected,
        requested_ids=requested_ids,
        test_case_count=int((operation.payload or {}).get("test_case_count") or 5),
        provider=provider,
        claimed_workflow_revision=int((operation.payload or {}).get("base_workflow_revision") or 0) + 1,
        expected_lease_token=operation.lease_token,
        durable_operation=operation,
        recovered_candidates=recovered_candidates,
        result_artifact=result_artifact,
        result_provider_id=result_provider_id,
    )


def _auxiliary_result_name(operation, stage: str) -> str:
    return f"{operation.operation_id}-attempt-{operation.attempt}-{stage}.json"


def _find_auxiliary_result_artifact(*, operation, kind: str, stage: str):
    expected_name = _auxiliary_result_name(operation, stage)
    matches = [
        item for item in file_repository.list_files(
            owner_id=operation.owner_id, assignment_id=operation.assignment_id
        )
        if item.kind == kind and item.original_name == expected_name
    ]
    return max(matches, key=lambda item: item.created_at) if matches else None


def _save_auxiliary_result_artifact(*, operation, kind: str, stage: str, payload):
    return file_repository.save_file(
        storage=get_storage(), owner_id=operation.owner_id, kind=kind,
        original_name=_auxiliary_result_name(operation, stage),
        content=json.dumps(
            payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8"),
        content_type="application/json", assignment_id=operation.assignment_id,
    )


def _read_auxiliary_result(artifact):
    with get_storage().open(artifact.storage_key) as stream:
        return json.loads(stream.read().decode("utf-8"))


def _read_auxiliary_candidates(artifact, model_type):
    raw = _read_auxiliary_result(artifact)
    if not isinstance(raw, list):
        raise ValueError("Durable auxiliary result must be a list.")
    return [model_type.model_validate(item) for item in raw]


@router.get("/{task_id}/ai-completions/{job_id}")
async def get_ai_completion(task_id: str, job_id: str, current: User = Depends(require_teacher)):
    try:
        job = workflow_repository.get_operation(job_id, owner_id=current.id)
        if job.assignment_id != task_id or job.operation_type != "ai_completion":
            raise NotFound("ai_completion")
        payload = dict(job.payload or {})
        workflow = workflow_repository.get_workflow(task_id, owner_id=current.id)
        requested = list(payload.get("target_ids") or [])
        applied = list(payload.get("applied_target_ids") or [])
        skipped = list(payload.get("skipped_target_ids") or [])
        progress = job.progress or None
        if job.status == "running" and (reporter := get_reporter(job.id)) is not None:
            progress = (await reporter.snapshot()).model_dump(mode="json")
        by_target = {target: sum(item.endswith(f":{target}") for item in applied) for target in (
            "criterion", "reference_answer", "solution_code", "test_cases"
        )}
        return {
            "job_id": job.id, "task_id": task_id,
            "status": "done" if job.status == "done" else ("error" if job.status == "error" else "running"),
            "overwrite_policy": "missing_only", "target_ids": requested,
            "summary": {
                "requested_count": len(requested), "generated_count": len(payload.get("candidates") or []),
                "applied_count": len(applied), "skipped_count": len(skipped),
                "invalid_count": 0, "by_target": by_target,
            },
            "applied_target_ids": applied, "skipped_target_ids": skipped,
            "error": job.error_code, "progress": progress,
            "workflow_revision": workflow.workflow_revision,
            "created_at": job.created_at, "completed_at": job.completed_at,
            "expires_at": job.expires_at or job.created_at + SOURCE_TTL_SECONDS,
            "storage": "database",
        }
    except DomainError as exc:
        return domain_error_response(exc)


# ─── Legacy focused auxiliary uploads retained by existing UI actions ───────


@router.post("/{task_id}/upload_reference")
async def upload_reference(
    task_id: str, file: UploadFile = File(...),
    current: User = Depends(require_teacher),
    registry: ExpertRegistry = Depends(get_scoped_expert_registry),
):
    return await _apply_auxiliary_upload(
        task_id=task_id, file=file, current=current, registry=registry,
        target="reference_answer",
    )


@router.post("/{task_id}/upload_test_cases")
async def upload_test_cases(
    task_id: str, file: UploadFile = File(...),
    current: User = Depends(require_teacher),
    registry: ExpertRegistry = Depends(get_scoped_expert_registry),
):
    return await _apply_auxiliary_upload(
        task_id=task_id, file=file, current=current, registry=registry,
        target="test_cases",
    )


async def _apply_auxiliary_upload(
    *, task_id: str, file: UploadFile, current: User,
    registry: ExpertRegistry, target: str,
):
    try:
        task = task_facade.get_task(task_id=task_id, owner_id=current.id, full=True)
        _provider_id, route = _resolve_recognition_provider(
            owner_id=current.id,
            registry=registry,
            requested_provider_id=None,
        )
        text, _ = await _read_source(
            file=file,
            library_material_id=None,
            inline_text=None,
            owner_id=current.id,
            registry=registry,
            role=(
                "reference_answer"
                if target == "reference_answer"
                else "programming_tests"
            ),
            provider=route.provider,
        )
        if target == "reference_answer":
            mapping = await parse_reference_to_per_question(
                text, task["problem_data"], route.provider
            )
        else:
            mapping = await parse_test_cases_to_per_question(
                text, task["problem_data"], route.provider
            )
        count = 0
        for q_id, value in mapping.items():
            if q_id not in task["problem_data"]:
                continue
            if target == "test_cases":
                value = [
                    item.model_dump(mode="json") if hasattr(item, "model_dump") else item
                    for item in value
                ]
            task_facade.update_problem(
                task_id=task_id, owner_id=current.id, q_id=q_id,
                patch={target: value},
            )
            count += 1
        workflow_repository.update_workflow(
            task_id, owner_id=current.id,
            **({"reference_file_name": file.filename} if target == "reference_answer" else {"test_cases_file_name": file.filename}),
        )
        return {"status": "success", "task_id": task_id, f"{target}_count": count}
    except DomainError as exc:
        return domain_error_response(exc)
