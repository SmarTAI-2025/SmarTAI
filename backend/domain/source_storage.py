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
SOURCE_REASON_TASK_DELETED = "task_deleted"
SOURCE_REASON_REPLACED = "replaced"
SOURCE_REASON_MISSING = "missing"
SOURCE_REASON_STORAGE_DELETE_FAILED = "storage_delete_failed"

SOURCE_FILE_STATUSES = frozenset({
    SOURCE_FILE_AVAILABLE,
    SOURCE_FILE_CLEANUP_PENDING,
    SOURCE_FILE_UNAVAILABLE,
})
SOURCE_UNAVAILABLE_REASONS = frozenset({
    SOURCE_REASON_TASK_FINALIZED,
    SOURCE_REASON_TASK_DELETED,
    SOURCE_REASON_REPLACED,
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

PROBLEM_RAW_SOURCE_KINDS = frozenset({"problem", "problem_source"})
SUBMISSION_RAW_SOURCE_KINDS = frozenset(
    RAW_SOURCE_KINDS - PROBLEM_RAW_SOURCE_KINDS
)

# Archive members are logical submission sources, but the preserved archive is
# the sole user-supplied original. Successful members remain available as
# zero-quota derived preview objects; failed members use small deterministic
# pointers. Both still belong to the task lifecycle and are retired with a
# replaced generation and after task completion.
TASK_SOURCE_DERIVED_KINDS = frozenset({
    "submission_archive_member",
    "submission_archive_member_reference",
})
TASK_SOURCE_CLEANUP_KINDS = frozenset(
    RAW_SOURCE_KINDS | TASK_SOURCE_DERIVED_KINDS
)
ASSIGNMENT_LINKED_TASK_SOURCE_CLEANUP_KINDS = frozenset(
    ASSIGNMENT_LINKED_RAW_SOURCE_KINDS | TASK_SOURCE_DERIVED_KINDS
)
SUBMISSION_TASK_SOURCE_CLEANUP_KINDS = frozenset(
    SUBMISSION_RAW_SOURCE_KINDS | TASK_SOURCE_DERIVED_KINDS
)

SOURCE_CLEANUP_OPERATION = "source_cleanup"
SOURCE_REPLACEMENT_CLEANUP_OPERATION = "source_replacement_cleanup"
SOURCE_RESERVATION_CLEANUP_OPERATION = "source_reservation_cleanup"
TASK_DELETE_OPERATION = "task_delete"
DELAYED_SOURCE_OPERATION_TYPES = frozenset({
    SOURCE_CLEANUP_OPERATION,
    SOURCE_REPLACEMENT_CLEANUP_OPERATION,
    SOURCE_RESERVATION_CLEANUP_OPERATION,
    TASK_DELETE_OPERATION,
})


def is_raw_source_kind(kind: str) -> bool:
    return kind in RAW_SOURCE_KINDS
