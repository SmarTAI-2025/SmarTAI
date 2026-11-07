import asyncio
from io import BytesIO
import threading
from types import SimpleNamespace

from docx import Document
import pytest

from backend.knowledge.ingestion import native_units
from backend.tools import file_processing


def word_document(text):
    document = Document()
    document.add_paragraph(text)
    stream = BytesIO()
    document.save(stream)
    return stream.getvalue()


@pytest.mark.asyncio
async def test_background_docx_waits_past_interactive_admission_limit(monkeypatch):
    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    clock = [0.0]
    waits = []

    class AdmissionClock:
        def __getattr__(self, name):
            return getattr(asyncio, name)

        def get_running_loop(self):
            return SimpleNamespace(time=lambda: clock[0])

        async def sleep(self, delay):
            waits.append(delay)
            clock[0] += 11.0
            if len(waits) == 2:
                slots.release()
            await asyncio.sleep(0)

    monkeypatch.setattr(file_processing, 'asyncio', AdmissionClock())
    monkeypatch.setattr(file_processing, '_PDF_EXTRACTION_SLOTS', slots)
    try:
        result = await native_units('lesson.docx', word_document('Whole lesson'))
        assert clock[0] == 22.0
        assert result == {'units': ['Whole lesson'], 'unit': 'section', 'warning_codes': []}
        assert slots.acquire(blocking=False)
    finally:
        # This test owns exactly one slot, including when old code rejects at 11s.
        slots.release()


@pytest.mark.asyncio
async def test_background_docx_wait_is_cancellable_without_starting_worker(monkeypatch):
    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    queued = asyncio.Event()

    class AdmissionWait:
        def __getattr__(self, name):
            return getattr(asyncio, name)

        async def sleep(self, delay):
            queued.set()
            await asyncio.Event().wait()

        async def create_subprocess_exec(self, *args, **kwargs):
            raise AssertionError('No worker may start before admission')

    monkeypatch.setattr(file_processing, 'asyncio', AdmissionWait())
    monkeypatch.setattr(file_processing, '_PDF_EXTRACTION_SLOTS', slots)
    task = asyncio.create_task(native_units('lesson.docx', b'unread while queued'))
    try:
        await asyncio.wait_for(queued.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not slots.acquire(blocking=False)
    finally:
        slots.release()
