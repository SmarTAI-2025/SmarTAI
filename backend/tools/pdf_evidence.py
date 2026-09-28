"""Bounded async client for the owned, killable PDF evidence worker.

Callers supply bytes from an already-authorized source. No arbitrary paths,
providers or storage lookups are accepted here. Existing and new PDF operations
share the same process slots and cancellation-safe reap implementation.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import math
import struct
import sys
from typing import Annotated, Literal, TYPE_CHECKING

from pydantic import Field, TypeAdapter, ValidationError, model_validator

from backend.domain.errors import PDF_EVIDENCE_STATUS_CODES, PdfEvidenceError
from backend.recognition.models import EvidenceModel, NormalizedRegionV1
from backend.recognition.planner import PageObservationV1
from backend.tools import file_processing
from backend.tools.pdf_worker_lifecycle import reap_pdf_worker

if TYPE_CHECKING:
    from backend.progress.tracker import ProgressReporter

MAX_INPUT_BYTES = 100 * 1024 * 1024
MAX_IMAGE_INPUT_BYTES = 10 * 1024 * 1024
INDEX_RESPONSE_BYTES = 512 * 1024
DETAIL_RESPONSE_BYTES = 8 * 1024 * 1024
Region = tuple[float, float, float, float]
PageNumber = Annotated[int, Field(strict=True, ge=1, le=10000)]


class PdfIndexRequest(EvidenceModel):
    operation: Literal["index"] = "index"
    start_page: PageNumber = 1
    window_pages: int = Field(default=500, strict=True, ge=1, le=500)
    targets: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def valid_targets(self):
        if len(self.targets) != len(set(self.targets)) or any(
            not target.strip() or any(character in target for character in "\r\n\x00") for target in self.targets
        ):
            raise ValueError("targets must be distinct nonempty single-line identifiers")
        return self


class PdfPagesRequest(EvidenceModel):
    operation: Literal["detail", "export_pages"] = "detail"
    pages: list[PageNumber] = Field(min_length=1, max_length=24)

    @model_validator(mode="after")
    def ordered_pages(self):
        if self.pages != sorted(set(self.pages)):
            raise ValueError("pages must be unique and ascending")
        return self


class PdfRenderRequest(EvidenceModel):
    operation: Literal["render"] = "render"
    page_number: PageNumber
    region: Region = (0, 0, 1, 1)
    scale: float = Field(default=2, gt=0, le=2)

    @model_validator(mode="after")
    def valid_region(self):
        NormalizedRegionV1(**dict(zip(("x0", "y0", "x1", "y1"), self.region)))
        return self


class PdfContactSheetRequest(EvidenceModel):
    operation: Literal["contact_sheet"] = "contact_sheet"
    pages: list[PageNumber] = Field(min_length=1, max_length=8)
    tile_long_edge: int = Field(default=768, strict=True, ge=64, le=1024)

    @model_validator(mode="after")
    def ordered_pages(self):
        if self.pages != sorted(set(self.pages)):
            raise ValueError("contact sheet pages must be unique and ascending")
        return self


class ImagePrepareRequest(EvidenceModel):
    operation: Literal["image_prepare"] = "image_prepare"
    content_type: Literal["image/jpeg", "image/png", "image/webp"]
    region: Region = (0, 0, 1, 1)

    @model_validator(mode="after")
    def valid_region(self):
        NormalizedRegionV1(**dict(zip(("x0", "y0", "x1", "y1"), self.region)))
        return self


PdfRequest = PdfIndexRequest | PdfPagesRequest | PdfRenderRequest | PdfContactSheetRequest


class PdfBlock(EvidenceModel):
    order_index: int = Field(ge=0)
    kind: Literal["text", "image", "drawing"]
    region: Region
    text: str = Field(max_length=500_000)
    native_char_start: int | None = Field(default=None, ge=0)
    native_char_end: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def block_identity(self):
        NormalizedRegionV1(**dict(zip(("x0", "y0", "x1", "y1"), self.region)))
        if self.kind == "text":
            if self.native_char_start is None or self.native_char_end is None or self.native_char_end < self.native_char_start:
                raise ValueError("text blocks require ordered offsets")
        elif self.text or self.native_char_start is not None or self.native_char_end is not None:
            raise ValueError("visual blocks cannot invent native text")
        return self


class PdfIndexPage(EvidenceModel):
    page_number: PageNumber
    width_points: float = Field(gt=0)
    height_points: float = Field(gt=0)
    rotation: Literal[0, 90, 180, 270]
    observation: PageObservationV1
    target_matches: list[str] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def observation_identity(self):
        if self.observation.page_number != self.page_number:
            raise ValueError("observation page mismatch")
        return self


class PdfDetailPage(PdfIndexPage):
    page_index: int = Field(ge=0, le=9999)
    native_text: str = Field(max_length=500_000)
    blocks: list[PdfBlock] = Field(max_length=10000)

    @model_validator(mode="after")
    def detail_identity(self):
        if self.page_index + 1 != self.page_number:
            raise ValueError("page index mismatch")
        if self.observation.native_char_count != len(self.native_text):
            raise ValueError("native character count mismatch")
        if [block.order_index for block in self.blocks] != sorted({block.order_index for block in self.blocks}):
            raise ValueError("block order mismatch")
        previous_end = 0
        for block in self.blocks:
            if block.kind == "text":
                if block.native_char_start != previous_end or block.native_char_end > len(self.native_text):
                    raise ValueError("native intervals must cover the page without gaps or overlaps")
                if self.native_text[block.native_char_start:block.native_char_end] != block.text:
                    raise ValueError("native block does not match its interval")
                previous_end = block.native_char_end
        if previous_end != len(self.native_text):
            raise ValueError("text blocks must cover all native text")
        return self


class PdfEnvelope(EvidenceModel):
    contract: Literal["smartai.pdf.evidence"]
    schema_version: Literal[1]
    status: Literal["ok"]
    total_pages: int = Field(ge=1, le=10000)


class PdfIndexResult(PdfEnvelope):
    operation: Literal["index"]
    window_start: PageNumber
    window_end: PageNumber
    complete_window: Literal[True]
    pages: list[PdfIndexPage] = Field(min_length=1, max_length=500)


class PdfDetailResult(PdfEnvelope):
    operation: Literal["detail"]
    pages: list[PdfDetailPage] = Field(min_length=1, max_length=24)


class PdfRenderResult(PdfEnvelope):
    operation: Literal["render"]
    page_number: PageNumber
    region: Region
    width: int = Field(strict=True, ge=1, le=8192)
    height: int = Field(strict=True, ge=1, le=8192)
    content_type: Literal["image/png"]
    payload_b64: str = Field(min_length=1, max_length=DETAIL_RESPONSE_BYTES, repr=False)


class PdfExportResult(PdfEnvelope):
    operation: Literal["export_pages"]
    page_numbers: list[PageNumber] = Field(min_length=1, max_length=24)
    content_type: Literal["application/pdf"]
    payload_b64: str = Field(min_length=1, max_length=DETAIL_RESPONSE_BYTES, repr=False)


Pixel = Annotated[int, Field(strict=True, ge=0, le=8192)]
PixelBox = tuple[Pixel, Pixel, Pixel, Pixel]


class PdfContactTile(EvidenceModel):
    tile_id: int = Field(strict=True, ge=1, le=8)
    page_number: PageNumber
    width_points: float = Field(gt=0)
    height_points: float = Field(gt=0)
    rotation: Literal[0, 90, 180, 270]
    tile_bbox_pixels: PixelBox
    page_bbox_pixels: PixelBox
    page_region: Region
    render_width: int = Field(strict=True, ge=1, le=1024)
    render_height: int = Field(strict=True, ge=1, le=1024)


class PdfContactSheetResult(PdfEnvelope):
    operation: Literal["contact_sheet"]
    page_numbers: list[PageNumber] = Field(min_length=1, max_length=8)
    tile_long_edge: int = Field(strict=True, ge=64, le=1024)
    tiles: list[PdfContactTile] = Field(min_length=1, max_length=8)
    width: int = Field(strict=True, ge=1, le=8192)
    height: int = Field(strict=True, ge=1, le=8192)
    content_type: Literal["image/png"]
    payload_b64: str = Field(min_length=1, max_length=DETAIL_RESPONSE_BYTES, repr=False)

    @model_validator(mode="after")
    def exact_tile_mapping(self):
        edge, columns = self.tile_long_edge, min(2, len(self.page_numbers))
        rows = math.ceil(len(self.page_numbers) / columns)
        if (self.width, self.height) != (columns * edge + (columns + 1) * 8,
                                        rows * (edge + 24) + (rows + 1) * 8):
            raise ValueError("contact sheet grid size mismatch")
        if (self.page_numbers != sorted(set(self.page_numbers)) or max(self.page_numbers) > self.total_pages
                or [tile.page_number for tile in self.tiles] != self.page_numbers):
            raise ValueError("contact sheet page mapping mismatch")
        for index, tile in enumerate(self.tiles):
            left, top = 8 + index % columns * (edge + 8), 8 + index // columns * (edge + 32)
            if tile.tile_id != index + 1 or tile.tile_bbox_pixels != (left, top, left + edge, top + edge + 24):
                raise ValueError("contact sheet tile mismatch")
            if max(tile.render_width, tile.render_height) > edge:
                raise ValueError("contact sheet image exceeds tile")
            image_left = left + (edge - tile.render_width) // 2
            image_top = top + 24 + (edge - tile.render_height) // 2
            if tile.page_bbox_pixels != (image_left, image_top, image_left + tile.render_width, image_top + tile.render_height):
                raise ValueError("contact sheet page image cannot include labels or padding")
            expected = tuple(value / (self.width if i % 2 == 0 else self.height)
                             for i, value in enumerate(tile.page_bbox_pixels))
            if any(abs(a - b) > 1e-9 for a, b in zip(tile.page_region, expected)):
                raise ValueError("contact sheet normalized geometry mismatch")
        return self


class ImagePreparedMetadata(EvidenceModel):
    """Persistable preparation provenance, without the ephemeral PNG payload."""
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_content_type: Literal["image/jpeg", "image/png", "image/webp"]
    source_mode: Literal["1", "L", "LA", "P", "RGB", "RGBA", "CMYK"]
    source_width: int = Field(strict=True, ge=1, le=8192)
    source_height: int = Field(strict=True, ge=1, le=8192)
    exif_orientation: int = Field(strict=True, ge=1, le=8)
    source_to_oriented_matrix: tuple[tuple[int, int, int], tuple[int, int, int]]
    oriented_width: int = Field(strict=True, ge=1, le=8192)
    oriented_height: int = Field(strict=True, ge=1, le=8192)
    region: Region
    effective_region: Region
    crop_box_pixels: PixelBox
    width: int = Field(strict=True, ge=1, le=8192)
    height: int = Field(strict=True, ge=1, le=8192)
    content_type: Literal["image/png"]
    alpha_background: Literal["white"]
    resampled: Literal[False]
    metadata_stripped: Literal[True]

    @model_validator(mode="after")
    def exact_transforms(self):
        NormalizedRegionV1(**dict(zip(("x0", "y0", "x1", "y1"), self.region)))
        raw_w, raw_h, orientation = self.source_width, self.source_height, self.exif_orientation
        if raw_w * raw_h > 16_000_000:
            raise ValueError("image source pixel budget exceeded")
        expected_size = (raw_h, raw_w) if orientation >= 5 else (raw_w, raw_h)
        if (self.oriented_width, self.oriented_height) != expected_size:
            raise ValueError("image orientation dimensions mismatch")
        matrices = {
            1: ((1, 0, 0), (0, 1, 0)), 2: ((-1, 0, raw_w), (0, 1, 0)),
            3: ((-1, 0, raw_w), (0, -1, raw_h)), 4: ((1, 0, 0), (0, -1, raw_h)),
            5: ((0, 1, 0), (1, 0, 0)), 6: ((0, -1, raw_h), (1, 0, 0)),
            7: ((0, -1, raw_h), (-1, 0, raw_w)), 8: ((0, 1, 0), (-1, 0, raw_w)),
        }
        if self.source_to_oriented_matrix != matrices[orientation]:
            raise ValueError("image orientation transform mismatch")
        width, height = expected_size
        box = (math.floor(self.region[0] * width), math.floor(self.region[1] * height),
               math.ceil(self.region[2] * width), math.ceil(self.region[3] * height))
        if self.crop_box_pixels != box or (self.width, self.height) != (box[2] - box[0], box[3] - box[1]):
            raise ValueError("image crop geometry mismatch")
        effective = (box[0] / width, box[1] / height, box[2] / width, box[3] / height)
        if any(abs(a - b) > 1e-9 for a, b in zip(effective, self.effective_region)):
            raise ValueError("image effective region mismatch")
        return self


class ImagePreparedResult(ImagePreparedMetadata):
    contract: Literal["smartai.image.evidence"]
    schema_version: Literal[1]
    status: Literal["ok"]
    operation: Literal["image_prepare"]
    payload_b64: str = Field(min_length=1, max_length=DETAIL_RESPONSE_BYTES, repr=False)


PdfResult = PdfIndexResult | PdfDetailResult | PdfRenderResult | PdfExportResult | PdfContactSheetResult
_RESULT = TypeAdapter(Annotated[PdfResult | ImagePreparedResult, Field(discriminator="operation")])


def decode_pdf_payload(result: PdfRenderResult | PdfExportResult | PdfContactSheetResult | ImagePreparedResult) -> bytes:
    try:
        payload = base64.b64decode(result.payload_b64, validate=True)
    except (binascii.Error, ValueError):
        raise PdfEvidenceError("pdf_evidence_protocol_invalid") from None
    signature = b"\x89PNG\r\n\x1a\n" if result.content_type == "image/png" else b"%PDF-"
    if not payload.startswith(signature):
        raise PdfEvidenceError("pdf_evidence_protocol_invalid")
    if result.content_type == "image/png":
        if len(payload) < 33 or payload[12:16] != b"IHDR" or struct.unpack(">II", payload[16:24]) != (result.width, result.height):
            raise PdfEvidenceError("pdf_evidence_protocol_invalid")
    return payload


def decode_image_payload(result: ImagePreparedResult) -> bytes:
    return decode_pdf_payload(result)


def _validate_result(payload: object, request: PdfRequest | ImagePrepareRequest) -> PdfResult | ImagePreparedResult:
    if not isinstance(payload, dict):
        raise PdfEvidenceError("pdf_evidence_protocol_invalid")
    contract = "smartai.image.evidence" if isinstance(request, ImagePrepareRequest) else "smartai.pdf.evidence"
    if payload.get("contract") != contract or type(payload.get("schema_version")) is not int or payload.get("schema_version") != 1:
        raise PdfEvidenceError("pdf_evidence_protocol_invalid")
    if payload.get("status") == "error":
        code = payload.get("code")
        if not isinstance(code, str) or code not in PDF_EVIDENCE_STATUS_CODES:
            code = "pdf_evidence_protocol_invalid"
        raise PdfEvidenceError(code)
    try:
        result = _RESULT.validate_python(payload)
    except ValidationError:
        raise PdfEvidenceError("pdf_evidence_protocol_invalid") from None
    if result.operation != request.operation:
        raise PdfEvidenceError("pdf_evidence_protocol_invalid")
    valid = False
    if isinstance(result, PdfIndexResult) and isinstance(request, PdfIndexRequest):
        end = min(result.total_pages, request.start_page + request.window_pages - 1)
        valid = (
            result.window_start == request.start_page and result.window_end == end
            and [page.page_number for page in result.pages] == list(range(request.start_page, end + 1))
            and all(set(page.target_matches).issubset(request.targets) for page in result.pages)
        )
    elif isinstance(result, PdfDetailResult) and isinstance(request, PdfPagesRequest):
        valid = [page.page_number for page in result.pages] == request.pages and max(request.pages) <= result.total_pages
    elif isinstance(result, PdfExportResult) and isinstance(request, PdfPagesRequest):
        valid = result.page_numbers == request.pages and max(request.pages) <= result.total_pages
        decode_pdf_payload(result)
    elif isinstance(result, PdfRenderResult) and isinstance(request, PdfRenderRequest):
        valid = (
            result.page_number == request.page_number <= result.total_pages
            and all(abs(a - b) <= 0.000001 for a, b in zip(result.region, request.region))
            and result.width * result.height <= 16_000_000
        )
        decode_pdf_payload(result)
    elif isinstance(result, PdfContactSheetResult) and isinstance(request, PdfContactSheetRequest):
        valid = result.page_numbers == request.pages and result.tile_long_edge == request.tile_long_edge
        decode_pdf_payload(result)
    elif isinstance(result, ImagePreparedResult) and isinstance(request, ImagePrepareRequest):
        valid = result.source_content_type == request.content_type and result.region == request.region
        decode_image_payload(result)
    if not valid:
        raise PdfEvidenceError("pdf_evidence_protocol_invalid")
    return result


async def _exchange(process, pdf_bytes: bytes, limit: int) -> bytes:
    async def write_input():
        try:
            view = memoryview(pdf_bytes)
            for start in range(0, len(view), 65536):
                process.stdin.write(view[start:start + 65536])
                await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            process.stdin.close()

    async def read_output():
        parts, count = [], 0
        while True:
            part = await process.stdout.read(65536)
            if not part:
                break
            count += len(part)
            if count > limit:
                raise PdfEvidenceError("pdf_response_too_large")
            parts.append(part)
        return b"".join(parts)

    writer = asyncio.create_task(write_input())
    reader = asyncio.create_task(read_output())
    try:
        _, output = await asyncio.gather(writer, reader)
        await process.wait()
        if process.returncode != 0:
            raise PdfEvidenceError("pdf_processing_failed")
        return output
    finally:
        for task in (writer, reader):
            if not task.done():
                task.cancel()
        cleanup = asyncio.gather(writer, reader, return_exceptions=True)
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
        if cancelled:
            raise asyncio.CancelledError


async def read_pdf_evidence(
    pdf_bytes: bytes,
    request: PdfRequest,
    *,
    timeout_seconds: float = 10,
    progress: ProgressReporter | None = None,
) -> PdfResult:
    """Inspect a bounded range without parsing PDFs in the parent process."""
    if not isinstance(request, (PdfIndexRequest, PdfPagesRequest, PdfRenderRequest, PdfContactSheetRequest)):
        raise PdfEvidenceError("pdf_invalid_request")
    return await _read_evidence(pdf_bytes, request, timeout_seconds=timeout_seconds, progress=progress)


async def read_image_evidence(
    image_bytes: bytes,
    request: ImagePrepareRequest,
    *,
    timeout_seconds: float = 10,
    progress: ProgressReporter | None = None,
) -> ImagePreparedResult:
    """Prepare authorized image bytes in the shared isolated worker, not the parent."""
    if not isinstance(request, ImagePrepareRequest):
        raise PdfEvidenceError("pdf_invalid_request")
    return await _read_evidence(image_bytes, request, timeout_seconds=timeout_seconds, progress=progress)


async def _read_evidence(
    pdf_bytes: bytes,
    request: PdfRequest | ImagePrepareRequest,
    *,
    timeout_seconds: float,
    progress: ProgressReporter | None,
) -> PdfResult | ImagePreparedResult:
    image = isinstance(request, ImagePrepareRequest)
    if image:
        if not isinstance(pdf_bytes, bytes) or not pdf_bytes:
            raise PdfEvidenceError("image_invalid")
        if len(pdf_bytes) > MAX_IMAGE_INPUT_BYTES:
            raise PdfEvidenceError("image_input_too_large")
    elif not isinstance(pdf_bytes, bytes) or not pdf_bytes or b"%PDF-" not in pdf_bytes[:1024]:
        raise PdfEvidenceError("pdf_invalid")
    if len(pdf_bytes) > MAX_INPUT_BYTES:
        raise PdfEvidenceError("pdf_input_too_large")
    if type(timeout_seconds) not in {float, int} or not 0 < timeout_seconds <= 30:
        raise PdfEvidenceError("pdf_invalid_request")
    # Revalidate snapshots, including callers that changed a mutable model.
    try:
        request = type(request).model_validate(request.model_dump(warnings=False))
    except ValidationError:
        raise PdfEvidenceError("pdf_invalid_request") from None
    if file_processing.fitz is None:
        raise PdfEvidenceError("pdf_processing_unavailable")
    slots = file_processing._PDF_EXTRACTION_SLOTS
    if not slots.acquire(blocking=False):
        raise PdfEvidenceError("pdf_evidence_busy")
    launch_task = None
    failure: BaseException | None = None
    try:
        async with asyncio.timeout(timeout_seconds):
            if progress:
                await progress.set_current_step("image_prepare" if image else "pdf_" + request.operation,
                                                message="Preparing image evidence" if image else "Reading PDF page evidence")
            launch_task = asyncio.create_task(asyncio.create_subprocess_exec(
                sys.executable, str(file_processing._PDF_WORKER_PATH), "evidence-v1",
                request.model_dump_json(), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            ))
            process = await asyncio.shield(launch_task)
            limit = INDEX_RESPONSE_BYTES if request.operation == "index" else DETAIL_RESPONSE_BYTES
            output = await _exchange(process, pdf_bytes, limit)
            try:
                payload = json.loads(output.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise PdfEvidenceError("pdf_evidence_protocol_invalid") from None
            result = _validate_result(payload, request)
            if isinstance(result, ImagePreparedResult) and result.source_sha256 != hashlib.sha256(pdf_bytes).hexdigest():
                raise PdfEvidenceError("pdf_evidence_protocol_invalid")
            if progress:
                count = len(result.pages) if hasattr(result, "pages") else (
                    len(result.page_numbers) if isinstance(result, (PdfExportResult, PdfContactSheetResult)) else 1
                )
                await progress.increment_stage_metrics(**({"images_prepared": 1} if image else {"pdf_pages_observed": count}))
            return result
    except TimeoutError:
        failure = PdfEvidenceError("pdf_evidence_timeout")
        raise failure from None
    except (PdfEvidenceError, asyncio.CancelledError) as exc:
        failure = exc
        raise
    except Exception:
        failure = PdfEvidenceError("pdf_processing_failed")
        raise failure from None
    finally:
        try:
            await reap_pdf_worker(launch_task)
        except asyncio.CancelledError:
            raise
        except Exception:
            task = asyncio.current_task()
            if isinstance(failure, asyncio.CancelledError) or (task is not None and task.cancelling()):
                raise asyncio.CancelledError from None
            if failure is None:
                raise PdfEvidenceError("pdf_processing_failed") from None
        finally:
            slots.release()
