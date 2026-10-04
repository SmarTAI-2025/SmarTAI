"""Stable domain error codes and typed exceptions.

The API layer maps every DomainError to ``{"error": {"code": ..., "message": ...}}``
with a fixed HTTP status. Frontends branch on the stable ``code`` string and
never parse the natural-language ``message``, so error wording can evolve
without breaking clients. Adding a new failure mode means adding a code here,
not inventing an ad-hoc string in a service.
"""
from __future__ import annotations

from typing import Any, Optional


class DomainError(Exception):
    """Base for all normalized-domain failures.

    ``code`` is the stable identifier clients switch on; ``status_code`` is the
    HTTP status the API maps it to; ``message`` is human-readable and may be
    overridden per call site.
    """

    code: str = "domain_error"
    status_code: int = 400

    def __init__(self, message: Optional[str] = None, *, code: Optional[str] = None,
                 status_code: Optional[int] = None,
                 details: dict[str, Any] | None = None) -> None:
        self.message = message or self.code
        # Details are reserved for bounded, client-safe metadata such as the
        # caller's own quota usage.  Storage keys, paths, buckets, and provider
        # errors must never be placed here.
        self.details = dict(details) if details is not None else None
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        super().__init__(self.message)


class NotFound(DomainError):
    code = "not_found"
    status_code = 404


class Forbidden(DomainError):
    code = "forbidden"
    status_code = 403


class InvalidTransition(DomainError):
    code = "invalid_transition"
    status_code = 409


class VersionConflict(DomainError):
    """Optimistic-lock mismatch: the client's expected version is stale."""
    code = "version_conflict"
    status_code = 409


class DuplicateActiveRun(DomainError):
    """Another queued/running grading run already exists for the assignment."""
    code = "duplicate_active_run"
    status_code = 409


class ResultNotReleasable(DomainError):
    """A run cannot be released while it has unresolved hard failures."""
    code = "result_not_releasable"
    status_code = 409


class LeaseLost(DomainError):
    """The calling worker no longer owns the lease on this grading run."""
    code = "lease_lost"
    status_code = 409


class ValidationError(DomainError):
    code = "validation_error"
    status_code = 422


class DuplicateSubmission(DomainError):
    """A conflicting immutable revision already exists for the same content."""
    code = "duplicate_submission"
    status_code = 409


class AssignmentClosed(DomainError):
    """The assignment deadline has passed or the assignment is closed for submissions."""
    code = "assignment_closed"
    status_code = 409


class SourceStorageQuotaExceeded(DomainError):
    """The owner's durable raw-source allocation cannot fit an upload."""

    code = "source_storage_quota_exceeded"
    status_code = 413


class SourceStorageReservationConflict(DomainError):
    """A quota reservation could not be serialized safely."""

    code = "source_storage_reservation_conflict"
    status_code = 409


class SourceStorageWriteFailed(DomainError):
    """Storage did not confirm a raw-source write."""

    code = "source_storage_write_failed"
    status_code = 503


class SourceStorageIntegrityFailed(DomainError):
    """The stored raw-source bytes failed length or digest verification."""

    code = "source_storage_integrity_failed"
    status_code = 503


class KnowledgeStorageQuotaExceeded(DomainError):
    """The owner's independent knowledge-library allocation is full."""

    code = "knowledge_storage_quota_exceeded"
    status_code = 413


class KnowledgeStorageReservationConflict(DomainError):
    """A knowledge upload/lifecycle transition cannot be serialized safely."""

    code = "knowledge_storage_reservation_conflict"
    status_code = 409


class KnowledgeStorageWriteFailed(DomainError):
    code = "knowledge_storage_write_failed"
    status_code = 503


class KnowledgeStorageIntegrityFailed(DomainError):
    code = "knowledge_storage_integrity_failed"
    status_code = 503


PDF_EVIDENCE_STATUS_CODES = {
    "pdf_invalid_request": 422,
    "pdf_input_too_large": 413,
    "pdf_invalid": 400,
    "pdf_encrypted": 422,
    "pdf_page_limit_exceeded": 413,
    "pdf_page_out_of_range": 422,
    "pdf_character_limit_exceeded": 413,
    "pdf_structure_limit_exceeded": 413,
    "pdf_response_too_large": 413,
    "pdf_render_limit_exceeded": 413,
    "pdf_processing_failed": 400,
    "pdf_evidence_busy": 429,
    "pdf_evidence_timeout": 408,
    "pdf_evidence_protocol_invalid": 502,
    "pdf_processing_unavailable": 503,
    "image_invalid": 400,
    "image_input_too_large": 413,
    "image_format_mismatch": 422,
    "image_multiframe_unsupported": 422,
    "image_orientation_invalid": 422,
    "image_mode_unsupported": 422,
    "image_pixel_limit_exceeded": 413,
    "image_response_too_large": 413,
    "image_processing_failed": 400,
}


class PdfEvidenceError(DomainError):
    """Safe codes from the owned PDF/image worker; legacy exception name retained."""

    def __init__(self, code: str):
        safe_code = code if code in PDF_EVIDENCE_STATUS_CODES else "pdf_evidence_protocol_invalid"
        super().__init__(safe_code, code=safe_code, status_code=PDF_EVIDENCE_STATUS_CODES[safe_code])


RECOGNITION_ERROR_CODES = frozenset({
    "question_source_pages_out_of_range", "question_source_incomplete",
    "material_ocr_confirmation_required", "material_recognition_review_required", "material_parser_provider_required",
    "recognition_request_invalid", "recognition_source_mismatch", "recognition_plan_changed",
    "recognition_route_changed", "recognition_input_unsupported", "recognition_budget_exhausted",
    "image_recognition_unconfirmed", "recognition_response_invalid", "recognition_response_too_large", "recognition_timeout",
    "recognition_artifact_invalid", "recognition_artifact_limit", "recognition_artifact_unavailable",
    "recognition_artifact_scope_unsupported",
    "recognition_source_unavailable",
    "recognition_already_running",
    "target_location_needs_hint", "target_selection_limit_exceeded", "visual_capability_unavailable",
    "recognition_cache_not_success", "recognition_cache_corrupt", "recognition_cache_unavailable",
    "recognition_cache_source_unavailable",
    "provider_credentials_required", "provider_vision_not_supported", "provider_auth_failed",
    "shared_pool_disabled", "shared_pool_daily_limit_reached",
    "provider_permission_denied", "provider_region_unsupported", "provider_quota_exceeded", "provider_daily_quota_exceeded", "provider_rate_limited", "provider_overloaded",
    "provider_request_failed", "provider_request_rejected", "provider_response_invalid",
    "provider_recitation_blocked", "provider_content_blocked",
    "provider_result_too_large", "provider_result_unavailable", "provider_download_url_rejected",
    "provider_submit_uncertain", "provider_task_failed", "provider_timeout", "provider_unavailable",
    "provider_unreachable", "provider_endpoint_tls_failed", "provider_upstream_unavailable",
    "provider_model_or_endpoint_not_found", "ocr_empty_result", "ocr_file_too_large",
    "ocr_input_invalid", "ocr_unsupported_file",
    "provider_model_not_found", "provider_image_payload_invalid", "provider_message_payload_not_supported",
    "provider_endpoint_dns_failed", "provider_endpoint_redirect_blocked", "provider_endpoint_protocol_mismatch",
    "provider_endpoint_response_too_large",
}) | frozenset(PDF_EVIDENCE_STATUS_CODES)


class RecognitionError(DomainError):
    """A safe failure; ambiguous submissions are never eligible for auto-reread."""

    def __init__(self, code: str, *, submission_may_exist: bool = False,
                 failed_pages: list[int] | None = None, processed_pages: list[int] | None = None):
        safe_code = code if code in RECOGNITION_ERROR_CODES else "recognition_response_invalid"
        self.submission_may_exist = submission_may_exist
        page_details = {key: sorted({n for n in values if type(n) is int and 1 <= n <= 10000})[:10000]
                        for key, values in (("failed_pages", failed_pages), ("processed_pages", processed_pages)) if values is not None}
        super().__init__(safe_code, code=safe_code, status_code=422, details=page_details or None)
