"""Read one authorized image source without inventing PDF/native evidence."""
from __future__ import annotations

import hashlib
import time
from typing import TYPE_CHECKING

from backend.domain.errors import PdfEvidenceError, RecognitionError
from backend.recognition.budget import RecognitionBudget
from backend.recognition.engine import EngineReadInputV1, RecognitionEngine
from backend.recognition.executor import (
    ImageDetailPageV1, ReadUnitV1, RecognitionReadBatchV1, _snapshot, build_read_batch,
)
from backend.recognition.models import NormalizedRegionV1, RecognitionSourceRefV1
from backend.recognition.planner import RecognitionPlanV1
from backend.recognition.runtime import RecognitionCapacity, region_budget_key, run_initial_read
from backend.tools.pdf_evidence import (
    ImagePreparedMetadata, ImagePrepareRequest, decode_image_payload, read_image_evidence,
)

if TYPE_CHECKING:
    from backend.progress.tracker import ProgressReporter


async def read_image_plan(
    image_bytes: bytes,
    *,
    authorized_owner_id: str,
    source: RecognitionSourceRefV1,
    plan: RecognitionPlanV1,
    engine: RecognitionEngine | None,
    capacity: RecognitionCapacity,
    progress: ProgressReporter | None = None,
    budget: RecognitionBudget | None = None,
) -> RecognitionReadBatchV1:
    source, plan = _snapshot(source, plan, engine)
    if source.owner_id != authorized_owner_id or source.content_type not in {"image/png", "image/jpeg", "image/webp"} or not isinstance(image_bytes, bytes) or hashlib.sha256(image_bytes).hexdigest() != source.input_sha256:
        raise RecognitionError("recognition_source_mismatch")
    if plan.source_kind != "image" or plan.total_pages != 1 or plan.requested_pages != [1]:
        raise RecognitionError("recognition_plan_changed")
    if any(page.action in {"native", "blank"} or page.input_mode == "document" for page in plan.decisions):
        raise RecognitionError("recognition_plan_changed")
    budget = budget or RecognitionBudget(source, plan.policy, plan.engine_capabilities)
    budget.assert_context(source, plan.policy, plan.engine_capabilities)
    started = time.monotonic()
    pages, units, stops, failed = [], [], [], set()
    try:
        for decision in plan.decisions:
            if decision.action != "visual":
                continue
            if engine is None or plan.engine_capabilities is None:
                raise RecognitionError("recognition_route_changed")
            for region in decision.regions:
                budget.remaining("read")
                if len(units) >= plan.initial_calls:
                    raise RecognitionError("recognition_budget_exhausted")
                if engine.capabilities != plan.engine_capabilities:
                    raise RecognitionError("recognition_route_changed")
                token_limit = budget.output_limit(4096)
                prepared = await read_image_evidence(
                    image_bytes, ImagePrepareRequest(content_type=source.content_type, region=region.as_tuple()),
                    timeout_seconds=min(10, budget.remaining("read")), progress=progress,
                )
                metadata = ImagePreparedMetadata.model_validate({
                    name: getattr(prepared, name) for name in ImagePreparedMetadata.model_fields
                })
                if not pages:
                    pages.append(ImageDetailPageV1(preparation=metadata))
                payload = decode_image_payload(prepared)
                effective_region = NormalizedRegionV1(**dict(zip(("x0", "y0", "x1", "y1"), prepared.effective_region)))
                request = EngineReadInputV1(
                    purpose=plan.purpose, input_mode="page_image", page_number=1,
                    region=effective_region, content_type="image/png", payload=payload,
                    max_output_tokens=token_limit,
                )
                if progress:
                    await progress.set_current_step("recognition_read", message="Reading source evidence")
                outcome = await run_initial_read(
                    engine, request, source=source, policy=plan.policy, capabilities=plan.engine_capabilities,
                    region_keys=(region_budget_key(1, region),), budget=budget, capacity=capacity, progress=progress,
                )
                candidate = outcome.candidate
                units.append(ReadUnitV1(
                    unit_id=f"u{len(units):04d}", page_numbers=[1], region=region, input_mode="page_image",
                    payload_sha256=hashlib.sha256(payload).hexdigest(), payload_bytes=len(payload),
                    candidate=candidate, requested_output_tokens=request.max_output_tokens,
                    submission_may_exist=outcome.submission_may_exist,
                    output_mapping="single_region", image_preparation=metadata,
                ))
                if candidate.status == "error":
                    failed.add(1)
                    stops.append(candidate.safe_error_code or "recognition_response_invalid")
                    break
                if candidate.finish_reason == "refused" or "provider_refused" in candidate.warning_codes:
                    failed.add(1)
                    stops.append("provider_request_rejected")
                    break
    except (RecognitionError, PdfEvidenceError) as exc:
        stops.append(exc.code)
    return build_read_batch(source=source, plan=plan, pages=pages, units=units,
                            native_pages=[], failed=failed, stops=stops, started=started)
