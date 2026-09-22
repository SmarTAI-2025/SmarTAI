"""Cancellation-safe cleanup for the application's own PDF worker processes."""
from __future__ import annotations

import asyncio


async def reap_pdf_worker(launch_task: asyncio.Task | None) -> None:
    """Drain and reap the owned worker before its concurrency slot is released.

    The launch is shielded by the caller. Even cancellation during process
    creation therefore leaves a task from which cleanup can obtain the handle.
    """
    if launch_task is None:
        return

    async def cleanup() -> None:
        try:
            process = await launch_task
        except (Exception, asyncio.CancelledError):
            return
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        try:
            await process.communicate()
        finally:
            await process.wait()

    cleanup_task = asyncio.create_task(cleanup())
    cancelled = False
    while not cleanup_task.done():
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            cancelled = True
    # Retrieve any error instead of leaving an unhandled background exception.
    await cleanup_task
    if cancelled:
        raise asyncio.CancelledError
