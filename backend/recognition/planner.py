"""Pure routing decisions over observed evidence, not a paid planning agent.

The caller resolves one owner-authorized route before planning. Capabilities are
adapter declarations, never proof of authorization or a model-name heuristic.
Execution, deadlines, credential lookup and provider fallback belong elsewhere.
"""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from backend.recognition.models import (
    Code,
    EvidenceModel,
    NormalizedRegionV1,
    Purpose,
    RecognitionPolicyV1,
)

VisualInput = Literal["page_image", "document"]
Risk = Literal["math", "table", "diagram", "handwriting", "layout", "damaged_text"]


class EngineCapabilitiesV1(EvidenceModel):
    route_id: str = Field(min_length=1, max_length=240)
    fingerprint: str = Field(min_length=1, max_length=256)
    visual_inputs: list[VisualInput] = Field(default_factory=list, max_length=2)
    region_reads: bool = False
    semantic_repair: bool = False
    structured_output: Literal["supported", "unsupported", "unknown"] = "unknown"
    # Only completed empty/uncertain responses qualify, never ambiguous submits.
    response_recheck: bool = False
    candidate_kind: Literal["vision", "ocr"] = "vision"
    document_batching: bool = False
    max_document_pages: int = Field(default=1, ge=1, le=24)
    bounded_output_tokens: bool = False
    target_location: bool = False
    max_locator_images: int = Field(default=1, ge=1, le=2)

    @model_validator(mode="after")
    def consistent_capabilities(self):
        if len(set(self.visual_inputs)) != len(self.visual_inputs):
            raise ValueError("duplicate input capability")
        if self.region_reads and "page_image" not in self.visual_inputs:
            raise ValueError("region reads require page images")
        if self.semantic_repair and not self.visual_inputs:
            raise ValueError("visual repair requires visual input")
        if self.document_batching and "document" not in self.visual_inputs:
            raise ValueError("document batching requires document input")
        if not self.document_batching and self.max_document_pages != 1:
            raise ValueError("multiple document pages require batching")
        if self.target_location and "page_image" not in self.visual_inputs:
            raise ValueError("scan location requires image input")
        return self


class PageObservationV1(EvidenceModel):
    page_number: int = Field(ge=1, le=10000)
    native_char_count: int = Field(default=0, ge=0, le=500_000)
    native_quality: Literal["clean", "suspect", "missing"] = "missing"
    verified_blank: bool = False
    risks: list[Risk] = Field(default_factory=list, max_length=6)
    regions: list[NormalizedRegionV1] = Field(default_factory=list, max_length=24)
    regions_cover_all_risks: bool = False

    @model_validator(mode="after")
    def consistent_observation(self):
        if (self.native_quality == "missing") != (self.native_char_count == 0):
            raise ValueError("native quality must agree with text availability")
        if self.verified_blank and (self.native_char_count or self.risks or self.regions):
            raise ValueError("blank pages cannot also contain evidence")
        if len(set(self.risks)) != len(self.risks):
            raise ValueError("duplicate page risk")
        if self.regions_cover_all_risks and not self.regions:
            raise ValueError("complete region coverage requires regions")
        if len({region.as_tuple() for region in self.regions}) != len(self.regions):
            raise ValueError("duplicate region")
        return self


class RecognitionPlanRequestV1(EvidenceModel):
    purpose: Purpose
    source_kind: Literal["pdf", "image"] = "pdf"
    scope: Literal["targets", "pages", "document"]
    total_pages: int = Field(ge=1, le=10000)
    requested_pages: list[int] = Field(min_length=1, max_length=10000)
    observations: list[PageObservationV1] = Field(default_factory=list, max_length=10000)
    requested_targets: list[str] = Field(default_factory=list, max_length=256)
    unlocated_targets: list[str] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def valid_scope(self):
        if self.source_kind == "image" and (
            self.total_pages != 1 or self.requested_pages != [1]
            or any(page.native_char_count or page.verified_blank for page in self.observations)
        ):
            raise ValueError("images require one pixel page without native or blank proof")
        if len(set(self.requested_pages)) != len(self.requested_pages):
            raise ValueError("duplicate requested page")
        if any(page < 1 or page > self.total_pages for page in self.requested_pages):
            raise ValueError("requested page outside document")
        observed = [page.page_number for page in self.observations]
        if len(set(observed)) != len(observed) or not set(observed).issubset(self.requested_pages):
            raise ValueError("observations must uniquely belong to requested pages")
        if self.scope == "document" and set(self.requested_pages) != set(range(1, self.total_pages + 1)):
            raise ValueError("document scope requires every page")
        if len(set(self.requested_targets)) != len(self.requested_targets):
            raise ValueError("duplicate requested target")
        if not set(self.unlocated_targets).issubset(self.requested_targets):
            raise ValueError("unlocated targets must have been requested")
        if self.scope == "targets" and not self.requested_targets:
            raise ValueError("target scope requires target identities")
        return self


class PageDecisionV1(EvidenceModel):
    page_number: int = Field(ge=1, le=10000)
    action: Literal["native", "blank", "visual", "blocked", "deferred"]
    input_mode: VisualInput | None = None
    regions: list[NormalizedRegionV1] = Field(default_factory=list, max_length=24)
    reason_codes: list[Code] = Field(min_length=1, max_length=16)
    initial_calls: int = Field(default=0, ge=0, le=24)
    document_call_group: str | None = Field(default=None, pattern=r"^d\d{4}$")
    repair_allowed: bool = False
    # Native evidence is retained even when visual candidates disagree with it.
    retain_native: Literal[True] = True

    @model_validator(mode="after")
    def consistent_action(self):
        if len({region.as_tuple() for region in self.regions}) != len(self.regions):
            raise ValueError("call regions must be unique within a page")
        if self.action == "visual":
            if self.input_mode is None or not self.regions:
                raise ValueError("visual decisions require explicit call units")
            if self.document_call_group is not None:
                if self.input_mode != "document" or self.initial_calls not in {0, 1}:
                    raise ValueError("document group pages share one initial call")
            elif self.initial_calls != len(self.regions):
                raise ValueError("visual decisions require explicit call units")
        elif self.input_mode is not None or self.regions or self.initial_calls or self.repair_allowed or self.document_call_group:
            raise ValueError("non-visual decisions cannot issue calls")
        return self


class RecognitionPlanV1(EvidenceModel):
    contract: Literal["smartai.recognition.plan"] = "smartai.recognition.plan"
    schema_version: Literal[1] = 1
    policy_version: str
    policy: RecognitionPolicyV1
    purpose: Purpose
    source_kind: Literal["pdf", "image"] = "pdf"
    scope: Literal["targets", "pages", "document"]
    total_pages: int = Field(ge=1, le=10000)
    requested_pages: list[int] = Field(min_length=1, max_length=10000)
    requested_targets: list[str] = Field(default_factory=list, max_length=256)
    engine_capabilities: EngineCapabilitiesV1 | None
    route_id: str | None
    provider_fingerprint: str | None
    decisions: list[PageDecisionV1] = Field(max_length=10000)
    initial_calls: int = Field(ge=0)
    reserved_extra_calls: int = Field(ge=0)
    per_region_max_extra_calls: Literal[1] = 1
    unlocated_targets: list[str] = Field(default_factory=list)
    # A plan is not execution evidence, regardless of how many pages it covers.
    recognition_complete: Literal[False] = False
    # No profile has been calibrated yet; no numeric accuracy/cost is fabricated.
    strategy_status: Literal["uncalibrated"] = "uncalibrated"
    strategy: Literal["single_route_evidence_first"] = "single_route_evidence_first"

    @model_validator(mode="after")
    def consistent_summary(self):
        RecognitionPlanRequestV1(
            purpose=self.purpose, source_kind=self.source_kind, scope=self.scope, total_pages=self.total_pages,
            requested_pages=self.requested_pages, requested_targets=self.requested_targets,
            unlocated_targets=self.unlocated_targets,
        )
        if self.policy_version != self.policy.version:
            raise ValueError("policy version mismatch")
        if [page.page_number for page in self.decisions] != sorted(self.requested_pages):
            raise ValueError("plan must account for every requested page in order")
        if len({page.page_number for page in self.decisions}) != len(self.decisions):
            raise ValueError("duplicate page decision")
        if self.initial_calls != sum(page.initial_calls for page in self.decisions):
            raise ValueError("plan call total mismatch")
        if self.reserved_extra_calls > self.initial_calls:
            raise ValueError("at most one extra call per region")
        if self.initial_calls and (self.route_id is None or self.provider_fingerprint is None):
            raise ValueError("visual plan requires a selected route")
        engine = self.engine_capabilities
        if self.route_id != (engine.route_id if engine else None) or self.provider_fingerprint != (engine.fingerprint if engine else None):
            raise ValueError("route must match frozen capabilities")
        if self.initial_calls > self.policy.max_initial_calls or self.initial_calls + self.reserved_extra_calls > self.policy.max_calls:
            raise ValueError("plan exceeds call budget")
        if sum(page.action in {"native", "blank", "visual"} for page in self.decisions) > self.policy.max_detail_pages:
            raise ValueError("plan exceeds page budget")
        if sum(len(page.regions) for page in self.decisions) > self.policy.max_regions:
            raise ValueError("plan exceeds region budget")
        retry_limit = 0
        if engine and engine.response_recheck:
            retry_limit = self.policy.max_empty_recoveries
            if self.policy.enable_repair and engine.semantic_repair:
                retry_limit += self.policy.max_patches
        if self.reserved_extra_calls > retry_limit:
            raise ValueError("plan exceeds recheck capability or budget")
        groups: dict[str, list[PageDecisionV1]] = {}
        for page in self.decisions:
            if self.source_kind == "image" and (page.action in {"native", "blank"} or page.input_mode == "document"):
                raise ValueError("images cannot use PDF/native execution modes")
            if page.action != "visual":
                continue
            if engine is None or page.input_mode not in engine.visual_inputs:
                raise ValueError("plan requires unavailable input capability")
            if (page.input_mode == "document" or not engine.region_reads) and page.regions != [NormalizedRegionV1()]:
                raise ValueError("engine cannot read localized regions")
            if page.document_call_group:
                if not engine.document_batching:
                    raise ValueError("engine cannot batch document pages")
                groups.setdefault(page.document_call_group, []).append(page)
            elif engine.document_batching and page.input_mode == "document":
                raise ValueError("document batching requires a frozen group")
            if page.repair_allowed and not (
                engine.semantic_repair and engine.response_recheck and self.policy.enable_repair
                and self.policy.max_patches and self.reserved_extra_calls
            ):
                raise ValueError("plan cannot authorize unavailable repair")
        for pages in groups.values():
            if len(pages) > engine.max_document_pages or [page.initial_calls for page in pages] != [1] + [0] * (len(pages) - 1):
                raise ValueError("document group must charge one call and preserve page limits")
        return self


def _visual_reasons(page: PageObservationV1, purpose: Purpose, force: bool) -> list[str]:
    reasons = []
    if force:
        reasons.append("forced_visual")
    if page.native_quality != "clean":
        reasons.append("native_" + page.native_quality)
    # Student transcription must preserve marks/cross-outs that text layers omit.
    if purpose == "submissions":
        reasons.append("submission_fidelity")
    reasons.extend("risk_" + risk for risk in sorted(page.risks))
    return reasons


def plan_recognition(
    request: RecognitionPlanRequestV1,
    *,
    engine: EngineCapabilitiesV1 | None,
    policy: RecognitionPolicyV1 | None = None,
) -> RecognitionPlanV1:
    """Plan one bounded batch; deferred pages remain explicit for the next batch.

    Clean native prose costs no recognition call. Risky regions are used only
    with complete localization evidence; uncertain layouts require the full page.
    Knowledge ingestion uses the same primitives but never applies a target-only
    scope to a whole book. No provider or semantic companion is selected here.
    """
    policy = policy or RecognitionPolicyV1()
    observations = {page.page_number: page for page in request.observations}
    decisions: list[PageDecisionV1] = []
    used_pages = used_regions = calls = document_pages = 0
    mode = None
    if engine:
        preference = ("document", "page_image") if engine.document_batching else ("page_image", "document")
        mode = next((value for value in preference if value in engine.visual_inputs
                     and (request.source_kind == "pdf" or value == "page_image")), None)

    for number in sorted(request.requested_pages):
        page = observations.get(number)
        if page is None:
            decisions.append(PageDecisionV1(page_number=number, action="blocked", reason_codes=["page_not_indexed"]))
            continue
        if used_pages >= policy.max_detail_pages:
            decisions.append(PageDecisionV1(page_number=number, action="deferred", reason_codes=["batch_page_budget"]))
            continue
        reasons = _visual_reasons(page, request.purpose, policy.force_visual)
        economy_math = (request.purpose == "knowledge" and policy.version == "knowledge-economy-v1"
                        and page.native_quality == "clean" and page.risks == ["math"] and not policy.force_visual)
        if economy_math:
            reasons = []
        if page.verified_blank or not reasons:
            decisions.append(PageDecisionV1(
                page_number=number, action="blank" if page.verified_blank else "native",
                reason_codes=["verified_blank" if page.verified_blank else "knowledge_native_math_unverified"
                              if economy_math else "clean_native_text"],
            ))
            used_pages += 1
            continue
        if mode is None:
            decisions.append(PageDecisionV1(
                page_number=number, action="blocked", reason_codes=reasons + ["visual_capability_unavailable"],
            ))
            continue
        region_safe = (
            engine.region_reads and page.regions_cover_all_risks and page.native_quality == "clean"
            and "layout" not in page.risks and request.purpose != "submissions" and not policy.force_visual
            and (request.purpose != "knowledge" or policy.version != "knowledge-economy-v1"
                 or len(page.regions) <= min(policy.max_regions, policy.max_initial_calls))
        )
        regions = page.regions if region_safe else [NormalizedRegionV1()]
        regions_count = len(regions)
        group = None
        units = regions_count
        if mode == "document" and engine.document_batching:
            group = f"d{document_pages // engine.max_document_pages:04d}"
            units = int(document_pages % engine.max_document_pages == 0)
        if used_regions + regions_count > policy.max_regions or calls + units > min(policy.max_initial_calls, policy.max_calls):
            decisions.append(PageDecisionV1(page_number=number, action="deferred", reason_codes=reasons + ["batch_call_budget"]))
            continue
        decisions.append(PageDecisionV1(
            page_number=number, action="visual", input_mode=mode,
            regions=regions, reason_codes=reasons + ["localized_risks" if region_safe else "full_page_context"],
            initial_calls=units, document_call_group=group,
            repair_allowed=policy.enable_repair and policy.max_patches > 0 and engine.semantic_repair and engine.response_recheck,
        ))
        used_pages += 1
        used_regions += regions_count
        document_pages += int(group is not None)
        calls += units

    extra = 0
    if engine and engine.response_recheck:
        retry_budget = policy.max_empty_recoveries
        if policy.enable_repair and engine.semantic_repair:
            retry_budget += policy.max_patches
        extra = min(calls, retry_budget, policy.max_calls - calls)
    if not extra:
        for decision in decisions:
            decision.repair_allowed = False
    return RecognitionPlanV1(
        policy_version=policy.version, policy=policy.model_copy(deep=True), purpose=request.purpose,
        source_kind=request.source_kind,
        scope=request.scope, total_pages=request.total_pages, requested_pages=sorted(request.requested_pages),
        requested_targets=list(request.requested_targets),
        engine_capabilities=engine.model_copy(deep=True) if engine else None,
        route_id=engine.route_id if engine else None,
        provider_fingerprint=engine.fingerprint if engine else None,
        decisions=decisions, initial_calls=calls, reserved_extra_calls=extra,
        unlocated_targets=request.unlocated_targets,
    )
