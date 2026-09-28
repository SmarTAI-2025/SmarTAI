"""Pure eligibility selection, not live reservation or authorization to submit.

Only D1's one-to-one whole-page spans are aligned well enough for a recheck.
Selection cannot reconstruct a live budget, prove fidelity, or widen coverage.
"""
from __future__ import annotations

from typing import Literal, TYPE_CHECKING

from pydantic import Field, ValidationError, model_validator

from backend.domain.errors import RecognitionError
from backend.recognition.models import Code, EvidenceModel, NormalizedRegionV1
from backend.recognition.repair_response import RepairContextV1

if TYPE_CHECKING:
    from backend.recognition.fusion import RecognitionAssemblyV1

_ACTIONABLE = frozenset({
    "transcription_conflict", "source_form_uncertain", "replacement_character", "control_character",
    "packaging_unclosed_fence", "packaging_math_wrapper_uncertain",
})


class RepairTargetV1(EvidenceModel):
    unit_id: str = Field(strict=True, pattern=r"^u\d{4}$")
    kind: Literal["empty_recovery", "patch"]
    context: RepairContextV1 = Field(repr=False)


class RepairSkipV1(EvidenceModel):
    unit_id: str = Field(strict=True, pattern=r"^u\d{4}$")
    reason_code: Code


class RepairSelectionV1(EvidenceModel):
    contract: Literal["smartai.recognition.repair_selection"] = "smartai.recognition.repair_selection"
    schema_version: Literal[1] = 1
    targets: list[RepairTargetV1] = Field(default_factory=list, max_length=6)
    skipped: list[RepairSkipV1] = Field(default_factory=list, max_length=24)

    @model_validator(mode="after")
    def unique_units_and_regions(self):
        ids = [target.unit_id for target in self.targets] + [skip.unit_id for skip in self.skipped]
        if len(ids) > 24 or len(ids) != len(set(ids)):
            raise ValueError("every repair unit must have exactly one selection outcome")
        regions = [(target.context.page_number, target.context.region.as_tuple()) for target in self.targets]
        if len(regions) != len(set(regions)):
            raise ValueError("one region cannot receive multiple extra calls")
        if (sum(target.kind == "empty_recovery" for target in self.targets) > 2
                or sum(target.kind == "patch" for target in self.targets) > 4):
            raise ValueError("selection exceeds hard extra-call limits")
        return self


def _global_skip(initial):
    if initial.document is None:
        return "repair_document_unavailable"
    policy, caps = initial.raw.execution_policy, initial.raw.engine_capabilities
    if not policy.enable_repair:
        return "repair_disabled"
    if caps is None or not caps.semantic_repair or not caps.response_recheck or "page_image" not in caps.visual_inputs:
        return "repair_capability_unavailable"
    return None


def _target(unit, initial, pages, details, unaligned, incomplete_pages):
    candidate = unit.candidate
    if unit.submission_may_exist:
        return None, "repair_initial_pending"
    if candidate.safe_error_code is not None or candidate.status in {"error", "not_run"}:
        return None, "repair_initial_failed"
    if candidate.finish_reason == "refused" or "provider_refused" in candidate.warning_codes:
        return None, "repair_initial_refused"
    if candidate.finish_reason == "length" or "output_truncated" in candidate.warning_codes:
        return None, "repair_initial_truncated"
    if (len(unit.page_numbers) != 1 or unit.output_mapping != "single_region"
            or unit.region != NormalizedRegionV1() or unit.unit_id in unaligned):
        return None, "repair_unit_unaligned"
    number = unit.page_numbers[0]
    if number in incomplete_pages:
        return None, "repair_page_incomplete"
    page, detail = pages.get(number), details.get(number)
    if page is None or detail is None or len(page.spans) != 1:
        return None, "repair_span_unaligned"
    span = page.spans[0]
    native = detail.native_text if detail.native_text.strip() else ""
    if (page.native_text != detail.native_text or span.region != unit.region or span.visual != candidate
            or (native and (span.native is None or span.native.status != "ok" or span.native.text != native
                           or span.native_char_start != 0 or span.native_char_end != len(native)))
            or (not native and span.native is not None)
            or span.final_text not in {native, candidate.text}):
        return None, "repair_span_unaligned"
    if span.patch.attempt_count:
        return None, "repair_already_attempted"
    if candidate.status == "empty":
        kind = "empty_recovery"
    elif candidate.status == "ok" and _ACTIONABLE.intersection(span.issues):
        kind = "patch"
    else:
        return None, "repair_no_actionable_issue"
    if any(len(text) > 6000 for text in (native, candidate.text, span.final_text)):
        return None, "repair_context_too_large"
    try:
        context = RepairContextV1(
            purpose=initial.raw.request.purpose, span_id=span.span_id, page_number=number,
            region=unit.region, native_text=native, visual_text=candidate.text, before_text=span.final_text,
            issue_codes=list(span.issues),
        )
    except (ValidationError, ValueError):
        return None, "repair_context_invalid"
    return RepairTargetV1(unit_id=unit.unit_id, kind=kind, context=context), None


def select_repair_targets(initial: RecognitionAssemblyV1) -> RepairSelectionV1:
    """Freeze actual initial evidence and select bounded extras in unit order.

    Callers must retain the initial assembly and compare this selection when
    validating execution artifacts. It does not restore budget reservations or
    permit replay after restart, timeout, cancellation, or a route change.
    """
    # Local import keeps the pure selector usable by later Agent orchestration.
    from backend.recognition.fusion import RecognitionAssemblyV1

    try:
        if not isinstance(initial, RecognitionAssemblyV1) or getattr(initial, "repair_execution", None) is not None:
            raise ValueError
        assembly_type = RecognitionAssemblyV1
        if initial.schema_version == 2:
            from backend.recognition.workflow_v2 import RecognitionAssemblyV2
            assembly_type = RecognitionAssemblyV2
        initial = assembly_type.model_validate(initial.model_dump(warnings=False))
    except (ValidationError, ValueError, AttributeError, TypeError):
        raise RecognitionError("recognition_request_invalid") from None
    batch, document = initial.raw.read_batch, initial.document
    if batch is None:
        return RepairSelectionV1()
    global_skip = _global_skip(initial)
    if global_skip:
        return RepairSelectionV1(skipped=[RepairSkipV1(unit_id=unit.unit_id, reason_code=global_skip) for unit in batch.units])

    pages = {page.page_number: page for page in document.pages}
    details = {page.page_number: page for page in batch.pages}
    unaligned = {unit.unit_id for unit in document.unaligned_units}
    incomplete_pages = (set(batch.partial_pages)
                        | {number for unit in document.unaligned_units for number in unit.page_numbers}
                        | {region.page_number for region in batch.unprocessed_regions})
    policy = initial.raw.execution_policy
    ledger = initial.raw.logical_budget if initial.raw.schema_version == 2 else initial.raw.budget
    remaining = max(0, policy.max_calls - ledger.read_calls)
    counts = {"empty_recovery": 0, "patch": 0}
    limits = {"empty_recovery": policy.max_empty_recoveries, "patch": policy.max_patches}
    targets, skipped, selected_regions = [], [], set()
    for unit in batch.units:
        target, reason = _target(unit, initial, pages, details, unaligned, incomplete_pages)
        if target is not None:
            region = target.context.page_number, target.context.region.as_tuple()
            if region in selected_regions:
                reason = "repair_region_already_selected"
            elif len(targets) >= remaining:
                reason = "repair_read_call_limit"
            elif len(targets) >= 6:
                reason = "repair_extra_call_limit"
            elif counts[target.kind] >= limits[target.kind]:
                reason = "repair_" + target.kind + "_limit"
            else:
                targets.append(target)
                counts[target.kind] += 1
                selected_regions.add(region)
                continue
        skipped.append(RepairSkipV1(unit_id=unit.unit_id, reason_code=reason))
    return RepairSelectionV1(targets=targets, skipped=skipped)
