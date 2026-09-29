"""Execute a frozen plan into raw candidates, without fusion or automatic repair.

The application authorizes source access and durable job ownership before this
boundary. It must share one capacity instance across target and knowledge jobs.
Page coverage here means evidence read, not a claim of correct transcription.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import time
from typing import Literal, TYPE_CHECKING

from pydantic import Field, ValidationError, model_validator

from backend.domain.errors import PdfEvidenceError, RecognitionError
from backend.recognition.engine import EngineReadInputV1, RecognitionEngine
from backend.recognition.models import (
    Code, EvidenceModel, NormalizedRegionV1, RecognitionCandidateV1,
    RecognitionSourceRefV1, RecognitionUsageV1,
)
from backend.recognition.planner import RecognitionPlanV1
from backend.tools.pdf_evidence import (
    PdfDetailPage, PdfDetailResult, PdfExportResult, PdfPagesRequest,
    PdfRenderRequest, PdfRenderResult, decode_pdf_payload, read_pdf_evidence,
)

if TYPE_CHECKING:
    from backend.progress.tracker import ProgressReporter


class RecognitionCapacity:
    """Process-local limits, shared across purposes; not a distributed quota."""

    def __init__(self, *, global_limit: int = 2, owner_limit: int = 2):
        if type(global_limit) is not int or not 1 <= global_limit <= 64 or type(owner_limit) is not int or not 1 <= owner_limit <= 2:
            raise RecognitionError("recognition_request_invalid")
        self._global = asyncio.Semaphore(global_limit)
        self._owner_limit = owner_limit
        self._owners: dict[str, tuple[asyncio.Semaphore, int]] = {}
        self._loop = None

    @asynccontextmanager
    async def lease(self, owner_id: str):
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise RecognitionError("recognition_request_invalid")
        self._loop = loop
        semaphore, users = self._owners.get(owner_id, (asyncio.Semaphore(self._owner_limit), 0))
        self._owners[owner_id] = semaphore, users + 1
        try:
            async with semaphore, self._global:
                yield
        finally:
            _, users = self._owners[owner_id]
            if users == 1:
                del self._owners[owner_id]
            else:
                self._owners[owner_id] = semaphore, users - 1


class ReadUnitV1(EvidenceModel):
    unit_id: str = Field(pattern=r"^u\d{4}$")
    page_numbers: list[int] = Field(min_length=1, max_length=24)
    region: NormalizedRegionV1
    input_mode: Literal["page_image", "document"]
    payload_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    payload_bytes: int = Field(ge=1, le=10 * 1024 * 1024)
    candidate: RecognitionCandidateV1
    submission_may_exist: bool = False
    # Multiple submitted pages do not provide output-to-page correspondence.
    output_mapping: Literal["single_region", "document_only"]

    @model_validator(mode="after")
    def valid_mapping(self):
        if self.page_numbers != sorted(set(self.page_numbers)) or any(not 1 <= p <= 10000 for p in self.page_numbers):
            raise ValueError("invalid unit page mapping")
        if len(self.page_numbers) > 1 and (self.input_mode != "document" or self.output_mapping != "document_only"):
            raise ValueError("multi-page candidates cannot claim page alignment")
        if self.input_mode == "document" and self.region != NormalizedRegionV1():
            raise ValueError("document units require the full page")
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
    pages: list[PdfDetailPage] = Field(default_factory=list, max_length=24)
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
        if self.usage.initial_calls != len(self.units) or self.usage.empty_recovery_calls or self.usage.patch_calls:
            raise ValueError("reader usage must match initial call evidence")
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
) -> RecognitionReadBatchV1:
    """Read a bounded, owner-authorized PDF batch, preserving both evidence paths.

No retries, recovery, parser or semantic companion. An outer durable operation
must prevent replay after ambiguous submits, including cancellation. Byte/hash
checks here do not replace the caller's storage ACL and operation lease.
"""
    source, plan = _snapshot(source, plan, engine)
    if source.owner_id != authorized_owner_id or source.content_type != "application/pdf" or not isinstance(pdf_bytes, bytes) or hashlib.sha256(pdf_bytes).hexdigest() != source.input_sha256:
        raise RecognitionError("recognition_source_mismatch")
    started = time.monotonic()
    deadline = started + plan.policy.total_seconds
    eligible = [page for page in plan.decisions if page.action in {"native", "blank", "visual"}]
    detail_pages: list[PdfDetailPage] = []
    units: list[ReadUnitV1] = []
    native_pages: list[int] = []
    failed: set[int] = set()
    stops: list[str] = []
    used_output_budget = 0

    def remaining() -> float:
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise RecognitionError("recognition_timeout")
        return seconds

    async def pdf_read(request):
        return await read_pdf_evidence(
            pdf_bytes, request, timeout_seconds=min(10, remaining()), progress=progress,
        )

    try:
        if eligible:
            result = await pdf_read(PdfPagesRequest(pages=[page.page_number for page in eligible]))
            if not isinstance(result, PdfDetailResult) or result.total_pages != plan.total_pages:
                raise RecognitionError("recognition_plan_changed")
            detail_pages = result.pages
        by_number = {page.page_number: page for page in detail_pages}
        for decision in eligible:
            observed = by_number[decision.page_number].observation
            if decision.action == "blank" and not observed.verified_blank:
                raise RecognitionError("recognition_plan_changed")
            if decision.action == "native" and (observed.native_quality != "clean" or observed.risks or plan.purpose == "submissions"):
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
            remaining()
            if len(units) >= min(plan.initial_calls, plan.policy.max_initial_calls, plan.policy.max_calls):
                raise RecognitionError("recognition_budget_exhausted")
            if engine.capabilities != plan.engine_capabilities:
                raise RecognitionError("recognition_route_changed")
            token_limit = min(4096, plan.policy.max_output_tokens - used_output_budget)
            if token_limit <= 0:
                raise RecognitionError("recognition_budget_exhausted")
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
            uncertain = False
            dispatched = False
            try:
                # Waiting for capacity counts toward the same frozen total deadline.
                async with asyncio.timeout(remaining()), capacity.lease(source.owner_id):
                    if engine.capabilities != plan.engine_capabilities:
                        raise RecognitionError("recognition_route_changed")
                    dispatched = True
                    async with asyncio.timeout(min(plan.policy.per_call_seconds, remaining())):
                        candidate = await engine.recognize(request)
                    candidate = RecognitionCandidateV1.model_validate(candidate.model_dump(warnings=False))
                    if candidate.status == "not_run" or candidate.provider_route_id != plan.route_id or candidate.kind != plan.engine_capabilities.candidate_kind:
                        raise RecognitionError("recognition_response_invalid", submission_may_exist=True)
            except TimeoutError:
                if not dispatched:
                    raise RecognitionError("recognition_timeout") from None
                uncertain = True
                candidate = RecognitionCandidateV1(
                    kind=plan.engine_capabilities.candidate_kind, status="error",
                    provider_route_id=plan.route_id, safe_error_code="provider_submit_uncertain",
                )
            except RecognitionError as exc:
                if not dispatched:
                    raise
                uncertain = exc.submission_may_exist
                candidate = RecognitionCandidateV1(
                    kind=plan.engine_capabilities.candidate_kind, status="error",
                    provider_route_id=plan.route_id, safe_error_code=exc.code,
                )
            except Exception:
                if not dispatched:
                    raise RecognitionError("recognition_response_invalid") from None
                uncertain = True
                candidate = RecognitionCandidateV1(
                    kind=plan.engine_capabilities.candidate_kind, status="error",
                    provider_route_id=plan.route_id, safe_error_code="recognition_response_invalid",
                )
            units.append(ReadUnitV1(
                unit_id=f"u{len(units):04d}", page_numbers=numbers, region=region,
                input_mode=mode, payload_sha256=hashlib.sha256(payload).hexdigest(), payload_bytes=len(payload),
                candidate=candidate, submission_may_exist=uncertain,
                output_mapping="document_only" if len(numbers) > 1 else "single_region",
            ))
            # Unknown usage reserves the requested bound; OCR has no token meter.
            if plan.engine_capabilities.bounded_output_tokens:
                used_output_budget += candidate.output_tokens if candidate.output_tokens is not None else token_limit
            if progress:
                await progress.increment_stage_metrics(recognition_initial_calls=1)
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

    seen = set(native_pages)
    for unit in units:
        seen.update(unit.page_numbers)
    unprocessed = sorted(set(plan.requested_pages) - seen - failed)
    attempted = {(number, unit.input_mode, unit.region.as_tuple()) for unit in units for number in unit.page_numbers}
    pending_regions = [PendingReadRegionV1(page_number=page.page_number, region=region, input_mode=page.input_mode)
                       for page in plan.decisions for region in page.regions
                       if (page.page_number, page.input_mode, region.as_tuple()) not in attempted]
    candidates = [unit.candidate for unit in units]
    input_known = all(candidate.input_tokens is not None for candidate in candidates)
    output_known = all(candidate.output_tokens is not None for candidate in candidates)
    return RecognitionReadBatchV1(
        source=source, plan=plan, pages=detail_pages, units=units, native_only_pages=native_pages,
        failed_pages=sorted(failed), unprocessed_pages=unprocessed, unprocessed_regions=pending_regions,
        stop_codes=list(dict.fromkeys(stops)),
        usage=RecognitionUsageV1(
            initial_calls=len(units), duration_ms=(time.monotonic() - started) * 1000,
            input_tokens=sum(candidate.input_tokens for candidate in candidates) if input_known else None,
            output_tokens=sum(candidate.output_tokens for candidate in candidates) if output_known else None,
            usage_complete=input_known and output_known,
        ),
    )
