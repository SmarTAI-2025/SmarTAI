"""Bounded cleanup and permit quarantine for owned PDF worker processes."""
from __future__ import annotations

import asyncio
from collections.abc import Callable

PDF_CLEANUP_SECONDS = 30.0
_quarantined: set[asyncio.Future] = set()


class PdfWorkerCleanupTimeout(RuntimeError):
    pass


async def wait_pdf_cleanup(task: asyncio.Future) -> tuple[bool, bool]:
    """Wait at most one cleanup allowance, even under repeated cancellation."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + PDF_CLEANUP_SECONDS
    current = asyncio.current_task()
    cancelled = bool(current is not None and current.cancelling())
    while not task.done():
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            await asyncio.wait({task}, timeout=remaining)
        except asyncio.CancelledError:
            cancelled = True
    return task.done(), cancelled


def retain_cleanup(task: asyncio.Future) -> None:
    """Keep unfinished cleanup alive; release only after confirmed safe reaping.

    A stuck worker retains its pool permit, bounding quarantined workers by the
    pool capacity. Late successful cleanup restores capacity automatically.
    """
    _quarantined.add(task)
    def finished(done):
        _quarantined.discard(done)
        if done.cancelled():
            return
        done.exception()  # Always consume background exceptions.
    task.add_done_callback(finished)


async def reap_pdf_worker(launch_task: asyncio.Task | None, *, release_slot: Callable[[], None] | None = None) -> None:
    """Transfer permit ownership to cleanup; do not free unreaped capacity."""
    if launch_task is None:
        if release_slot is not None:
            release_slot()
        return

    async def cleanup() -> None:
        try:
            process = await launch_task
        except Exception:
            if release_slot is not None:
                release_slot()
            return  # Process creation failed before a handle existed.
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        try:
            await process.communicate()
        finally:
            await process.wait()
            # A transport-drain error does not keep capacity quarantined once
            # wait() has positively confirmed the process was reaped.
            if release_slot is not None:
                release_slot()

    cleanup_task = asyncio.create_task(cleanup())
    retain_cleanup(cleanup_task)
    done, cancelled = await wait_pdf_cleanup(cleanup_task)
    if cancelled:
        raise asyncio.CancelledError
    if not done:
        raise PdfWorkerCleanupTimeout("pdf_worker_cleanup_timeout")
    cleanup_task.result()
