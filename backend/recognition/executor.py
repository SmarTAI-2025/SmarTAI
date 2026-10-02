"""Execute a frozen plan into raw candidates, without fusion or automatic repair.

The application authorizes source access and durable job ownership before this
boundary. It must share one capacity instance across target and knowledge jobs.
Page coverage here means evidence read, not a claim of correct transcription.
"""
from __future__ import annotations

import hashlib
import time
from typing import Literal, TYPE_CHECKING

from pydantic import Field, ValidationError, model_validator

from backend.domain.errors import PdfEvidenceError, RecognitionError
from backend.recognition.budget import RecognitionBudget
from backend.recognition.engine import EngineReadInputV1, RecognitionEngine
from backend.recognition.models import (
    Code, EvidenceModel, NormalizedRegionV1, RecognitionCandidateV1,
    RecognitionSourceRefV1, RecognitionUsageV1,
)
from backend.recognition.planner import PageObservationV1, RecognitionPlanV1
from backend.recognition.runtime import RecognitionCapacity, region_budget_key, run_initial_read
from backend.tools.pdf_evidence import (
    ImagePreparedMetadata,
    PdfDetailPage, PdfDetailResult, PdfExportResult, PdfPagesRequest,
    PdfRenderRequest, PdfRenderResult, decode_pdf_payload, read_pdf_evidence,
)

if TYPE_CHECKING:
    from backend.progress.tracker import ProgressReporter
    from backend.services.recognition_local_evidence import RecognitionLocalEvidenceReader


class ImageDetailPageV1(EvidenceModel):
    """An image is one pixel-space page, never a fabricated physical PDF page."""

    page_number: Literal[1] = 1
    page_index: Literal[0] = 0
    geometry_unit: Literal["pixels"] = "pixels"
    native_text: Literal[""] = ""
    preparation: ImagePreparedMetadata
    observation: PageObservationV1 = Field(default_factory=lambda: PageObservationV1(page_number=1))

    @model_validator(mode="after")
    def image_has_no_native_or_blank_proof(self):
        if self.observation != PageObservationV1(page_number=1):
            raise ValueError("decoded pixels do not prove native text or blankness")
        return self

    @property
    def width_pixels(self) -> int:
        return self.preparation.oriented_width

    @property
    def height_pixels(self) -> int:
        return self.preparation.oriented_height


class ReadUnitV1(EvidenceModel):
    unit_id: str = Field(pattern=r"^u\d{4}$")
    page_numbers: list[int] = Field(min_length=1, max_length=24)
    region: NormalizedRegionV1
    input_mode: Literal["page_image", "document"]
    payload_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    payload_bytes: int = Field(ge=1, le=10 * 1024 * 1024)
    candidate: RecognitionCandidateV1
    requested_output_tokens: int = Field(strict=True, ge=1, le=32768)
    submission_may_exist: bool = False
    # Multiple submitted pages do not provide output-to-page correspondence.
    output_mapping: Literal["single_region", "document_only"]
    image_preparation: ImagePreparedMetadata | None = None

    @model_validator(mode="after")
    def valid_mapping(self):
        if self.page_numbers != sorted(set(self.page_numbers)) or any(not 1 <= p <= 10000 for p in self.page_numbers):
            raise ValueError("invalid unit page mapping")
        if len(self.page_numbers) > 1 and (self.input_mode != "document" or self.output_mapping != "document_only"):
            raise ValueError("multi-page candidates cannot claim page alignment")
        if self.input_mode == "document" and self.region != NormalizedRegionV1():
            raise ValueError("document units require the full page")
        if self.image_preparation is not None and (
            self.input_mode != "page_image" or self.page_numbers != [1]
            or self.region.as_tuple() != self.image_preparation.region
        ):
            raise ValueError("image input must preserve its planned source region")
        return self


class PendingReadRegionV1(EvidenceModel):
    page_number: int = Field(ge=1, le=10000)
    region: NormalizedRegionV1
    input_mode: Literal["page_image", "document"]


class RecognitionReadBatchV1(EvidenceModel):
    contract: Literal["smartai.recognition.read_batch"] = "smartai.recognition.read_batch"
    schema_version: Literal[1] = 1
    source: RecognitionSourceRefV1
    plan: RecognitionPlanV1
    pages: list[PdfDetailPage | ImageDetailPageV1] = Field(default_factory=list, max_length=24)
    units: list[ReadUnitV1] = Field(default_factory=list, max_length=24)
    native_only_pages: list[int] = Field(default_factory=list, max_length=24)
    failed_pages: list[int] = Field(default_factory=list, max_length=10000)
    unprocessed_pages: list[int] = Field(default_factory=list, max_length=10000)
    unprocessed_regions: list[PendingReadRegionV1] = Field(default_factory=list, max_length=24)
    stop_codes: list[Code] = Field(default_factory=list, max_length=32)
    usage: RecognitionUsageV1
    # D decides fusion/quality; merely dispatching all planned calls is not success.
    recognition_complete: Literal[False] = False

    @property
    def partial_pages(self) -> list[int]:
        attempted = {number for unit in self.units for number in unit.page_numbers}
        return sorted(attempted & {region.page_number for region in self.unprocessed_regions})

    @model_validator(mode="after")
    def valid_scope(self):
        requested = set(self.plan.requested_pages)
        if self.plan.source_kind != ("pdf" if self.source.content_type == "application/pdf" else "image"):
            raise ValueError("plan kind must match source evidence")
        if self.source.content_type == "application/pdf":
            if any(not isinstance(page, PdfDetailPage) for page in self.pages) or any(unit.image_preparation is not None for unit in self.units):
                raise ValueError("PDF evidence cannot contain image-source geometry")
        else:
            if self.source.content_type not in {"image/png", "image/jpeg", "image/webp", "image/bmp", "image/tiff"} or self.plan.total_pages != 1 or requested != {1}:
                raise ValueError("image sources require one explicitly requested page")
            if any(not isinstance(page, ImageDetailPageV1) for page in self.pages) or self.native_only_pages:
                raise ValueError("images cannot invent native PDF evidence")
            preparations = [page.preparation for page in self.pages]
            for unit in self.units:
                if unit.image_preparation is None:
                    raise ValueError("image call units require preparation provenance")
                preparations.append(unit.image_preparation)
            for metadata in preparations:
                if metadata.source_sha256 != self.source.input_sha256 or metadata.source_content_type != self.source.content_type:
                    raise ValueError("image preparation belongs to a different source")
                if preparations and any(getattr(metadata, name) != getattr(preparations[0], name) for name in (
                    "source_width", "source_height", "source_mode", "exif_orientation", "oriented_width", "oriented_height",
                )):
                    raise ValueError("image preparations disagree about source geometry")
        numbers = [page.page_number for page in self.pages]
        if numbers != sorted(set(numbers)) or not set(numbers).issubset(requested):
            raise ValueError("detail outside plan")
        if len({unit.unit_id for unit in self.units}) != len(self.units):
            raise ValueError("duplicate call evidence")
        seen = set(self.native_only_pages)
        for unit in self.units:
            if not set(unit.page_numbers).issubset(numbers):
                raise ValueError("unit lacks page evidence")
            seen.update(unit.page_numbers)
        if not seen.issubset(numbers) or not set(self.failed_pages).issubset(requested):
            raise ValueError("invalid page accounting")
        if set(self.unprocessed_pages) & (seen | set(self.failed_pages)):
            raise ValueError("unprocessed pages cannot also have outcomes")
        if seen | set(self.failed_pages) | set(self.unprocessed_pages) != requested:
            raise ValueError("all requested pages need an outcome")
        self._validate_accounting()
        planned = {(page.page_number, page.input_mode, region.as_tuple())
                   for page in self.plan.decisions for region in page.regions}
        attempted = [(number, unit.input_mode, unit.region.as_tuple())
                     for unit in self.units for number in unit.page_numbers]
        pending = [(region.page_number, region.input_mode, region.region.as_tuple())
                   for region in self.unprocessed_regions]
        if len(attempted) != len(set(attempted)) or not set(attempted).issubset(planned):
            raise ValueError("call evidence must uniquely match planned regions")
        if len(pending) != len(set(pending)) or set(pending) != planned - set(attempted):
            raise ValueError("unread regions must remain explicit, including partially read pages")
        return self

    def _validate_accounting(self):
        if self.usage.initial_calls != len(self.units) or self.usage.empty_recovery_calls or self.usage.patch_calls:
            raise ValueError("reader usage must match initial call evidence")


def _snapshot(source, plan, engine):
    try:
        source = RecognitionSourceRefV1.model_validate(source.model_dump(warnings=False))
        plan = RecognitionPlanV1.model_validate(plan.model_dump(warnings=False))
        capabilities = engine.capabilities if engine is not None else None
        if capabilities != plan.engine_capabilities:
            raise RecognitionError("recognition_route_changed")
        return source, plan
    except (ValidationError, AttributeError, TypeError):
        raise RecognitionError("recognition_request_invalid") from None


async def read_pdf_plan(
    pdf_bytes: bytes,
    *,
    authorized_owner_id: str,
    source: RecognitionSourceRefV1,
    plan: RecognitionPlanV1,
    engine: RecognitionEngine | None,
    capacity: RecognitionCapacity,
    progress: ProgressReporter | None = None,
    budget: RecognitionBudget | None = None,
    local_reader: RecognitionLocalEvidenceReader | None = None,
    call_session=None,
) -> RecognitionReadBatchV1:
    """Read a bounded, owner-authorized PDF batch, preserving both evidence paths.

No retries, recovery, parser or semantic companion. An outer durable operation
must prevent replay after ambiguous submits, including cancellation. Byte/hash
checks here do not replace the caller's storage ACL and operation lease.
"""
    source, plan = _snapshot(source, plan, engine)
    if source.owner_id != authorized_owner_id or source.content_type != "application/pdf" or not isinstance(pdf_bytes, bytes) or hashlib.sha256(pdf_bytes).hexdigest() != source.input_sha256:
        raise RecognitionError("recognition_source_mismatch")
    if plan.source_kind != "pdf":
        raise RecognitionError("recognition_plan_changed")
    if local_reader is not None:
        local_reader.assert_context(source, authorized_owner_id=authorized_owner_id)
    started = time.monotonic()
    budget = budget or RecognitionBudget(source, plan.policy, plan.engine_capabilities)
    budget.assert_context(source, plan.policy, plan.engine_capabilities)
    eligible = [page for page in plan.decisions if page.action in {"native", "blank", "visual"}]
    # Knowledge can index a blocked page's native text without claiming that
    # its diagrams were read. Keep the same detail budget and persisted evidence.
    detail_decisions = list(eligible)
    if plan.purpose == "knowledge" and plan.policy.version == "knowledge-economy-v1":
        remaining = max(0, plan.policy.max_detail_pages - len(eligible))
        detail_decisions.extend([page for page in plan.decisions if page.action == "blocked"
                                 and "visual_capability_unavailable" in page.reason_codes][:remaining])
    detail_decisions.sort(key=lambda page: page.page_number)
    detail_pages: list[PdfDetailPage] = []
    units: list[ReadUnitV1] = []
    native_pages: list[int] = []
    failed: set[int] = set()
    stops: list[str] = []

    async def pdf_read(request):
        if local_reader is not None:
            return (await local_reader.read(
                pdf_bytes, request, timeout_seconds=min(10, budget.remaining("read")),
            )).evidence
        return await read_pdf_evidence(
            pdf_bytes, request, timeout_seconds=min(10, budget.remaining("read")), progress=progress,
        )

    try:
        if detail_decisions:
            result = await pdf_read(PdfPagesRequest(pages=[page.page_number for page in detail_decisions]))
            if not isinstance(result, PdfDetailResult) or result.total_pages != plan.total_pages:
                raise RecognitionError("recognition_plan_changed")
            detail_pages = result.pages
        by_number = {page.page_number: page for page in detail_pages}
        for decision in eligible:
            observed = by_number[decision.page_number].observation
            if decision.action == "blank" and not observed.verified_blank:
                raise RecognitionError("recognition_plan_changed")
            economy_math = (plan.purpose == "knowledge" and plan.policy.version == "knowledge-economy-v1"
                            and observed.risks == ["math"] and not plan.policy.force_visual
                            and "knowledge_native_math_unverified" in decision.reason_codes)
            if decision.action == "native" and (observed.native_quality != "clean"
                                                 or (observed.risks and not economy_math)
                                                 or plan.purpose == "submissions"):
                raise RecognitionError("recognition_plan_changed")
            if decision.action in {"native", "blank"}:
                native_pages.append(decision.page_number)

        visual = [page for page in eligible if page.action == "visual"]
        calls: list[tuple[list[int], NormalizedRegionV1, str]] = []
        if visual:
            capabilities = plan.engine_capabilities
            if engine is None or capabilities is None:
                raise RecognitionError("recognition_route_changed")
            # Keep frozen document groups intact even in a mixed-mode plan.
            groups: dict[str, list[int]] = {}
            for page in visual:
                if page.document_call_group:
                    if page.document_call_group not in groups:
                        groups[page.document_call_group] = []
                        calls.append((groups[page.document_call_group], NormalizedRegionV1(), "document"))
                    groups[page.document_call_group].append(page.page_number)
                else:
                    calls.extend(([page.page_number], region, page.input_mode) for region in page.regions)

        for numbers, region, mode in calls:
            budget.remaining("read")
            if len(units) >= min(plan.initial_calls, plan.policy.max_initial_calls, plan.policy.max_calls):
                raise RecognitionError("recognition_budget_exhausted")
            if engine.capabilities != plan.engine_capabilities:
                raise RecognitionError("recognition_route_changed")
            token_limit = budget.output_limit(4096)
            if mode == "document":
                rendered = await pdf_read(PdfPagesRequest(operation="export_pages", pages=numbers))
                if not isinstance(rendered, PdfExportResult):
                    raise RecognitionError("recognition_response_invalid")
            else:
                rendered = await pdf_read(PdfRenderRequest(page_number=numbers[0], region=region.as_tuple()))
                if not isinstance(rendered, PdfRenderResult):
                    raise RecognitionError("recognition_response_invalid")
            payload = decode_pdf_payload(rendered)
            request = EngineReadInputV1(
                purpose=plan.purpose, input_mode=mode, page_number=numbers[0], region=region,
                document_pages=numbers if mode == "document" else [],
                content_type=rendered.content_type, payload=payload, max_output_tokens=token_limit,
            )
            if progress:
                await progress.set_current_step("recognition_read", message="Reading source evidence")
            outcome = await run_initial_read(
                engine, request, source=source, policy=plan.policy, capabilities=plan.engine_capabilities,
                region_keys=tuple(region_budget_key(number, region) for number in numbers),
                budget=budget, capacity=capacity, progress=progress,
                call_session=call_session,
            )
            candidate = outcome.candidate
            units.append(ReadUnitV1(
                unit_id=f"u{len(units):04d}", page_numbers=numbers, region=region,
                input_mode=mode, payload_sha256=hashlib.sha256(payload).hexdigest(), payload_bytes=len(payload),
                candidate=candidate, requested_output_tokens=request.max_output_tokens,
                submission_may_exist=outcome.submission_may_exist,
                output_mapping="document_only" if len(numbers) > 1 else "single_region",
            ))
            if call_session is not None:
                error = await call_session.record("initial", units[-1])
                if error:
                    stops.append(error)
                    break
            if candidate.status == "error":
                failed.update(numbers)
                stops.append(candidate.safe_error_code or "recognition_response_invalid")
                break
            if candidate.finish_reason == "refused" or "provider_refused" in candidate.warning_codes:
                failed.update(numbers)
                stops.append("provider_request_rejected")
                break
    except (RecognitionError, PdfEvidenceError) as exc:
        stops.append(exc.code)

    return build_read_batch(source=source, plan=plan, pages=detail_pages, units=units,
                            native_pages=native_pages, failed=failed, stops=stops, started=started,
                            call_session=call_session)


def build_read_batch(
    *, source: RecognitionSourceRefV1, plan: RecognitionPlanV1,
    pages: list[PdfDetailPage | ImageDetailPageV1], units: list[ReadUnitV1],
    native_pages: list[int], failed: set[int], stops: list[str], started: float,
    call_session=None,
) -> RecognitionReadBatchV1:
    """Account for every planned region, including partially read source pages."""
    seen = set(native_pages)
    for unit in units:
        seen.update(unit.page_numbers)
    unprocessed = sorted(set(plan.requested_pages) - seen - failed)
    attempted = {(number, unit.input_mode, unit.region.as_tuple()) for unit in units for number in unit.page_numbers}
    pending_regions = [PendingReadRegionV1(page_number=page.page_number, region=region, input_mode=page.input_mode)
                       for page in plan.decisions for region in page.regions
                       if (page.page_number, page.input_mode, region.as_tuple()) not in attempted]
    provenance = call_session.records("initial") if call_session is not None else []
    candidates = [unit.candidate for index, unit in enumerate(units)
                  if call_session is None or provenance[index].origin == "dispatch"]
    input_known = all(candidate.input_tokens is not None for candidate in candidates)
    output_known = all(candidate.output_tokens is not None for candidate in candidates)
    batch_type, extra = RecognitionReadBatchV1, {}
    if call_session is not None:
        from backend.recognition.workflow_v2 import RecognitionReadBatchV2
        batch_type, extra = RecognitionReadBatchV2, {"provenance": provenance}
    return batch_type(
        **extra,
        source=source, plan=plan, pages=pages, units=units, native_only_pages=native_pages,
        failed_pages=sorted(failed), unprocessed_pages=unprocessed, unprocessed_regions=pending_regions,
        stop_codes=list(dict.fromkeys(stops)),
        usage=RecognitionUsageV1(
            initial_calls=len(candidates), duration_ms=(time.monotonic() - started) * 1000,
            cache_hits=len(units) - len(candidates),
            input_tokens=sum(candidate.input_tokens for candidate in candidates) if input_known else None,
            output_tokens=sum(candidate.output_tokens for candidate in candidates) if output_known else None,
            usage_complete=input_known and output_known,
        ),
    )
