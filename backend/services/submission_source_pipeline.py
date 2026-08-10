"""Persist and prepare every submission source independently."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi.concurrency import run_in_threadpool

from backend.db import source_outcome_repository
from backend.db.file_repository import save_file
from backend.services.background_errors import (
    classify_background_error,
    is_retryable_background_error,
)
from backend.storage import get_storage
from backend.tools.file_processing import (
    RawUploadSource,
    extract_raw_files_from_archive,
    extract_text_from_upload,
    infer_upload_content_type,
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


def failure_phase_for_code(code: str) -> str:
    if code.startswith("submission_archive_"):
        return "archive"
    if code in {
        "vision_provider_required",
        "ocr_empty_result",
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
        "provider_credentials_unavailable",
    }:
        return "recognition"
    return "recognition"


async def _persist_and_register(
    *,
    raw: RawUploadSource,
    owner_id: str,
    task_id: str,
    job_id: str,
    job_attempt: int,
    order_index: int,
) -> tuple[str, str]:
    try:
        stored = await run_in_threadpool(
            save_file,
            storage=get_storage(),
            owner_id=owner_id,
            kind="submission_source",
            original_name=raw.filename,
            content=raw.content,
            content_type=raw.content_type,
            storage_prefix=f"assignments/{task_id}/submission-sources/{job_id}/{job_attempt}",
            assignment_id=task_id,
        )
        source, _created = await run_in_threadpool(
            source_outcome_repository.register_source,
            owner_id=owner_id,
            assignment_id=task_id,
            operation_id=job_id,
            expected_attempt=job_attempt,
            order_index=order_index,
            stored_file_id=stored.id,
        )
    except Exception as exc:
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
    reporter=None,
) -> list[PreparedSubmissionSource]:
    """Persist originals, then OCR/read each source without batch-wide collapse."""
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
            content=content,
            content_type=infer_upload_content_type(filename or "submissions", content_type),
        )]
        source_id, stored_file_id = await _persist_and_register(
            raw=raw_sources[0],
            owner_id=owner_id,
            task_id=task_id,
            job_id=job_id,
            job_attempt=job_attempt,
            order_index=0,
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
        source_id, stored_file_id = await _persist_and_register(
            raw=raw,
            owner_id=owner_id,
            task_id=task_id,
            job_id=job_id,
            job_attempt=job_attempt,
            order_index=order_index,
        )
        try:
            text = await extract_text_from_upload(
                raw.content,
                raw.filename,
                ocr_skill=ocr_skill,
                purpose="submissions",
                reporter=reporter,
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
            code = classify_background_error(exc, "submission_parse_failed")
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
                failure_phase=failure_phase_for_code(code),
                retryable=is_retryable_background_error(code),
            ))
    return prepared
