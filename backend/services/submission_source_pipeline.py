"""Persist and prepare every submission source independently."""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from fastapi.concurrency import run_in_threadpool

from backend.db import source_outcome_repository, workflow_repository
from backend.db.file_repository import (
    StoredFile,
    archive_member_reference_content,
    create_archive_member_reference,
    delete_unlinked_file,
    get_file,
    save_file,
)
from backend.services.background_errors import (
    classify_background_error,
    is_retryable_background_error,
)
from backend.storage import get_storage
from backend.tools.file_processing import (
    ARCHIVE_EXTENSIONS,
    RawUploadSource,
    extract_raw_files_from_archive,
    extract_text_from_upload,
    infer_upload_content_type,
    inspect_baidu_ocr_upload,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreparedSubmissionSource:
    source_id: str
    stored_file_id: str
    filename: str
    content_type: str
    text: str | None
    pre_error_code: str | None = None
    failure_phase: str | None = None
    retryable: bool = False


async def _persist_archive_container(
    *,
    content: bytes,
    filename: str,
    content_type: str | None,
    owner_id: str,
    task_id: str,
    job_id: str,
    job_attempt: int,
) -> StoredFile:
    digest = hashlib.sha256(content).hexdigest()
    operation = await run_in_threadpool(
        workflow_repository.get_operation,
        job_id,
        owner_id=owner_id,
    )
    if operation.attempt != job_attempt:
        raise RuntimeError("submission_source_persistence_failed")
    for file_id in operation.artifact_refs:
        candidate = await run_in_threadpool(get_file, file_id=file_id, owner_id=owner_id)
        if (
            candidate is not None
            and candidate.assignment_id == task_id
            and candidate.kind == "submission_container"
            and candidate.sha256 == digest
        ):
            return candidate

    storage = get_storage()
    stored = await run_in_threadpool(
        save_file,
        storage=storage,
        owner_id=owner_id,
        kind="submission_container",
        original_name=filename,
        content=content,
        content_type=infer_upload_content_type(filename, content_type, content),
        storage_prefix=(
            f"assignments/{task_id}/submission-containers/{job_id}/{job_attempt}"
        ),
        assignment_id=task_id,
    )
    try:
        await run_in_threadpool(
            workflow_repository.save_operation_checkpoint,
            job_id,
            owner_id=owner_id,
            expected_attempt=job_attempt,
            expected_checkpoint_revision=operation.checkpoint_revision,
            stage="submission_container_saved",
            checkpoint={"container_file_id": stored.id},
            artifact_refs=[stored.id],
        )
    except Exception:
        winner: StoredFile | None = None
        try:
            latest = await run_in_threadpool(
                workflow_repository.get_operation,
                job_id,
                owner_id=owner_id,
            )
            for file_id in latest.artifact_refs:
                candidate = await run_in_threadpool(
                    get_file,
                    file_id=file_id,
                    owner_id=owner_id,
                )
                if (
                    candidate is not None
                    and candidate.assignment_id == task_id
                    and candidate.kind == "submission_container"
                    and candidate.sha256 == digest
                ):
                    winner = candidate
                    break
        except Exception as recovery_exc:
            logger.warning(
                "Archive checkpoint winner could not be read; exception_type=%s",
                type(recovery_exc).__name__,
            )
        await run_in_threadpool(
            delete_unlinked_file,
            storage=storage,
            file_id=stored.id,
            owner_id=owner_id,
            assignment_id=task_id,
        )
        if winner is not None:
            return winner
        raise
    return stored


def failure_phase_for_code(code: str) -> str:
    if code.startswith("submission_archive_"):
        return "archive"
    if code == "submission_source_persistence_failed":
        return "source_persistence"
    if code == "submission_outcome_persistence_failed":
        return "outcome_persistence"
    if code == "submission_persistence_failed":
        return "result_persistence"
    if code in {
        "vision_provider_required",
        "provider_vision_not_supported",
        "ocr_empty_result",
        "ocr_credential_not_found",
        "provider_permission_denied",
        "provider_quota_exceeded",
        "provider_unavailable",
        "provider_submit_uncertain",
        "provider_task_failed",
        "provider_request_failed",
        "provider_download_url_rejected",
        "provider_result_unavailable",
        "provider_result_too_large",
        "ocr_input_invalid",
        "ocr_unsupported_file",
        "ocr_file_too_large",
        "ocr_image_dimension_limit_exceeded",
        "media_inspection_unavailable",
        "media_inspection_busy",
        "media_inspection_timeout",
        "media_inspection_failed",
        "pdf_page_limit_exceeded",
        "pdf_ocr_render_failed",
    }:
        return "ocr"
    if code.startswith("pdf_") or code.startswith("source_") or code.startswith("submission_source_"):
        return "source_read"
    if code in {
        "provider_timeout",
        "provider_unreachable",
        "provider_rate_limited",
        "provider_auth_failed",
        "provider_model_not_found",
        "provider_request_rejected",
        "provider_credentials_unavailable",
        "provider_model_or_endpoint_not_found",
        "provider_upstream_unavailable",
        "provider_response_invalid",
        "provider_image_payload_invalid",
        "provider_message_payload_not_supported",
        "provider_endpoint_dns_failed",
        "provider_endpoint_non_public_address",
        "provider_endpoint_tls_failed",
        "provider_endpoint_redirect_blocked",
        "provider_endpoint_protocol_mismatch",
        "provider_endpoint_response_too_large",
    }:
        return "recognition"
    return "recognition"


def _source_read_failure_phase(code: str, content_type: str) -> str:
    if (
        code in {
            "provider_timeout",
            "provider_unreachable",
            "provider_rate_limited",
            "provider_auth_failed",
            "provider_model_not_found",
            "provider_request_rejected",
            "provider_credentials_unavailable",
            "provider_model_or_endpoint_not_found",
            "provider_upstream_unavailable",
            "provider_response_invalid",
            "provider_image_payload_invalid",
            "provider_message_payload_not_supported",
            "provider_endpoint_dns_failed",
            "provider_endpoint_non_public_address",
            "provider_endpoint_tls_failed",
            "provider_endpoint_redirect_blocked",
            "provider_endpoint_protocol_mismatch",
            "provider_endpoint_response_too_large",
            "ocr_credential_not_found",
            "provider_permission_denied",
            "provider_quota_exceeded",
            "provider_unavailable",
            "provider_submit_uncertain",
            "provider_task_failed",
            "provider_request_failed",
            "provider_download_url_rejected",
            "provider_result_unavailable",
            "provider_result_too_large",
            "ocr_input_invalid",
            "ocr_unsupported_file",
            "ocr_file_too_large",
            "ocr_image_dimension_limit_exceeded",
            "media_inspection_unavailable",
            "media_inspection_busy",
            "media_inspection_timeout",
            "media_inspection_failed",
            "pdf_page_limit_exceeded",
        }
        and (content_type.startswith("image/") or content_type == "application/pdf")
    ):
        return "ocr"
    return failure_phase_for_code(code)


async def _persist_and_register(
    *,
    raw: RawUploadSource,
    owner_id: str,
    task_id: str,
    job_id: str,
    job_attempt: int,
    order_index: int,
    container_file: StoredFile | None = None,
) -> tuple[str, str]:
    storage = get_storage()
    expected_sha256 = (
        hashlib.sha256(raw.content).hexdigest()
        if raw.content is not None
        else hashlib.sha256(archive_member_reference_content(
            container_sha256=container_file.sha256,
            member_name=raw.filename,
        )).hexdigest() if container_file is not None else ""
    )
    existing = await run_in_threadpool(
        source_outcome_repository.get_source_at_position,
        operation_id=job_id,
        owner_id=owner_id,
        attempt=job_attempt,
        order_index=order_index,
    )
    if existing is not None:
        if not expected_sha256 or existing.sha256 != expected_sha256:
            raise RuntimeError("submission_source_persistence_failed")
        return existing.id, existing.stored_file_id

    stored: StoredFile | None = None
    try:
        if raw.content is None:
            if container_file is None:
                raise RuntimeError("submission_source_persistence_failed")
            stored = await run_in_threadpool(
                create_archive_member_reference,
                storage=storage,
                source_file_id=container_file.id,
                owner_id=owner_id,
                assignment_id=task_id,
                member_name=raw.filename,
            )
        else:
            stored = await run_in_threadpool(
                save_file,
                storage=storage,
                owner_id=owner_id,
                kind="submission_source",
                original_name=raw.filename,
                content=raw.content,
                content_type=raw.content_type,
                storage_prefix=f"assignments/{task_id}/submission-sources/{job_id}/{job_attempt}",
                assignment_id=task_id,
            )
        retry_of_source_id = await run_in_threadpool(
            source_outcome_repository.find_retry_source_id,
            operation_id=job_id,
            owner_id=owner_id,
            expected_attempt=job_attempt,
            order_index=order_index,
            sha256=stored.sha256,
        )
        source, _created = await run_in_threadpool(
            source_outcome_repository.register_source,
            owner_id=owner_id,
            assignment_id=task_id,
            operation_id=job_id,
            expected_attempt=job_attempt,
            order_index=order_index,
            stored_file_id=stored.id,
            retry_of_source_id=retry_of_source_id,
        )
    except Exception as exc:
        if stored is not None:
            await run_in_threadpool(
                delete_unlinked_file,
                storage=storage,
                file_id=stored.id,
                owner_id=owner_id,
                assignment_id=task_id,
            )
        try:
            committed = await run_in_threadpool(
                source_outcome_repository.get_source_at_position,
                operation_id=job_id,
                owner_id=owner_id,
                attempt=job_attempt,
                order_index=order_index,
            )
        except Exception:
            committed = None
        if (
            committed is not None
            and expected_sha256
            and committed.sha256 == expected_sha256
        ):
            return committed.id, committed.stored_file_id
        logger.warning(
            "Submission source persistence failed; exception_type=%s",
            type(exc).__name__,
        )
        raise RuntimeError("submission_source_persistence_failed") from exc
    return source.id, stored.id


async def prepare_submission_sources(
    *,
    content: bytes,
    filename: str,
    content_type: str | None,
    owner_id: str,
    task_id: str,
    job_id: str,
    job_attempt: int,
    ocr_skill,
    document_ocr_skill=None,
    recovered_ocr_text_by_source: dict[str, str] | None = None,
    blocked_ocr_source_ids: set[str] | None = None,
    before_document_ocr: Callable[[str], Awaitable[None]] | None = None,
    save_document_ocr: Callable[[str, str], Awaitable[None]] | None = None,
    document_ocr_failed: Callable[[str, Exception], Awaitable[None]] | None = None,
    vision_unavailable_code: str | None = None,
    reporter=None,
) -> list[PreparedSubmissionSource]:
    """Persist originals, then OCR/read each source without batch-wide collapse."""
    container_file: StoredFile | None = None
    is_archive = (filename or "").lower().endswith(ARCHIVE_EXTENSIONS)
    if is_archive:
        try:
            container_file = await _persist_archive_container(
                content=content,
                filename=filename or "submissions",
                content_type=content_type,
                owner_id=owner_id,
                task_id=task_id,
                job_id=job_id,
                job_attempt=job_attempt,
            )
        except Exception as exc:
            logger.warning(
                "Submission archive container persistence failed; exception_type=%s",
                type(exc).__name__,
            )
            raise RuntimeError("submission_source_persistence_failed") from exc

    archive_error: str | None = None
    try:
        raw_sources = await run_in_threadpool(
            extract_raw_files_from_archive,
            content,
            filename,
            content_type=content_type,
        )
    except Exception as exc:
        logger.warning(
            "Submission archive preparation failed; exception_type=%s",
            type(exc).__name__,
        )
        archive_error = classify_background_error(exc, "submission_archive_invalid")
        raw_sources = []

    if not raw_sources:
        code = archive_error or "submission_archive_empty"
        raw_sources = [RawUploadSource(
            filename=filename or "submissions",
            content=None if container_file is not None else content,
            content_type=infer_upload_content_type(
                filename or "submissions", content_type, content
            ),
            pre_error_code=code,
            failure_phase=failure_phase_for_code(code),
            retryable=is_retryable_background_error(code),
        )]
        source_id, stored_file_id = await _persist_and_register(
            raw=raw_sources[0],
            owner_id=owner_id,
            task_id=task_id,
            job_id=job_id,
            job_attempt=job_attempt,
            order_index=0,
            container_file=container_file,
        )
        return [PreparedSubmissionSource(
            source_id=source_id,
            stored_file_id=stored_file_id,
            filename=raw_sources[0].filename,
            content_type=raw_sources[0].content_type,
            text=None,
            pre_error_code=code,
            failure_phase="archive",
            retryable=is_retryable_background_error(code),
        )]

    prepared: list[PreparedSubmissionSource] = []
    for order_index, raw in enumerate(raw_sources):
        try:
            source_id, stored_file_id = await _persist_and_register(
                raw=raw,
                owner_id=owner_id,
                task_id=task_id,
                job_id=job_id,
                job_attempt=job_attempt,
                order_index=order_index,
                container_file=container_file,
            )
        except Exception:
            if container_file is None or raw.content is None:
                raise
            failed_raw = RawUploadSource(
                filename=raw.filename,
                content=None,
                content_type=raw.content_type,
                pre_error_code="submission_source_persistence_failed",
                failure_phase="source_persistence",
                retryable=True,
            )
            source_id, stored_file_id = await _persist_and_register(
                raw=failed_raw,
                owner_id=owner_id,
                task_id=task_id,
                job_id=job_id,
                job_attempt=job_attempt,
                order_index=order_index,
                container_file=container_file,
            )
            raw = failed_raw
        if raw.pre_error_code:
            prepared.append(PreparedSubmissionSource(
                source_id=source_id,
                stored_file_id=stored_file_id,
                filename=raw.filename,
                content_type=raw.content_type,
                text=None,
                pre_error_code=raw.pre_error_code,
                failure_phase=raw.failure_phase or failure_phase_for_code(
                    raw.pre_error_code
                ),
                retryable=raw.retryable,
            ))
            continue
        if raw.content is None:
            raise RuntimeError("submission_source_persistence_failed")
        document_ocr_checkpointed = False
        try:
            if document_ocr_skill is not None:
                recovered = (recovered_ocr_text_by_source or {}).get(source_id)
                if recovered is not None:
                    text = recovered
                elif source_id in (blocked_ocr_source_ids or set()):
                    raise RuntimeError("provider_submit_uncertain")
                else:
                    await inspect_baidu_ocr_upload(
                        raw.content,
                        raw.filename,
                        content_type=raw.content_type,
                    )
                    if before_document_ocr is not None:
                        await before_document_ocr(source_id)
                        document_ocr_checkpointed = True
                    result = await document_ocr_skill.recognize_document(
                        raw.content,
                        raw.filename,
                        "submissions",
                    )
                    text = result.text
                    if not text.strip():
                        raise RuntimeError("ocr_empty_result")
                    if save_document_ocr is not None:
                        await save_document_ocr(source_id, text)
            else:
                text = await extract_text_from_upload(
                    raw.content,
                    raw.filename,
                    ocr_skill=ocr_skill,
                    purpose="submissions",
                    reporter=reporter,
                    content_type=raw.content_type,
                )
            if not text.strip():
                prepared.append(PreparedSubmissionSource(
                    source_id=source_id,
                    stored_file_id=stored_file_id,
                    filename=raw.filename,
                    content_type=raw.content_type,
                    text=None,
                    pre_error_code="submission_source_empty",
                    failure_phase="source_read",
                    retryable=False,
                ))
            else:
                prepared.append(PreparedSubmissionSource(
                    source_id=source_id,
                    stored_file_id=stored_file_id,
                    filename=raw.filename,
                    content_type=raw.content_type,
                    text=text,
                ))
        except Exception as exc:
            if document_ocr_checkpointed and document_ocr_failed is not None:
                try:
                    await document_ocr_failed(source_id, exc)
                except Exception as checkpoint_exc:
                    logger.warning(
                        "Submission OCR checkpoint cleanup failed; exception_type=%s",
                        type(checkpoint_exc).__name__,
                    )
            code = classify_background_error(exc, "submission_parse_failed")
            if code == "vision_provider_required" and vision_unavailable_code:
                code = vision_unavailable_code
            retryable = is_retryable_background_error(code)
            if (
                document_ocr_checkpointed
                and bool(getattr(exc, "submission_may_exist", True))
            ):
                if retryable:
                    code = "provider_submit_uncertain"
                retryable = False
            logger.warning(
                "One submission source could not be read; code=%s exception_type=%s",
                code,
                type(exc).__name__,
            )
            prepared.append(PreparedSubmissionSource(
                source_id=source_id,
                stored_file_id=stored_file_id,
                filename=raw.filename,
                content_type=raw.content_type,
                text=None,
                pre_error_code=code,
                failure_phase=_source_read_failure_phase(code, raw.content_type),
                retryable=retryable,
            ))
    return prepared
