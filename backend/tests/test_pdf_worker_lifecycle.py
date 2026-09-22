"""Cancellation tests use owned fake process handles; no hostile PDFs."""
from __future__ import annotations

import asyncio
import threading

import pytest
from fastapi import HTTPException

from backend.tools import file_processing


class Process:
    def __init__(self):
        self.returncode = None
        self.started = asyncio.Event()
        self.reaped = False
        self.drained = False
    async def communicate(self, data=None):
        if data is not None:
            self.started.set()
            await asyncio.Event().wait()
        self.drained = True
        return b"", b""
    def kill(self):
        self.returncode = -9
    async def wait(self):
        self.reaped = True
        return self.returncode


@pytest.mark.asyncio
@pytest.mark.parametrize("during_launch", [False, True])
async def test_cancelled_pdf_request_reaps_before_releasing_slot(monkeypatch, during_launch):
    process = Process()
    launch_started = asyncio.Event()
    allow_launch = asyncio.Event()
    slots = threading.BoundedSemaphore(1)
    async def launch(*args, **kwargs):
        launch_started.set()
        if during_launch:
            await allow_launch.wait()
        return process
    monkeypatch.setattr(file_processing.asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(file_processing, "_PDF_EXTRACTION_SLOTS", slots)
    task = asyncio.create_task(file_processing._extract_pdf_payload(b"synthetic"))
    await launch_started.wait()
    if not during_launch:
        await process.started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not slots.acquire(blocking=False)
    allow_launch.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.returncode == -9 and process.drained and process.reaped
    assert slots.acquire(blocking=False)
    slots.release()


@pytest.mark.asyncio
async def test_pdf_timeout_reaps_and_preserves_stable_error(monkeypatch):
    process = Process()
    async def launch(*args, **kwargs):
        return process
    monkeypatch.setattr(file_processing.asyncio, "create_subprocess_exec", launch)
    with pytest.raises(HTTPException) as exc:
        await file_processing._extract_pdf_payload(b"synthetic", timeout_seconds=0.01)
    assert exc.value.status_code == 408
    assert exc.value.detail["code"] == "pdf_extraction_timeout"
    assert process.returncode == -9 and process.reaped and process.drained
