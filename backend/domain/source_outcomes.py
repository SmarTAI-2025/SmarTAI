"""Public-safe diagnostics for immutable submission-source outcomes.

This module is deliberately dependency-free so both persistence and service
layers share one contract without importing each other.
"""
from __future__ import annotations


SOURCE_OUTCOME_STATUSES = frozenset({
    "parsed",
    "parse_failed",
    "identity_conflict",
    "no_matching_answer",
})

SOURCE_FAILURE_PHASES = frozenset({
    "archive",
    "source_persistence",
    "source_read",
    "ocr",
    "recognition",
    "structured_parse",
    "answer_detection",
    "question_matching",
    "identity",
    "outcome_persistence",
    "result_persistence",
})

SAFE_SOURCE_REASON_CODES = frozenset({
    "workflow_failed",
    "no_provider_configured",
    "provider_not_enabled",
    "provider_credentials_unavailable",
    "recognition_provider_not_enabled",
    "vision_provider_required",
    "provider_timeout",
    "provider_unreachable",
    "provider_rate_limited",
    "provider_auth_failed",
    "provider_model_or_endpoint_not_found",
    "provider_request_rejected",
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
    "source_decode_failed",
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
    "submission_archive_member_too_large",
    "submission_archive_member_unreadable",
    "submission_archive_member_unsafe_path",
    "submission_source_content_type_mismatch",
    "submission_source_empty",
    "submission_source_unsupported",
    "submission_source_too_large",
    "submission_source_persistence_failed",
    "submission_parse_failed",
    "submission_parse_invalid",
    "submission_model_field_too_long",
    "submission_outcome_persistence_failed",
    "submission_persistence_failed",
    "no_answer_content_detected",
    "no_matching_answer",
    "identity_needs_review",
    "duplicate_student_identity",
    "student_identity_conflict",
})

IDENTITY_REASON_CODES = frozenset({
    "identity_needs_review",
    "duplicate_student_identity",
    "student_identity_conflict",
})

NO_MATCH_DIAGNOSTICS = frozenset({
    ("no_answer_content_detected", "answer_detection"),
    ("no_matching_answer", "question_matching"),
})
NO_MATCH_REASON_CODES = frozenset(reason for reason, _phase in NO_MATCH_DIAGNOSTICS)


def source_diagnostic_is_valid(
    status: str,
    reason_code: str | None,
    failure_phase: str | None,
) -> bool:
    if status not in SOURCE_OUTCOME_STATUSES:
        return False
    if status == "parsed":
        return reason_code is None and failure_phase is None
    if reason_code not in SAFE_SOURCE_REASON_CODES or failure_phase not in SOURCE_FAILURE_PHASES:
        return False
    if status == "identity_conflict":
        return reason_code in IDENTITY_REASON_CODES and failure_phase == "identity"
    if status == "no_matching_answer":
        return (reason_code, failure_phase) in NO_MATCH_DIAGNOSTICS
    return reason_code not in IDENTITY_REASON_CODES | NO_MATCH_REASON_CODES


def safe_source_diagnostic(
    status: str,
    reason_code: str | None,
    failure_phase: str | None,
) -> tuple[str | None, str | None]:
    """Return a safe public diagnostic even for legacy or corrupted rows."""
    if source_diagnostic_is_valid(status, reason_code, failure_phase):
        return reason_code, failure_phase
    if status == "parsed":
        return None, None
    if status == "identity_conflict":
        return "identity_needs_review", "identity"
    if status == "no_matching_answer":
        return "no_matching_answer", "question_matching"
    return "submission_parse_failed", "recognition"
