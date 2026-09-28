import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from backend.domain.errors import PdfEvidenceError
from backend.tests.test_media_evidence_worker import encoded_image, restore_pillow_limits, sheet_pdf
from backend.tests.test_pdf_evidence_client import install_process
from backend.tools import file_processing, pdf_evidence, pdf_evidence_worker
from backend.tools.pdf_evidence import (
    ImagePrepareRequest, PdfContactSheetRequest, decode_image_payload, read_image_evidence, read_pdf_evidence,
)


def image_response(body):
    return json.loads(pdf_evidence_worker.response_bytes({"operation": "image_prepare", "content_type": "image/png"}, body))


@pytest.mark.asyncio
async def test_real_media_worker_round_trips_share_existing_lifecycle():
    prepared = await read_image_evidence(encoded_image(), ImagePrepareRequest(content_type="image/png"))
    assert (prepared.width, prepared.height) == (12, 8)
    assert decode_image_payload(prepared).startswith(b"\x89PNG\r\n\x1a\n")
    sheet = await read_pdf_evidence(sheet_pdf(), PdfContactSheetRequest(pages=[1, 3, 5, 8]))
    assert sheet.page_numbers == [1, 3, 5, 8] and [tile.tile_id for tile in sheet.tiles] == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_image_progress_does_not_clear_parent_counters_or_leak_payload_in_argv(monkeypatch):
    body = encoded_image()
    process, launch = install_process(monkeypatch, json.dumps(image_response(body)).encode())
    progress = AsyncMock()
    await read_image_evidence(body, ImagePrepareRequest(content_type="image/png"), progress=progress)
    assert process.stdin.body == body and process.reaped and process.drained
    progress.increment_stage_metrics.assert_awaited_once_with(images_prepared=1)
    progress.set_stage_metrics.assert_not_awaited()
    assert json.loads(launch.call_args.args[3]) == {
        "operation": "image_prepare", "content_type": "image/png", "region": [0.0, 0.0, 1.0, 1.0],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("source_sha256", "b" * 64), ("exif_orientation", 9), ("width", 11),
    ("effective_region", [0.01, 0, 1, 1]), ("crop_box_pixels", [1, 0, 12, 8]),
    ("source_to_oriented_matrix", [[0, 1, 0], [1, 0, 0]]),
    ("metadata_stripped", False), ("resampled", True),
    ("payload_b64", "PRIVATE_INVALID_BASE64"),
])
async def test_image_protocol_rejects_false_geometry_hash_or_transform(monkeypatch, field, value):
    body = encoded_image()
    payload = image_response(body)
    payload[field] = value
    install_process(monkeypatch, json.dumps(payload).encode())
    with pytest.raises(PdfEvidenceError) as exc:
        await read_image_evidence(body, ImagePrepareRequest(content_type="image/png"))
    assert exc.value.code == "pdf_evidence_protocol_invalid" and "PRIVATE" not in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["page", "label_box", "region", "order"])
async def test_sheet_protocol_rejects_wrong_page_or_label_in_source_geometry(monkeypatch, mutation):
    body = sheet_pdf()
    payload = json.loads(pdf_evidence_worker.response_bytes({"operation": "contact_sheet", "pages": [2, 4]}, body))
    if mutation == "page":
        payload["tiles"][0]["page_number"] = 1
    elif mutation == "label_box":
        payload["tiles"][0]["page_bbox_pixels"] = payload["tiles"][0]["tile_bbox_pixels"]
    elif mutation == "region":
        payload["tiles"][0]["page_region"] = [0, 0, 1, 1]
    else:
        payload["tiles"].reverse()
    install_process(monkeypatch, json.dumps(payload).encode())
    with pytest.raises(PdfEvidenceError, match="pdf_evidence_protocol_invalid"):
        await read_pdf_evidence(body, PdfContactSheetRequest(pages=[2, 4]))


@pytest.mark.asyncio
async def test_image_timeout_and_cancellation_reap_before_slot_release(monkeypatch):
    process, _ = install_process(monkeypatch, None)
    body, request = encoded_image(), ImagePrepareRequest(content_type="image/png")
    with pytest.raises(PdfEvidenceError, match="pdf_evidence_timeout"):
        await read_image_evidence(body, request, timeout_seconds=0.01)
    assert process.reaped and process.returncode == -9
    process, _ = install_process(monkeypatch, None)
    task = asyncio.create_task(read_image_evidence(body, request))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.reaped and process.returncode == -9
    assert file_processing._PDF_EXTRACTION_SLOTS.acquire(blocking=False)
    file_processing._PDF_EXTRACTION_SLOTS.release()


@pytest.mark.asyncio
async def test_image_reuses_pdf_busy_slot_without_launch(monkeypatch):
    _, launch = install_process(monkeypatch, b"")
    slots = file_processing._PDF_EXTRACTION_SLOTS
    slots.acquire()
    try:
        with pytest.raises(PdfEvidenceError, match="pdf_evidence_busy"):
            await read_image_evidence(encoded_image(), ImagePrepareRequest(content_type="image/png"))
        launch.assert_not_awaited()
    finally:
        slots.release()


@pytest.mark.asyncio
async def test_image_input_and_response_limits_are_enforced_by_parent(monkeypatch):
    _, launch = install_process(monkeypatch, b"")
    monkeypatch.setattr(pdf_evidence, "MAX_IMAGE_INPUT_BYTES", 5)
    with pytest.raises(PdfEvidenceError, match="image_input_too_large"):
        await read_image_evidence(encoded_image(), ImagePrepareRequest(content_type="image/png"))
    launch.assert_not_awaited()
    monkeypatch.setattr(pdf_evidence, "MAX_IMAGE_INPUT_BYTES", 10 * 1024 * 1024)
    monkeypatch.setattr(pdf_evidence, "DETAIL_RESPONSE_BYTES", 100)
    process, _ = install_process(monkeypatch, b"x" * 101)
    with pytest.raises(PdfEvidenceError, match="pdf_response_too_large"):
        await read_image_evidence(encoded_image(), ImagePrepareRequest(content_type="image/png"))
    assert process.reaped and process.returncode == -9


@pytest.mark.parametrize("make", [
    lambda: ImagePrepareRequest(content_type="image/gif"),
    lambda: ImagePrepareRequest(content_type="image/png", region=(0, 0, 0, 1)),
    lambda: PdfContactSheetRequest(pages=list(range(1, 10))),
    lambda: PdfContactSheetRequest(pages=[2, 1]),
    lambda: PdfContactSheetRequest(pages=[1], tile_long_edge=1025),
])
def test_public_media_requests_have_bounded_typed_parameters(make):
    with pytest.raises(ValidationError):
        make()
