"""Stable, non-sensitive error codes for durable background jobs.

Background workers persist only codes from this module.  Raw provider, file,
database, and traceback text stays in server-side diagnostics and never becomes
part of the task-state contract consumed by the frontend.
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from backend.domain.errors import DomainError
from backend.tools.structured_llm import PermanentLLMError, RateLimitError


SAFE_BACKGROUND_ERROR_CODES = frozenset({
    "no_provider_configured",
    "provider_not_enabled",
    "provider_credentials_unavailable",
    "recognition_provider_not_enabled",
    "vision_provider_required",
    "problem_extraction_failed",
    "provider_timeout",
    "provider_unreachable",
    "provider_rate_limited",
    "provider_auth_failed",
    "material_import_failed",
    "ai_completion_failed",
    "replacement_confirmation_required",
    "stale_revision",
    "submission_parse_failed",
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
    # File/source failures that are safe and actionable in a progress page.
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
    "submission_archive_invalid",
    "submission_archive_limit_exceeded",
    "submission_source_empty",
    "submission_source_unsupported",
    "submission_source_too_large",
})


def safe_background_error_code(value: Any, fallback: str) -> str:
    """Return a public stable code, never arbitrary persisted/raw text."""
    candidate = value.strip() if isinstance(value, str) else ""
    if candidate in SAFE_BACKGROUND_ERROR_CODES:
        return candidate
    return fallback if fallback in SAFE_BACKGROUND_ERROR_CODES else "workflow_failed"


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
    timeout_types: tuple[type[BaseException], ...] = (
        asyncio.TimeoutError,
        TimeoutError,
    )
    # Do not classify every OSError as a provider outage: disk/file failures are
    # local infrastructure problems. ConnectionError plus SDK transport types
    # covers actual network failures without that false positive.
    connection_types: tuple[type[BaseException], ...] = (ConnectionError,)
    try:
        import httpx

        timeout_types += (httpx.TimeoutException,)
        connection_types += (httpx.TransportError,)
    except ImportError:  # pragma: no cover - httpx is a runtime dependency
        pass
    try:
        from openai import APIConnectionError, APITimeoutError

        timeout_types += (APITimeoutError,)
        connection_types += (APIConnectionError,)
    except ImportError:  # pragma: no cover - optional adapter
        pass
    try:
        from anthropic import APIConnectionError as AnthropicAPIConnectionError
        from anthropic import APITimeoutError as AnthropicAPITimeoutError

        timeout_types += (AnthropicAPITimeoutError,)
        connection_types += (AnthropicAPIConnectionError,)
    except ImportError:  # pragma: no cover - optional adapter
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
    """Classify a worker exception into one stable, public-safe code.

    The full cause/context chain is inspected because retry wrappers commonly
    replace the outer exception while keeping the provider/network cause.
    """
    chain = _exception_chain(exc)

    # Explicit stable codes always win, including a PDF-specific 429/timeout.
    for item in chain:
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
        normalized = f"{item}".lower()
        if any(marker in normalized for marker in (
            "does not support vision",
            "does not support image input",
            "doesn't support image input",
            "image input is not supported",
            "vision input is not supported",
        )):
            return "vision_provider_required"

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
        if status_code == 429:
            return "provider_rate_limited"
        if isinstance(item, PermanentLLMError) and any(
            marker in f"{item}".lower()
            for marker in (
                "401", "403", "auth", "unauthorized", "invalid api key", "permission",
            )
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
        )):
            return "submission_archive_invalid"
        if any(marker in normalized for marker in (
            "submission archive contains too many files",
            "submission archive contains an oversized file",
            "submission archive expands beyond the safe limit",
        )):
            return "submission_archive_limit_exceeded"

    return safe_background_error_code(fallback, "workflow_failed")
