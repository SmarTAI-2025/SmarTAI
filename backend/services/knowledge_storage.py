"""Storage-I/O boundary and permanent cleanup worker for knowledge objects."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import time

from backend.config import settings
from backend.db import knowledge_storage_repository as repository
from backend.domain.errors import (
    KnowledgeStorageIntegrityFailed,
    KnowledgeStorageReservationConflict,
    KnowledgeStorageWriteFailed,
)
from backend.domain.knowledge_storage import (
    KNOWLEDGE_CLEANUP_STORAGE_DELETE_FAILED,
    KNOWLEDGE_CLEANUP_UPLOAD_INTEGRITY_FAILED,
    KNOWLEDGE_CLEANUP_UPLOAD_WRITE_FAILED,
    KNOWLEDGE_RETENTION_RETAINED,
    KnowledgeCleanupRequest,
    KnowledgeUploadReservation,
)
from backend.storage import get_storage
from backend.storage.base import StorageBackend, StorageObjectNotFound


logger = logging.getLogger(__name__)


def _backend_name(storage: StorageBackend) -> str:
    name = getattr(storage, "name", None)
    return str(name) if isinstance(name, str) and name else settings.storage_backend


def _verify_object(
    *,
    storage: StorageBackend,
    storage_key: str,
    expected_size: int,
    expected_sha256: str,
) -> None:
    size = 0
    digest = hashlib.sha256()
    try:
        with storage.open(storage_key) as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if chunk in (b"", None):
                    break
                if not isinstance(chunk, bytes):
                    raise KnowledgeStorageIntegrityFailed(
                        "Stored knowledge bytes could not be verified."
                    )
                size += len(chunk)
                digest.update(chunk)
    except KnowledgeStorageIntegrityFailed:
        raise
    except Exception as exc:
        raise KnowledgeStorageIntegrityFailed(
            "Stored knowledge bytes could not be verified."
        ) from exc
    if size != expected_size or digest.hexdigest() != expected_sha256:
        raise KnowledgeStorageIntegrityFailed(
            "Stored knowledge bytes failed integrity verification."
        )


def _writer_token(reservation: KnowledgeUploadReservation) -> str:
    token = reservation.entry.writer_claim_token
    if not token:
        raise KnowledgeStorageReservationConflict(
            "The knowledge upload reservation has no writer claim.",
            code="knowledge_storage_writer_claim_lost",
        )
    return token


def _renew_writer(reservation: KnowledgeUploadReservation) -> None:
    repository.renew_upload_writer_claim(
        reservation_id=reservation.entry.id,
        owner_id=reservation.entry.owner_id,
        writer_claim_token=_writer_token(reservation),
    )


def _discard_stale_writer(
    *,
    storage: StorageBackend,
    reservation: KnowledgeUploadReservation,
) -> bool:
    """Delete a stale writer's opaque exact key, then acknowledge its guard."""
    try:
        storage.delete(reservation.entry.storage_key)
    except StorageObjectNotFound:
        pass
    except Exception:
        # The charged ledger or permanent guard owns all future retries. Never
        # acknowledge while physical deletion remains uncertain.
        return False
    try:
        return repository.acknowledge_stale_writer_cleanup(
            reservation_id=reservation.entry.id,
            owner_id=reservation.entry.owner_id,
            storage_key=reservation.entry.storage_key,
            writer_claim_token=_writer_token(reservation),
        )
    except Exception:
        # A cleanup claim may be committing the permanent guard concurrently.
        # Its forever-recheck loop remains sufficient if acknowledgement loses.
        return False


def _delete_uncertain_writer_key(
    *,
    storage: StorageBackend,
    reservation: KnowledgeUploadReservation,
) -> bool:
    """Best-effort delete without ACK; a timed-out PUT may still arrive later."""
    try:
        storage.delete(reservation.entry.storage_key)
    except StorageObjectNotFound:
        return True
    except Exception:
        return False
    return True


class _WriterHeartbeat:
    """Renew a writer claim independently of blocking object-store calls."""

    def __init__(self, reservation: KnowledgeUploadReservation) -> None:
        self._reservation = reservation
        lease = max(1.0, float(settings.knowledge_storage_writer_lease_seconds))
        configured = max(
            0.05, float(settings.knowledge_storage_writer_heartbeat_seconds)
        )
        self._interval = max(0.05, min(configured, lease / 3.0))
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"knowledge-writer-heartbeat-{reservation.entry.id}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                _renew_writer(self._reservation)
            except KnowledgeStorageReservationConflict:
                # The foreground path performs exact-key stale cleanup after its
                # blocking I/O returns. Continuing cannot regain the same token.
                return
            except Exception:
                # A transient DB outage is not proof of lease loss. Retry while
                # the foreground operation remains active; its synchronous CAS
                # decides whether publication is still legal.
                continue

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=max(1.0, self._interval * 2.0))


def compensate_failed_upload(
    *,
    storage: StorageBackend,
    reservation: KnowledgeUploadReservation,
    reason: str,
    error_code: str,
    uncertain_write: bool,
) -> KnowledgeCleanupRequest:
    """Delete a failed upload or durably enqueue it when deletion is uncertain."""
    current = repository.get_storage_entry(
        reservation.entry.id, reservation.entry.owner_id
    )
    if current is None:
        deleted = (
            _delete_uncertain_writer_key(
                storage=storage, reservation=reservation,
            )
            if uncertain_write
            else _discard_stale_writer(
                storage=storage, reservation=reservation,
            )
        )
        return KnowledgeCleanupRequest(
            status="deleted" if deleted else "cleanup_pending",
            entry_id=reservation.entry.id,
            document_id=reservation.entry.document_id,
            cleanup_operation_id=None,
            size_bytes=reservation.entry.size_bytes,
        )
    if current.state == "available":
        # A publish commit whose acknowledgement was lost wins over request
        # compensation. Never delete bytes referenced by an available row.
        return KnowledgeCleanupRequest(
            status="published",
            entry_id=current.id,
            document_id=current.document_id,
            cleanup_operation_id=None,
            size_bytes=current.size_bytes,
        )
    try:
        pending = repository.mark_upload_cleanup_pending(
            reservation_id=reservation.entry.id,
            owner_id=reservation.entry.owner_id,
            writer_claim_token=_writer_token(reservation),
            writer_completed=not uncertain_write,
            reason=reason,
            error_code=error_code,
        )
    except KnowledgeStorageReservationConflict:
        deleted = (
            _delete_uncertain_writer_key(
                storage=storage, reservation=reservation,
            )
            if uncertain_write
            else _discard_stale_writer(
                storage=storage, reservation=reservation,
            )
        )
        return KnowledgeCleanupRequest(
            status="deleted" if deleted else "cleanup_pending",
            entry_id=reservation.entry.id,
            document_id=reservation.entry.document_id,
            cleanup_operation_id=None,
            size_bytes=reservation.entry.size_bytes,
        )
    except Exception:
        # Do not delete without first fencing publish. The original reservation
        # remains charged and its expiry makes cleanup recoverable.
        return KnowledgeCleanupRequest(
            status="reserved",
            entry_id=reservation.entry.id,
            document_id=reservation.entry.document_id,
            cleanup_operation_id=None,
            size_bytes=reservation.entry.size_bytes,
        )
    try:
        storage.delete(reservation.entry.storage_key)
    except StorageObjectNotFound:
        pass
    except Exception:
        return pending
    if uncertain_write:
        # A provider may still complete a timed-out/failed PUT after this exact
        # delete. Keep the charged cleanup row until its worker confirms absence,
        # and keep the pre-I/O guard permanently after quota is released.
        return pending
    try:
        repository.abort_reservation_after_confirmed_delete(
            reservation_id=reservation.entry.id,
            owner_id=reservation.entry.owner_id,
            writer_claim_token=_writer_token(reservation),
        )
    except Exception:
        # A transient database failure leaves the already-fenced cleanup row
        # charged. The worker will delete the now-missing key idempotently.
        current = repository.get_storage_entry(
            reservation.entry.id, reservation.entry.owner_id
        )
        if current is None:
            return KnowledgeCleanupRequest(
                status="deleted",
                entry_id=reservation.entry.id,
                document_id=reservation.entry.document_id,
                cleanup_operation_id=None,
                size_bytes=reservation.entry.size_bytes,
            )
        return KnowledgeCleanupRequest(
            status="cleanup_pending",
            entry_id=current.id,
            document_id=current.document_id,
            cleanup_operation_id=current.cleanup_operation_id,
            size_bytes=current.size_bytes,
        )
    return KnowledgeCleanupRequest(
        status="deleted",
        entry_id=reservation.entry.id,
        document_id=reservation.entry.document_id,
        cleanup_operation_id=None,
        size_bytes=reservation.entry.size_bytes,
    )


def persist_knowledge_upload(
    *,
    storage: StorageBackend,
    owner_id: str,
    original_name: str,
    content: bytes,
    content_type: str | None = None,
    title: str | None = None,
    retention_policy: str = KNOWLEDGE_RETENTION_RETAINED,
    origin_assignment_id: str | None = None,
    parser_version: str = "v1",
) -> KnowledgeUploadReservation:
    """Reserve quota, save and verify bytes, then publish metadata atomically.

    ``document.status`` remains ``processing``. Parsing/chunking stays at the
    knowledge-service layer; this function owns only durable byte lifecycle.
    """
    digest = hashlib.sha256(content).hexdigest()
    reservation = repository.reserve_upload(
        owner_id=owner_id,
        original_name=original_name,
        size_bytes=len(content),
        sha256=digest,
        content_type=content_type,
        title=title,
        retention_policy=retention_policy,
        origin_assignment_id=origin_assignment_id,
        storage_backend=_backend_name(storage),
        parser_version=parser_version,
    )
    if not reservation.created:
        return reservation

    # Renew synchronously before handing control to a potentially blocking SDK,
    # then keep the claim alive from a dedicated thread until publication.
    _renew_writer(reservation)
    heartbeat = _WriterHeartbeat(reservation)
    heartbeat.start()
    try:
        try:
            storage.save(reservation.entry.storage_key, content)
        except Exception:
            compensate_failed_upload(
                storage=storage,
                reservation=reservation,
                reason=KNOWLEDGE_CLEANUP_UPLOAD_WRITE_FAILED,
                error_code="knowledge_storage_write_failed",
                uncertain_write=True,
            )
            raise KnowledgeStorageWriteFailed(
                "Knowledge storage did not confirm the upload."
            ) from None

        # A late PUT may have returned after cleanup reclaimed the reservation.
        # This synchronous CAS is authoritative even if heartbeat attempts were
        # delayed or failed ambiguously.
        try:
            _renew_writer(reservation)
        except Exception:
            _discard_stale_writer(storage=storage, reservation=reservation)
            raise

        try:
            _verify_object(
                storage=storage,
                storage_key=reservation.entry.storage_key,
                expected_size=len(content),
                expected_sha256=digest,
            )
        except KnowledgeStorageIntegrityFailed:
            compensate_failed_upload(
                storage=storage,
                reservation=reservation,
                reason=KNOWLEDGE_CLEANUP_UPLOAD_INTEGRITY_FAILED,
                error_code="knowledge_storage_integrity_failed",
                uncertain_write=False,
            )
            raise

        try:
            # Verify can also block. Renew once more, stop new heartbeats, and
            # let publish perform the final token+lease CAS under the User gate.
            _renew_writer(reservation)
            heartbeat.stop()
            return repository.publish_upload(
                reservation_id=reservation.entry.id,
                owner_id=owner_id,
                writer_claim_token=_writer_token(reservation),
            )
        except Exception:
            # Resolve a lost commit acknowledgement before touching verified
            # bytes. Available is terminal and publish already cleared its token.
            current = repository.get_storage_entry(reservation.entry.id, owner_id)
            if current is not None and current.state == "available":
                return KnowledgeUploadReservation(
                    entry=current, disposition="reserved"
                )
            compensate_failed_upload(
                storage=storage,
                reservation=reservation,
                reason=KNOWLEDGE_CLEANUP_UPLOAD_WRITE_FAILED,
                error_code="knowledge_storage_publish_failed",
                uncertain_write=False,
            )
            raise
    finally:
        heartbeat.stop()


class KnowledgeStorageWorker:
    """Best-effort poller whose durable ledger retries deletion forever."""

    def __init__(
        self,
        *,
        storage: StorageBackend | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.storage = storage or get_storage()
        self.worker_id = worker_id or f"knowledge-cleanup-{id(self):x}"
        self._stop = asyncio.Event()
        # Persist preference across max_claims=1 polls so neither a sustained
        # ledger backlog nor a permanent-guard backlog can starve the other.
        self._prefer_guard = True

    async def _process_cleanup_claim(self, claim) -> None:
        deleted = False
        error_code: str | None = None
        if claim.storage_backend != _backend_name(self.storage):
            error_code = "knowledge_storage_backend_unavailable"
        else:
            try:
                await asyncio.to_thread(self.storage.delete, claim.storage_key)
                deleted = True
            except StorageObjectNotFound:
                deleted = True
            except Exception:
                error_code = "knowledge_storage_delete_failed"
        try:
            await asyncio.to_thread(
                repository.finish_cleanup,
                claim,
                deleted=deleted,
                error_code=error_code,
            )
        except Exception:
            # A stale/failed completion never releases quota. Claim expiry makes
            # it retryable without relying on in-memory state.
            logger.exception(
                "Knowledge cleanup completion failed; entry_id=%s",
                claim.entry_id,
            )

    async def _process_guard_claim(self, guard_claim) -> None:
        deleted = False
        error_code: str | None = None
        if guard_claim.storage_backend != _backend_name(self.storage):
            error_code = "knowledge_storage_backend_unavailable"
        else:
            try:
                await asyncio.to_thread(
                    self.storage.delete, guard_claim.storage_key
                )
                deleted = True
            except StorageObjectNotFound:
                deleted = True
            except Exception:
                error_code = "knowledge_storage_delete_failed"
        try:
            await asyncio.to_thread(
                repository.finish_orphan_guard_check,
                guard_claim,
                deleted=deleted,
                error_code=error_code,
            )
        except Exception:
            logger.exception(
                "Knowledge orphan-guard completion failed; guard_id=%s",
                guard_claim.guard_id,
            )

    async def run_once(self, *, max_claims: int | None = None) -> int:
        processed = 0
        limit = max(1, int(
            settings.knowledge_cleanup_batch_size
            if max_claims is None else max_claims
        ))
        for _ in range(limit):
            claim = None
            guard_claim = None
            if self._prefer_guard:
                guard_claim = await asyncio.to_thread(
                    repository.claim_next_orphan_guard,
                    worker_id=self.worker_id,
                )
                if guard_claim is None:
                    claim = await asyncio.to_thread(
                        repository.claim_next_cleanup,
                        worker_id=self.worker_id,
                    )
            else:
                claim = await asyncio.to_thread(
                    repository.claim_next_cleanup,
                    worker_id=self.worker_id,
                )
                if claim is None:
                    guard_claim = await asyncio.to_thread(
                        repository.claim_next_orphan_guard,
                        worker_id=self.worker_id,
                    )
            if claim is None and guard_claim is None:
                break
            self._prefer_guard = not self._prefer_guard
            if guard_claim is not None:
                await self._process_guard_claim(guard_claim)
            else:
                assert claim is not None
                await self._process_cleanup_claim(claim)
            processed += 1
        return processed

    async def run_forever(self) -> None:
        poll_seconds = max(0.05, float(settings.knowledge_cleanup_poll_seconds))
        while not self._stop.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Knowledge cleanup poll failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=poll_seconds)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()


__all__ = [
    "KnowledgeStorageWorker",
    "compensate_failed_upload",
    "persist_knowledge_upload",
]
