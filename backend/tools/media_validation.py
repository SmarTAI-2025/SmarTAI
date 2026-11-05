"""Reusable, isolated validation of original image/PDF bytes before use."""
from __future__ import annotations

import asyncio
import json
import math
import sys

from fastapi import HTTPException

from backend.domain.errors import RecognitionError
from backend.services.submission_work_budget import current_budget, wait_for_admission
from backend.tools.image_evidence_worker import FORMATS, MAX_INPUT_BYTES as MAX_IMAGE_BYTES
from backend.tools.pdf_worker_lifecycle import reap_pdf_worker

MAX_PDF_BYTES = 100 * 1024 * 1024
MEDIA_ADMISSION_WAIT_SECONDS = 600.0
_INVALID_CODES = frozenset({
    "pdf_invalid", "pdf_encrypted", "image_invalid", "image_format_mismatch",
    "image_multiframe_unsupported", "image_mode_unsupported", "image_orientation_invalid",
})
_LIMIT_CODES = frozenset({
    "pdf_input_too_large", "pdf_page_limit_exceeded", "image_input_too_large",
    "image_pixel_limit_exceeded", "media_inspection_resource_limit",
})


async def validate_media_content(
    body: bytes,
    detected_content_type: str,
    *,
    max_pdf_pages: int | None = None,
    timeout_seconds: float = 10,
    wait_for_slot: bool = False,
    slot_timeout_seconds: float | None = 10,
) -> dict:
    """Verify actual decoding, not filename/MIME claims; never call a provider.

    Call only for a PDF/image candidate. Text and archives use their own
    decoders. The returned MIME is verified against the decoded media format.
    HTTP callers retain a short admission limit. Already-queued jobs may set
    slot_timeout_seconds=None for an independent, bounded 600-second queue wait.
    Owning-task cancellation/deadlines still apply. The decoder's timeout starts
    only after admission; waiting never launches or holds another worker's slot.
    """
    from backend.tools import file_processing
    from backend.tools.pdf_evidence import _exchange

    if detected_content_type not in {"application/pdf", *FORMATS}:
        raise HTTPException(415, detail={"code": "source_content_type_not_allowed"})
    if type(body) is not bytes or not body:
        raise HTTPException(400, detail={"code": "source_empty"})
    image = detected_content_type in FORMATS
    if len(body) > (MAX_IMAGE_BYTES if image else MAX_PDF_BYTES):
        raise HTTPException(413, detail={"code": "image_input_too_large" if image else "pdf_input_too_large"})
    if (type(timeout_seconds) not in {float, int} or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 30 or
            (max_pdf_pages is not None and (type(max_pdf_pages) is not int or max_pdf_pages < 1))
            or type(wait_for_slot) is not bool
            or (slot_timeout_seconds is not None and (
                type(slot_timeout_seconds) not in {float, int}
                or not math.isfinite(slot_timeout_seconds) or slot_timeout_seconds <= 0))):
        raise ValueError("Invalid media validation limits")
    if file_processing.fitz is None:
        raise HTTPException(503, detail={"code": "media_inspection_unavailable"})
    budget = current_budget()
    if budget is not None:
        budget.check()
    slots = file_processing._PDF_EXTRACTION_SLOTS
    loop = asyncio.get_running_loop()
    deadline = loop.time() + (slot_timeout_seconds if slot_timeout_seconds is not None
                              else MEDIA_ADMISSION_WAIT_SECONDS)
    while True:
        if budget is not None:
            budget.check()
        if wait_for_slot and loop.time() >= deadline:
            raise HTTPException(429, detail={"code": "media_inspection_busy"})
        if slots.acquire(blocking=False):
            break
        if not wait_for_slot:
            raise HTTPException(429, detail={"code": "media_inspection_busy"})
        # No slot is held while waiting, so cancellation needs no cleanup.
        await wait_for_admission(asyncio.sleep(min(0.02, max(0, deadline - loop.time()))))
    launch_task = None
    failure: BaseException | None = None
    work_started = False
    try:
        if budget is not None:
            budget.start()
            work_started = True
        async with asyncio.timeout(timeout_seconds):
            launch_task = asyncio.create_task(asyncio.create_subprocess_exec(
                sys.executable, str(file_processing._PDF_WORKER_PATH), "inspect-upload",
                detected_content_type, str(max_pdf_pages or 0),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            ))
            process = await asyncio.shield(launch_task)
            output = await _exchange(process, body, 4096)
            try:
                payload = json.loads(output.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise HTTPException(400, detail={"code": "media_inspection_failed"}) from None
            if not isinstance(payload, dict):
                raise HTTPException(400, detail={"code": "media_inspection_failed"})
            if payload.get("status") != "ok":
                code = payload.get("code")
                if code in _INVALID_CODES:
                    raise HTTPException(400, detail={"code": code})
                if code in _LIMIT_CODES:
                    raise HTTPException(413, detail={"code": code})
                raise HTTPException(400, detail={"code": "media_inspection_failed"})
            dimensions = ("width", "height") if image else ("page_count",)
            if (payload.get("content_type") != detected_content_type
                    or any(type(payload.get(key)) is not int or payload[key] < 1 for key in dimensions)):
                raise HTTPException(400, detail={"code": "media_inspection_failed"})
            return {key: payload[key] for key in ("content_type", *dimensions)}
    except TimeoutError:
        failure = HTTPException(408, detail={"code": "media_inspection_timeout"})
        raise failure from None
    except (HTTPException, RecognitionError, asyncio.CancelledError) as exc:
        failure = exc
        raise
    except Exception:
        failure = HTTPException(400, detail={"code": "media_inspection_failed"})
        raise failure from None
    finally:
        try:
            # Cleanup owns the permit even if its bounded wait returns early.
            # Unreaped workers must not create capacity for more processes.
            await reap_pdf_worker(launch_task, release_slot=slots.release)
        except asyncio.CancelledError:
            raise
        except Exception:
            task = asyncio.current_task()
            if isinstance(failure, asyncio.CancelledError) or (task is not None and task.cancelling()):
                raise asyncio.CancelledError from None
            if failure is None:
                raise HTTPException(400, detail={"code": "media_inspection_failed"}) from None
        finally:
            if work_started:
                budget.finish()
