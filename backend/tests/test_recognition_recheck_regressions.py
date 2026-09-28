"""Independent bounded-assembly regression fixtures; no provider calls."""
import json
import struct

import fitz
import pytest
from pydantic import ValidationError

from backend.recognition.budget import RecognitionBudget
from backend.recognition.fusion import RecognitionAssemblyV1
from backend.recognition.models import RecognitionCandidateV1
from backend.recognition.recheck import build_rechecked_document
from backend.recognition.repair_records import RepairCallEvidenceV1, RepairExecutionV1, RepairImageEvidenceV1
from backend.recognition.repair_response import parse_repair_response
from backend.recognition.repair_selection import select_repair_targets
from backend.recognition.runtime import region_budget_key
from backend.skills.recognition_reader import REPAIR_PROMPT_VERSION
from backend.tests.test_recognition_repair_selection import candidate, initial
from backend.tools.pdf_evidence import (
    PdfDetailResult, PdfPagesRequest, PdfRenderRequest, PdfRenderResult, decode_pdf_payload,
    read_pdf_evidence, whole_page_render_size,
)


def overflow_fixture(prefix_length):
    original = initial(outcomes=[candidate("x" * prefix_length), candidate("a")],
                       texts=["x" * prefix_length, "b"])
    raw = original.raw
    selection = select_repair_targets(original)
    assert len(selection.targets) == 1 and selection.targets[0].unit_id == "u0001"
    target = selection.targets[0]
    # This pure artifact fixture models recorded spend, not dispatch authority.
    now = [0.0]
    ledger = RecognitionBudget(raw.request.source, raw.execution_policy, raw.engine_capabilities, clock=lambda: now[0])
    for unit in raw.read_batch.units:
        ticket = ledger.reserve("initial", 4096,
                                region_keys=(region_budget_key(unit.page_numbers[0], unit.region),))
        ledger.settle(ticket, 10, 2, outcome="ok")
    replacement = "a longer replacement"
    reply = RecognitionCandidateV1(
        kind="vision", status="ok", provider_route_id="selected", input_tokens=10, output_tokens=2,
        text=json.dumps({"decision": "replace", "text": replacement, "source_evidence": "Visible text"}),
    )
    parsed = parse_repair_response(reply, target.context)
    ticket = ledger.reserve("patch", 2048,
                            region_keys=(region_budget_key(target.context.page_number, target.context.region),))
    ledger.settle(ticket, 10, 2, outcome="ok")
    now[0] = raw.budget.duration_ms / 1000 + .1
    image = RepairImageEvidenceV1(
        source_sha256=raw.request.source.input_sha256, page_number=2, region=target.context.region,
        payload_sha256="b" * 64, payload_bytes=100, width=200, height=400, preparation="pdf_render_scale_2",
    )
    execution = RepairExecutionV1(
        prompt_version=REPAIR_PROMPT_VERSION, selection=selection,
        calls=[RepairCallEvidenceV1(unit_id=target.unit_id, image=image, requested_output_tokens=2048, result=parsed)],
        budget=ledger.snapshot(),
    )
    return original, execution, replacement


@pytest.mark.parametrize("final_length", [400000, 400001])
def test_repair_assembly_limit_preserves_every_recorded_call_without_truncation(final_length):
    replacement_length = len("a longer replacement")
    prefix_length = final_length - 2 - replacement_length
    original, execution, replacement = overflow_fixture(prefix_length)
    assert len(original.document.final_markdown) < 400000
    document, error = build_rechecked_document(original, execution)
    if final_length > 400000:
        assert document is None and error == "recognition_assembly_limit"
    else:
        assert error is None and len(document.final_markdown) == 400000
        assert document.final_markdown.endswith(replacement)
    assembled = RecognitionAssemblyV1(
        raw=original.raw, prompt_version=original.prompt_version, document=document,
        safe_error_code=error, repair_execution=execution,
    )
    restored = RecognitionAssemblyV1.model_validate_json(assembled.model_dump_json())
    assert restored == assembled
    assert restored.raw.read_batch.units[0].candidate.text == "x" * prefix_length
    assert restored.raw.read_batch.units[1].candidate.text == "a"
    assert restored.repair_execution.calls[0].result.candidate == execution.calls[0].result.candidate
    assert restored.repair_execution.calls[0].result.final_text == replacement
    assert restored.repair_execution.budget.initial_calls == 2
    assert restored.repair_execution.budget.patch_calls == 1
    assert restored.repair_execution.budget.total_calls == 3
    assert restored.repair_execution.budget.input_tokens == 30
    assert restored.repair_execution.budget.output_tokens == 6


@pytest.mark.parametrize("dimension", ["width", "height"])
def test_pdf_repair_pixel_dimensions_cannot_drift_from_frozen_source_geometry(dimension):
    original, execution, _ = overflow_fixture(100)
    document, error = build_rechecked_document(original, execution)
    assembled = RecognitionAssemblyV1(
        raw=original.raw, prompt_version=original.prompt_version, document=document,
        safe_error_code=error, repair_execution=execution,
    )
    serialized = assembled.model_dump()
    serialized["repair_execution"]["calls"][0]["image"][dimension] = 1
    with pytest.raises(ValidationError, match="dimensions disagree with source geometry"):
        RecognitionAssemblyV1.model_validate(serialized)
    altered = execution.model_copy(deep=True)
    setattr(altered.calls[0].image, dimension, 1)
    with pytest.raises(ValueError, match="dimensions disagree with source geometry"):
        build_rechecked_document(original, altered)


@pytest.mark.asyncio
@pytest.mark.parametrize("rotation", [0, 90])
@pytest.mark.parametrize("width,height", [(42.25, 75.75), (123.50003, 77.00005)])
async def test_fractional_rotated_pdf_size_matches_real_worker_png(rotation, width, height):
    with fitz.open() as pdf:
        page = pdf.new_page(width=width, height=height)
        page.draw_rect((5, 5, 20, 30), fill=(1, 0, 0))
        page.set_rotation(rotation)
        content = pdf.tobytes()
    details = await read_pdf_evidence(content, PdfPagesRequest(pages=[1]))
    rendered = await read_pdf_evidence(content, PdfRenderRequest(page_number=1, scale=2))
    assert isinstance(details, PdfDetailResult) and isinstance(rendered, PdfRenderResult)
    source_page = details.pages[0]
    assert source_page.rotation == rotation
    expected_points = (height, width) if rotation == 90 else (width, height)
    assert (source_page.width_points, source_page.height_points) == pytest.approx(expected_points)
    predicted = whole_page_render_size(source_page.width_points, source_page.height_points)
    actual_png = decode_pdf_payload(rendered)
    assert actual_png[:8] == b"\x89PNG\r\n\x1a\n"
    actual_pixels = struct.unpack(">II", actual_png[16:24])
    assert predicted == (rendered.width, rendered.height) == actual_pixels
    if (width, height) == (42.25, 75.75):
        assert actual_pixels == ((152, 85) if rotation == 90 else (85, 152))
