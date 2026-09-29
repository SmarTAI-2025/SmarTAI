from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pymupdf as fitz
import pytest

from backend.tools import pdf_evidence_worker as worker


def pdf_bytes(texts=("1. Solve x_1=2",), *, rotation=0):
    with fitz.open() as document:
        for text in texts:
            page = document.new_page(width=200, height=100)
            if text:
                page.insert_text((10, 30), text)
            page.set_rotation(rotation)
        return document.tobytes()


def reply(body, operation="index", **fields):
    value = json.loads(worker.response_bytes({"operation": operation, **fields}, body))
    assert value["contract"] == "smartai.pdf.evidence"
    assert value["schema_version"] == 1
    assert value["operation"] == operation
    return value


def assert_error(value, code):
    assert value["status"] == "error"
    assert value["code"] == code
    assert set(value) <= {"contract", "schema_version", "status", "operation", "code"}


def cli(arguments, body, *, cwd):
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    path = Path(worker.__file__).with_name("_pdf_worker.py")
    result = subprocess.run([sys.executable, "-B", str(path), *arguments], input=body,
                            capture_output=True, timeout=20, cwd=cwd, env=env)
    assert result.returncode == 0
    return json.loads(result.stdout)


def test_production_resource_limits_are_explicit():
    assert worker.MAX_INPUT_BYTES == 100 * 1024 * 1024
    assert worker.MAX_PAGES == 10_000
    assert worker.MAX_PAGE_CHARACTERS == 500_000
    assert worker.MAX_INDEX_BYTES == 512 * 1024
    assert worker.MAX_RESPONSE_BYTES == 8 * 1024 * 1024
    assert worker.MAX_RENDER_PIXELS == 16_000_000
    assert worker.MAX_RENDER_RGB_BYTES == 48 * 1024 * 1024
    assert worker.MAX_RENDER_SIDE == 8192


def test_thousand_page_book_window_tail_and_index_never_leaks_native_text():
    with fitz.open() as document:
        for number in range(1, 1001):
            page = document.new_page(width=100, height=100)
            if number in {1, 500, 999, 1000}:
                page.insert_text((5, 25), f"PRIVATE_PAGE_{number}", fontsize=5)
        body = document.tobytes()
    middle = reply(body, start_page=499, window_pages=3)
    assert middle["total_pages"] == 1000
    assert middle["window_start"] == 499 and middle["window_end"] == 501
    assert middle["complete_window"] is True
    assert [page["page_number"] for page in middle["pages"]] == [499, 500, 501]
    assert middle["pages"][1]["observation"]["native_char_count"] > 0
    assert "PRIVATE_PAGE" not in json.dumps(middle)
    assert all(set(page) == {"page_number", "width_points", "height_points", "rotation",
                             "observation", "target_matches"} for page in middle["pages"])
    tail = reply(body, start_page=999)
    assert tail["window_end"] == 1000 and tail["complete_window"]
    assert [page["page_number"] for page in tail["pages"]] == [999, 1000]
    first = reply(body)
    assert len(first["pages"]) == 500 and first["window_end"] == 500
    assert_error(reply(body, start_page=1001), "pdf_page_out_of_range")


def test_targets_use_full_question_boundaries_and_preserve_global_page_identity():
    body = pdf_bytes(("11. 1.20 1.2.3 A1 Q11", "1. target\n1.2 condition\nQ1. next", "continuation"))
    value = reply(body, targets=["1", "1.2", "Q1"])
    assert value["pages"][0]["target_matches"] == []
    assert value["pages"][1]["target_matches"] == ["1", "1.2", "Q1"]
    assert value["pages"][2]["target_matches"] == []
    window = reply(body, start_page=2, window_pages=2, targets=["1.2"])
    assert window["pages"][0]["page_number"] == 2
    assert window["pages"][0]["target_matches"] == ["1.2"]


def test_detail_has_exact_unicode_offsets_and_visual_blocks_have_no_text():
    with fitz.open() as document:
        page = document.new_page(width=300, height=300)
        page.insert_text((10, 30), "English 1.2")
        page.insert_text((10, 70), "数学条件", fontname="china-s")
        page.insert_text((10, 120), "x_1 = -2")
        page.draw_rect(fitz.Rect(10, 180, 80, 220), color=(0, 0, 0))
        pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 2, 2), False)
        pixmap.clear_with(200)
        page.insert_image(fitz.Rect(100, 180, 150, 230), stream=pixmap.tobytes("png"))
        body = document.tobytes()
    value = reply(body, "detail", pages=[1])
    page = value["pages"][0]
    assert page["page_number"] == 1 and page["page_index"] == 0
    assert "数学条件" in page["native_text"]
    assert "x_1 = -2" in page["native_text"]
    assert page["observation"]["native_char_count"] == len(page["native_text"])
    assert "math" in page["observation"]["risks"]
    assert page["target_matches"] == []
    assert "payload_b64" not in json.dumps(value)
    assert {block["kind"] for block in page["blocks"]} == {"text", "image", "drawing"}
    assert [block["order_index"] for block in page["blocks"]] == list(range(len(page["blocks"])))
    end = 0
    for block in page["blocks"]:
        if block["kind"] == "text":
            assert block["native_char_start"] == end
            end = block["native_char_end"]
            assert page["native_text"][block["native_char_start"]:end] == block["text"]
        else:
            assert block["text"] == ""
            assert block["native_char_start"] is None and block["native_char_end"] is None
    assert end == len(page["native_text"])


def test_scan_mixed_ink_and_white_content_are_not_verified_blank():
    with fitz.open() as document:
        document.new_page(width=200, height=100)
        page = document.new_page(width=200, height=100)
        pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 4, 4), False)
        pixmap.clear_with(255)
        page.insert_image(page.rect, stream=pixmap.tobytes("png"))
        page = document.new_page(width=200, height=100)
        page.insert_image(page.rect, stream=pixmap.tobytes("png"))
        page.insert_text((10, 30), "native label")
        page = document.new_page(width=200, height=100)
        page.draw_rect(page.rect, fill=(1, 1, 1), color=None)
        page = document.new_page(width=200, height=100)
        page.add_ink_annot([[(10, 10), (30, 30), (50, 10)]])
        body = document.tobytes()
    observations = [page["observation"] for page in reply(body)["pages"]]
    assert [item["verified_blank"] for item in observations] == [True, False, False, False, False]
    assert observations[1]["native_quality"] == "missing"
    assert observations[2]["native_quality"] == "clean"
    assert "diagram" in observations[1]["risks"]
    assert "handwriting" in observations[4]["risks"]
    assert "layout" in observations[4]["risks"]


def test_table_and_damaged_text_signals_are_heuristics_not_native_rewrites():
    with fitz.open() as document:
        page = document.new_page(width=200, height=300)
        page.insert_text((10, 30), "a\nb\nc\nd\ne\nf\ng\nh")
        page.draw_line((10, 200), (150, 200))
        page.draw_line((10, 240), (150, 240))
        page.draw_line((10, 200), (10, 240))
        page.draw_line((150, 200), (150, 240))
        body = document.tobytes()
    detail = reply(body, "detail", pages=[1])["pages"][0]
    assert detail["native_text"] == "a\nb\nc\nd\ne\nf\ng\nh\n"
    assert detail["observation"]["native_quality"] == "suspect"
    assert {"damaged_text", "table", "diagram"} <= set(detail["observation"]["risks"])


@pytest.mark.parametrize("rotation,region,size", [
    (0, [0, 0, 0.25, 1], (50, 100)),
    (90, [0, 0, 1, 0.25], (100, 50)),
    (180, [0.75, 0, 1, 1], (50, 100)),
    (270, [0, 0.75, 1, 1], (100, 50)),
])
def test_rotated_visual_regions_match_actual_pixels_and_drawing_bounds(rotation, region, size):
    with fitz.open() as document:
        page = document.new_page(width=200, height=100)
        page.draw_rect(fitz.Rect(0, 0, 50, 100), fill=(1, 0, 0), color=None)
        page.set_rotation(rotation)
        body = document.tobytes()
    detail = reply(body, "detail", pages=[1])["pages"][0]
    assert detail["rotation"] == rotation
    assert detail["blocks"][0]["region"] == pytest.approx(region)
    value = reply(body, "render", page_number=1, region=region, scale=1)
    assert value["region"] == region
    assert (value["width"], value["height"]) == size
    pixmap = fitz.Pixmap(base64.b64decode(value["payload_b64"]))
    assert pixmap.pixel(pixmap.width // 2, pixmap.height // 2) == (255, 0, 0)
    assert pixmap.n == 3


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_cropped_rotated_page_text_regions_locate_actual_rendered_text(rotation):
    with fitz.open() as document:
        page = document.new_page(width=300, height=200)
        page.insert_text((60, 70), "TARGET 1.2", fontsize=15)
        page.set_cropbox(fitz.Rect(50, 30, 250, 150))
        page.set_rotation(rotation)
        body = document.tobytes()
    page = reply(body, "detail", pages=[1])["pages"][0]
    region = page["blocks"][0]["region"]
    assert all(0 <= value <= 1 for value in region)
    result = reply(body, "render", page_number=1, region=region, scale=2)
    pixmap = fitz.Pixmap(base64.b64decode(result["payload_b64"]))
    assert pixmap.width < page["width_points"] * 2
    assert pixmap.height < page["height_points"] * 2
    assert sum(value < 100 for value in pixmap.samples) > 30
    assert page["native_text"] == "TARGET 1.2\n"


def test_export_pages_preserves_mapping_content_rotation_and_original_bytes():
    body = pdf_bytes(("first", "second", "third"), rotation=90)
    before = hashlib.sha256(body).hexdigest()
    value = reply(body, "export_pages", pages=[1, 3])
    assert value["page_numbers"] == [1, 3] and value["total_pages"] == 3
    assert value["content_type"] == "application/pdf"
    with fitz.open(stream=base64.b64decode(value["payload_b64"]), filetype="pdf") as selected:
        assert len(selected) == 2
        assert [page.get_text().strip() for page in selected] == ["first", "third"]
        assert [page.rotation for page in selected] == [90, 90]
    assert hashlib.sha256(body).hexdigest() == before


@pytest.mark.parametrize("payload", [
    None, [], {}, {"operation": []}, {"operation": "unknown"},
    {"operation": "index", "extra": 1}, {"operation": "index", "start_page": 0},
    {"operation": "index", "window_pages": 501}, {"operation": "index", "window_pages": True},
    {"operation": "index", "targets": ["1"] * 2}, {"operation": "index", "targets": [" "]},
    {"operation": "index", "targets": ["a\nb"]}, {"operation": "index", "targets": ["x" * 81]},
    {"operation": "index", "targets": [str(n) for n in range(65)]},
    {"operation": "detail", "pages": []}, {"operation": "detail", "pages": [1, 1]},
    {"operation": "detail", "pages": [2, 1]}, {"operation": "detail", "pages": [True]},
    {"operation": "detail", "pages": list(range(1, 26))},
    {"operation": "render", "page_number": 1, "region": [0, 0, 0, 1]},
    {"operation": "render", "page_number": 1, "region": [0, 0, float("nan"), 1]},
    {"operation": "render", "page_number": 1, "region": [-0.1, 0, 1, 1]},
    {"operation": "render", "page_number": 1, "scale": 2.1},
    {"operation": "render", "page_number": 1, "scale": True},
    {"operation": "render", "page_number": 1, "scale": float("inf")},
])
def test_invalid_requests_fail_closed(payload):
    value = json.loads(worker.response_bytes(payload, pdf_bytes()))
    assert_error(value, "pdf_invalid_request")


@pytest.mark.parametrize("raw", ["{", "[]", '{"operation":"index","operation":"detail"}',
                                '{"operation":"render","scale":NaN}', "x" * (64 * 1024 + 1)])
def test_json_request_parser_is_strict(raw):
    with pytest.raises(worker.EvidenceFailure) as exc:
        worker.parse_request(raw)
    assert exc.value.code == "pdf_invalid_request"


@pytest.mark.parametrize("operation,fields", [
    ("index", {"start_page": 2}), ("detail", {"pages": [2]}),
    ("render", {"page_number": 2}), ("export_pages", {"pages": [2]}),
])
def test_all_operations_reject_nonexistent_pages(operation, fields):
    assert_error(reply(pdf_bytes(), operation, **fields), "pdf_page_out_of_range")


@pytest.mark.parametrize("body", [b"", b"PRIVATE_BROKEN_PDF", b"%PDF-1.7\nPRIVATE_BROKEN_PDF"])
def test_invalid_pdf_never_leaks_parser_input_or_exception(body):
    value = reply(body)
    assert_error(value, "pdf_invalid")
    assert "PRIVATE" not in json.dumps(value)


def test_encrypted_pdf_requires_explicit_external_handling():
    with fitz.open(stream=pdf_bytes(), filetype="pdf") as document:
        body = document.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="owner-test", user_pw="user-test")
    assert_error(reply(body), "pdf_encrypted")


@pytest.mark.parametrize("constant,limit,expected", [
    ("MAX_INPUT_BYTES", 16, "pdf_input_too_large"),
    ("MAX_PAGES", 1, "pdf_page_limit_exceeded"),
    ("MAX_PAGE_CHARACTERS", 3, "pdf_character_limit_exceeded"),
    ("MAX_STRUCTURE_ITEMS", 1, "pdf_structure_limit_exceeded"),
    ("MAX_BLOCKS", 0, "pdf_structure_limit_exceeded"),
    ("MAX_INDEX_BYTES", 200, "pdf_response_too_large"),
])
def test_index_resource_limits_return_error_not_partial_success(monkeypatch, constant, limit, expected):
    body = pdf_bytes(("PRIVATE first", "PRIVATE second"))
    monkeypatch.setattr(worker, constant, limit)
    value = reply(body)
    assert_error(value, expected)
    assert "pages" not in value and "PRIVATE" not in json.dumps(value)


@pytest.mark.parametrize("operation,fields", [
    ("detail", {"pages": [1]}), ("render", {"page_number": 1}), ("export_pages", {"pages": [1]}),
])
def test_non_index_output_limit_is_explicit_without_truncation(monkeypatch, operation, fields):
    monkeypatch.setattr(worker, "MAX_RESPONSE_BYTES", 100)
    assert_error(reply(pdf_bytes(), operation, **fields), "pdf_response_too_large")


@pytest.mark.parametrize("width,height", [(5000, 5000), (9000, 20)])
def test_oversized_render_is_rejected_before_allocating_pixmap(monkeypatch, width, height):
    with fitz.open() as document:
        document.new_page(width=width, height=height)
        body = document.tobytes()

    def forbidden(*args, **kwargs):
        pytest.fail("pixel limit must be enforced before get_pixmap")

    monkeypatch.setattr(fitz.Page, "get_pixmap", forbidden)
    assert_error(reply(body, "render", page_number=1, scale=1), "pdf_render_limit_exceeded")


def test_internal_failure_does_not_echo_exception_or_partial_content(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("PRIVATE_PROBLEM_AND_STUDENT_DATA")

    monkeypatch.setattr(worker, "_page_evidence", fail)
    value = reply(pdf_bytes())
    assert_error(value, "pdf_processing_failed")
    assert "PRIVATE" not in json.dumps(value)


def test_worker_cli_runs_without_backend_import_path_and_keeps_legacy_modes(tmp_path):
    body = pdf_bytes()
    value = cli(["evidence-v1", '{"operation":"detail","pages":[1]}'], body, cwd=tmp_path)
    assert value["status"] == "ok" and value["pages"][0]["native_text"] == "1. Solve x_1=2\n"
    assert_error(cli(["evidence-v1", "{"], body, cwd=tmp_path), "pdf_invalid_request")
    assert cli(["inspect-pdf", "1"], body, cwd=tmp_path) == {"status": "ok", "page_count": 1}
    legacy = cli(["1", "1000"], body, cwd=tmp_path)
    assert legacy == {"status": "ok", "page_count": 1, "text": "1. Solve x_1=2\n"}
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 2, 3), False)
    assert cli(["inspect-image", "3"], pixmap.tobytes("png"), cwd=tmp_path) == {
        "status": "ok", "width": 2, "height": 3,
    }


@pytest.mark.asyncio
async def test_real_worker_round_trip_through_strict_parent_contract():
    from backend.tools.pdf_evidence import PdfIndexRequest, PdfPagesRequest, PdfRenderRequest, read_pdf_evidence

    body = pdf_bytes(("1. Solve x_1=2", "continuation"), rotation=90)
    index = await read_pdf_evidence(body, PdfIndexRequest(targets=["1"]))
    assert index.total_pages == 2 and index.pages[0].target_matches == ["1"]
    detail = await read_pdf_evidence(body, PdfPagesRequest(pages=[1, 2]))
    assert detail.pages[1].page_index == 1
    assert detail.pages[0].observation.native_char_count == len(detail.pages[0].native_text)
    rendered = await read_pdf_evidence(body, PdfRenderRequest(page_number=2, scale=1))
    assert (rendered.width, rendered.height) == (100, 200)
    exported = await read_pdf_evidence(body, PdfPagesRequest(operation="export_pages", pages=[2]))
    assert exported.page_numbers == [2] and exported.total_pages == 2
