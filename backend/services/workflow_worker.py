"""Durable workflow-operation worker core (DB-W2-2A).

The worker polls only operation types it has a registered handler for, claims
each listed row through the repository's conditional update (so another worker
winning the race between listing and claiming is tolerated), runs one async
handler per claim with an immutable, lease-fenced operation context, and
heartbeats the claim's lease while the handler runs. A handler whose lease is
lost (token rotated or lease expired) is fenced and cancelled so it can no
longer mutate the row. Shutdown stops claiming new work, cancels and awaits the
worker's own handler/heartbeat tasks, and never bulk-clears database leases.

No production operation type is registered here. Durable handlers arrive in
DB-W2-3/W2-4; until then the worker runs with whatever explicit mapping a later
FastAPI lifespan wrapper provides.
"""
from __future__ import annotations

import asyncio
import logging
import time
from types import MappingProxyType
from typing import Awaitable, Callable, Mapping

from backend.config import settings
from backend.db import workflow_repository
from backend.domain.errors import (
    DomainError,
    InvalidTransition,
    LeaseLost,
    NotFound,
    ValidationError,
    VersionConflict,
)
from backend.domain.source_storage import DELAYED_SOURCE_OPERATION_TYPES

logger = logging.getLogger(__name__)

_UNEXPECTED_HANDLER_ERROR_CODE = "workflow_failed"


def _stable_error_code(exc: BaseException) -> str:
    if isinstance(exc, DomainError) and exc.code != "domain_error":
        return exc.code
    return _UNEXPECTED_HANDLER_ERROR_CODE


class LeasedOperation:
    """Immutable, lease-fenced handle to one claimed workflow operation.

    The worker hands this to a registered handler. The handler sees a stable
    snapshot of the operation row and advances durable state only through the
    fenced methods below; every write carries the matching lease token, so a
    worker that has been fenced (token rotated or lease expired) is rejected by
    the repository instead of mutating another worker's claim.
    """

    __slots__ = (
        "_operation",
        "_worker_id",
        "_lease_seconds",
        "_on_lease_lost",
        "_lease_lost",
        "_lease_deadline",
    )

    def __init__(
        self,
        operation,
        *,
        worker_id: str,
        lease_seconds: int,
        on_lease_lost: Callable[[], None] | None = None,
    ) -> None:
        self._operation = operation
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._on_lease_lost = on_lease_lost
        self._lease_lost = False
        self._lease_deadline = float(operation.lease_expires_at or 0)

    # ─── Read-only operation snapshot ────────────────────────────────────

    @property
    def operation_id(self) -> str:
        return self._operation.id

    @property
    def owner_id(self) -> str:
        return self._operation.owner_id

    @property
    def assignment_id(self) -> str:
        return self._operation.assignment_id

    @property
    def operation_type(self) -> str:
        return self._operation.operation_type

    @property
    def attempt(self) -> int:
        return self._operation.attempt

    @property
    def input_hash(self) -> str:
        return self._operation.input_hash

    @property
    def status(self) -> str:
        return self._operation.status

    @property
    def payload(self) -> dict:
        return self._operation.payload

    @property
    def progress(self) -> dict:
        return self._operation.progress

    @property
    def checkpoint_data(self) -> dict:
        return self._operation.checkpoint

    @property
    def checkpoint_revision(self) -> int:
        return self._operation.checkpoint_revision

    @property
    def checkpoint_stage(self) -> str | None:
        return self._operation.checkpoint_stage

    @property
    def artifact_refs(self) -> list[str]:
        return self._operation.artifact_refs

    @property
    def lease_token(self) -> str | None:
        return self._operation.lease_token

    @property
    def lease_expires_at(self) -> float | None:
        return self._operation.lease_expires_at

    @property
    def worker_id(self) -> str:
        return self._worker_id

    # ─── Lease fencing ───────────────────────────────────────────────────

    def _check_lease(self) -> None:
        if self._lease_lost:
            raise LeaseLost(
                "The workflow operation lease was lost.",
                code="lease_lost",
            )

    def lose_lease(self) -> None:
        """Fence the context and cancel the running handler.

        Called by the background heartbeat when the lease expires or the token
        rotates. Subsequent context writes fail fast with ``LeaseLost`` and the
        handler task is cancelled so it cannot keep mutating the row. Safe to
        call repeatedly.
        """
        self._lease_lost = True
        if self._on_lease_lost is not None:
            self._on_lease_lost()

    # ─── Fenced durable writes ───────────────────────────────────────────

    async def heartbeat(self) -> None:
        self._check_lease()
        try:
            workflow_repository.heartbeat_operation(
                self._operation.id,
                owner_id=self._operation.owner_id,
                worker_id=self._worker_id,
                lease_token=self._operation.lease_token,
                lease_seconds=self._lease_seconds,
            )
        except LeaseLost:
            self._lease_lost = True
            raise
        self._lease_deadline = time.time() + self._lease_seconds

    async def update_progress(self, progress: dict) -> None:
        self._check_lease()
        try:
            saved = workflow_repository.update_operation(
                self._operation.id,
                owner_id=self._operation.owner_id,
                expected_attempt=self._operation.attempt,
                expected_lease_token=self._operation.lease_token,
                progress=progress,
            )
        except LeaseLost:
            self._lease_lost = True
            raise
        self._operation = saved

    async def checkpoint(
        self,
        *,
        stage: str | None,
        checkpoint: dict,
        artifact_refs: list[str] | None = None,
    ) -> int:
        self._check_lease()
        try:
            saved = workflow_repository.save_operation_checkpoint(
                self._operation.id,
                owner_id=self._operation.owner_id,
                expected_attempt=self._operation.attempt,
                expected_checkpoint_revision=self._operation.checkpoint_revision,
                stage=stage,
                checkpoint=checkpoint,
                artifact_refs=artifact_refs,
                expected_lease_token=self._operation.lease_token,
            )
        except LeaseLost:
            self._lease_lost = True
            raise
        self._operation = saved
        return saved.checkpoint_revision

    async def complete(
        self,
        *,
        status: str,
        summary: dict,
        stage: str = "completed",
        checkpoint: dict | None = None,
        artifact_refs: list[str] | None = None,
    ) -> None:
        self._check_lease()
        try:
            saved = workflow_repository.save_operation_checkpoint(
                self._operation.id,
                owner_id=self._operation.owner_id,
                expected_attempt=self._operation.attempt,
                expected_checkpoint_revision=self._operation.checkpoint_revision,
                stage=stage,
                checkpoint=(
                    checkpoint if checkpoint is not None else self._operation.checkpoint
                ),
                artifact_refs=artifact_refs,
                terminal_summary=summary,
                terminal_status=status,
                expected_lease_token=self._operation.lease_token,
            )
        except LeaseLost:
            self._lease_lost = True
            raise
        self._operation = saved

    async def retry_later(
        self,
        *,
        retry_at: float,
        progress: dict,
        error_code: str,
    ) -> None:
        """Release this lease into a persisted, not-before automatic retry."""
        self._check_lease()
        try:
            saved = workflow_repository.reschedule_operation(
                self._operation.id,
                owner_id=self._operation.owner_id,
                worker_id=self._worker_id,
                lease_token=self._operation.lease_token,
                expected_attempt=self._operation.attempt,
                retry_at=retry_at,
                progress=progress,
                error_code=error_code,
            )
        except LeaseLost:
            self._lease_lost = True
            raise
        self._operation = saved


OperationHandler = Callable[[LeasedOperation], Awaitable[None]]


class WorkflowWorker:
    """Polls registered operation types and dispatches one handler per claim.

    One instance owns one process's claiming identity. The handler mapping is
    frozen at construction, so a registered type is either always handled or
    never claimed.
    """

    def __init__(
        self,
        *,
        handlers: Mapping[str, OperationHandler],
        worker_id: str | None = None,
        lease_seconds: int | None = None,
        heartbeat_seconds: float | None = None,
        poll_seconds: float | None = None,
        claim_batch_size: int | None = None,
        max_in_flight: int | None = None,
        shutdown_seconds: float | None = None,
    ) -> None:
        worker_id = worker_id if worker_id is not None else settings.workflow_worker_id
        if (
            not isinstance(worker_id, str)
            or not worker_id
            or len(worker_id) > workflow_repository.MAX_LEASE_OWNER_LENGTH
        ):
            raise ValueError(
                "worker_id must be a non-empty string of at most "
                f"{workflow_repository.MAX_LEASE_OWNER_LENGTH} characters."
            )
        self._worker_id = worker_id

        if not isinstance(handlers, Mapping):
            raise ValueError(
                "handlers must be a mapping of operation type to async handler."
            )
        frozen: dict[str, OperationHandler] = {}
        for operation_type, handler in handlers.items():
            if not isinstance(operation_type, str) or not operation_type:
                raise ValueError(
                    "handler operation types must be non-empty strings."
                )
            if not callable(handler):
                raise ValueError(
                    f"handler for {operation_type!r} must be callable."
                )
            frozen[operation_type] = handler
        self._handlers: Mapping[str, OperationHandler] = MappingProxyType(frozen)

        self._lease_seconds = (
            lease_seconds if lease_seconds is not None else settings.workflow_lease_seconds
        )
        self._heartbeat_seconds = (
            heartbeat_seconds
            if heartbeat_seconds is not None
            else settings.workflow_heartbeat_seconds
        )
        self._poll_seconds = (
            poll_seconds if poll_seconds is not None else settings.workflow_poll_seconds
        )
        self._claim_batch_size = (
            claim_batch_size
            if claim_batch_size is not None
            else settings.workflow_claim_batch_size
        )
        self._max_in_flight = (
            max_in_flight if max_in_flight is not None else settings.workflow_max_in_flight
        )
        self._shutdown_seconds = (
            shutdown_seconds
            if shutdown_seconds is not None
            else settings.workflow_shutdown_seconds
        )
        for name, value in (
            ("lease_seconds", self._lease_seconds),
            ("heartbeat_seconds", self._heartbeat_seconds),
            ("poll_seconds", self._poll_seconds),
            ("claim_batch_size", self._claim_batch_size),
            ("max_in_flight", self._max_in_flight),
            ("shutdown_seconds", self._shutdown_seconds),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive.")

        self._stopping = False
        self._stop_event = asyncio.Event()
        self._in_flight: set[asyncio.Task] = set()
        self._heartbeats: set[asyncio.Task] = set()

    # ─── Read-only surface ───────────────────────────────────────────────

    @property
    def worker_id(self) -> str:
        return self._worker_id

    @property
    def handlers(self) -> Mapping[str, OperationHandler]:
        return self._handlers

    @property
    def in_flight_count(self) -> int:
        return len(self._in_flight)

    @property
    def is_stopping(self) -> bool:
        return self._stopping

    # ─── Poll / run / shutdown ───────────────────────────────────────────

    def stop(self) -> None:
        """Request a graceful stop: no new claims after the current tick."""
        self._stopping = True
        self._stop_event.set()

    async def poll_once(self) -> int:
        """Claim and dispatch one bounded batch; returns rows dispatched.

        Lists only registered operation types, then claims each listed row with
        the worker identity. A row claimed by another worker between listing and
        claiming raises ``LeaseLost`` inside the conditional update and is
        skipped, so a race is tolerated rather than fatal.
        """
        if self._stopping:
            return 0
        capacity = max(0, self._max_in_flight - len(self._in_flight))
        if capacity <= 0:
            return 0
        limit = min(self._claim_batch_size, capacity)
        rows = workflow_repository.list_claimable_operations(
            tuple(self._handlers), limit=limit
        )
        dispatched = 0
        for row in rows:
            if self._stopping or len(self._in_flight) >= self._max_in_flight:
                break
            try:
                claimed = workflow_repository.claim_operation(
                    row.id,
                    owner_id=row.owner_id,
                    worker_id=self._worker_id,
                    lease_seconds=self._lease_seconds,
                )
            except (LeaseLost, NotFound):
                # Another worker won the row, or it was removed, since listing.
                continue
            task = asyncio.create_task(self._run_claim(claimed))
            self._in_flight.add(task)
            task.add_done_callback(self._in_flight.discard)
            dispatched += 1
        return dispatched

    async def run_forever(self) -> None:
        """Poll until :meth:`stop` or cancellation. One loop per process."""
        while not self._stopping:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("workflow worker tick failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._poll_seconds
                )
            except asyncio.TimeoutError:
                pass

    async def shutdown(self) -> None:
        """Stop claiming, then cancel and await every worker-owned task.

        Leases are never bulk-cleared: a cancelled handler's lease is left to
        expire so another worker reclaims the row after ``lease_seconds``.
        """
        self.stop()
        tasks = set(self._in_flight) | set(self._heartbeats)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=self._shutdown_seconds)

    # ─── Per-claim lifecycle ─────────────────────────────────────────────

    async def _run_claim(self, operation) -> None:
        """Run one handler for a claimed row under a heartbeat lifecycle."""
        ctx = LeasedOperation(
            operation,
            worker_id=self._worker_id,
            lease_seconds=self._lease_seconds,
            on_lease_lost=asyncio.current_task().cancel,
        )
        heartbeat_task = asyncio.create_task(self._heartbeat(ctx))
        self._heartbeats.add(heartbeat_task)
        heartbeat_task.add_done_callback(self._heartbeats.discard)
        try:
            try:
                await self._handlers[operation.operation_type](ctx)
            except asyncio.CancelledError:
                raise
            except LeaseLost:
                pass  # fenced; the context blocks further mutation
            except Exception as exc:
                await self._record_failure(ctx, exc)
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except (asyncio.CancelledError, Exception):
                pass

    async def _heartbeat(self, ctx: LeasedOperation) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_seconds)
            try:
                await ctx.heartbeat()
            except asyncio.CancelledError:
                raise
            except LeaseLost:
                ctx.lose_lease()
                return
            except Exception:
                logger.warning(
                    "workflow worker heartbeat failed for operation %s",
                    ctx.operation_id,
                    exc_info=True,
                )
                if time.time() >= ctx._lease_deadline:
                    ctx.lose_lease()
                    return

    async def _record_failure(
        self, ctx: LeasedOperation, exc: BaseException
    ) -> None:
        code = _stable_error_code(exc)
        # Only the operation id and the stable code are logged; the exception
        # and any payload the handler carried are never written to logs.
        logger.warning(
            "workflow operation %s failed (%s)", ctx.operation_id, code
        )
        if (
            ctx.operation_type in DELAYED_SOURCE_OPERATION_TYPES
            and not isinstance(exc, ValidationError)
        ):
            retry_count = int(ctx.progress.get("retry_count") or 0) + 1
            base = max(1, int(settings.source_cleanup_retry_base_seconds))
            maximum = max(base, int(settings.source_cleanup_retry_max_seconds))
            retry_at = time.time() + min(
                maximum, base * (2 ** min(retry_count, 16))
            )
            try:
                await ctx.retry_later(
                    retry_at=retry_at,
                    error_code=code,
                    progress={
                        **ctx.progress,
                        "state": "retrying",
                        "retry_count": retry_count,
                        "retry_at": retry_at,
                    },
                )
            except (LeaseLost, VersionConflict, InvalidTransition, NotFound):
                pass
            return
        try:
            workflow_repository.update_operation(
                ctx.operation_id,
                owner_id=ctx.owner_id,
                expected_attempt=ctx.attempt,
                expected_lease_token=ctx.lease_token,
                status="error",
                error_code=code,
                completed_at=time.time(),
            )
        except (LeaseLost, VersionConflict, InvalidTransition, NotFound):
            # Fenced or superseded: a newer attempt, a terminal write, or
            # another worker already owns the durable state.
            pass
