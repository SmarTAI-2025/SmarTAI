"""Stable, non-sensitive error classification for background work.

Only codes from this module may cross the durable task/API boundary. Provider
messages and tracebacks remain server-side, while the cause chain is still
inspected so teachers receive a useful, specific reason.
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from backend.domain.errors import DomainError
from backend.domain.source_outcomes import SAFE_SOURCE_REASON_CODES
from backend.tools.structured_llm import PermanentLLMError, RateLimitError


SAFE_BACKGROUND_ERROR_CODES = frozenset({
    "workflow_failed",
    "no_provider_configured",
    "provider_not_enabled",
    "provider_credentials_unavailable",
    "recognition_provider_not_enabled",
    "vision_provider_required",
    "provider_vision_not_supported",
    "problem_extraction_failed",
    "question_preparation_contract_invalid",
    "question_preparation_source_unavailable",
    "question_preparation_retry_source_unavailable",
    "question_preparation_artifact_invalid",
    "question_preparation_artifact_too_large",
    "question_preparation_artifact_sensitive_field",
    "question_preparation_artifact_conflict",
    "question_preparation_provider_configuration_changed",
    "provider_timeout",
    "provider_unreachable",
    "provider_rate_limited",
    "provider_auth_failed",
    "provider_model_or_endpoint_not_found",
    "provider_model_not_found",
    "provider_request_rejected",
    "provider_upstream_unavailable",
    "provider_response_invalid",
    "provider_image_payload_invalid",
    "provider_message_payload_not_supported",
    "provider_endpoint_dns_failed",
    "provider_endpoint_tls_failed",
    "provider_endpoint_redirect_blocked",
    "provider_endpoint_protocol_mismatch",
    "provider_endpoint_response_too_large",
    "ocr_credential_not_found",
    "provider_permission_denied",
    "provider_quota_exceeded",
    "provider_unavailable",
    "shared_pool_disabled",
    "shared_pool_daily_limit_reached",
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
    "ocr_provider_grading_not_supported",
    "material_import_failed",
    "ai_completion_failed",
    "replacement_confirmation_required",
    "stale_revision",
    "submission_parse_failed",
    "submission_parse_invalid",
    "submission_persistence_failed",
    "submission_source_persistence_failed",
    "duplicate_student_identity",
    "identity_needs_review",
    "no_answer_content_detected",
    "no_matching_answer",
    "grading_failed",
    "grading_inputs_changed",
    "grading_persistence_failed",
    "grading_provider_configuration_changed",
    "grading_provider_selection_invalid",
    "grading_question_snapshot_invalid",
    "grading_question_snapshot_missing",
    "grading_setup_invalid",
    "unknown_ai_completion_target",
    "workflow_busy",
    "workflow_revision_conflict",
    "source_decode_failed",
    "source_empty",
    "source_mime_type_not_allowed",
    "source_text_too_large",
    "source_too_large",
    "source_type_not_allowed",
    "problem_source_decode_failed",
    "pdf_character_limit_exceeded",
    "pdf_extraction_busy",
    "pdf_extraction_failed",
    "pdf_extraction_timeout",
    "pdf_ocr_render_failed",
    "pdf_page_limit_exceeded",
    "pdf_processing_unavailable",
    "ocr_empty_result",
    "submission_archive_empty",
    "submission_archive_invalid",
    "submission_archive_limit_exceeded",
    "submission_source_empty",
    "submission_source_unsupported",
    "submission_source_too_large",
}) | SAFE_SOURCE_REASON_CODES

RETRYABLE_BACKGROUND_ERROR_CODES = frozenset({
    "provider_timeout",
    "provider_unreachable",
    "provider_rate_limited",
    "provider_upstream_unavailable",
    "provider_endpoint_dns_failed",
    "provider_vision_not_supported",
    "provider_unavailable",
    "media_inspection_busy",
    "media_inspection_timeout",
    "pdf_extraction_busy",
    "pdf_extraction_timeout",
    "submission_parse_failed",
    "submission_parse_invalid",
    "submission_persistence_failed",
    "submission_outcome_persistence_failed",
    "submission_source_persistence_failed",
    "workflow_failed",
})


def safe_background_error_code(value: Any, fallback: str) -> str:
    candidate = value.strip() if isinstance(value, str) else ""
    if candidate in SAFE_BACKGROUND_ERROR_CODES:
        return candidate
    return fallback if fallback in SAFE_BACKGROUND_ERROR_CODES else "workflow_failed"


def is_retryable_background_error(code: str) -> bool:
    return code in RETRYABLE_BACKGROUND_ERROR_CODES


def _exception_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _provider_network_exception_types() -> tuple[
    tuple[type[BaseException], ...], tuple[type[BaseException], ...]
]:
    timeout_types: tuple[type[BaseException], ...] = (asyncio.TimeoutError, TimeoutError)
    connection_types: tuple[type[BaseException], ...] = (ConnectionError,)
    try:
        import httpx

        timeout_types += (httpx.TimeoutException,)
        connection_types += (httpx.TransportError,)
    except ImportError:  # pragma: no cover - runtime dependency in production
        pass
    try:
        from openai import APIConnectionError, APITimeoutError

        timeout_types += (APITimeoutError,)
        connection_types += (APIConnectionError,)
    except ImportError:  # pragma: no cover - optional provider adapter
        pass
    try:
        from anthropic import APIConnectionError as AnthropicAPIConnectionError
        from anthropic import APITimeoutError as AnthropicAPITimeoutError

        timeout_types += (AnthropicAPITimeoutError,)
        connection_types += (AnthropicAPIConnectionError,)
    except ImportError:  # pragma: no cover - optional provider adapter
        pass
    return timeout_types, connection_types


def _http_status(item: BaseException) -> int | None:
    status_code = getattr(item, "status_code", None)
    if status_code is None:
        status_code = getattr(getattr(item, "response", None), "status_code", None)
    return status_code if isinstance(status_code, int) else None


def _http_detail(item: HTTPException) -> tuple[str | None, str]:
    detail = item.detail
    if isinstance(detail, dict):
        code = detail.get("code")
        return (str(code) if isinstance(code, str) else None), ""
    return None, detail if isinstance(detail, str) else ""


def classify_background_error(
    exc: Exception,
    fallback: str,
    *,
    persistence_code: str | None = None,
) -> str:
    """Classify an exception and its causes into a stable public code."""
    chain = _exception_chain(exc)

    for item in chain:
        literal_code = f"{item}".strip()
        if literal_code in SAFE_BACKGROUND_ERROR_CODES:
            return literal_code
        if isinstance(item, DomainError):
            for candidate in (item.code, item.message):
                code = candidate.strip() if isinstance(candidate, str) else ""
                if code in SAFE_BACKGROUND_ERROR_CODES:
                    return code
        if isinstance(item, HTTPException):
            code, text = _http_detail(item)
            if code in SAFE_BACKGROUND_ERROR_CODES:
                return code
            normalized = text.lower()
            if "requires ocr" in normalized:
                return "vision_provider_required"
            if "saved provider credentials cannot be loaded" in normalized:
                return "provider_credentials_unavailable"
            if "ocr returned empty text" in normalized:
                return "ocr_empty_result"
            if "pdf processing requires" in normalized:
                return "pdf_processing_unavailable"
            if "unable to decode file" in normalized:
                return "source_decode_failed"
            if "unsupported file type" in normalized:
                return "submission_source_unsupported"
            if "too large for ocr" in normalized:
                return "submission_source_too_large"

        normalized = literal_code.lower()
        if any(marker in normalized for marker in (
            "does not support vision",
            "does not support image input",
            "doesn't support image input",
            "image input is not supported",
            "vision input is not supported",
        )):
            return "provider_vision_not_supported"

    if any(isinstance(item, RateLimitError) for item in chain):
        return "provider_rate_limited"

    if persistence_code and any(isinstance(item, SQLAlchemyError) for item in chain):
        return safe_background_error_code(persistence_code, fallback)

    timeout_types, connection_types = _provider_network_exception_types()
    if any(isinstance(item, timeout_types) for item in chain):
        return "provider_timeout"

    for item in chain:
        status_code = _http_status(item)
        if status_code in {401, 403}:
            return "provider_auth_failed"
        if status_code == 404:
            return "provider_model_not_found"
        if status_code == 400:
            return "provider_request_rejected"
        if status_code == 429:
            return "provider_rate_limited"
        if isinstance(item, PermanentLLMError) and any(
            marker in f"{item}".lower()
            for marker in ("401", "403", "auth", "unauthorized", "invalid api key", "permission")
        ):
            return "provider_auth_failed"

    if any(isinstance(item, connection_types) for item in chain):
        return "provider_unreachable"

    for item in chain:
        normalized = f"{item}".lower()
        if any(marker in normalized for marker in (
            "unsafe path in submission archive",
            "bad zip file",
            "not a gzip file",
            "not a bzip2 file",
            "could not be opened successfully",
            "rar extraction failed",
        )):
            return "submission_archive_invalid"
        if any(marker in normalized for marker in (
            "submission archive contains too many files",
            "submission archive contains an oversized file",
            "submission archive expands beyond the safe limit",
        )):
            return "submission_archive_limit_exceeded"

    return safe_background_error_code(fallback, "workflow_failed")
