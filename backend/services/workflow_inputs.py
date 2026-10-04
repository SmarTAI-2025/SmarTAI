"""Read the last formally submitted inputs without replaying any model work.

These are owner/task-bound projections, never raw operation payloads. Explicit
browser drafts remain separate and may contain newer, unsubmitted edits.
"""
import hashlib

from backend.db import course_library_repository, file_repository, workflow_repository
from backend.domain.errors import DomainError, InvalidTransition, NotFound
from backend.services.source_files import safe_display_name
from backend.storage import get_storage, StorageObjectNotFound
from backend.tools.file_processing import SUBMISSION_UPLOAD_MAX_BYTES


def latest_input_operation(*, task_id: str, owner_id: str, stage: str):
    workflow = workflow_repository.get_live_workflow(task_id, owner_id=owner_id)
    job_id = workflow.extract_job_id if stage == "problems" else workflow.parse_job_id
    if not job_id:
        return workflow, None
    operation = workflow_repository.get_operation(job_id, owner_id=owner_id)
    expected = "question_preparation" if stage == "problems" else "submission_recognition"
    if operation.assignment_id != task_id or operation.operation_type != expected:
        return workflow, None
    return workflow, operation


def question_inputs(*, task_id: str, owner_id: str) -> dict:
    workflow, operation = latest_input_operation(task_id=task_id, owner_id=owner_id, stage="problems")
    result = {"task_id": task_id, "workflow_revision": workflow.workflow_revision, "input": None}
    if operation is None:
        return result
    payload = operation.payload or {}
    sources = []
    for token in payload.get("source_tokens") or []:
        source = workflow_repository.get_operation(token, owner_id=owner_id)
        if source.assignment_id != task_id or source.operation_type != "problem_source":
            raise NotFound("problem_source")
        data = source.payload or {}
        ref = data.get("source_ref") or {}
        kind = data.get("source_kind", "upload")
        stored_id = ref.get("stored_file_id")
        material_id = data.get("library_material_id") if kind == "library" else None
        available = kind == "inline_text"
        if kind == "upload" and stored_id:
            stored = file_repository.get_file(file_id=stored_id, owner_id=owner_id)
            available = bool(stored and stored.assignment_id == task_id and stored.kind == "problem_source" and stored.availability_status == "available")
        elif kind == "library" and material_id:
            available = course_library_repository.get_material(material_id, owner_id) is not None
        sources.append({
            "source_token": token, "role": data.get("role", "problem"), "source_kind": kind,
            "filename": safe_display_name(data.get("filename") or "source"),
            "stored_file_id": stored_id if kind == "upload" and available else None,
            "library_material_id": material_id,
            "inline_text": data.get("text", "") if kind == "inline_text" else "",
            "structure_mode": data.get("structure_mode", "organized"),
            "extraction_hint": data.get("extraction_hint", ""),
            "recognition_options": data.get("recognition_options") or {},
            "enable_material_ocr": bool(data.get("enable_material_ocr")),
            "save_to_library": bool(data.get("save_to_library")), "available": available,
        })
    result["input"] = {
        "job_id": operation.id, "sources": sources,
        "recognition_provider_id": workflow.question_recognition_provider_id or payload.get("recognition_provider_id"),
        "score_policy": payload.get("score_policy") or {"mode": "default_10"},
    }
    return result


def submission_inputs(*, task_id: str, owner_id: str) -> dict:
    workflow, operation = latest_input_operation(task_id=task_id, owner_id=owner_id, stage="submissions")
    result = {"task_id": task_id, "workflow_revision": workflow.workflow_revision, "input": None}
    if operation is None:
        return result
    data = operation.payload or {}
    stored = file_repository.get_file(file_id=str(data.get("input_file_id") or ""), owner_id=owner_id)
    available = bool(stored and stored.assignment_id == task_id and stored.kind in {"submission_source", "submission_container"} and stored.availability_status == "available")
    result["input"] = {
        "job_id": operation.id, "stored_file_id": stored.id if stored and available else None,
        "filename": safe_display_name(stored.original_name) if stored else None,
        "available": available, "identity_mode": data.get("identity_mode", "filename"),
        "roster_name": safe_display_name(data["roster_name"]) if data.get("roster_name") else None,
        "roster_count": len(data.get("roster_entries") or []),
        "recognition_provider_id": workflow.submission_recognition_provider_id or data.get("recognition_provider_id"),
    }
    return result


def load_submission_input(*, task_id: str, owner_id: str, stored_file_id: str):
    stored = file_repository.get_file(file_id=stored_file_id, owner_id=owner_id)
    if not stored or stored.assignment_id != task_id or stored.kind not in {"submission_source", "submission_container"} or stored.availability_status != "available":
        raise NotFound("submission_source")
    try:
        with get_storage().open(stored.storage_key) as stream:
            content = stream.read(SUBMISSION_UPLOAD_MAX_BYTES + 1)
    except StorageObjectNotFound:
        raise InvalidTransition("The saved upload is unavailable.", code="submission_retry_source_unavailable") from None
    except Exception:
        raise DomainError("Saved files could not be read. Please retry.", code="source_preview_storage_unavailable", status_code=503) from None
    if not content or len(content) > SUBMISSION_UPLOAD_MAX_BYTES or hashlib.sha256(content).hexdigest() != stored.sha256:
        raise InvalidTransition("The saved upload is unavailable.", code="submission_retry_source_unavailable")
    return stored, content


def load_saved_roster(*, task_id: str, owner_id: str, job_id: str):
    _, operation = latest_input_operation(task_id=task_id, owner_id=owner_id, stage="submissions")
    if operation is None or operation.id != job_id:
        raise NotFound("submission_roster")
    data = operation.payload or {}
    return list(data.get("roster_entries") or []), data.get("roster_name")
