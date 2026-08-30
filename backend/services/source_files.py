"""Resolve and safely read the original files selected by a task workflow.

This is intentionally not a generic stored-file download service.  Every read
starts from an owner-scoped task and recomputes the current workflow operation,
attempt, source, and stored-file relationship before opening storage.
"""
from __future__ import annotations

import hashlib
import logging
from contextlib import closing
from dataclasses import dataclass
from pathlib import PurePosixPath

from backend.db import (
    assignment_repository,
    course_library_repository,
    file_repository,
    source_outcome_repository,
    source_storage_repository,
    workflow_repository,
)
from backend.domain.errors import DomainError
from backend.domain.source_storage import (
    SOURCE_FILE_AVAILABLE,
    SOURCE_FILE_CLEANUP_PENDING,
    SOURCE_FILE_UNAVAILABLE,
    SOURCE_REASON_MISSING,
    SOURCE_REASON_STORAGE_DELETE_FAILED,
    SOURCE_REASON_TASK_FINALIZED,
)
from backend.storage.base import (
    StorageBackend,
    StorageObjectNotFound,
    StorageUnavailable,
)
from backend.tools.file_processing import inspect_upload_content


logger = logging.getLogger(__name__)

_ALLOWED_PREVIEW_MIME = {
    "application/pdf": "pdf",
    "image/jpeg": "image",
    "image/png": "image",
    "image/webp": "image",
}
_SNIFF_BYTES = 4096
_MAX_DISPLAY_NAME = 255


class SourcePreviewNotFound(DomainError):
    code = "source_preview_not_found"
    status_code = 404


class SourcePreviewProcessing(DomainError):
    code = "source_preview_processing"
    status_code = 409


class SourcePreviewUnavailable(DomainError):
    code = "source_preview_unavailable"
    status_code = 410


class SourceCleanupPending(DomainError):
    code = "source_cleanup_pending"
    status_code = 409


class SourceUnavailableTaskFinalized(DomainError):
    code = "source_unavailable_task_finalized"
    status_code = 410


class SourceUnavailableMissing(DomainError):
    code = "source_unavailable_missing"
    status_code = 404


class SourcePreviewUnsupportedType(DomainError):
    code = "source_preview_unsupported_type"
    status_code = 415


class SourcePreviewStorageUnavailable(DomainError):
    code = "source_preview_storage_unavailable"
    status_code = 503


@dataclass(frozen=True)
class SourceFileContent:
    content: bytes
    mime_type: str
    display_name: str


@dataclass(frozen=True)
class _ResolvedSource:
    descriptor: dict
    stored: file_repository.StoredFile | None


def _raise_if_source_not_available(stored: file_repository.StoredFile) -> None:
    if stored.availability_status == SOURCE_FILE_CLEANUP_PENDING:
        raise SourceCleanupPending(
            "The task original is being cleaned automatically."
        )
    if stored.availability_status == SOURCE_FILE_UNAVAILABLE:
        if stored.availability_reason == SOURCE_REASON_TASK_FINALIZED:
            raise SourceUnavailableTaskFinalized(
                "The task original was cleaned after completion."
            )
        raise SourceUnavailableMissing("The task original is missing.")


def safe_display_name(value: str | None, *, fallback: str = "source") -> str:
    """Return a bounded basename with control characters removed."""

    normalized = str(value or fallback).replace("\\", "/")
    name = PurePosixPath(normalized).name or fallback
    name = "".join(character for character in name if ord(character) >= 32 and ord(character) != 127)
    if not name:
        name = fallback
    if len(name) <= _MAX_DISPLAY_NAME:
        return name
    suffix = PurePosixPath(name).suffix[:20]
    digest = hashlib.sha256(name.encode("utf-8", errors="replace")).hexdigest()[:12]
    prefix_length = _MAX_DISPLAY_NAME - len(suffix) - len(digest) - 1
    return f"{name[:prefix_length]}~{digest}{suffix}"


def _active_content_type(prefix: bytes) -> str | None:
    """Detect browser-active markup before applying the binary allowlist."""

    sample = prefix[:_SNIFF_BYTES]
    stripped = sample.lstrip(b"\xef\xbb\xbf\x00\t\r\n ").lower()
    if not stripped:
        return None
    if stripped.startswith((b"<!doctype html", b"<html", b"<script", b"<iframe")):
        return "text/html"
    if stripped.startswith(b"<svg") or (
        stripped.startswith(b"<?xml") and b"<svg" in stripped
    ):
        return "image/svg+xml"
    if b"<html" in stripped[:1024] or b"<script" in stripped[:1024]:
        return "text/html"
    return None


def detected_preview_mime(content_prefix: bytes, display_name: str) -> str:
    """Return a content-derived MIME, explicitly preferring active markup."""

    active_type = _active_content_type(content_prefix)
    if active_type is not None:
        return active_type
    return inspect_upload_content(content_prefix, display_name).content_type


def persist_problem_source(
    *,
    storage: StorageBackend,
    owner_id: str,
    task_id: str,
    original_name: str,
    content: bytes,
    content_type: str,
) -> tuple[file_repository.StoredFile, bool]:
    """Save one assignment-linked original, reusing only verified live bytes."""

    digest = hashlib.sha256(content).hexdigest()
    for candidate in file_repository.list_files(
        owner_id=owner_id, assignment_id=task_id
    ):
        if (
            candidate.kind != "problem_source"
            or candidate.sha256 != digest
            or candidate.availability_status != SOURCE_FILE_AVAILABLE
        ):
            continue
        try:
            with closing(storage.open(candidate.storage_key)) as stream:
                existing = stream.read()
        except StorageObjectNotFound:
            continue
        except StorageUnavailable:
            raise
        except Exception:
            raise StorageUnavailable("storage_unavailable") from None
        if existing == content:
            return candidate, False

    saved = file_repository.save_file(
        storage=storage,
        owner_id=owner_id,
        kind="problem_source",
        original_name=safe_display_name(original_name),
        content=content,
        content_type=content_type,
        assignment_id=task_id,
    )
    return saved, True


def _unavailable_descriptor(
    *,
    display_name: str,
    source_id: str | None = None,
    file_id: str | None = None,
    mime_type: str | None = None,
    size_bytes: int | None = None,
    reason: str,
    processing: bool = False,
) -> dict:
    return {
        "source_id": source_id,
        "file_id": file_id,
        "display_name": safe_display_name(display_name),
        "mime_type": mime_type,
        "size_bytes": size_bytes,
        "status": "processing" if processing else "unavailable",
        "preview_kind": "unsupported",
        "unavailable_reason": None if processing else reason,
    }


def _open_for_inspection(
    *, storage: StorageBackend, stored: file_repository.StoredFile, task_id: str
) -> tuple[bytes, int]:
    try:
        with closing(storage.open(stored.storage_key)) as stream:
            prefix = stream.read(_SNIFF_BYTES)
            if not isinstance(prefix, bytes):
                raise StorageUnavailable("storage_unavailable")
            try:
                stream.seek(0, 2)
                size = int(stream.tell())
            except (AttributeError, OSError):
                remainder = stream.read()
                if not isinstance(remainder, bytes):
                    raise StorageUnavailable("storage_unavailable")
                size = len(prefix) + len(remainder)
            return prefix, size
    except StorageObjectNotFound:
        raise
    except StorageUnavailable:
        raise
    except Exception as exc:
        logger.warning(
            "Source preview storage inspection failed; task_id=%s file_id=%s exception_type=%s",
            task_id,
            stored.id,
            type(exc).__name__,
        )
        raise StorageUnavailable("storage_unavailable") from None


def _descriptor_for_stored(
    *,
    storage: StorageBackend,
    task_id: str,
    source_id: str | None,
    stored: file_repository.StoredFile,
) -> _ResolvedSource:
    display_name = safe_display_name(stored.original_name)
    if stored.availability_status == SOURCE_FILE_CLEANUP_PENDING:
        reason = (
            SOURCE_REASON_STORAGE_DELETE_FAILED
            if stored.availability_reason == SOURCE_REASON_STORAGE_DELETE_FAILED
            else "cleanup_pending"
        )
        return _ResolvedSource(
            descriptor={
                **_unavailable_descriptor(
                    source_id=source_id,
                    file_id=stored.id,
                    display_name=display_name,
                    mime_type=stored.content_type,
                    size_bytes=stored.size_bytes,
                    reason=reason,
                ),
                "status": SOURCE_FILE_CLEANUP_PENDING,
            },
            stored=stored,
        )
    if stored.availability_status == SOURCE_FILE_UNAVAILABLE:
        reason = (
            SOURCE_REASON_TASK_FINALIZED
            if stored.availability_reason == SOURCE_REASON_TASK_FINALIZED
            else SOURCE_REASON_MISSING
        )
        return _ResolvedSource(
            descriptor=_unavailable_descriptor(
                source_id=source_id,
                file_id=stored.id,
                display_name=display_name,
                mime_type=stored.content_type,
                size_bytes=stored.size_bytes,
                reason=reason,
            ),
            stored=stored,
        )
    try:
        prefix, actual_size = _open_for_inspection(
            storage=storage, stored=stored, task_id=task_id
        )
    except StorageObjectNotFound:
        logger.warning(
            "Current source object is missing; task_id=%s file_id=%s",
            task_id,
            stored.id,
        )
        current = file_repository.get_file(
            file_id=stored.id, owner_id=stored.owner_id
        )
        if (
            current is not None
            and current.availability_status == SOURCE_FILE_AVAILABLE
            and current.source_quota_owner_id is not None
        ):
            source_storage_repository.mark_available_source_missing(
                file_id=current.id,
                owner_id=current.owner_id,
                assignment_id=task_id,
            )
            current = file_repository.get_file(
                file_id=stored.id, owner_id=stored.owner_id
            )
        # Finalization may have changed lifecycle state after the initial DTO
        # was loaded but before storage.open. Project that durable state instead
        # of misclassifying a normal cleanup race as an independently missing
        # object. The recursive call cannot reopen a non-available row.
        if (
            current is not None
            and current.availability_status != SOURCE_FILE_AVAILABLE
        ):
            return _descriptor_for_stored(
                storage=storage,
                task_id=task_id,
                source_id=source_id,
                stored=current,
            )
        return _ResolvedSource(
            descriptor=_unavailable_descriptor(
                source_id=source_id,
                file_id=stored.id,
                display_name=display_name,
                mime_type=None,
                size_bytes=stored.size_bytes,
                reason=SOURCE_REASON_MISSING,
            ),
            stored=current or stored,
        )
    except StorageUnavailable:
        raise SourcePreviewStorageUnavailable(
            "Source storage is temporarily unavailable."
        ) from None

    mime_type = detected_preview_mime(prefix, display_name)
    preview_kind = _ALLOWED_PREVIEW_MIME.get(mime_type)
    if preview_kind is None:
        return _ResolvedSource(
            descriptor=_unavailable_descriptor(
                source_id=source_id,
                file_id=stored.id,
                display_name=display_name,
                mime_type=mime_type,
                size_bytes=actual_size,
                reason="unsupported_type",
            ),
            stored=stored,
        )
    return _ResolvedSource(
        descriptor={
            "source_id": source_id,
            "file_id": stored.id,
            "display_name": display_name,
            "mime_type": mime_type,
            "size_bytes": actual_size,
            "status": "available",
            "preview_kind": preview_kind,
            "unavailable_reason": None,
        },
        stored=stored,
    )


def _owned_assignment_file(
    *, file_id: str, owner_id: str, task_id: str
) -> file_repository.StoredFile:
    stored = file_repository.get_file(file_id=file_id, owner_id=owner_id)
    if stored is None or stored.assignment_id != task_id:
        raise SourcePreviewNotFound("Source preview not found.")
    return stored


def _problem_from_workflow_source(
    *,
    storage: StorageBackend,
    task_id: str,
    owner_id: str,
    source_id: str,
    expected_operation_id: str,
    expected_attempt: int,
    expected_operation_type: str,
) -> _ResolvedSource:
    try:
        source_operation = workflow_repository.get_operation(
            expected_operation_id, owner_id=owner_id
        )
        source = source_outcome_repository.get_source(source_id, owner_id=owner_id)
    except DomainError:
        raise SourcePreviewNotFound("Source preview not found.") from None
    if (
        source_operation.assignment_id != task_id
        or source_operation.operation_type != expected_operation_type
        or source_operation.attempt != expected_attempt
        or source.assignment_id != task_id
        or source.operation_id != expected_operation_id
        or source.attempt != expected_attempt
    ):
        raise SourcePreviewNotFound("Source preview not found.")
    stored = _owned_assignment_file(
        file_id=source.stored_file_id, owner_id=owner_id, task_id=task_id
    )
    if stored.kind != "problem_source":
        raise SourcePreviewNotFound("Source preview not found.")
    return _descriptor_for_stored(
        storage=storage, task_id=task_id, source_id=source.id, stored=stored
    )


def _problem_from_question_preparation(
    *,
    storage: StorageBackend,
    task_id: str,
    owner_id: str,
    operation,
    workflow,
) -> _ResolvedSource | None:
    refs = operation.payload.get("source_refs")
    if not isinstance(refs, list):
        refs = operation.checkpoint.get("source_refs")
    if not isinstance(refs, list):
        refs = []
    selected = next(
        (
            ref
            for ref in refs
            if isinstance(ref, dict) and ref.get("role") == "problem"
        ),
        None,
    )
    if selected is None:
        if operation.status in {"preparing", "pending"}:
            return _ResolvedSource(
                descriptor=_unavailable_descriptor(
                    display_name=workflow.problem_file_name or "source",
                    reason="not_persisted",
                    processing=True,
                ),
                stored=None,
            )
        return None

    display_name = safe_display_name(selected.get("display_name") or "source")
    source_kind = selected.get("source_kind")
    if source_kind == "inline_text":
        return _ResolvedSource(
            descriptor=_unavailable_descriptor(
                display_name=display_name, reason="not_persisted"
            ),
            stored=None,
        )
    if source_kind == "library":
        material_id = selected.get("library_material_id")
        file_id = selected.get("stored_file_id")
        if not isinstance(material_id, str) or not isinstance(file_id, str):
            return _ResolvedSource(
                descriptor=_unavailable_descriptor(
                    display_name=display_name, reason="not_persisted"
                ),
                stored=None,
            )
        material = course_library_repository.get_material(material_id, owner_id)
        if material is None or material.stored_file_id != file_id:
            raise SourcePreviewNotFound("Source preview not found.")
        stored = file_repository.get_file(file_id=file_id, owner_id=owner_id)
        if (
            stored is None
            or stored.knowledge_document_id != material.document_id
            or stored.assignment_id is not None
        ):
            raise SourcePreviewNotFound("Source preview not found.")
        return _descriptor_for_stored(
            storage=storage, task_id=task_id, source_id=None, stored=stored
        )

    source_id = selected.get("source_id")
    file_id = selected.get("stored_file_id")
    source_operation_id = selected.get("source_operation_id")
    source_attempt = selected.get("source_attempt")
    if not all(isinstance(value, str) for value in (source_id, file_id, source_operation_id)):
        return _ResolvedSource(
            descriptor=_unavailable_descriptor(
                display_name=display_name, reason="not_persisted"
            ),
            stored=None,
        )
    if not isinstance(source_attempt, int) or source_attempt <= 0:
        raise SourcePreviewNotFound("Source preview not found.")
    resolved = _problem_from_workflow_source(
        storage=storage,
        task_id=task_id,
        owner_id=owner_id,
        source_id=source_id,
        expected_operation_id=source_operation_id,
        expected_attempt=source_attempt,
        expected_operation_type="problem_source",
    )
    if resolved.stored is None or resolved.stored.id != file_id:
        raise SourcePreviewNotFound("Source preview not found.")
    return resolved


def _resolve_problem_source(
    *, storage: StorageBackend, task_id: str, owner_id: str, workflow
) -> _ResolvedSource | None:
    operation_id = workflow.extract_job_id
    if not operation_id:
        if workflow.problem_file_name:
            return _ResolvedSource(
                descriptor=_unavailable_descriptor(
                    display_name=workflow.problem_file_name,
                    reason="not_persisted",
                ),
                stored=None,
            )
        return None
    try:
        operation = workflow_repository.get_operation(
            operation_id, owner_id=owner_id
        )
    except DomainError:
        raise SourcePreviewNotFound("Source preview not found.") from None
    if operation.assignment_id != task_id:
        raise SourcePreviewNotFound("Source preview not found.")
    if operation.operation_type == "question_preparation":
        resolved = _problem_from_question_preparation(
            storage=storage,
            task_id=task_id,
            owner_id=owner_id,
            operation=operation,
            workflow=workflow,
        )
        if resolved is not None:
            return resolved
    elif operation.operation_type == "problem_extraction":
        source_id = operation.payload.get("source_id")
        if isinstance(source_id, str):
            return _problem_from_workflow_source(
                storage=storage,
                task_id=task_id,
                owner_id=owner_id,
                source_id=source_id,
                expected_operation_id=operation.id,
                expected_attempt=operation.attempt,
                expected_operation_type="problem_extraction",
            )
    return _ResolvedSource(
        descriptor=_unavailable_descriptor(
            display_name=workflow.problem_file_name or "source",
            reason="not_persisted",
        ),
        stored=None,
    )


def _resolve_submission_sources(
    *, storage: StorageBackend, task_id: str, owner_id: str, workflow
) -> list[_ResolvedSource]:
    if not workflow.parse_job_id:
        return []
    try:
        operation = workflow_repository.get_operation(
            workflow.parse_job_id, owner_id=owner_id
        )
        if (
            operation.assignment_id != task_id
            or operation.operation_type != "submission_recognition"
        ):
            raise SourcePreviewNotFound("Source preview not found.")
        sources = source_outcome_repository.list_sources(
            operation_id=operation.id,
            owner_id=owner_id,
            attempt=operation.attempt,
        )
    except SourcePreviewNotFound:
        raise
    except DomainError:
        raise SourcePreviewNotFound("Source preview not found.") from None

    resolved: list[_ResolvedSource] = []
    for source in sources:
        if (
            source.assignment_id != task_id
            or source.operation_id != operation.id
            or source.attempt != operation.attempt
        ):
            raise SourcePreviewNotFound("Source preview not found.")
        stored = _owned_assignment_file(
            file_id=source.stored_file_id, owner_id=owner_id, task_id=task_id
        )
        if stored.kind == "submission_source_reference":
            resolved.append(_ResolvedSource(
                descriptor=_unavailable_descriptor(
                    source_id=source.id,
                    display_name=source.original_name,
                    reason="not_persisted",
                ),
                stored=None,
            ))
            continue
        if stored.kind != "submission_source":
            raise SourcePreviewNotFound("Source preview not found.")
        resolved.append(_descriptor_for_stored(
            storage=storage, task_id=task_id, source_id=source.id, stored=stored
        ))
    return resolved


def _resolve_current_sources(
    *, task_id: str, owner_id: str, storage: StorageBackend
) -> tuple[object, _ResolvedSource | None, list[_ResolvedSource]]:
    try:
        assignment_repository.get_assignment(task_id, actor_id=owner_id)
        workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    except DomainError:
        raise SourcePreviewNotFound("Source preview not found.") from None
    problem = _resolve_problem_source(
        storage=storage, task_id=task_id, owner_id=owner_id, workflow=workflow
    )
    submissions = _resolve_submission_sources(
        storage=storage, task_id=task_id, owner_id=owner_id, workflow=workflow
    )
    return workflow, problem, submissions


def describe_source_files(
    *, task_id: str, owner_id: str, storage: StorageBackend
) -> dict:
    workflow, problem, submissions = _resolve_current_sources(
        task_id=task_id, owner_id=owner_id, storage=storage
    )
    usage = source_storage_repository.source_quota_usage(owner_id)
    return {
        "task_id": task_id,
        "workflow_revision": workflow.workflow_revision,
        "source_storage": usage.as_dict(),
        "source_cleanup": source_storage_repository.cleanup_summary(
            assignment_id=task_id, owner_id=owner_id
        ),
        "problem_source": problem.descriptor if problem is not None else None,
        "submission_sources": {
            item.descriptor["source_id"]: item.descriptor
            for item in submissions
            if item.descriptor["source_id"] is not None
        },
    }


def _read_current_content(
    *, storage: StorageBackend, stored: file_repository.StoredFile, task_id: str
) -> bytes:
    current = file_repository.get_file(
        file_id=stored.id, owner_id=stored.owner_id
    )
    if current is None:
        raise SourcePreviewNotFound("Source preview not found.")
    _raise_if_source_not_available(current)
    try:
        with closing(storage.open(stored.storage_key)) as stream:
            content = stream.read()
    except StorageObjectNotFound:
        refreshed = file_repository.get_file(
            file_id=current.id, owner_id=current.owner_id
        )
        if refreshed is None:
            raise SourcePreviewNotFound("Source preview not found.") from None
        _raise_if_source_not_available(refreshed)
        if refreshed.source_quota_owner_id is not None:
            source_storage_repository.mark_available_source_missing(
                file_id=refreshed.id,
                owner_id=refreshed.owner_id,
                assignment_id=task_id,
            )
            refreshed = file_repository.get_file(
                file_id=current.id, owner_id=current.owner_id
            )
            if refreshed is None:
                raise SourcePreviewNotFound("Source preview not found.") from None
            _raise_if_source_not_available(refreshed)
        raise SourceUnavailableMissing("The task original is missing.") from None
    except StorageUnavailable:
        raise SourcePreviewStorageUnavailable(
            "Source storage is temporarily unavailable."
        ) from None
    except Exception as exc:
        logger.warning(
            "Source preview storage read failed; task_id=%s file_id=%s exception_type=%s",
            task_id,
            stored.id,
            type(exc).__name__,
        )
        raise SourcePreviewStorageUnavailable(
            "Source storage is temporarily unavailable."
        ) from None
    if not isinstance(content, bytes):
        raise SourcePreviewStorageUnavailable(
            "Source storage is temporarily unavailable."
        )
    if (
        len(content) != stored.size_bytes
        or hashlib.sha256(content).hexdigest() != stored.sha256
    ):
        logger.warning(
            "Current source object integrity check failed; task_id=%s file_id=%s",
            task_id,
            stored.id,
        )
        raise SourcePreviewStorageUnavailable(
            "Source storage is temporarily unavailable."
        )
    return content


def read_source_file_content(
    *, task_id: str, file_id: str, owner_id: str, storage: StorageBackend
) -> SourceFileContent:
    _workflow, problem, submissions = _resolve_current_sources(
        task_id=task_id, owner_id=owner_id, storage=storage
    )
    candidates = ([problem] if problem is not None else []) + submissions
    selected = next(
        (
            item
            for item in candidates
            if item.stored is not None and item.descriptor.get("file_id") == file_id
        ),
        None,
    )
    if selected is None:
        raise SourcePreviewNotFound("Source preview not found.")
    descriptor = selected.descriptor
    if descriptor["status"] == "processing":
        raise SourcePreviewProcessing("Source preview is still processing.")
    if descriptor["status"] == SOURCE_FILE_CLEANUP_PENDING:
        raise SourceCleanupPending(
            "The task original is being cleaned automatically."
        )
    if descriptor.get("unavailable_reason") == SOURCE_REASON_TASK_FINALIZED:
        raise SourceUnavailableTaskFinalized(
            "The task original was cleaned after completion."
        )
    if descriptor.get("unavailable_reason") == "missing":
        raise SourceUnavailableMissing("The task original is missing.")
    if descriptor.get("unavailable_reason") == "unsupported_type":
        raise SourcePreviewUnsupportedType(
            "The current source type cannot be previewed."
        )
    if descriptor["status"] != "available" or selected.stored is None:
        raise SourcePreviewUnavailable("Source preview is unavailable.")

    content = _read_current_content(
        storage=storage, stored=selected.stored, task_id=task_id
    )
    mime_type = detected_preview_mime(
        content[:_SNIFF_BYTES], descriptor["display_name"]
    )
    if mime_type not in _ALLOWED_PREVIEW_MIME:
        raise SourcePreviewUnsupportedType(
            "The current source type cannot be previewed."
        )
    return SourceFileContent(
        content=content,
        mime_type=mime_type,
        display_name=descriptor["display_name"],
    )
