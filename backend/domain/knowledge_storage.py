"""Stable vocabulary and public DTOs for knowledge-library storage.

Knowledge storage is deliberately independent from the F-B task-original
lifecycle.  A ledger row is charged in every state until the storage backend
confirms that the exact object is gone.
"""
from __future__ import annotations

from dataclasses import dataclass


KNOWLEDGE_RETENTION_RETAINED = "retained"
KNOWLEDGE_RETENTION_TASK_ONLY = "task_only"
KNOWLEDGE_RETENTION_POLICIES = frozenset({
    KNOWLEDGE_RETENTION_RETAINED,
    KNOWLEDGE_RETENTION_TASK_ONLY,
})

KNOWLEDGE_STORAGE_RESERVED = "reserved"
KNOWLEDGE_STORAGE_AVAILABLE = "available"
KNOWLEDGE_STORAGE_CLEANUP_PENDING = "cleanup_pending"
KNOWLEDGE_STORAGE_STATES = frozenset({
    KNOWLEDGE_STORAGE_RESERVED,
    KNOWLEDGE_STORAGE_AVAILABLE,
    KNOWLEDGE_STORAGE_CLEANUP_PENDING,
})

KNOWLEDGE_CLEANUP_EXPLICIT_DELETE = "explicit_delete"
KNOWLEDGE_CLEANUP_TASK_UNREFERENCED = "task_unreferenced"
KNOWLEDGE_CLEANUP_TASK_DELETED = "task_deleted"
KNOWLEDGE_CLEANUP_TASK_ATTACH_FAILED = "task_attach_failed"
KNOWLEDGE_CLEANUP_UPLOAD_ABANDONED = "upload_abandoned"
KNOWLEDGE_CLEANUP_UPLOAD_WRITE_FAILED = "upload_write_failed"
KNOWLEDGE_CLEANUP_UPLOAD_INTEGRITY_FAILED = "upload_integrity_failed"
KNOWLEDGE_CLEANUP_STORAGE_DELETE_FAILED = "storage_delete_failed"
KNOWLEDGE_CLEANUP_REASONS = frozenset({
    KNOWLEDGE_CLEANUP_EXPLICIT_DELETE,
    KNOWLEDGE_CLEANUP_TASK_UNREFERENCED,
    KNOWLEDGE_CLEANUP_TASK_DELETED,
    KNOWLEDGE_CLEANUP_TASK_ATTACH_FAILED,
    KNOWLEDGE_CLEANUP_UPLOAD_ABANDONED,
    KNOWLEDGE_CLEANUP_UPLOAD_WRITE_FAILED,
    KNOWLEDGE_CLEANUP_UPLOAD_INTEGRITY_FAILED,
    KNOWLEDGE_CLEANUP_STORAGE_DELETE_FAILED,
})


@dataclass(frozen=True)
class KnowledgeStorageUsage:
    used_bytes: int
    limit_bytes: int
    available_document_bytes: int
    cleanup_pending_bytes: int
    retrying_cleanup_bytes: int
    reserved_bytes: int
    cleanup_pending_count: int
    retrying_cleanup_count: int
    reserved_count: int

    @property
    def available_bytes(self) -> int:
        return max(0, self.limit_bytes - self.used_bytes)

    def as_dict(self) -> dict[str, int]:
        return {
            "used_bytes": self.used_bytes,
            "limit_bytes": self.limit_bytes,
            "available_bytes": self.available_bytes,
            "available_document_bytes": self.available_document_bytes,
            "cleanup_pending_bytes": self.cleanup_pending_bytes,
            "retrying_cleanup_bytes": self.retrying_cleanup_bytes,
            "reserved_bytes": self.reserved_bytes,
            "cleanup_pending_count": self.cleanup_pending_count,
            "retrying_cleanup_count": self.retrying_cleanup_count,
            "reserved_count": self.reserved_count,
        }


@dataclass(frozen=True)
class KnowledgeStorageEntry:
    id: str
    owner_id: str
    document_id: str | None
    stored_file_id: str | None
    origin_assignment_id: str | None
    retention_policy: str
    state: str
    original_name: str
    content_type: str | None
    size_bytes: int
    sha256: str
    storage_backend: str
    storage_key: str
    cleanup_reason: str | None
    error_code: str | None
    cleanup_operation_id: str | None
    reservation_expires_at: float | None
    unattached_expires_at: float | None
    cleanup_retry_at: float | None
    cleanup_attempt_count: int
    cleanup_claim_token: str | None
    cleanup_claimed_at: float | None
    writer_claim_token: str | None
    writer_claimed_at: float | None
    writer_heartbeat_at: float | None
    writer_lease_expires_at: float | None
    created_at: float
    updated_at: float

    @property
    def cleanup_pending(self) -> bool:
        return self.state == KNOWLEDGE_STORAGE_CLEANUP_PENDING

    @property
    def retrying_cleanup(self) -> bool:
        return (
            self.cleanup_pending
            and self.cleanup_reason == KNOWLEDGE_CLEANUP_STORAGE_DELETE_FAILED
        )


@dataclass(frozen=True)
class KnowledgeUploadReservation:
    entry: KnowledgeStorageEntry
    disposition: str

    @property
    def created(self) -> bool:
        return self.disposition == "reserved"

    @property
    def document_id(self) -> str:
        assert self.entry.document_id is not None
        return self.entry.document_id

    @property
    def file_id(self) -> str:
        # New files deliberately reuse the ledger UUID in the stored_files
        # table.  The tables have separate namespaces and this avoids another
        # pre-publication identity column.
        return self.entry.stored_file_id or self.entry.id


@dataclass(frozen=True)
class KnowledgeCleanupRequest:
    status: str
    entry_id: str
    document_id: str | None
    cleanup_operation_id: str | None
    size_bytes: int


@dataclass(frozen=True)
class KnowledgeCleanupClaim:
    entry_id: str
    owner_id: str
    document_id: str | None
    stored_file_id: str | None
    cleanup_operation_id: str
    storage_backend: str
    storage_key: str
    size_bytes: int
    attempt: int
    claim_token: str


@dataclass(frozen=True)
class KnowledgeCleanupResult:
    status: str
    entry_id: str
    size_bytes: int
    retry_at: float | None = None


@dataclass(frozen=True)
class KnowledgeOrphanGuardClaim:
    guard_id: str
    owner_id: str
    storage_backend: str
    storage_key: str
    writer_claim_token: str
    attempt: int
    claim_token: str
