"""Restart-safe automatic task deletion worker."""
from __future__ import annotations

import time

from fastapi.concurrency import run_in_threadpool

from backend.config import settings
from backend.db import task_deletion_repository
from backend.storage import get_storage
from backend.storage.base import StorageObjectNotFound


def _retry_delay(retry_count: int) -> int:
    base = max(1, int(settings.source_cleanup_retry_base_seconds))
    maximum = max(base, int(settings.source_cleanup_retry_max_seconds))
    return min(maximum, base * (2 ** min(max(0, retry_count), 16)))


async def _retry(operation, *, code: str, state: str, **details) -> None:
    retry_count = int(operation.progress.get("retry_count") or 0) + 1
    retry_at = time.time() + _retry_delay(retry_count)
    requested_retry_at = details.pop("requested_retry_at", None)
    if isinstance(requested_retry_at, (int, float)):
        retry_at = max(retry_at, float(requested_retry_at))
    await operation.retry_later(
        retry_at=retry_at,
        error_code=code,
        progress={
            **details,
            "state": state,
            "retry_count": retry_count,
            "retry_at": retry_at,
        },
    )


async def run_task_deletion(operation) -> None:
    preparation = await run_in_threadpool(
        task_deletion_repository.prepare_task_deletion_pass,
        operation_id=operation.operation_id,
        owner_id=operation.owner_id,
        assignment_id=operation.assignment_id,
        attempt=operation.attempt,
        worker_id=operation.worker_id,
        lease_token=str(operation.lease_token),
    )
    if preparation.producer_blockers:
        await _retry(
            operation,
            code="task_delete_waiting_for_producers",
            state="draining_producers",
            producer_blockers=preparation.producer_blockers,
            requested_retry_at=preparation.next_producer_retry_at,
        )
        return

    storage = get_storage()
    deleted = 0
    while True:
        claim = await run_in_threadpool(
            task_deletion_repository.claim_next_task_file,
            operation_id=operation.operation_id,
            owner_id=operation.owner_id,
            assignment_id=operation.assignment_id,
            attempt=operation.attempt,
            worker_id=operation.worker_id,
            lease_token=str(operation.lease_token),
        )
        if claim.status == "empty":
            break
        storage_deleted = False
        if (
            claim.storage_backend == getattr(storage, "name", "unknown")
            and claim.storage_key
            and claim.claim_token
        ):
            try:
                await run_in_threadpool(storage.delete, claim.storage_key)
            except StorageObjectNotFound:
                storage_deleted = True
            except Exception:
                storage_deleted = False
            else:
                storage_deleted = True
        result = await run_in_threadpool(
            task_deletion_repository.finish_task_file_delete,
            operation_id=operation.operation_id,
            owner_id=operation.owner_id,
            assignment_id=operation.assignment_id,
            attempt=operation.attempt,
            worker_id=operation.worker_id,
            lease_token=str(operation.lease_token),
            file_id=claim.file_id,
            claim_token=str(claim.claim_token),
            deleted=storage_deleted,
        )
        if result == "failed":
            await _retry(
                operation,
                code="task_storage_delete_failed",
                state="retrying",
                deleted=deleted,
                failed_file_id=claim.file_id,
                pending_reservations=preparation.pending_reservations,
            )
            return
        if result == "deleted":
            deleted += 1

    # Reconcile exact assignment/revision prefixes as a final safety net for a
    # process crash after storage.save but before metadata publication.
    prefixes = await run_in_threadpool(
        task_deletion_repository.task_storage_prefixes,
        operation_id=operation.operation_id,
        owner_id=operation.owner_id,
        assignment_id=operation.assignment_id,
        attempt=operation.attempt,
        worker_id=operation.worker_id,
        lease_token=str(operation.lease_token),
    )
    prefix_keys: list[str] = []
    try:
        for prefix in prefixes:
            prefix_keys.extend(await run_in_threadpool(storage.list_keys, prefix))
        for key in sorted(set(prefix_keys)):
            try:
                await run_in_threadpool(storage.delete, key)
            except StorageObjectNotFound:
                pass
    except Exception:
        await _retry(
            operation,
            code="task_storage_delete_failed",
            state="retrying",
            deleted=deleted,
            prefix_cleanup_failed=True,
            pending_reservations=preparation.pending_reservations,
        )
        return

    if preparation.pending_reservations:
        await _retry(
            operation,
            code="task_delete_waiting_for_upload_cleanup",
            state="waiting_for_upload_cleanup",
            deleted=deleted,
            pending_reservations=preparation.pending_reservations,
            requested_retry_at=preparation.next_reservation_retry_at,
        )
        return

    # Require two durable empty-prefix observations after producers drained.
    # This makes a late storage call visible before the parent row is cascaded.
    if prefix_keys or not operation.progress.get("empty_prefix_scan_at"):
        await _retry(
            operation,
            code="task_delete_verifying_storage_empty",
            state="verifying_storage_empty",
            deleted=deleted,
            empty_prefix_scan_at=time.time() if not prefix_keys else None,
        )
        return

    completed = await run_in_threadpool(
        task_deletion_repository.finalize_task_deletion,
        operation_id=operation.operation_id,
        owner_id=operation.owner_id,
        assignment_id=operation.assignment_id,
        attempt=operation.attempt,
        worker_id=operation.worker_id,
        lease_token=str(operation.lease_token),
    )
    if not completed:
        await _retry(
            operation,
            code="task_delete_reconciliation_pending",
            state="reconciling",
            deleted=deleted,
        )
    # On success the assignment cascade removes this operation.  Returning
    # without a terminal write is intentional: physical absence and parent
    # absence are the durable completion record.
