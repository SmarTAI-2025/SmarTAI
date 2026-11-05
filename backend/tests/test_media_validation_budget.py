"""Media safety checks obey the submission active-work budget, never queue time."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import threading

import pytest

from backend.domain.errors import RecognitionError
from backend.services import submission_work_budget as clocks
from backend.tools import file_processing
from backend.tools.media_validation import validate_media_content


@pytest.fixture
def budget(monkeypatch):
    value = clocks.SubmissionWorkBudget("media-budget", limit_seconds=0.02)
    monkeypatch.setattr(clocks, "_current", ContextVar("media_test_budget", default=value))
    yield value
    value.close()


@pytest.mark.asyncio
async def test_expired_budget_refuses_media_before_acquiring_or_launching(monkeypatch, budget):
    class ForbiddenSlots:
        def acquire(self, **kwargs):
            pytest.fail("expired budget must not acquire a worker slot")
    monkeypatch.setattr(file_processing, "_PDF_EXTRACTION_SLOTS", ForbiddenSlots())
    budget.expire()
    with pytest.raises(RecognitionError, match=clocks.CODE):
        await validate_media_content(b"%PDF-1.7", "application/pdf", wait_for_slot=True, slot_timeout_seconds=None)
    assert budget.blocked and budget.active == 0


@pytest.mark.asyncio
async def test_other_active_work_expiry_stops_queued_media_promptly(monkeypatch, budget):
    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    monkeypatch.setattr(file_processing, "_PDF_EXTRACTION_SLOTS", slots)
    budget.start()  # A different admitted decoder/model consumes the shared job budget.
    try:
        with pytest.raises(RecognitionError, match=clocks.CODE):
            await asyncio.wait_for(validate_media_content(
                b"%PDF-1.7", "application/pdf", wait_for_slot=True, slot_timeout_seconds=None,
            ), 1)
        assert budget.blocked and budget.active == 1
        assert not slots.acquire(blocking=False)
    finally:
        budget.finish()
        slots.release()


@pytest.mark.asyncio
async def test_budget_race_after_slot_acquisition_releases_slot_without_launch(monkeypatch, budget):
    class ExpiringSlots:
        held = False
        def acquire(self, **kwargs):
            self.held = True
            budget.expire()
            return True
        def release(self):
            assert self.held
            self.held = False
    slots = ExpiringSlots()
    monkeypatch.setattr(file_processing, "_PDF_EXTRACTION_SLOTS", slots)
    with pytest.raises(RecognitionError, match=clocks.CODE):
        await validate_media_content(b"%PDF-1.7", "application/pdf", wait_for_slot=True, slot_timeout_seconds=None)
    assert not slots.held and budget.active == 0 and budget.blocked


@pytest.mark.asyncio
async def test_queue_does_not_spend_budget_and_admitted_decoder_drains(monkeypatch, tmp_path, budget):
    worker = tmp_path / "valid_worker.py"
    worker.write_text(
        'import json, sys, time\nsys.stdin.buffer.read()\ntime.sleep(0.05)\n'
        'print(json.dumps({"status": "ok", "content_type": "application/pdf", "page_count": 1}))\n'
    )
    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    monkeypatch.setattr(file_processing, "_PDF_EXTRACTION_SLOTS", slots)
    monkeypatch.setattr(file_processing, "_PDF_WORKER_PATH", worker)
    task = asyncio.create_task(validate_media_content(
        b"%PDF-1.7", "application/pdf", wait_for_slot=True, slot_timeout_seconds=None,
    ))
    try:
        await asyncio.sleep(0.05)
        assert not task.done() and budget.elapsed == 0 and budget.active == 0
    finally:
        slots.release()
    assert await asyncio.wait_for(task, 2) == {"content_type": "application/pdf", "page_count": 1}
    assert budget.exhausted.is_set() and budget.active == 0
    assert 0.05 <= budget.elapsed < 1
    assert slots.acquire(blocking=False)
    slots.release()
    with pytest.raises(RecognitionError, match=clocks.CODE):
        await validate_media_content(b"%PDF-1.7", "application/pdf", wait_for_slot=True, slot_timeout_seconds=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("queued", [False, True])
async def test_media_cancellation_releases_only_owned_slot_and_work_clock(monkeypatch, tmp_path, budget, queued):
    budget.limit = 10
    worker = tmp_path / "slow_worker.py"
    worker.write_text("import time\ntime.sleep(60)\n")
    slots = threading.BoundedSemaphore(1)
    if queued:
        slots.acquire()
    monkeypatch.setattr(file_processing, "_PDF_EXTRACTION_SLOTS", slots)
    monkeypatch.setattr(file_processing, "_PDF_WORKER_PATH", worker)
    launched = asyncio.Event()
    processes = []
    original = asyncio.create_subprocess_exec

    async def launch(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        launched.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    task = asyncio.create_task(validate_media_content(
        b"%PDF-1.7", "application/pdf", wait_for_slot=True, slot_timeout_seconds=None,
    ))
    if queued:
        await asyncio.sleep(0.03)
        assert not processes and budget.active == 0 and budget.elapsed == 0
    else:
        await asyncio.wait_for(launched.wait(), 2)
        assert budget.active == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert budget.active == 0 and budget.timer is None
    assert all(process.returncode is not None for process in processes)
    if queued:
        assert not slots.acquire(blocking=False)
        slots.release()
    assert slots.acquire(blocking=False)
    assert not slots.acquire(blocking=False)
    slots.release()
