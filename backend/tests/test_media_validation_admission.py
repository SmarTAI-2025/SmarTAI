"""Queued workflows outlive HTTP admission without relaxing decoder safety."""
from __future__ import annotations

import asyncio
import io
import threading
import time
import zipfile
from types import SimpleNamespace

from fastapi import HTTPException
import pytest

from backend.agents.ingest_agent import SubmissionSourceParseResult
from backend.db import workflow_repository
from backend.services import task_facade
from backend.services.workflow_worker import LeasedOperation
from backend.tests.test_media_upload_validation import pdf_bytes
from backend.tests.test_task_background_workflows import _Registry, _seed_task
from backend.tools import file_processing
from backend.tools.media_validation import validate_media_content


@pytest.mark.asyncio
async def test_queued_submission_waits_past_http_limit_then_reads_original_pdf(monkeypatch):
    """Hold both actual shared slots for 10.4s, without consuming CPU/model work."""
    import pymupdf as fitz

    owner, task_id = _seed_task(with_question=True)
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.insert_text((72, 72), "Question one. Explain this complete native paragraph in your own words.\n" * 4)
        body = pdf.tobytes()
    with io.BytesIO() as stream:
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("answer.pdf", body)
        archive_body = stream.getvalue()
    parsed_texts = []

    async def parse(sources, *args, **kwargs):
        results = []
        for source in sources:
            assert source.pre_error_code is None
            parsed_texts.append(source.text)
            student = {
                "stu_id": "answer", "stu_name": "Synthetic",
                "stu_ans": [{"q_id": "q1", "content": source.text, "flag": []}],
                "identity_status": "matched", "source_id": source.source_id,
                "stored_file_id": source.stored_file_id, "source_filename": source.filename,
            }
            results.append(SubmissionSourceParseResult(
                source.source_id, source.stored_file_id, source.filename,
                "parsed", student, "answer", 1, (), None, None, False,
            ))
        return results

    monkeypatch.setattr(task_facade, "parse_student_answer_sources", parse)
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: _Registry())
    queued = task_facade.queue_task_submission_parsing(
        task_id=task_id, owner_id=owner, filename="responses.zip", content=archive_body,
        content_type="application/zip", registry=_Registry(),
    )
    claimed = workflow_repository.claim_operation(
        queued["job_id"], owner_id=owner, worker_id="media-admission-test", lease_seconds=60,
    )
    slots = threading.BoundedSemaphore(2)
    slots.acquire()
    slots.acquire()
    monkeypatch.setattr(file_processing, "_PDF_EXTRACTION_SLOTS", slots)

    http_done = asyncio.Event()

    async def release():
        await asyncio.sleep(10.4)
        # Under a delayed event loop, admission and release may wake together.
        # Confirm HTTP protection before returning capacity to the queued job.
        await http_done.wait()
        slots.release()
        slots.release()

    async def http_upload():
        try:
            with pytest.raises(HTTPException) as caught:
                await file_processing.validate_submission_upload(body, "answer.pdf", "application/pdf")
            assert caught.value.status_code == 429
            assert caught.value.detail == {"code": "media_inspection_busy"}
        finally:
            http_done.set()

    releaser = asyncio.create_task(release())
    started = time.monotonic()
    try:
        await asyncio.wait_for(asyncio.gather(
            http_upload(),
            task_facade.run_durable_submission_recognition(LeasedOperation(
                claimed, worker_id="media-admission-test", lease_seconds=60,
            )),
        ), 30)
    finally:
        await releaser
    state = task_facade.get_task(task_id=task_id, owner_id=owner)
    source = state["submission_sources"][0]
    assert time.monotonic() - started >= 10.4
    assert state["status"] == "submissions_ready"
    assert source["status"] == "parsed" and source["reason_code"] is None
    assert len(parsed_texts) == 1 and "native paragraph" in parsed_texts[0]
    assert slots.acquire(blocking=False) and slots.acquire(blocking=False)
    assert not slots.acquire(blocking=False)
    slots.release()
    slots.release()


@pytest.mark.asyncio
async def test_lease_loss_cancels_queued_admission_without_launching_worker(monkeypatch):
    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    monkeypatch.setattr(file_processing, "_PDF_EXTRACTION_SLOTS", slots)

    async def forbidden(*args, **kwargs):
        pytest.fail("a fenced queued job must not launch a decoder")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    task = asyncio.create_task(validate_media_content(
        pdf_bytes(), "application/pdf", wait_for_slot=True, slot_timeout_seconds=None,
    ))
    operation = LeasedOperation(
        SimpleNamespace(lease_expires_at=time.time() + 60),
        worker_id="test", lease_seconds=60, on_lease_lost=task.cancel,
    )
    await asyncio.sleep(0.03)
    assert not task.done()
    operation.lose_lease()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not slots.acquire(blocking=False)
    slots.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_reader", [False, True])
@pytest.mark.parametrize("slot_timeout", [None, 10])
async def test_text_quoting_pdf_preserves_admission_policy_on_repeated_validation(monkeypatch, explicit_reader, slot_timeout):
    from backend.services import question_sources
    from backend.services.stage_provider_routing import StageProviderRoute
    from backend.tools import media_validation

    body = b"A file format example: %PDF-1.7\nThis is ordinary teaching text."
    timeouts = []

    async def inspect(content, content_type, **kwargs):
        assert content == body and content_type == "application/pdf"
        timeouts.append(kwargs["slot_timeout_seconds"])
        raise HTTPException(400, detail={"code": "pdf_invalid"})

    monkeypatch.setattr(media_validation, "validate_media_content", inspect)
    read = await question_sources.read_question_source(
        owner_id="owner", task_id="task", content=body, filename="notes.txt",
        route=StageProviderRoute(route_id="unused", kind="llm", provider=None), registry=None,
        text_reader=file_processing.extract_text_from_upload if explicit_reader else None,
        media_slot_timeout_seconds=slot_timeout,
    )
    assert read.text == body.decode() and timeouts == [slot_timeout, slot_timeout]


@pytest.mark.asyncio
async def test_custom_text_reader_keeps_legacy_callback_signature():
    from backend.services import question_sources
    from backend.services.stage_provider_routing import StageProviderRoute

    async def legacy_reader(content, filename, *, purpose, reporter):
        assert content == b"ordinary text" and filename == "notes.txt"
        return "Legacy reader output"

    read = await question_sources.read_question_source(
        owner_id="owner", task_id="task", content=b"ordinary text", filename="notes.txt",
        route=StageProviderRoute(route_id="unused", kind="llm", provider=None), registry=None,
        text_reader=legacy_reader, media_slot_timeout_seconds=None,
    )
    assert read.text == "Legacy reader output"
