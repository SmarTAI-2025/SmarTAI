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


@pytest.mark.asyncio
@pytest.mark.parametrize("stuck_phase", ["launch", "communicate", "wait"])
async def test_cleanup_cap_quarantines_slot_until_late_success(monkeypatch, stuck_phase):
    from backend.tools import pdf_worker_lifecycle as lifecycle
    monkeypatch.setattr(lifecycle, "PDF_CLEANUP_SECONDS", .02)
    resume = asyncio.Event()
    process = Process()
    original_communicate, original_wait = process.communicate, process.wait
    async def communicate(data=None):
        if stuck_phase == "communicate":
            await resume.wait()
        return await original_communicate(data)
    async def wait():
        if stuck_phase == "wait":
            await resume.wait()
        return await original_wait()
    process.communicate, process.wait = communicate, wait
    async def launch():
        if stuck_phase == "launch":
            await resume.wait()
        return process
    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    with pytest.raises(lifecycle.PdfWorkerCleanupTimeout):
        await lifecycle.reap_pdf_worker(asyncio.create_task(launch()), release_slot=slots.release)
    assert not slots.acquire(blocking=False)
    assert lifecycle._quarantined
    resume.set()
    for _ in range(20):
        await asyncio.sleep(0)
        if not lifecycle._quarantined:
            break
    assert process.reaped and process.drained
    assert not lifecycle._quarantined
    assert slots.acquire(blocking=False)
    slots.release()


@pytest.mark.asyncio
async def test_repeated_cancellation_does_not_restart_cleanup_allowance(monkeypatch):
    from backend.tools import pdf_worker_lifecycle as lifecycle
    monkeypatch.setattr(lifecycle, "PDF_CLEANUP_SECONDS", .03)
    resume = asyncio.Event()
    process = Process()
    async def launch():
        await resume.wait()
        return process
    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    reaper = asyncio.create_task(lifecycle.reap_pdf_worker(asyncio.create_task(launch()), release_slot=slots.release))
    async def cancel_repeatedly():
        while not reaper.done():
            await asyncio.sleep(.005)
            reaper.cancel()
    canceller = asyncio.create_task(cancel_repeatedly())
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(reaper), .5)
        assert not slots.acquire(blocking=False)
    finally:
        canceller.cancel()
        await asyncio.gather(canceller, return_exceptions=True)
        resume.set()
        for _ in range(20):
            await asyncio.sleep(0)
            if not lifecycle._quarantined:
                break
    assert slots.acquire(blocking=False)
    slots.release()


@pytest.mark.asyncio
async def test_original_request_cancellation_survives_cleanup_timeout(monkeypatch):
    from backend.tools import pdf_worker_lifecycle as lifecycle
    monkeypatch.setattr(lifecycle, "PDF_CLEANUP_SECONDS", .02)
    process = Process()
    resume = asyncio.Event()
    original = process.communicate
    async def communicate(data=None):
        if data is None:
            await resume.wait()
        return await original(data)
    process.communicate = communicate
    async def launch(*args, **kwargs):
        return process
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(file_processing, "_PDF_EXTRACTION_SLOTS", slots)
    monkeypatch.setattr(file_processing.asyncio, "create_subprocess_exec", launch)
    request = asyncio.create_task(file_processing._extract_pdf_payload(b"synthetic"))
    await process.started.wait()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert not slots.acquire(blocking=False)
    resume.set()
    for _ in range(20):
        await asyncio.sleep(0)
        if not lifecycle._quarantined:
            break
    assert process.reaped
    assert slots.acquire(blocking=False)
    slots.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("inspect", [False, True])
@pytest.mark.parametrize("cleanup_expires", [False, True])
async def test_legacy_startup_timeout_reaps_late_handle_without_releasing_early(
    monkeypatch, inspect, cleanup_expires,
):
    from backend.tools import pdf_worker_lifecycle as lifecycle

    monkeypatch.setattr(lifecycle, "PDF_CLEANUP_SECONDS", .02 if cleanup_expires else 1)
    process = Process()
    allow_launch = asyncio.Event()
    cleanup_started = asyncio.Event()
    slots = threading.BoundedSemaphore(1)
    launches = 0

    async def launch(*args, **kwargs):
        nonlocal launches
        launches += 1
        await allow_launch.wait()
        return process

    original_reaper = file_processing.reap_pdf_worker

    async def reap(*args, **kwargs):
        cleanup_started.set()
        return await original_reaper(*args, **kwargs)

    monkeypatch.setattr(file_processing.asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(file_processing, "_PDF_EXTRACTION_SLOTS", slots)
    monkeypatch.setattr(file_processing, "reap_pdf_worker", reap)

    async def invoke():
        if inspect:
            return await file_processing.inspect_baidu_ocr_upload(
                b"%PDF-1.7\n", "answer.pdf", timeout_seconds=.01,
            )
        return await file_processing._extract_pdf_payload(b"synthetic", timeout_seconds=.01)

    request = asyncio.create_task(invoke())
    try:
        # The old implementation never entered cleanup while launch was stuck.
        await asyncio.wait_for(cleanup_started.wait(), .5)
        assert not slots.acquire(blocking=False)
        assert not process.started.is_set()
        if not cleanup_expires:
            allow_launch.set()
        with pytest.raises(HTTPException) as captured:
            await asyncio.wait_for(asyncio.shield(request), .5)
        prefix = "media_inspection" if inspect else "pdf_extraction"
        assert captured.value.status_code == (503 if cleanup_expires else 408)
        assert captured.value.detail["code"] == prefix + ("_busy" if cleanup_expires else "_timeout")
        if cleanup_expires:
            assert not slots.acquire(blocking=False)
            with pytest.raises(HTTPException) as busy:
                await invoke()
            assert busy.value.status_code == 429
            assert launches == 1
    finally:
        allow_launch.set()
        if not request.done():
            request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        for _ in range(30):
            await asyncio.sleep(0)
            if not lifecycle._quarantined:
                break
    assert process.returncode == -9 and process.reaped and process.drained
    assert not process.started.is_set()  # A late handle never processes the input.
    assert slots.acquire(blocking=False)
    assert not slots.acquire(blocking=False)
    slots.release()
