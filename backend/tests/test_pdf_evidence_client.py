from __future__ import annotations

import asyncio
import json
import threading
import warnings
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from backend.domain.errors import PdfEvidenceError
from backend.tools import file_processing, pdf_evidence
from backend.tools.pdf_evidence import PdfDetailPage, PdfIndexRequest, PdfPagesRequest, PdfRenderRequest, read_pdf_evidence


def index_payload(**changes):
    return {
        "contract": "smartai.pdf.evidence", "schema_version": 1, "status": "ok", "operation": "index",
        "total_pages": 1, "window_start": 1, "window_end": 1, "complete_window": True,
        "pages": [{
            "page_number": 1, "width_points": 595, "height_points": 842, "rotation": 0,
            "observation": {"page_number": 1, "native_char_count": 0, "native_quality": "missing", "risks": [], "verified_blank": True},
            "target_matches": [],
        }], **changes,
    }


class Writer:
    def __init__(self):
        self.body = bytearray()
        self.closed = False

    def write(self, data):
        self.body.extend(data)

    async def drain(self):
        await asyncio.sleep(0)

    def close(self):
        self.closed = True


@pytest.mark.parametrize("command", [None, {}, PdfIndexRequest().model_copy(update={"start_page": 0})])
def test_public_evidence_validator_revalidates_request_before_matching_result(command):
    with pytest.raises(PdfEvidenceError, match="pdf_invalid_request"):
        pdf_evidence.validate_evidence_result(index_payload(), command)


class Process:
    def __init__(self, output=None):
        self.returncode = None
        self.stdin = Writer()
        self.stdout = asyncio.StreamReader()
        self.reaped = False
        self.drained = False
        if output is not None:
            self.stdout.feed_data(output)
            self.stdout.feed_eof()

    async def wait(self):
        if self.returncode is None:
            self.returncode = 0
        self.reaped = True
        return self.returncode

    def kill(self):
        self.returncode = -9
        self.stdout.feed_eof()

    async def communicate(self):
        self.drained = True
        return await self.stdout.read(), b""


def install_process(monkeypatch, output):
    process = Process(output)
    launch = AsyncMock(return_value=process)
    monkeypatch.setattr(pdf_evidence.asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(file_processing, "_PDF_EXTRACTION_SLOTS", threading.BoundedSemaphore(1))
    return process, launch


@pytest.mark.asyncio
async def test_worker_client_preserves_typed_metadata_and_uses_shared_slots(monkeypatch):
    process, launch = install_process(monkeypatch, json.dumps(index_payload()).encode())
    progress = AsyncMock()
    result = await read_pdf_evidence(b"%PDF-synthetic", PdfIndexRequest(), progress=progress)
    assert result.total_pages == 1 and result.pages[0].observation.verified_blank
    assert process.stdin.body == b"%PDF-synthetic"
    assert process.stdin.closed and process.reaped and process.drained
    args = launch.call_args.args
    assert args[2] == "evidence-v1"
    assert json.loads(args[3])["operation"] == "index"
    assert "synthetic" not in repr(args)
    progress.set_current_step.assert_awaited_once_with("pdf_index", message="Reading PDF page evidence")
    progress.increment_stage_metrics.assert_awaited_once_with(pdf_pages_observed=1)
    progress.set_stage_metrics.assert_not_awaited()
    assert file_processing._PDF_EXTRACTION_SLOTS.acquire(blocking=False)
    file_processing._PDF_EXTRACTION_SLOTS.release()


@pytest.mark.asyncio
async def test_worker_client_refuses_busy_shared_pool_without_launching(monkeypatch):
    _, launch = install_process(monkeypatch, b"")
    slots = file_processing._PDF_EXTRACTION_SLOTS
    slots.acquire()
    with pytest.raises(PdfEvidenceError) as exc:
        await read_pdf_evidence(b"%PDF-synthetic", PdfIndexRequest())
    assert exc.value.code == "pdf_evidence_busy"
    launch.assert_not_awaited()
    slots.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("during_launch", [False, True])
async def test_cancelled_client_reaps_before_releasing_shared_slot(monkeypatch, during_launch):
    process, _ = install_process(monkeypatch, None)
    started, allow_launch = asyncio.Event(), asyncio.Event()

    async def launch(*args, **kwargs):
        started.set()
        if during_launch:
            await allow_launch.wait()
        return process

    monkeypatch.setattr(pdf_evidence.asyncio, "create_subprocess_exec", launch)
    task = asyncio.create_task(read_pdf_evidence(b"%PDF-synthetic", PdfIndexRequest()))
    await started.wait()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not file_processing._PDF_EXTRACTION_SLOTS.acquire(blocking=False)
    task.cancel()
    allow_launch.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.returncode == -9 and process.reaped and process.drained
    assert file_processing._PDF_EXTRACTION_SLOTS.acquire(blocking=False)
    file_processing._PDF_EXTRACTION_SLOTS.release()


@pytest.mark.asyncio
async def test_timeout_kills_and_reaps_worker_with_stable_code(monkeypatch):
    process, _ = install_process(monkeypatch, None)
    with pytest.raises(PdfEvidenceError) as exc:
        await read_pdf_evidence(b"%PDF-synthetic", PdfIndexRequest(), timeout_seconds=0.01)
    assert exc.value.code == "pdf_evidence_timeout"
    assert process.returncode == -9 and process.reaped and process.drained


@pytest.mark.asyncio
async def test_parent_caps_output_even_if_worker_does_not(monkeypatch):
    process, _ = install_process(monkeypatch, b"x" * 1025)
    monkeypatch.setattr(pdf_evidence, "INDEX_RESPONSE_BYTES", 1024)
    with pytest.raises(PdfEvidenceError) as exc:
        await read_pdf_evidence(b"%PDF-synthetic", PdfIndexRequest())
    assert exc.value.code == "pdf_response_too_large"
    assert process.returncode == -9 and process.reaped


@pytest.mark.asyncio
@pytest.mark.parametrize("output", [b"PRIVATE_BAD_JSON", b"\xff", b"[]", b"null"])
async def test_bad_protocol_is_safely_classified(monkeypatch, output):
    install_process(monkeypatch, output)
    with pytest.raises(PdfEvidenceError) as exc:
        await read_pdf_evidence(b"%PDF-synthetic", PdfIndexRequest())
    assert exc.value.code == "pdf_evidence_protocol_invalid"
    assert "PRIVATE" not in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"window_end": 2}, {"pages": []}, {"schema_version": True}, {"total_pages": 0},
    {"operation": "detail"}, {"complete_window": False},
])
async def test_incomplete_or_wrong_operation_cannot_be_success(monkeypatch, changes):
    install_process(monkeypatch, json.dumps(index_payload(**changes)).encode())
    with pytest.raises(PdfEvidenceError) as exc:
        await read_pdf_evidence(b"%PDF-synthetic", PdfIndexRequest())
    assert exc.value.code == "pdf_evidence_protocol_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize("code,expected", [("pdf_encrypted", "pdf_encrypted"), ("PRIVATE_PARSE_FAILURE", "pdf_evidence_protocol_invalid")])
async def test_worker_error_codes_are_whitelisted(monkeypatch, code, expected):
    install_process(monkeypatch, json.dumps({
        "contract": "smartai.pdf.evidence", "schema_version": 1,
        "status": "error", "code": code,
    }).encode())
    with pytest.raises(PdfEvidenceError) as exc:
        await read_pdf_evidence(b"%PDF-synthetic", PdfIndexRequest())
    assert exc.value.code == expected
    assert "PRIVATE" not in str(exc.value)


@pytest.mark.asyncio
async def test_invalid_or_mutated_request_never_launches_worker(monkeypatch):
    _, launch = install_process(monkeypatch, b"")
    request = PdfPagesRequest(pages=[1])
    request.pages.append(1)
    with pytest.raises(PdfEvidenceError) as exc:
        await read_pdf_evidence(b"%PDF-synthetic", request)
    assert exc.value.code == "pdf_invalid_request"
    launch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("content,code", [(b"", "pdf_invalid"), (b"private non PDF", "pdf_invalid"), (b"%PDF-" + b"x" * 100, "pdf_input_too_large")])
async def test_invalid_or_oversized_input_never_launches(monkeypatch, content, code):
    _, launch = install_process(monkeypatch, b"")
    monkeypatch.setattr(pdf_evidence, "MAX_INPUT_BYTES", 100)
    with pytest.raises(PdfEvidenceError) as exc:
        await read_pdf_evidence(content, PdfIndexRequest())
    assert exc.value.code == code
    launch.assert_not_awaited()


@pytest.mark.asyncio
async def test_launch_failure_releases_slot_without_exposing_error(monkeypatch):
    _, launch = install_process(monkeypatch, b"")
    launch.side_effect = OSError("PRIVATE_PROCESS_ERROR")
    with pytest.raises(PdfEvidenceError) as exc:
        await read_pdf_evidence(b"%PDF-synthetic", PdfIndexRequest())
    assert exc.value.code == "pdf_processing_failed"
    assert "PRIVATE" not in str(exc.value)
    assert file_processing._PDF_EXTRACTION_SLOTS.acquire(blocking=False)
    file_processing._PDF_EXTRACTION_SLOTS.release()


@pytest.mark.asyncio
async def test_invalid_snapshot_never_prints_private_serializer_warning(monkeypatch):
    _, launch = install_process(monkeypatch, b"")
    request = PdfPagesRequest(pages=[1])
    request.pages.append("PRIVATE_PAGE_VALUE")
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        with pytest.raises(PdfEvidenceError) as exc:
            await read_pdf_evidence(b"%PDF-synthetic", request)
    assert exc.value.code == "pdf_invalid_request"
    assert recorded == []
    launch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [None, "PRIVATE_TIMEOUT", True, float("nan"), float("inf"), 0, 31])
async def test_invalid_timeout_has_stable_error_without_launching(monkeypatch, timeout):
    _, launch = install_process(monkeypatch, b"")
    with pytest.raises(PdfEvidenceError) as exc:
        await read_pdf_evidence(b"%PDF-synthetic", PdfIndexRequest(), timeout_seconds=timeout)
    assert exc.value.code == "pdf_invalid_request"
    assert "PRIVATE" not in str(exc.value)
    launch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_error", [False, True])
async def test_cleanup_failure_never_exposes_transport_context_or_masks_safe_error(monkeypatch, initial_error):
    process, _ = install_process(monkeypatch, b"bad json" if initial_error else json.dumps(index_payload()).encode())

    async def fail_cleanup():
        raise OSError("PRIVATE_TRANSPORT_CONTEXT")

    process.communicate = fail_cleanup
    with pytest.raises(PdfEvidenceError) as exc:
        await read_pdf_evidence(b"%PDF-synthetic", PdfIndexRequest())
    assert exc.value.code == ("pdf_evidence_protocol_invalid" if initial_error else "pdf_processing_failed")
    assert "PRIVATE" not in str(exc.value)
    assert process.reaped
    assert file_processing._PDF_EXTRACTION_SLOTS.acquire(blocking=False)
    file_processing._PDF_EXTRACTION_SLOTS.release()


@pytest.mark.asyncio
async def test_cancel_during_failing_cleanup_still_propagates_cancellation(monkeypatch):
    process, _ = install_process(monkeypatch, json.dumps(index_payload()).encode())
    cleanup_started, finish_cleanup = asyncio.Event(), asyncio.Event()

    async def fail_cleanup():
        cleanup_started.set()
        await finish_cleanup.wait()
        raise OSError("PRIVATE_TRANSPORT_CONTEXT")

    process.communicate = fail_cleanup
    task = asyncio.create_task(read_pdf_evidence(b"%PDF-synthetic", PdfIndexRequest()))
    await cleanup_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not file_processing._PDF_EXTRACTION_SLOTS.acquire(blocking=False)
    finish_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.reaped
    assert file_processing._PDF_EXTRACTION_SLOTS.acquire(blocking=False)
    file_processing._PDF_EXTRACTION_SLOTS.release()


@pytest.mark.parametrize("make_request", [
    lambda: PdfIndexRequest(window_pages=501), lambda: PdfIndexRequest(start_page=0),
    lambda: PdfIndexRequest(targets=["x" * 81]), lambda: PdfPagesRequest(pages=[True]),
    lambda: PdfIndexRequest(targets=[" "]), lambda: PdfIndexRequest(targets=["1", "1"]),
    lambda: PdfIndexRequest(targets=["1\n2"]), lambda: PdfIndexRequest(targets=["1\x00"]),
    lambda: PdfPagesRequest(pages=[2, 1]), lambda: PdfPagesRequest(pages=list(range(1, 26))),
    lambda: PdfRenderRequest(page_number=1, region=(0, 0, 0, 1)),
    lambda: PdfRenderRequest(page_number=1, scale=3),
])
def test_public_requests_enforce_bounded_ranges(make_request):
    with pytest.raises(ValidationError):
        make_request()


def test_detail_cannot_omit_a_native_block_but_claim_full_page_text():
    page = index_payload()["pages"][0]
    page.update(page_index=0, native_text="retained native text", blocks=[])
    page["observation"].update(native_char_count=len(page["native_text"]), native_quality="clean", verified_blank=False)
    with pytest.raises(ValidationError, match="cover all native text"):
        PdfDetailPage.model_validate(page)
