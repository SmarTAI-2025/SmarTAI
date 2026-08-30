"""Restart-safe automatic cleanup handlers for task originals and orphans."""
from __future__ import annotations

import time
from typing import Any

from fastapi.concurrency import run_in_threadpool

from backend.config import settings
from backend.db import source_storage_repository
from backend.domain.errors import ValidationError
from backend.storage import get_storage
from backend.storage.base import StorageObjectNotFound


def _retry_delay(retry_count: int) -> int:
    base = max(1, int(settings.source_cleanup_retry_base_seconds))
    maximum = max(base, int(settings.source_cleanup_retry_max_seconds))
    return min(maximum, base * (2 ** min(max(0, retry_count), 16)))


def _required_string(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValidationError(
            "The source-cleanup payload is invalid.",
            code="invalid_source_cleanup_payload",
        )
    return value


def _required_positive_int(payload: dict, key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValidationError(
            "The source-cleanup payload is invalid.",
            code="invalid_source_cleanup_payload",
        )
    return value


async def run_source_cleanup(operation) -> None:
    payload = operation.payload
    if not isinstance(payload, dict):
        raise ValidationError(
            "The source-cleanup payload is invalid.",
            code="invalid_source_cleanup_payload",
        )
    grading_run_id = _required_string(payload, "grading_run_id")
    result_version = _required_positive_int(payload, "final_result_version")
    finalized_at = payload.get("finalized_at")
    entries = payload.get("files")
    if (
        not isinstance(finalized_at, (int, float))
        or isinstance(finalized_at, bool)
        or not isinstance(entries, list)
        or any(not isinstance(entry, dict) for entry in entries)
    ):
        raise ValidationError(
            "The source-cleanup payload is invalid.",
            code="invalid_source_cleanup_payload",
        )

    authorized = await run_in_threadpool(
        source_storage_repository.cleanup_generation_is_authorized,
        assignment_id=operation.assignment_id,
        owner_id=operation.owner_id,
        grading_run_id=grading_run_id,
        final_result_version=result_version,
        finalized_at=float(finalized_at),
    )
    reconciliation_ids: frozenset[str] = frozenset()
    superseded_generation = not authorized
    restored = 0
    if not authorized:
        reconciliation_ids = await run_in_threadpool(
            source_storage_repository.cleanup_reconciliation_file_ids,
            operation_id=operation.operation_id,
            owner_id=operation.owner_id,
            assignment_id=operation.assignment_id,
            operation_attempt=operation.attempt,
            worker_id=operation.worker_id,
            lease_token=str(operation.lease_token),
        )
        restored = await run_in_threadpool(
            source_storage_repository.restore_unclaimed_sources_for_superseded_cleanup,
            operation_id=operation.operation_id,
            owner_id=operation.owner_id,
            assignment_id=operation.assignment_id,
            operation_attempt=operation.attempt,
            worker_id=operation.worker_id,
            lease_token=str(operation.lease_token),
        )
    if not authorized and not reconciliation_ids:
        await operation.complete(
            status="superseded",
            summary={
                "status": "superseded",
                "deleted_count": 0,
                "restored_count": restored,
            },
            checkpoint={
                "status": "superseded",
                "deleted_count": 0,
                "restored_count": restored,
            },
        )
        return

    # When review supersedes the generation after a crash, reconcile only the
    # exact files whose delete attempt had already begun.  Untouched rows were
    # restored above.  This does not authorize a new stale-generation delete.
    if reconciliation_ids:
        entries = [
            entry for entry in entries
            if str(entry.get("file_id") or "") in reconciliation_ids
        ]

    storage = get_storage()
    deleted = 0
    already_deleted = 0
    failed_ids: list[str] = []
    for index, entry in enumerate(entries):
        claim = await run_in_threadpool(
            source_storage_repository.claim_finalized_source_delete,
            operation_id=operation.operation_id,
            owner_id=operation.owner_id,
            assignment_id=operation.assignment_id,
            operation_attempt=operation.attempt,
            worker_id=operation.worker_id,
            lease_token=str(operation.lease_token),
            grading_run_id=grading_run_id,
            final_result_version=result_version,
            finalized_at=float(finalized_at),
            entry=entry,
        )
        if claim.status == "superseded":
            restored += await run_in_threadpool(
                source_storage_repository.restore_unclaimed_sources_for_superseded_cleanup,
                operation_id=operation.operation_id,
                owner_id=operation.owner_id,
                assignment_id=operation.assignment_id,
                operation_attempt=operation.attempt,
                worker_id=operation.worker_id,
                lease_token=str(operation.lease_token),
            )
            superseded_generation = True
            continue
        if claim.status in {"already_deleted", "missing_record"}:
            result = source_storage_repository.CleanupDeleteResult(
                claim.status, claim.file_id, claim.size_bytes
            )
        else:
            storage_deleted = False
            if (
                claim.status == "ready"
                and claim.storage_backend == getattr(storage, "name", "unknown")
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
                source_storage_repository.finish_finalized_source_delete,
                operation_id=operation.operation_id,
                owner_id=operation.owner_id,
                assignment_id=operation.assignment_id,
                operation_attempt=operation.attempt,
                worker_id=operation.worker_id,
                lease_token=str(operation.lease_token),
                grading_run_id=grading_run_id,
                final_result_version=result_version,
                finalized_at=float(finalized_at),
                entry=entry,
                claim_token=str(claim.claim_token),
                deleted=storage_deleted,
            )
            if result.status == "superseded":
                restored += await run_in_threadpool(
                    source_storage_repository.restore_unclaimed_sources_for_superseded_cleanup,
                    operation_id=operation.operation_id,
                    owner_id=operation.owner_id,
                    assignment_id=operation.assignment_id,
                    operation_attempt=operation.attempt,
                    worker_id=operation.worker_id,
                    lease_token=str(operation.lease_token),
                )
                superseded_generation = True
                continue
        if result.status == "deleted":
            deleted += 1
        elif result.status in {"already_deleted", "missing_record"}:
            already_deleted += 1
        elif result.status == "failed":
            failed_ids.append(result.file_id)
        await operation.update_progress({
            "state": "cleaning" if index + 1 < len(entries) else "settling",
            "total": len(entries),
            "processed": index + 1,
            "deleted": deleted,
            "already_deleted": already_deleted,
            "failed": len(failed_ids),
            # Keep UI-safe stable IDs bounded; the next pass derives truth from
            # StoredFile lifecycle rows rather than trusting this sample.
            "failed_file_ids": failed_ids[-128:],
            "retry_count": int(operation.progress.get("retry_count") or 0),
        })

    if failed_ids:
        retry_count = int(operation.progress.get("retry_count") or 0) + 1
        retry_at = time.time() + _retry_delay(retry_count)
        await operation.retry_later(
            retry_at=retry_at,
            error_code="source_cleanup_failed",
            progress={
                "state": "retrying",
                "total": len(entries),
                "deleted": deleted,
                "already_deleted": already_deleted,
                "failed": len(failed_ids),
                "failed_file_ids": failed_ids[-128:],
                "retry_count": retry_count,
                "retry_at": retry_at,
            },
        )
        return

    terminal_status = "superseded" if superseded_generation else "completed"
    await operation.complete(
        status=terminal_status,
        summary={
            "status": terminal_status,
            "total_count": len(entries),
            "deleted_count": deleted,
            "already_deleted_count": already_deleted,
            "failed_count": 0,
            "restored_count": restored,
        },
        checkpoint={
            "status": terminal_status,
            "processed_count": len(entries),
            "deleted_count": deleted,
            "already_deleted_count": already_deleted,
            "restored_count": restored,
        },
    )


async def run_source_reservation_cleanup(operation) -> None:
    payload = operation.payload
    if not isinstance(payload, dict):
        raise ValidationError(
            "The source-reservation payload is invalid.",
            code="invalid_source_cleanup_payload",
        )
    reservation_id = _required_string(payload, "reservation_id")
    claim = await run_in_threadpool(
        source_storage_repository.claim_reserved_object_delete,
        operation_id=operation.operation_id,
        owner_id=operation.owner_id,
        assignment_id=operation.assignment_id,
        attempt=operation.attempt,
        worker_id=operation.worker_id,
        lease_token=str(operation.lease_token),
        reservation_id=reservation_id,
    )
    if claim.status == "already_deleted":
        result = source_storage_repository.CleanupDeleteResult(
            claim.status, claim.file_id, claim.size_bytes
        )
    else:
        storage = get_storage()
        storage_deleted = False
        if (
            claim.status == "ready"
            and claim.storage_backend == getattr(storage, "name", "unknown")
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
            source_storage_repository.finish_reserved_object_delete,
            operation_id=operation.operation_id,
            owner_id=operation.owner_id,
            assignment_id=operation.assignment_id,
            attempt=operation.attempt,
            worker_id=operation.worker_id,
            lease_token=str(operation.lease_token),
            reservation_id=reservation_id,
            claim_token=str(claim.claim_token),
            deleted=storage_deleted,
        )
    if result.status == "failed":
        retry_count = int(operation.progress.get("retry_count") or 0) + 1
        retry_at = time.time() + _retry_delay(retry_count)
        await operation.retry_later(
            retry_at=retry_at,
            error_code="source_storage_delete_failed",
            progress={
                "state": "retrying",
                "retry_count": retry_count,
                "retry_at": retry_at,
            },
        )
        return
    await operation.complete(
        status="completed",
        summary={"status": "completed", "deleted": result.status == "deleted"},
        checkpoint={"status": "completed"},
    )
