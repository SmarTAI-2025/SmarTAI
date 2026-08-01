"""Stable domain error codes and typed exceptions.

The API layer maps every DomainError to ``{"error": {"code": ..., "message": ...}}``
with a fixed HTTP status. Frontends branch on the stable ``code`` string and
never parse the natural-language ``message``, so error wording can evolve
without breaking clients. Adding a new failure mode means adding a code here,
not inventing an ad-hoc string in a service.
"""
from __future__ import annotations

from typing import Optional


class DomainError(Exception):
    """Base for all normalized-domain failures.

    ``code`` is the stable identifier clients switch on; ``status_code`` is the
    HTTP status the API maps it to; ``message`` is human-readable and may be
    overridden per call site.
    """

    code: str = "domain_error"
    status_code: int = 400

    def __init__(self, message: Optional[str] = None, *, code: Optional[str] = None,
                 status_code: Optional[int] = None) -> None:
        self.message = message or self.code
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
    """A run cannot be released while it has unresolved failed/needs_review results."""
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
