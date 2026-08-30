"""Frozen raw-source lifecycle and quota vocabulary.

Task originals are deliberately a narrow allowlist.  Knowledge documents,
OCR/recognition artifacts, normalized questions and answers, grading results,
reviews, and downloadable report manifests never enter this lifecycle.
"""
from __future__ import annotations


SOURCE_FILE_AVAILABLE = "available"
SOURCE_FILE_CLEANUP_PENDING = "cleanup_pending"
SOURCE_FILE_UNAVAILABLE = "unavailable"

SOURCE_REASON_TASK_FINALIZED = "task_finalized"
SOURCE_REASON_MISSING = "missing"
SOURCE_REASON_STORAGE_DELETE_FAILED = "storage_delete_failed"

SOURCE_FILE_STATUSES = frozenset({
    SOURCE_FILE_AVAILABLE,
    SOURCE_FILE_CLEANUP_PENDING,
    SOURCE_FILE_UNAVAILABLE,
})
SOURCE_UNAVAILABLE_REASONS = frozenset({
    SOURCE_REASON_TASK_FINALIZED,
    SOURCE_REASON_MISSING,
    SOURCE_REASON_STORAGE_DELETE_FAILED,
})

# Current task-centric sources plus the still-mounted normalized legacy paths.
# The legacy ``submission`` row is revision-linked and may be ACL-owned by the
# student, but its quota owner is always the assignment teacher.
RAW_SOURCE_KINDS = frozenset({
    "problem",
    "problem_source",
    "submission",
    "submission_container",
    "submission_source",
    "submission_source_reference",
})

ASSIGNMENT_LINKED_RAW_SOURCE_KINDS = frozenset(
    RAW_SOURCE_KINDS - {"submission"}
)
REVISION_LINKED_RAW_SOURCE_KINDS = frozenset({"submission"})

SOURCE_CLEANUP_OPERATION = "source_cleanup"
SOURCE_RESERVATION_CLEANUP_OPERATION = "source_reservation_cleanup"
DELAYED_SOURCE_OPERATION_TYPES = frozenset({
    SOURCE_CLEANUP_OPERATION,
    SOURCE_RESERVATION_CLEANUP_OPERATION,
})


def is_raw_source_kind(kind: str) -> bool:
    return kind in RAW_SOURCE_KINDS
