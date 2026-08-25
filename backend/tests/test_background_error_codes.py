from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from backend.domain.errors import ValidationError
from backend.llm.endpoint_policy import ProviderEndpointError
from backend.services.background_errors import classify_background_error
from backend.tools.structured_llm import RateLimitError, TransientLLMError


def test_classifier_reads_stable_domain_message_when_domain_code_is_generic():
    error = ValidationError("no_provider_configured")

    assert classify_background_error(error, "grading_failed") == "no_provider_configured"


def test_classifier_walks_wrapped_provider_cause():
    try:
        raise TimeoutError("provider request timed out")
    except TimeoutError as cause:
        error = TransientLLMError("provider request failed")
        error.__cause__ = cause

    assert classify_background_error(error, "grading_failed") == "provider_timeout"


def test_explicit_pdf_429_code_is_not_misreported_as_provider_rate_limit():
    error = HTTPException(429, detail={"code": "pdf_extraction_busy"})

    assert classify_background_error(error, "problem_extraction_failed") == "pdf_extraction_busy"


def test_provider_rejection_of_image_input_reports_selected_model_capability():
    error = RuntimeError("This model does not support image input")

    assert (
        classify_background_error(error, "submission_parse_failed")
        == "provider_vision_not_supported"
    )


def test_rate_limit_and_persistence_failures_use_distinct_codes():
    assert (
        classify_background_error(RateLimitError("429"), "grading_failed")
        == "provider_rate_limited"
    )
    assert classify_background_error(
        SQLAlchemyError("database unavailable"),
        "grading_failed",
        persistence_code="grading_persistence_failed",
    ) == "grading_persistence_failed"


def test_unknown_exception_uses_safe_fallback_without_exposing_message():
    error = RuntimeError("secret provider response")

    assert classify_background_error(error, "grading_failed") == "grading_failed"


def test_dns_rebinding_block_keeps_its_safe_specific_code():
    error = ProviderEndpointError("provider_endpoint_non_public_address")

    assert classify_background_error(error, "grading_failed") == (
        "provider_endpoint_non_public_address"
    )
