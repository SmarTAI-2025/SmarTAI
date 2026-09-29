"""Bounded PDF evidence operations for the isolated worker, without app imports."""
from __future__ import annotations

import base64
import json
import math
import re
import sys
import unicodedata

import pymupdf as fitz

CONTRACT = "smartai.pdf.evidence"
MAX_INPUT_BYTES = 100 * 1024 * 1024
MAX_REQUEST_BYTES = 64 * 1024
MAX_PAGES = 10_000
MAX_PAGE_CHARACTERS = 500_000
MAX_STRUCTURE_ITEMS = 100_000
MAX_BLOCKS = 10_000
MAX_INDEX_BYTES = 512 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_RENDER_PIXELS = 16_000_000
MAX_RENDER_RGB_BYTES = 48 * 1024 * 1024
MAX_RENDER_SIDE = 8192
OPERATIONS = frozenset({"index", "detail", "render", "export_pages"})
ERROR_CODES = frozenset({
    "pdf_invalid_request", "pdf_input_too_large", "pdf_invalid", "pdf_encrypted",
    "pdf_page_limit_exceeded", "pdf_page_out_of_range", "pdf_character_limit_exceeded",
    "pdf_structure_limit_exceeded", "pdf_response_too_large", "pdf_render_limit_exceeded",
    "pdf_processing_failed",
})


class EvidenceFailure(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _base(operation: str | None, *, code: str | None = None) -> dict:
    result = {"contract": CONTRACT, "schema_version": 1, "status": "error" if code else "ok"}
    if isinstance(operation, str) and operation in OPERATIONS:
        result["operation"] = operation
    if code:
        result["code"] = code
    return result


def _integer(value: object, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise EvidenceFailure("pdf_invalid_request")
    return value


def _region(value: object) -> list[float]:
    if not isinstance(value, list) or len(value) != 4:
        raise EvidenceFailure("pdf_invalid_request")
    if any(type(item) not in {int, float} or not math.isfinite(item) or not 0 <= item <= 1 for item in value):
        raise EvidenceFailure("pdf_invalid_request")
    if value[0] >= value[2] or value[1] >= value[3]:
        raise EvidenceFailure("pdf_invalid_request")
    return [float(item) for item in value]


def _validate_request(request: object) -> dict:
    if (not isinstance(request, dict) or not isinstance(request.get("operation"), str)
            or request["operation"] not in OPERATIONS):
        raise EvidenceFailure("pdf_invalid_request")
    operation = request["operation"]
    allowed = {
        "index": {"operation", "start_page", "window_pages", "targets"},
        "detail": {"operation", "pages"},
        "render": {"operation", "page_number", "region", "scale"},
        "export_pages": {"operation", "pages"},
    }[operation]
    if set(request) - allowed:
        raise EvidenceFailure("pdf_invalid_request")
    result = dict(request)
    if operation == "index":
        result["start_page"] = _integer(request.get("start_page", 1), 1, MAX_PAGES)
        result["window_pages"] = _integer(request.get("window_pages", 500), 1, 500)
        targets = request.get("targets", [])
        if not isinstance(targets, list) or len(targets) > 64:
            raise EvidenceFailure("pdf_invalid_request")
        if any(not isinstance(target, str) or not 1 <= len(target) <= 80 or not target.strip()
               or any(char in target for char in "\x00\r\n") for target in targets):
            raise EvidenceFailure("pdf_invalid_request")
        if len(set(targets)) != len(targets):
            raise EvidenceFailure("pdf_invalid_request")
        result["targets"] = targets
    elif operation in {"detail", "export_pages"}:
        pages = request.get("pages")
        if not isinstance(pages, list) or not 1 <= len(pages) <= 24:
            raise EvidenceFailure("pdf_invalid_request")
        for number in pages:
            _integer(number, 1, MAX_PAGES)
        if pages != sorted(set(pages)):
            raise EvidenceFailure("pdf_invalid_request")
    else:
        result["page_number"] = _integer(request.get("page_number"), 1, MAX_PAGES)
        result["region"] = _region(request.get("region", [0, 0, 1, 1]))
        scale = request.get("scale", 2)
        if type(scale) not in {int, float} or not math.isfinite(scale) or not 0 < scale <= 2:
            raise EvidenceFailure("pdf_invalid_request")
        result["scale"] = float(scale)
    return result


def _json_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceFailure("pdf_invalid_request")
        result[key] = value
    return result


def _reject_constant(_value: str):
    raise EvidenceFailure("pdf_invalid_request")


def parse_request(raw: str) -> dict:
    try:
        if len(raw.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise EvidenceFailure("pdf_invalid_request")
        return _validate_request(json.loads(raw, object_pairs_hook=_json_object, parse_constant=_reject_constant))
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise EvidenceFailure("pdf_invalid_request") from None


def _geometry(page) -> tuple[fitz.Rect, dict]:
    rect = page.rect
    if (not all(math.isfinite(value) for value in rect) or rect.is_infinite
            or rect.width <= 0 or rect.height <= 0):
        raise EvidenceFailure("pdf_structure_limit_exceeded")
    return rect, {"page_number": page.number + 1, "width_points": rect.width,
                  "height_points": rect.height, "rotation": page.rotation}


def _normalized_box(page, box, visual: fitz.Rect) -> list[float] | None:
    if len(box) != 4 or not all(math.isfinite(value) for value in box):
        raise EvidenceFailure("pdf_structure_limit_exceeded")
    rect = fitz.Rect(box)
    if rect.is_infinite or rect.x1 < rect.x0 or rect.y1 < rect.y0:
        raise EvidenceFailure("pdf_structure_limit_exceeded")
    # Hairline vector paths still occupy visible pixels and need a nonempty region.
    if rect.width == 0:
        rect.x0 -= 0.25
        rect.x1 += 0.25
    if rect.height == 0:
        rect.y0 -= 0.25
        rect.y1 += 0.25
    rect = (rect * page.rotation_matrix) & visual
    if rect.is_empty:
        return None
    values = [(rect.x0 - visual.x0) / visual.width, (rect.y0 - visual.y0) / visual.height,
              (rect.x1 - visual.x0) / visual.width, (rect.y1 - visual.y0) / visual.height]
    return [max(0.0, min(1.0, (math.floor(value * 1_000_000) if i < 2 else math.ceil(value * 1_000_000)) / 1_000_000))
            for i, value in enumerate(values)]


def _target_pattern(target: str) -> re.Pattern:
    if re.fullmatch(r"[A-Za-z]*\d+(?:\.\d+)*", target):
        # A terminal punctuation dot is allowed, but another numbered component is not.
        return re.compile(r"(?<![A-Za-z0-9_.])" + re.escape(target) + r"(?![A-Za-z0-9_])(?!\.\d)")
    return re.compile(r"(?<!\w)" + re.escape(target) + r"(?!\w)")


def _page_evidence(page, targets: list[str], *, detail: bool) -> dict:
    visual, result = _geometry(page)
    # Do not decode/embed images in text dictionaries; image_info supplies geometry only.
    text_dict = page.get_text("dict", flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES, sort=False)
    raw_blocks = text_dict.get("blocks", [])
    if len(raw_blocks) > MAX_BLOCKS:
        raise EvidenceFailure("pdf_structure_limit_exceeded")
    blocks = []
    text_parts = []
    char_count = 0
    item_count = 0
    risks: set[str] = set()
    short_lines = line_count = 0
    text_boxes = []
    for block in raw_blocks:
        if block.get("type") != 0:
            continue
        lines = []
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            item_count += 1 + len(spans)
            if item_count > MAX_STRUCTURE_ITEMS:
                raise EvidenceFailure("pdf_structure_limit_exceeded")
            content = "".join(span.get("text", "") for span in spans)
            if content:
                lines.append(content)
                line_count += 1
                short_lines += len(content.strip()) < 4
            if tuple(line.get("dir", (1, 0))) != (1, 0):
                risks.add("layout")
            if any(span.get("flags", 0) & 1 or any(name in span.get("font", "").lower()
                   for name in ("math", "symbol", "cmsy", "cmmi")) for span in spans):
                risks.add("math")
        if not lines:
            continue
        text = "\n".join(lines) + "\n"
        start = char_count
        char_count += len(text)
        if char_count > MAX_PAGE_CHARACTERS:
            raise EvidenceFailure("pdf_character_limit_exceeded")
        region = _normalized_box(page, block["bbox"], visual)
        if region is None:
            raise EvidenceFailure("pdf_structure_limit_exceeded")
        text_parts.append(text)
        text_boxes.append(region)
        blocks.append({"order_index": len(blocks), "kind": "text", "region": region,
                       "text": text, "native_char_start": start, "native_char_end": char_count})
    native_text = "".join(text_parts)
    if any(unicodedata.category(char) == "Sm" or char in "_^" for char in native_text):
        risks.add("math")
    damaged = any(char == "\ufffd" or (unicodedata.category(char) == "Cc" and char not in "\n\t\r") for char in native_text)
    if damaged or (line_count >= 8 and short_lines * 2 > line_count):
        risks.add("damaged_text")
    # These are candidate layout risks, not a recovered reading-order assertion.
    for left, right in zip(text_boxes, text_boxes[1:]):
        if min(left[3], right[3]) > max(left[1], right[1]) and (left[2] < right[0] or right[2] < left[0]):
            risks.add("layout")

    images = page.get_image_info(hashes=False, xrefs=False)
    drawings = page.get_cdrawings()
    annotation_refs = page.annot_xrefs()
    if len(images) + len(drawings) + len(annotation_refs) + len(blocks) > MAX_BLOCKS:
        raise EvidenceFailure("pdf_structure_limit_exceeded")
    horizontal = vertical = 0
    for image in images:
        region = _normalized_box(page, image["bbox"], visual)
        if region is not None:
            risks.add("diagram")
            blocks.append({"order_index": len(blocks), "kind": "image", "region": region,
                           "text": "", "native_char_start": None, "native_char_end": None})
    for drawing in drawings:
        items = drawing.get("items", [])
        item_count += len(items)
        if item_count > MAX_STRUCTURE_ITEMS:
            raise EvidenceFailure("pdf_structure_limit_exceeded")
        region = _normalized_box(page, drawing["rect"], visual)
        if region is None:
            continue
        risks.add("diagram")
        for item in items:
            if item[0] == "re":
                horizontal += 2
                vertical += 2
            elif item[0] == "l":
                horizontal += abs(item[1][1] - item[2][1]) < 0.1
                vertical += abs(item[1][0] - item[2][0]) < 0.1
        blocks.append({"order_index": len(blocks), "kind": "drawing", "region": region,
                       "text": "", "native_char_start": None, "native_char_end": None})
    if horizontal >= 2 and vertical >= 2:
        risks.add("table")
    if annotation_refs:
        risks.add("layout")
        if any(item[1] == fitz.PDF_ANNOT_INK for item in annotation_refs):
            risks.add("handwriting")
    # Only an empty content-stream set without annotations is certified here.
    # An unrecognized/scanned/painted-white page is never inferred to be blank.
    verified_blank = not (native_text or images or drawings or annotation_refs or page.get_contents() or page.first_widget)
    quality = "missing" if not char_count else ("suspect" if damaged or "damaged_text" in risks or not native_text.strip() else "clean")
    result["observation"] = {"page_number": page.number + 1, "native_char_count": char_count,
                             "native_quality": quality, "verified_blank": verified_blank, "risks": sorted(risks)}
    result["target_matches"] = [target for target in targets if _target_pattern(target).search(native_text)]
    if detail:
        result.update(page_index=page.number, native_text=native_text, blocks=blocks)
    return result


def _render(page, request: dict, total_pages: int) -> dict:
    visual, _ = _geometry(page)
    region = request["region"]
    clip = fitz.Rect(visual.x0 + region[0] * visual.width, visual.y0 + region[1] * visual.height,
                     visual.x0 + region[2] * visual.width, visual.y0 + region[3] * visual.height)
    matrix = fitz.Matrix(request["scale"], request["scale"])
    bounds = clip * matrix
    width = math.ceil(bounds.x1) - math.floor(bounds.x0)
    height = math.ceil(bounds.y1) - math.floor(bounds.y0)
    if (min(width, height) < 1 or max(width, height) > MAX_RENDER_SIDE or width * height > MAX_RENDER_PIXELS
            or width * height * 3 > MAX_RENDER_RGB_BYTES):
        raise EvidenceFailure("pdf_render_limit_exceeded")
    # get_pixmap respects page rotation; clip is already in visible-page coordinates.
    pixmap = page.get_pixmap(matrix=matrix, colorspace=fitz.csRGB, alpha=False, clip=clip, annots=True)
    if (min(pixmap.width, pixmap.height) < 1 or max(pixmap.width, pixmap.height) > MAX_RENDER_SIDE
            or pixmap.width * pixmap.height > MAX_RENDER_PIXELS
            or pixmap.width * pixmap.height * pixmap.n > MAX_RENDER_RGB_BYTES):
        raise EvidenceFailure("pdf_render_limit_exceeded")
    png = pixmap.tobytes("png")
    if len(png) * 4 // 3 > MAX_RESPONSE_BYTES:
        raise EvidenceFailure("pdf_response_too_large")
    return {"total_pages": total_pages, "page_number": page.number + 1, "region": region,
            "width": pixmap.width, "height": pixmap.height, "content_type": "image/png",
            "payload_b64": base64.b64encode(png).decode("ascii")}


def _process(request: dict, body: bytes) -> dict:
    if len(body) > MAX_INPUT_BYTES:
        raise EvidenceFailure("pdf_input_too_large")
    fitz.TOOLS.mupdf_display_errors(False)
    fitz.TOOLS.mupdf_display_warnings(False)
    try:
        document = fitz.open(stream=body, filetype="pdf")
    except Exception:
        raise EvidenceFailure("pdf_invalid") from None
    with document:
        if document.needs_pass:
            raise EvidenceFailure("pdf_encrypted")
        if not 1 <= document.page_count <= MAX_PAGES:
            raise EvidenceFailure("pdf_page_limit_exceeded")
        total = document.page_count
        operation = request["operation"]
        if operation == "index":
            start = request["start_page"]
            if start > total:
                raise EvidenceFailure("pdf_page_out_of_range")
            end = min(total, start + request["window_pages"] - 1)
            return {"total_pages": total, "window_start": start, "window_end": end,
                    "complete_window": True,
                    "pages": [_page_evidence(document[number - 1], request["targets"], detail=False)
                              for number in range(start, end + 1)]}
        numbers = request.get("pages", [request.get("page_number")])
        if any(number > total for number in numbers):
            raise EvidenceFailure("pdf_page_out_of_range")
        if operation == "detail":
            return {"total_pages": total, "pages": [_page_evidence(document[number - 1], [], detail=True)
                                                   for number in numbers]}
        if operation == "render":
            return _render(document[numbers[0] - 1], request, total)
        with fitz.open() as selected:
            for number in numbers:
                selected.insert_pdf(document, from_page=number - 1, to_page=number - 1)
            output = selected.tobytes(garbage=3, deflate=True)
        if len(output) * 4 // 3 > MAX_RESPONSE_BYTES:
            raise EvidenceFailure("pdf_response_too_large")
        return {"total_pages": total, "page_numbers": numbers, "content_type": "application/pdf",
                "payload_b64": base64.b64encode(output).decode("ascii")}


def response_bytes(request: object, body: bytes) -> bytes:
    operation = request.get("operation") if isinstance(request, dict) else None
    if not isinstance(operation, str) or operation not in OPERATIONS:
        operation = None
    try:
        validated = _validate_request(request)
        payload = _base(operation) | _process(validated, body)
        serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        limit = MAX_INDEX_BYTES if operation == "index" else MAX_RESPONSE_BYTES
        if len(serialized) > limit:
            raise EvidenceFailure("pdf_response_too_large")
        return serialized
    except EvidenceFailure as exc:
        payload = _base(operation, code=exc.code)
    except (MemoryError, OverflowError, RecursionError):
        payload = _base(operation, code="pdf_structure_limit_exceeded")
    except Exception:
        payload = _base(operation, code="pdf_processing_failed")
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def run_cli(raw_request: str) -> int:
    try:
        request = parse_request(raw_request)
        response = response_bytes(request, sys.stdin.buffer.read(MAX_INPUT_BYTES + 1))
    except EvidenceFailure as exc:
        response = json.dumps(_base(None, code=exc.code), separators=(",", ":")).encode("utf-8")
    except Exception:
        response = json.dumps(_base(None, code="pdf_invalid_request"), separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(response)
    return 0
