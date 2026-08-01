"""Question-preparation and material-completion task façade endpoints.

Source drafts and candidate plans are durable, TTL-bounded workflow-operation
rows.  Confirmed question fields are committed only to normalized assignment
question records.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
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
from pydantic import BaseModel, Field

from backend.agents.ingest_agent import (
    generate_missing_question_materials,
    parse_material_import_to_candidates,
    parse_reference_to_per_question,
    parse_test_cases_to_per_question,
)
from backend.agents.question_preparation_agent import (
    QUESTION_PREPARATION_STAGE_SEQUENCE,
    prepare_question_packages,
)
from backend.api.errors import domain_error_response
from backend.auth import require_teacher
from backend.db import assignment_repository, workflow_repository
from backend.domain.errors import (
    DomainError,
    InvalidTransition,
    NotFound,
    ValidationError,
    VersionConflict,
)
from backend.llm.registry import ExpertRegistry, get_scoped_expert_registry
from backend.knowledge.service import ingest_document
from backend.models import ProblemSourceDraft, User, is_programming_question_type
from backend.progress.tracker import get_or_create_reporter, get_reporter, remove_reporter
from backend.services import task_facade
from backend.skills.ocr_ingest import LLMVisionOCRSkill
from backend.tools.file_processing import extract_text_from_upload


router = APIRouter(prefix="/tasks", tags=["task-preparation"])
logger = logging.getLogger(__name__)

MAX_SOURCE_BYTES = 5 * 1024 * 1024
MAX_SOURCE_CHARACTERS = 400_000
SOURCE_TTL_SECONDS = 2 * 60 * 60


class StartQuestionPreparationRequest(BaseModel):
    source_tokens: list[str] = Field(min_length=1, max_length=20)
    expected_workflow_revision: int = Field(ge=0)
    replace_confirmed: bool = False
    generation_policy: Literal["complete_required_materials"] = "complete_required_materials"


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
    has_vision = registry.pick_vision() is not None
    common = {"accepted_extensions": [".pdf", ".txt", ".md"], "course_library": True, "inline_text": True}
    return {
        "contract_version": 1,
        "operation": "question_preparation",
        "stage_sequence": list(QUESTION_PREPARATION_STAGE_SEQUENCE),
        "source_roles": {
            "problem": dict(common),
            "reference_answer": dict(common),
            "rubric": dict(common),
            "programming_tests": {
                **common, "accepted_extensions": [".pdf", ".txt", ".md", ".json"]
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
    inline_text: str | None = Form(default=None),
    structure_mode: str = Form(default="organized"),
    role: str = Form(default="problem"),
    extraction_hint: str = Form(default=""),
    save_to_library: bool = Form(default=False),
    current: User = Depends(require_teacher),
    registry: ExpertRegistry = Depends(get_scoped_expert_registry),
):
    try:
        workflow = workflow_repository.get_workflow(task_id, owner_id=current.id)
        text, descriptor = await _read_source(
            file=file, library_material_id=library_material_id,
            inline_text=inline_text, owner_id=current.id, registry=registry,
            purpose="problems",
        )
        saved_material = await _save_source_to_library(
            save=save_to_library,
            descriptor=descriptor,
            owner_id=current.id,
            task_id=task_id,
            role=role,
            existing_material_id=library_material_id,
        )
        effective_material_id = (
            library_material_id
            or (saved_material or {}).get("material_id")
        )
        candidates = _detect_candidates(text)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        payload = {
            "text": text,
            "filename": descriptor["filename"],
            "content_type": descriptor.get("content_type"),
            "size_bytes": descriptor["size_bytes"],
            "sha256": digest,
            "source_kind": descriptor["kind"],
            "library_material_id": effective_material_id,
            "structure_mode": structure_mode,
            "role": role,
            "extraction_hint": extraction_hint,
            "candidates": candidates,
            "base_workflow_revision": workflow.workflow_revision,
        }
        operation, _ = workflow_repository.create_operation(
            assignment_id=task_id, owner_id=current.id,
            operation_type="problem_source", input_hash=_source_fingerprint(payload),
            payload=payload, expires_at=time.time() + SOURCE_TTL_SECONDS,
        )
        return {
            "status": "ready", "source_token": operation.id,
            "source": {
                "kind": descriptor["kind"], "filename": descriptor["filename"],
                "size_bytes": descriptor["size_bytes"], "sha256": digest,
                "library_material_id": effective_material_id,
            },
            "role": role, "structure_mode": structure_mode,
            "requires_confirmation": structure_mode == "extract_from_source" and bool(candidates),
            "candidate_summary": {
                "matched": candidates if structure_mode == "organized" else [],
                "possible_matches": candidates if structure_mode != "organized" else [],
                "not_found": [], "semantic_match_performed": False,
                "notice": None,
            },
            "base_workflow_revision": workflow.workflow_revision,
            "workflow_revision": workflow.workflow_revision,
            "saved_material": saved_material,
        }
    except DomainError as exc:
        return domain_error_response(exc)


async def _read_source(
    *, file: UploadFile | None, library_material_id: str | None,
    inline_text: str | None, owner_id: str, registry: ExpertRegistry,
    purpose: str,
) -> tuple[str, dict]:
    selected = int(file is not None) + int(bool(library_material_id)) + int(bool((inline_text or "").strip()))
    if selected != 1:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "exactly_one_source_required"})
    if file is not None:
        body = await file.read(MAX_SOURCE_BYTES + 1)
        if not body:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail={"code": "source_empty"})
        if len(body) > MAX_SOURCE_BYTES:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail={"code": "source_too_large"})
        provider = registry.pick_default()
        vision = registry.pick_vision(provider)
        ocr_skill = LLMVisionOCRSkill(vision) if vision is not None else None
        text = await extract_text_from_upload(
            body, file.filename or "source", ocr_skill=ocr_skill,
            purpose=purpose, reporter=None,
        )
        descriptor = {
            "kind": "upload", "filename": file.filename or "source",
            "size_bytes": len(body), "content_type": file.content_type,
            "_body": body,
        }
    elif library_material_id:
        from backend.db import course_library_repository
        from backend.db.knowledge_repository import list_chunks

        material = course_library_repository.get_material(
            material_id=library_material_id, owner_id=owner_id
        )
        if material is None:
            raise NotFound("course_material")
        chunks = list_chunks([material.document_id])
        text = "\n\n".join(chunk.content for chunk in chunks)
        descriptor = {
            "kind": "library", "filename": material.filename,
            "size_bytes": material.size_bytes or len(text.encode("utf-8")),
            "content_type": material.content_type,
        }
    else:
        text = (inline_text or "").strip()
        descriptor = {
            "kind": "inline_text", "filename": "inline-text.txt",
            "size_bytes": len(text.encode("utf-8")), "content_type": "text/plain",
            "_body": text.encode("utf-8"),
        }
    text = text.strip()
    if not text:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail={"code": "source_empty"})
    if len(text) > MAX_SOURCE_CHARACTERS:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail={"code": "source_text_too_large"})
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
            "sha256", "source_kind", "library_material_id", "structure_mode",
            "role", "extraction_hint", "targets", "base_workflow_revision",
        )
    }
    return hashlib.sha256(json.dumps(selected, sort_keys=True).encode()).hexdigest()


@router.post("/{task_id}/question-preparation/jobs")
async def start_question_preparation(
    task_id: str,
    request: StartQuestionPreparationRequest,
    background_tasks: BackgroundTasks,
    current: User = Depends(require_teacher),
    registry: ExpertRegistry = Depends(get_scoped_expert_registry),
):
    try:
        workflow = workflow_repository.get_workflow(task_id, owner_id=current.id)
        sources = []
        source_fingerprints = []
        for token in request.source_tokens:
            operation = workflow_repository.get_operation(token, owner_id=current.id)
            if operation.assignment_id != task_id or operation.operation_type != "problem_source":
                raise NotFound("problem_source")
            payload = dict(operation.payload or {})
            if operation.expires_at and operation.expires_at < time.time():
                raise InvalidTransition("Problem source expired.", code="stale_revision")
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
            if draft.base_workflow_revision != request.expected_workflow_revision:
                raise VersionConflict(
                    "A selected problem source was prepared from an older task version.",
                    code="stale_revision",
                )
            sources.append((draft, str(payload.get("text") or "")))
            source_fingerprints.append(operation.input_hash)
        operation_hash = hashlib.sha256(json.dumps({
            "sources": sorted(source_fingerprints),
            "base_revision": request.expected_workflow_revision,
            "replace_confirmed": request.replace_confirmed,
            "generation_policy": request.generation_policy,
        }, sort_keys=True).encode()).hexdigest()
        replay = task_facade.find_task_operation(
            task_id=task_id, owner_id=current.id,
            operation_type="question_preparation", input_hash=operation_hash,
        )
        if replay is not None and not task_facade._operation_is_retryable(replay):
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
        provider = registry.pick_default()
        if provider is None:
            raise ValidationError(
                "No enabled provider is available.", code="no_provider_configured"
            )
        workflow, active = task_facade._ensure_no_other_active_operation(
            task_id=task_id, owner_id=current.id,
            operation_type="question_preparation", input_hash=operation_hash,
        )
        if active is not None:
            return {
                "status": "already_running", "task_id": task_id,
                "job_id": active.id,
                "workflow_revision": workflow.workflow_revision,
            }
        job, created = workflow_repository.create_operation(
            assignment_id=task_id, owner_id=current.id,
            operation_type="question_preparation", input_hash=operation_hash,
            payload={
                "source_tokens": request.source_tokens,
                "base_workflow_revision": claim_base_revision,
                "replace_confirmed": request.replace_confirmed,
            },
            expires_at=time.time() + SOURCE_TTL_SECONDS,
        )
        if not created:
            state = task_facade._operation_state(job)
            return {"status": state, "task_id": task_id, "job_id": job.id,
                    "workflow_revision": workflow.workflow_revision}
        remove_reporter(job.id)
        try:
            claimed_revision = task_facade.claim_workflow_operation_atomic(
                task_id=task_id, owner_id=current.id, operation_id=job.id,
                expected_operation_attempt=job.attempt,
                expected_workflow_revision=claim_base_revision,
                workflow_changes={
                    "presentation_status": "extracting_problems",
                    "active_operation": "question_preparation",
                    "active_job_id": job.id, "extract_job_id": job.id,
                    "error_code": None,
                },
            )
        except VersionConflict:
            workflow_repository.update_operation(
                job.id, owner_id=current.id, expected_attempt=job.attempt,
                status="error",
                error_code="stale_revision", completed_at=time.time(),
            )
            task_facade._raise_stale_revision()
        background_tasks.add_task(
            _run_question_preparation,
            task_id=task_id, owner_id=current.id, job_id=job.id,
            job_attempt=job.attempt,
            sources=sources,
            provider=provider,
            claimed_workflow_revision=claimed_revision,
            replace_confirmed=request.replace_confirmed,
        )
        return {
            "status": "started", "task_id": task_id, "job_id": job.id,
            "source_count": len(sources), "operation": "question_preparation",
            "progress_contract_version": 1,
            "workflow_revision": claimed_revision,
        }
    except DomainError as exc:
        return domain_error_response(exc)


async def _run_question_preparation(
    *, task_id: str, owner_id: str, job_id: str, job_attempt: int,
    sources: list[tuple[ProblemSourceDraft, str]],
    provider, claimed_workflow_revision: int, replace_confirmed: bool,
) -> None:
    try:
        reporter = get_or_create_reporter(job_id)
        packages = await prepare_question_packages(
            sources, provider, provider_id=provider.provider_id,
            reporter=reporter,
        )
        await reporter.set_stage_progress(
            "committing_question_packages", total_steps=8, completed_steps=8,
            message="Question packages committed.",
        )
        await reporter.set_phase("done")
        snapshot = (await reporter.snapshot()).model_dump(mode="json")
        task_facade._replace_draft_questions(
            task_id, owner_id, packages,
            ", ".join(draft.filename for draft, _ in sources),
            expected_workflow_revision=claimed_workflow_revision,
            replace_confirmed=replace_confirmed,
            operation_id=job_id,
            expected_operation_attempt=job_attempt,
            operation_progress=snapshot,
        )
    except DomainError as exc:
        task_facade._fail_operation(
            task_id, owner_id, job_id, job_attempt,
            task_facade._detail_error(exc, "problem_extraction_failed"),
        )
    except Exception:
        logger.warning("Background question preparation failed; job_id=%s", job_id)
        task_facade._fail_operation(
            task_id, owner_id, job_id, job_attempt, "problem_extraction_failed"
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
        text, descriptor = await _read_source(
            file=file, library_material_id=library_material_id,
            inline_text=None, owner_id=current.id, registry=registry,
            purpose="problems",
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
        payload = {
            "text": text, "filename": descriptor["filename"],
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
        operation_hash = hashlib.sha256(json.dumps({
            "source": source.input_hash,
            "base_revision": base_revision,
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
        provider = registry.pick_default()
        if provider is None:
            raise ValidationError(
                "No enabled provider is available.", code="no_provider_configured"
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
            }, expires_at=time.time() + SOURCE_TTL_SECONDS,
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
            claimed_revision = task_facade.claim_workflow_operation_atomic(
                task_id=task_id, owner_id=current.id, operation_id=job.id,
                expected_operation_attempt=job.attempt,
                expected_workflow_revision=claim_base_revision,
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
        background_tasks.add_task(
            _run_material_import,
            task_id=task_id, owner_id=current.id, job_id=job.id,
            job_attempt=job.attempt,
            source_id=source.id, source_payload=payload,
            problems_data=task["problem_data"], provider=provider,
            claimed_workflow_revision=claimed_revision,
        )
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
) -> None:
    try:
        reporter = get_or_create_reporter(job_id)
        await reporter.configure_workflow(
            "material_import", ("matching_questions", "validating_matches", "plan_ready")
        )
        candidates = await parse_material_import_to_candidates(
            text=str(source_payload.get("text") or ""),
            problems_data=problems_data,
            targets=list(source_payload.get("targets") or []),
            structure_mode=str(source_payload.get("structure_mode") or "organized"),
            extraction_hint=str(source_payload.get("extraction_hint") or ""),
            provider=provider, reporter=reporter,
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
        task_facade.complete_planning_operation_atomic(
            task_id=task_id, owner_id=owner_id,
            expected_workflow_revision=claimed_workflow_revision,
            operation_id=job_id,
            expected_operation_attempt=job_attempt,
            progress=snapshot,
            payload={
                **source_payload, "source_token": source_id, "candidates": serialized,
                "applied_candidate_ids": [], "completed_at": completed_at,
                "base_workflow_revision": claimed_workflow_revision,
            },
        )
    except DomainError as exc:
        task_facade._fail_operation(
            task_id, owner_id, job_id, job_attempt,
            task_facade._detail_error(exc, "material_import_failed"),
        )
    except Exception:
        logger.warning("Background material import failed; job_id=%s", job_id)
        task_facade._fail_operation(
            task_id, owner_id, job_id, job_attempt, "material_import_failed"
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
        input_hash = hashlib.sha256(json.dumps({
            "target_ids": sorted(requested_ids),
            "test_case_count": request.test_case_count,
            "base_revision": request.expected_workflow_revision,
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
        provider = registry.pick_default()
        if provider is None:
            raise ValidationError(
                "No enabled provider is available.", code="no_provider_configured"
            )
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
            }, expires_at=time.time() + SOURCE_TTL_SECONDS,
        )
        if not created:
            state = "already_running" if job.status in {"pending", "running"} else "already_done"
            return {"status": state, "job_id": job.id, "task_id": task_id,
                    "request_fingerprint": input_hash, "workflow_revision": task["workflow_revision"]}
        remove_reporter(job.id)
        try:
            claimed_revision = task_facade.claim_workflow_operation_atomic(
                task_id=task_id, owner_id=current.id, operation_id=job.id,
                expected_operation_attempt=job.attempt,
                expected_workflow_revision=claim_base_revision,
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
        background_tasks.add_task(
            _run_ai_completion,
            task_id=task_id, owner_id=current.id, job_id=job.id,
            job_attempt=job.attempt,
            problems_data=task["problem_data"], selected=selected,
            requested_ids=requested_ids, test_case_count=request.test_case_count,
            provider=provider, claimed_workflow_revision=claimed_revision,
        )
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
) -> None:
    try:
        reporter = get_or_create_reporter(job_id)
        await reporter.configure_workflow(
            "ai_completion",
            ("generating_missing_materials", "validating_generated_materials", "applying_generated_materials"),
        )
        candidates = await generate_missing_question_materials(
            problems_data=problems_data, requested_targets=selected,
            test_case_count=test_case_count, provider=provider,
            reporter=reporter,
        )
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
                "source_kind": "ai_generated", "provider_id": provider.provider_id,
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
        )
    except Exception:
        logger.warning("Background AI completion failed; job_id=%s", job_id)
        task_facade._fail_operation(
            task_id, owner_id, job_id, job_attempt, "ai_completion_failed"
        )


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
        provider = registry.pick_default()
        if provider is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail={"code": "provider_required"})
        body = await file.read()
        vision = registry.pick_vision(provider)
        text = await extract_text_from_upload(
            body, file.filename or "material", ocr_skill=LLMVisionOCRSkill(vision) if vision else None,
            purpose="problems",
        )
        if target == "reference_answer":
            mapping = await parse_reference_to_per_question(
                text, task["problem_data"], provider
            )
        else:
            mapping = await parse_test_cases_to_per_question(
                text, task["problem_data"], provider
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
