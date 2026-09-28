"""Bounded evidence orchestration, not a hidden LLM planner or business parser.

The caller authorizes original bytes and durable job ownership. This stage only
returns raw evidence; fusion, recovery, caching and business confirmation follow.
"""
from __future__ import annotations

import hashlib
import re
import time
from typing import Callable, Literal, TYPE_CHECKING

from pydantic import Field, ValidationError, model_validator

from backend.domain.errors import PdfEvidenceError, RecognitionError
from backend.recognition.budget import BudgetSnapshot, RecognitionBudget, validate_budget_accounting
from backend.recognition.engine import EngineLocateInputV1, LocatorImageV1, RecognitionEngine
from backend.recognition.executor import RecognitionReadBatchV1, read_pdf_plan
from backend.recognition.image_executor import read_image_plan
from backend.recognition.locator import (
    NativeLocatorRequestV1, NativeLocatorResultV1, PageNumber, TargetId, locate_native_targets,
)
from backend.recognition.models import Code, EvidenceModel, Purpose, RecognitionPolicyV1, RecognitionSourceRefV1
from backend.recognition.planner import EngineCapabilitiesV1, PageObservationV1, RecognitionPlanRequestV1, plan_recognition
from backend.recognition.runtime import RecognitionCapacity, run_locator_call
from backend.recognition.scan_locator import ScanLocatorResultV1, parse_scan_locations
from backend.tools.pdf_evidence import (
    PdfContactSheetMetadata, PdfContactSheetRequest, PdfContactSheetResult, PdfDetailPage,
    PdfDetailResult, PdfIndexRequest, PdfIndexResult, PdfPagesRequest, decode_pdf_payload, read_pdf_evidence,
)

if TYPE_CHECKING:
    from backend.progress.tracker import ProgressReporter
    from backend.services.recognition_local_evidence import RecognitionLocalEvidenceReader


class RecognitionReadRequestV1(EvidenceModel):
    source: RecognitionSourceRefV1
    purpose: Purpose
    scope: Literal["document", "pages", "targets"] = "document"
    pages: list[PageNumber] = Field(default_factory=list, max_length=10000)
    targets: list[TargetId] = Field(default_factory=list, max_length=64)
    page_hints: dict[TargetId, list[PageNumber]] = Field(default_factory=dict, max_length=64)
    search_start_page: PageNumber = 1
    search_window_pages: int = Field(default=500, strict=True, ge=1, le=500)
    policy: RecognitionPolicyV1 = Field(default_factory=RecognitionPolicyV1)

    @model_validator(mode="after")
    def scope_contract(self):
        if (self.scope == "pages") != bool(self.pages) or self.pages != sorted(set(self.pages)):
            raise ValueError("page scope requires ordered explicit pages")
        if self.scope == "targets" and not self.targets:
            raise ValueError("target scope requires target identifiers")
        if len(set(self.targets)) != len(self.targets) or any(
            not target.strip() or any(char in target for char in "\r\n\x00") for target in self.targets
        ):
            raise ValueError("targets must be unique single-line identifiers")
        if self.page_hints and self.scope != "targets":
            raise ValueError("per-target page hints require target scope")
        if set(self.page_hints) - set(self.targets) or any(
            not pages or pages != sorted(set(pages)) for pages in self.page_hints.values()
        ) or len({number for pages in self.page_hints.values() for number in pages}) > 24:
            raise ValueError("page hints must be bounded and belong to requested targets")
        return self


class LocatorSheetEvidenceV1(EvidenceModel):
    metadata: PdfContactSheetMetadata
    payload_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class LocatorCallEvidenceV1(EvidenceModel):
    sheets: list[LocatorSheetEvidenceV1] = Field(min_length=1, max_length=2)
    result: ScanLocatorResultV1
    requested_output_tokens: int = Field(strict=True, ge=1, le=32768)
    submission_may_exist: bool = False

    @model_validator(mode="after")
    def exact_input_scope(self):
        pages = [number for sheet in self.sheets for number in sheet.metadata.page_numbers]
        if len(pages) != len(set(pages)) or sorted(pages) != self.result.inspected_pages:
            raise ValueError("locator result must refer only to its actual submitted sheets")
        return self


class RecognitionWorkflowReadV1(EvidenceModel):
    contract: Literal["smartai.recognition.workflow_read"] = "smartai.recognition.workflow_read"
    schema_version: Literal[1] = 1
    request: RecognitionReadRequestV1
    execution_policy: RecognitionPolicyV1
    engine_capabilities: EngineCapabilitiesV1 | None
    total_pages: int | None = Field(default=None, ge=1, le=10000)
    indexed_pages: list[PageNumber] = Field(default_factory=list, max_length=500)
    native_details: list[PdfDetailPage] = Field(default_factory=list, max_length=24)
    native_location: NativeLocatorResultV1 | None = None
    locator_calls: list[LocatorCallEvidenceV1] = Field(default_factory=list, max_length=12)
    locator_halted: bool = False
    selected_pages: list[PageNumber] = Field(default_factory=list, max_length=24)
    unlocated_targets: list[TargetId] = Field(default_factory=list, max_length=64)
    read_batch: RecognitionReadBatchV1 | None = None
    stop_codes: list[Code] = Field(default_factory=list, max_length=32)
    budget: BudgetSnapshot
    recognition_complete: Literal[False] = False

    @model_validator(mode="after")
    def evidence_scope(self):
        expected_policy = self.request.policy.model_copy(deep=True)
        if self.request.scope == "targets":
            expected_policy.max_detail_pages = min(expected_policy.max_detail_pages, 2 * len(self.request.targets) + 4)
        if self.execution_policy != expected_policy:
            raise ValueError("workflow must retain the effective request policy")
        if self.selected_pages != sorted(set(self.selected_pages)) or self.indexed_pages != sorted(set(self.indexed_pages)):
            raise ValueError("workflow page sets must be ordered and unique")
        pages = [*self.indexed_pages, *self.selected_pages, *(p.page_number for p in self.native_details)]
        if pages and (self.total_pages is None or max(pages) > self.total_pages):
            raise ValueError("workflow evidence exceeds source extent")
        detail_numbers = [page.page_number for page in self.native_details]
        if detail_numbers != sorted(set(detail_numbers)) or (detail_numbers and self.request.source.content_type != "application/pdf"):
            raise ValueError("native detail must uniquely belong to a PDF")
        if len(set(self.unlocated_targets)) != len(self.unlocated_targets) or not set(self.unlocated_targets).issubset(self.request.targets):
            raise ValueError("unlocated targets must belong to the request")
        if self.native_location is not None and (
            self.native_location.source != self.request.source or self.native_location.total_pages != self.total_pages
            or self.native_location.requested_targets != self.request.targets
        ):
            raise ValueError("native locator belongs to another request")
        if self.locator_halted and self.read_batch is not None:
            raise ValueError("failed localization must stop downstream dispatch")
        for call in self.locator_calls:
            if self.engine_capabilities is None or not self.engine_capabilities.target_location:
                raise ValueError("locator evidence requires its frozen capability")
            if (call.result.candidate.provider_route_id != self.engine_capabilities.route_id
                    or call.result.candidate.kind != self.engine_capabilities.candidate_kind):
                raise ValueError("locator evidence belongs to another route")
            if (call.submission_may_exist or call.result.parse_status != "ok") and not self.locator_halted:
                raise ValueError("failed locator outcomes cannot be ignored")
            if any(sheet.metadata.total_pages != self.total_pages for sheet in call.sheets):
                raise ValueError("locator sheet belongs to another document extent")
            if not set(call.result.requested_targets).issubset(self.request.targets):
                raise ValueError("locator call introduces unrequested targets")
        if self.read_batch is not None:
            batch = self.read_batch
            if batch.source != self.request.source or batch.plan.total_pages != self.total_pages or batch.plan.purpose != self.request.purpose:
                raise ValueError("reader belongs to another request")
            if (batch.plan.scope != self.request.scope or batch.plan.requested_targets != self.request.targets
                    or batch.plan.policy != self.execution_policy or batch.plan.engine_capabilities != self.engine_capabilities):
                raise ValueError("reader must preserve frozen scope, policy and capabilities")
            if not set(self.selected_pages).issubset(batch.plan.requested_pages):
                raise ValueError("selected pages must be accounted for in the read plan")
            expected_pages = (self.request.pages if self.request.scope == "pages" else self.selected_pages
                              if self.request.scope == "targets" else list(range(1, self.total_pages + 1)))
            if batch.plan.requested_pages != expected_pages:
                raise ValueError("reader page scope differs from the workflow")
            native_by_number = {page.page_number: page for page in self.native_details}
            for page in batch.pages:
                if page.page_number in native_by_number and page != native_by_number[page.page_number]:
                    raise ValueError("native snapshots changed within one workflow")
            for unit in batch.units:
                if (self.engine_capabilities is None or unit.candidate.provider_route_id != self.engine_capabilities.route_id
                        or unit.candidate.kind != self.engine_capabilities.candidate_kind):
                    raise ValueError("read evidence belongs to another engine")
        self._validate_accounting()
        return self

    def _validate_accounting(self):
        if self.budget.locator_calls != len(self.locator_calls):
            raise ValueError("workflow must retain every locator call outcome")
        if self.budget.initial_calls != (self.read_batch.usage.initial_calls if self.read_batch else 0):
            raise ValueError("workflow must retain every initial read outcome")
        if (self.budget.empty_recovery_calls or self.budget.patch_calls
                or self.budget.read_calls != self.budget.initial_calls
                or self.budget.total_calls != self.budget.locator_calls + self.budget.initial_calls):
            raise ValueError("raw read workflow cannot invent additional calls")
        _validate_workflow_usage(self)


def _validate_workflow_usage(raw: RecognitionWorkflowReadV1) -> None:
    records = [(call.result.candidate, call.submission_may_exist, call.requested_output_tokens) for call in raw.locator_calls]
    if raw.read_batch:
        records.extend((unit.candidate, unit.submission_may_exist, unit.requested_output_tokens) for unit in raw.read_batch.units)
    validate_budget_accounting(raw.budget, raw.engine_capabilities, records)
    if raw.read_batch:
        candidates = [unit.candidate for unit in raw.read_batch.units]
        inputs = None if any(c.input_tokens is None for c in candidates) else sum(c.input_tokens for c in candidates)
        outputs = None if any(c.output_tokens is None for c in candidates) else sum(c.output_tokens for c in candidates)
        usage = raw.read_batch.usage
        if (usage.locator_calls or usage.input_tokens != inputs or usage.output_tokens != outputs
                or usage.usage_complete != (inputs is not None and outputs is not None)):
            raise ValueError("reader usage must match its own call evidence without adding localization")


def _index_terms(targets: list[str]) -> list[str]:
    terms = list(targets)
    for target in targets:
        if re.fullmatch(r"\d+(?:\.\d+)+", target):
            prefix = target.rsplit(".", 1)[0]
            if prefix not in terms and len(terms) < 64:
                terms.append(prefix)
    return terms


def _detail_candidates(index: PdfIndexResult, request: RecognitionReadRequestV1, limit: int) -> list[int]:
    hints = sorted({number for pages in request.page_hints.values() for number in pages})
    literal = [page.page_number for page in index.pages if set(page.target_matches) & set(request.targets)]
    contextual = [page.page_number for page in index.pages if page.target_matches]
    candidates = list(dict.fromkeys(hints + literal + contextual))
    # Neighbors are inspected for context, never inherited section/continuation proof.
    neighbors = [number for page in contextual for number in (page - 1, page + 1)
                 if 1 <= number <= index.total_pages]
    return sorted(list(dict.fromkeys(candidates + neighbors))[:limit])


class RecognitionAgent:
    def __init__(self, engine: RecognitionEngine | None, *, capacity: RecognitionCapacity,
                 progress: ProgressReporter | None = None, clock: Callable[[], float] = time.monotonic,
                 local_reader: RecognitionLocalEvidenceReader | None = None,
                 call_service=None, elapsed_seconds: float = 0):
        self.engine, self.capacity, self.progress, self.clock = engine, capacity, progress, clock
        self.local_reader = local_reader
        self.call_service = call_service
        self.elapsed_seconds = elapsed_seconds

    async def read(self, request: RecognitionReadRequestV1, source_bytes: bytes, *, authorized_owner_id: str) -> RecognitionWorkflowReadV1:
        raw, _budget = await self._read(request, source_bytes, authorized_owner_id=authorized_owner_id)
        return raw

    async def _read(self, request: RecognitionReadRequestV1, source_bytes: bytes, *, authorized_owner_id: str, _with_session=False):
        try:
            request = RecognitionReadRequestV1.model_validate(request.model_dump(warnings=False))
        except (ValidationError, AttributeError, TypeError):
            raise RecognitionError("recognition_request_invalid") from None
        source = request.source
        if (source.owner_id != authorized_owner_id or not isinstance(source_bytes, bytes)
                or hashlib.sha256(source_bytes).hexdigest() != source.input_sha256):
            raise RecognitionError("recognition_source_mismatch")
        if source.content_type not in {"application/pdf", "image/png", "image/jpeg", "image/webp"}:
            raise RecognitionError("recognition_input_unsupported")
        local_reader = self.local_reader
        if local_reader is not None:
            local_reader.assert_context(source, authorized_owner_id=authorized_owner_id)
        capabilities = self.engine.capabilities.model_copy(deep=True) if self.engine else None
        policy = request.policy.model_copy(deep=True)
        if request.scope == "targets":
            policy.max_detail_pages = min(policy.max_detail_pages, 2 * len(request.targets) + 4)
        budget = RecognitionBudget(source, policy, capabilities, clock=self.clock, elapsed_seconds=self.elapsed_seconds)
        call_session = None
        if self.call_service is not None:
            from backend.services.recognition_session import RecognitionCallSessionV2
            call_session = RecognitionCallSessionV2(self.call_service, source=source,
                                                    authorized_owner_id=authorized_owner_id)
        total, indexed, details, native, calls, selected, stops, batch = None, [], [], None, [], [], [], None
        locator_halted = False
        unresolved = list(request.targets) if request.scope == "targets" else []

        async def pdf_read(command, phase="locator"):
            if local_reader is not None:
                return (await local_reader.read(
                    source_bytes, command, timeout_seconds=min(10, budget.remaining(phase)),
                )).evidence
            return await read_pdf_evidence(source_bytes, command, timeout_seconds=min(10, budget.remaining(phase)), progress=self.progress)

        try:
            if self.progress:
                await self.progress.set_current_step("recognition_inspect", message="Inspecting source evidence")
            if source.content_type != "application/pdf":
                total = 1
                if any(number != 1 for number in request.pages) or any(number != 1 for pages in request.page_hints.values() for number in pages):
                    raise RecognitionError("recognition_request_invalid")
                selected = [1]
                plan = plan_recognition(RecognitionPlanRequestV1(
                    purpose=request.purpose, source_kind="image", scope=request.scope, total_pages=1,
                    requested_pages=[1], requested_targets=request.targets, unlocated_targets=unresolved,
                    observations=[PageObservationV1(page_number=1)],
                ), engine=capabilities, policy=policy)
                batch = await read_image_plan(source_bytes, authorized_owner_id=authorized_owner_id, source=source,
                                             plan=plan, engine=self.engine, capacity=self.capacity, progress=self.progress, budget=budget,
                                             local_reader=local_reader, call_session=call_session)
            else:
                start = min(request.pages) if request.pages else request.search_start_page
                index = await pdf_read(PdfIndexRequest(start_page=start, window_pages=request.search_window_pages,
                                                       targets=_index_terms(request.targets)))
                if not isinstance(index, PdfIndexResult):
                    raise RecognitionError("recognition_response_invalid")
                total, indexed = index.total_pages, [p.page_number for p in index.pages]
                if any(number > total for number in request.pages) or any(number > total for pages in request.page_hints.values() for number in pages):
                    raise RecognitionError("recognition_request_invalid")
                if request.scope == "targets":
                    candidates = _detail_candidates(index, request, policy.max_detail_pages)
                    if candidates:
                        detail = await pdf_read(PdfPagesRequest(pages=candidates))
                        if not isinstance(detail, PdfDetailResult) or detail.total_pages != total:
                            raise RecognitionError("recognition_response_invalid")
                        details = detail.pages
                    native = locate_native_targets(NativeLocatorRequestV1(
                        source=source, total_pages=total, targets=request.targets, index_pages=index.pages,
                        detail_pages=details, page_hints=request.page_hints,
                    ))
                    resolved = {item.target for item in native.locations if item.resolved_scope == "page_only"}
                    selected = sorted({p for item in native.locations if item.resolved_scope == "page_only" for p in item.selected_pages})
                    unresolved = [target for target in request.targets if target not in resolved]
                    if unresolved and capabilities and capabilities.target_location:
                        scanned, scan_stops, locator_halted = await self._scan(
                            request, source_bytes, index, details, native, unresolved, policy, budget, capabilities,
                            local_reader=local_reader, call_session=call_session,
                        )
                        calls.extend(scanned)
                        stops.extend(scan_stops)
                        model_pages = {p for call in calls for item in call.result.locations for p in item.pages}
                        model_found = {item.target for call in calls for item in call.result.locations if item.status == "candidate"}
                        ambiguous = {item.target for item in native.locations if item.status == "ambiguous" and item.resolved_scope == "none"}
                        # Low-resolution guesses cannot erase competing strong native identities.
                        ambiguous_pages = {p for item in native.locations if item.target in ambiguous for p in item.candidate_pages}
                        known_anchors = {proof.page_number for item in native.locations for proof in item.evidence
                                         if proof.kind in {"literal", "local_label"}}
                        selected = sorted(set(selected) | model_pages | ambiguous_pages | known_anchors)
                        unresolved = [target for target in unresolved if target not in model_found or target in ambiguous]
                    if len(selected) > policy.max_detail_pages:
                        selected, unresolved = [], list(request.targets)
                        stops.append("target_selection_limit_exceeded")
                    if unresolved:
                        stops.append("target_location_needs_hint")
                    requested = selected
                else:
                    requested = request.pages if request.scope == "pages" else list(range(1, total + 1))
                    selected = requested[:policy.max_detail_pages]

                if requested and not locator_halted:
                    # Explicit page hints can lie outside the local search window.
                    known = {p.page_number: p.observation for p in index.pages}
                    known.update({p.page_number: p.observation for p in details})
                    missing = [p for p in selected if p not in known]
                    if missing:
                        extra = await pdf_read(PdfPagesRequest(pages=missing), "read")
                        if not isinstance(extra, PdfDetailResult) or extra.total_pages != total:
                            raise RecognitionError("recognition_response_invalid")
                        known.update({p.page_number: p.observation for p in extra.pages})
                    plan = plan_recognition(RecognitionPlanRequestV1(
                        purpose=request.purpose, scope=request.scope, total_pages=total,
                        requested_pages=requested, requested_targets=request.targets,
                        unlocated_targets=unresolved, observations=[known[p] for p in requested if p in known],
                    ), engine=capabilities, policy=policy)
                    batch = await read_pdf_plan(source_bytes, authorized_owner_id=authorized_owner_id, source=source,
                                               plan=plan, engine=self.engine, capacity=self.capacity, progress=self.progress, budget=budget,
                                               local_reader=local_reader, call_session=call_session)
        except (RecognitionError, PdfEvidenceError) as exc:
            stops.append(exc.code)
        if batch:
            stops.extend(batch.stop_codes)
            if any("visual_capability_unavailable" in page.reason_codes for page in batch.plan.decisions):
                stops.append("visual_capability_unavailable")
        workflow_type, extra = RecognitionWorkflowReadV1, {}
        if call_session is not None:
            from backend.recognition.workflow_v2 import RecognitionWorkflowReadV2
            workflow_type = RecognitionWorkflowReadV2
            extra = dict(locator_provenance=call_session.records("locator"), logical_budget=budget.logical_snapshot())
        raw = workflow_type(
            **extra,
            request=request, execution_policy=policy, engine_capabilities=capabilities,
            total_pages=total, indexed_pages=indexed, native_details=details, native_location=native,
            locator_calls=calls, selected_pages=selected, unlocated_targets=unresolved, read_batch=batch,
            locator_halted=locator_halted,
            stop_codes=list(dict.fromkeys(stops)), budget=budget.snapshot(),
        )
        return (raw, budget, call_session) if _with_session else (raw, budget)

    async def recognize(self, request: RecognitionReadRequestV1, source_bytes: bytes, *,
                        authorized_owner_id: str, prompt_version: str):
        from backend.recognition.fusion import assemble_recognition
        from backend.recognition.recheck import recheck_recognition

        local_reader = self.local_reader
        raw, budget, call_session = await self._read(request, source_bytes, authorized_owner_id=authorized_owner_id, _with_session=True)
        if self.progress:
            await self.progress.set_current_step("recognition_assess", message="Checking recognition evidence")
        if call_session is not None:
            from backend.recognition.workflow_v2 import assemble_recognition_v2
            result = assemble_recognition_v2(raw, prompt_version=prompt_version)
        else:
            result = assemble_recognition(raw, prompt_version=prompt_version)
        if self.progress:
            await self.progress.increment_stage_metrics(recognition_assemblies=1)
        return await recheck_recognition(
            result, source_bytes, authorized_owner_id=authorized_owner_id, engine=self.engine,
            budget=budget, capacity=self.capacity, progress=self.progress, local_reader=local_reader,
            call_session=call_session,
        )

    async def _scan(self, request, source_bytes, index, details, native, targets, policy, budget, capabilities, *, local_reader=None, call_session=None):
        calls, stops = [], []
        halted = False
        candidates = [p for item in native.locations if item.target in targets for p in item.candidate_pages]
        priority = list(dict.fromkeys(candidates + [p.page_number for p in details]))
        scope = set(p.page_number for p in index.pages)
        priority = [p for p in priority if p in scope]
        ordered = list(dict.fromkeys(priority + [p.page_number for p in index.pages]))
        width = 8 * capabilities.max_locator_images
        remaining = list(targets)
        try:
            for offset in range(0, len(ordered), width):
                if not remaining or len(calls) >= policy.max_locator_calls:
                    break
                if self.progress:
                    await self.progress.set_current_step("recognition_locate", message="Locating candidate source pages")
                budget.assert_context(request.source, policy, self.engine.capabilities)
                token_limit = budget.output_limit(4096)
                numbers = ordered[offset:offset + width]
                images, evidence = [], []
                for part in range(0, len(numbers), 8):
                    command = PdfContactSheetRequest(pages=sorted(numbers[part:part + 8]))
                    if local_reader is not None:
                        sheet = (await local_reader.read(
                            source_bytes, command, timeout_seconds=min(10, budget.remaining("locator")),
                        )).evidence
                    else:
                        sheet = await read_pdf_evidence(source_bytes, command,
                                                       timeout_seconds=min(10, budget.remaining("locator")), progress=self.progress)
                    if not isinstance(sheet, PdfContactSheetResult) or sheet.total_pages != index.total_pages:
                        raise RecognitionError("recognition_response_invalid")
                    payload = decode_pdf_payload(sheet)
                    images.append(LocatorImageV1(page_numbers=sheet.page_numbers, payload=payload))
                    evidence.append(LocatorSheetEvidenceV1(metadata=PdfContactSheetMetadata.model_validate(
                        {field: getattr(sheet, field) for field in PdfContactSheetMetadata.model_fields}),
                        payload_sha256=hashlib.sha256(payload).hexdigest()))
                outcome = await run_locator_call(self.engine, EngineLocateInputV1(
                    purpose=request.purpose, targets=remaining, images=images, max_output_tokens=token_limit,
                ), source=request.source, policy=policy, capabilities=capabilities, budget=budget, capacity=self.capacity, progress=self.progress,
                    call_session=call_session)
                result = parse_scan_locations(outcome.candidate, requested_targets=remaining, inspected_pages=sorted(numbers))
                calls.append(LocatorCallEvidenceV1(sheets=evidence, result=result, requested_output_tokens=token_limit,
                                                  submission_may_exist=outcome.submission_may_exist))
                if call_session is not None:
                    error = await call_session.record("locator", calls[-1])
                    if error:
                        stops.append(error)
                        halted = True
                        break
                if result.parse_status != "ok":
                    stops.extend(result.reason_codes)
                    halted = True
                    break
                # Once a visible but uncertain cue exists, spend the next call on
                # its full-resolution page, not more low-resolution search sheets.
                candidates = {item.target for item in result.locations if item.pages}
                remaining = [target for target in remaining if target not in candidates]
        except (RecognitionError, PdfEvidenceError) as exc:
            stops.append(exc.code)
            halted = True
        if remaining:
            stops.append("target_location_needs_hint")
        return calls, stops, halted
