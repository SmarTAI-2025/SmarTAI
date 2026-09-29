"""Deterministic, lossless evidence assembly; no model, solver, retry or cache I/O.

Whole-page candidates have a common input scope and can be compared directly.
Document-only and cropped output remains independent until a later authorized
alignment/repair stage proves its placement. Native text is never concatenated
with overlapping visual output merely to make the result look complete.
"""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from backend.agents.recognition_agent import RecognitionWorkflowReadV1
from backend.domain.errors import RecognitionError
from backend.recognition.executor import ImageDetailPageV1
from backend.recognition.models import (
    Code, EvidenceModel, NormalizedRegionV1, RecognitionCandidateV1,
    RecognitionCoverageV1, RecognitionDocumentV1, RecognitionPageV1,
    RecognitionSpanV1, RecognitionUnalignedUnitV1, RecognitionUsageV1,
)
from backend.recognition.quality import assess_candidates

ASSEMBLY_VERSION = "faithful-assembly-v1"


def _codes(values, limit=32):
    # These are fixed diagnostic codes, never source text or provider errors.
    unique = list(dict.fromkeys(values))
    return unique if len(unique) <= limit else unique[:limit - 1] + ["additional_diagnostics_omitted"]


def _usable(candidate):
    return (candidate is not None and candidate.status == "ok" and candidate.safe_error_code is None
            and candidate.finish_reason != "refused" and "provider_refused" not in candidate.warning_codes)


def _failed(unit):
    candidate = unit.candidate
    return (unit.submission_may_exist or not _usable(candidate) or candidate.finish_reason == "length"
            or "output_truncated" in candidate.warning_codes)


def _identity(raw, prompt_version):
    value = {"assembly_version": ASSEMBLY_VERSION, "request": raw.request.model_dump(mode="json"),
             "execution_policy": raw.execution_policy.model_dump(mode="json"),
             "engine": raw.engine_capabilities.model_dump(mode="json") if raw.engine_capabilities else None,
             "prompt_version": prompt_version}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def _native(text):
    return RecognitionCandidateV1(kind="native", status="ok", text=text) if text.strip() else None


def _span(page, native, visual, purpose, *, partial=False):
    assessment = assess_candidates(native, visual, purpose=purpose, page_risks=tuple(page.observation.risks), partial=partial)
    adopted = native if assessment.preferred == "native" else visual if assessment.preferred == "visual" else None
    text = adopted.text if adopted is not None else ""
    return RecognitionSpanV1(
        span_id=f"p{page.page_number:04d}-s0001", order_index=0,
        native_char_start=0 if native is not None else None,
        native_char_end=len(page.native_text) if native is not None else None,
        native=native, visual=visual, final_text=text, adopted_from=adopted.kind if text else "none",
        confidence=assessment.confidence if text else "low", confidence_reasons=list(assessment.reasons),
        issues=list(assessment.issues),
    )


def _build_document(raw: RecognitionWorkflowReadV1, prompt_version: str):
    if raw.total_pages is None:
        return None, "source_extent_unavailable"
    request, batch = raw.request, raw.read_batch
    if request.scope == "document":
        requested = list(range(1, raw.total_pages + 1))
    elif request.scope == "pages":
        requested = request.pages
    else:
        requested = raw.selected_pages

    # Localization detail is useful fallback evidence, not proof that a visual
    # read succeeded. Retain only pages in the requested transcription scope.
    details = {page.page_number: page for page in raw.native_details if page.page_number in requested}
    if batch:
        details.update({page.page_number: page for page in batch.pages})
    if len(details) > 24 or any(len(page.native_text) > 400_000 for page in details.values()):
        return None, "recognition_assembly_limit"
    units = batch.units if batch else []
    grouped = [unit for unit in units if len(unit.page_numbers) != 1 or unit.output_mapping != "single_region"
               or unit.region != NormalizedRegionV1()]
    unaligned = [RecognitionUnalignedUnitV1(
        unit_id=unit.unit_id, page_numbers=unit.page_numbers, region=unit.region, candidate=unit.candidate,
        submission_may_exist=unit.submission_may_exist,
        reason="document_page_alignment_unverified" if len(unit.page_numbers) > 1 else "region_composition_unverified",
    ) for unit in grouped]
    grouped_pages = {p for unit in grouped for p in unit.page_numbers}
    single = {unit.page_numbers[0]: unit for unit in units if unit not in grouped}
    decisions = {page.page_number: page for page in batch.plan.decisions} if batch else {}
    failed = set(batch.failed_pages if batch else []) | {p for unit in units if _failed(unit) for p in unit.page_numbers}
    partial = set(batch.partial_pages if batch else [])
    processed, pages = [], []
    warnings = list(raw.stop_codes)
    if grouped:
        warnings.extend(unit.reason for unit in unaligned)
    for number, detail in sorted(details.items()):
        decision, unit = decisions.get(number), single.get(number)
        native = _native(detail.native_text)
        blank = bool(detail.observation.verified_blank and not any(number in item.page_numbers for item in units))
        incomplete = number in partial or number in grouped_pages or number in failed or decision is None or decision.action in {"blocked", "deferred"}
        spans = [] if blank else [_span(detail, native, unit.candidate if unit else None, request.purpose, partial=incomplete)]
        if isinstance(detail, ImageDetailPageV1):
            geometry = {"geometry_unit": "pixels", "width_pixels": detail.width_pixels, "height_pixels": detail.height_pixels}
        else:
            geometry = {"width_points": detail.width_points, "height_points": detail.height_points}
        page = RecognitionPageV1(
            page_index=number - 1, page_number=number, **geometry, native_text=detail.native_text,
            verified_blank=blank, selected_for_visual=bool(decision and decision.action == "visual"),
            selection_reasons=decision.reason_codes if decision else ["localization_evidence_only"], spans=spans,
        )
        pages.append(page)
        if incomplete:
            continue
        if blank or (decision.action == "native" and native is not None) or (unit is not None and _usable(unit.candidate)):
            processed.append(number)
    failed &= set(requested)
    processed = sorted(set(processed) - failed)
    coverage = RecognitionCoverageV1(
        scope=request.scope, total_pages=raw.total_pages, requested_pages=requested, processed_pages=processed,
        failed_pages=sorted(failed), unprocessed_pages=sorted(set(requested) - failed - set(processed)),
        requested_targets=request.targets, missing_targets=raw.unlocated_targets,
        # Locating a label/page does not yet establish the complete problem body.
        unverified_targets=[target for target in request.targets if target not in raw.unlocated_targets],
    )
    final_text = "\n\n".join(span.final_text for page in pages for span in page.spans if span.final_text)
    if len(final_text) > 400_000:
        return None, "recognition_assembly_limit"
    low = not coverage.complete or any(span.confidence == "low" for page in pages for span in page.spans)
    reasons = ["quality_uncalibrated", "coverage_not_semantic_accuracy"]
    if not coverage.complete:
        reasons.append("coverage_incomplete")
    if request.targets:
        reasons.append("target_content_unverified")
    if raw.budget.pending_calls:
        low = True
        warnings.append("provider_submission_pending")
    usage = RecognitionUsageV1(
        locator_calls=raw.budget.locator_calls, initial_calls=raw.budget.initial_calls,
        empty_recovery_calls=raw.budget.empty_recovery_calls, patch_calls=raw.budget.patch_calls,
        input_tokens=raw.budget.input_tokens, output_tokens=raw.budget.output_tokens,
        duration_ms=raw.budget.duration_ms, usage_complete=raw.budget.usage_complete,
    )
    return RecognitionDocumentV1(
        policy_version=raw.execution_policy.version, prompt_version=prompt_version, source=request.source,
        purpose=request.purpose, provider_fingerprint=raw.engine_capabilities.fingerprint if raw.engine_capabilities else "native-only",
        pages=pages, unaligned_units=unaligned, final_markdown=final_text, coverage=coverage,
        confidence="low" if low else "medium", confidence_reasons=_codes(reasons), warning_codes=_codes(warnings, 64),
        usage=usage, result_cache_key=_identity(raw, prompt_version),
    ), None


class RecognitionAssemblyV1(EvidenceModel):
    contract: Literal["smartai.recognition.assembly"] = "smartai.recognition.assembly"
    schema_version: Literal[1] = 1
    assembly_version: Literal["faithful-assembly-v1"] = ASSEMBLY_VERSION
    prompt_version: str = Field(min_length=1, max_length=120)
    raw: RecognitionWorkflowReadV1 = Field(repr=False)
    document: RecognitionDocumentV1 | None
    safe_error_code: Code | None = None
    recognition_complete: Literal[False] = False

    @model_validator(mode="after")
    def exact_derivation(self):
        self.raw = RecognitionWorkflowReadV1.model_validate(self.raw.model_dump(warnings=False))
        expected, error = _build_document(self.raw, self.prompt_version)
        if self.document != expected or self.safe_error_code != error:
            raise ValueError("assembled output must derive from its complete raw evidence")
        return self


def assemble_recognition(raw: RecognitionWorkflowReadV1, *, prompt_version: str) -> RecognitionAssemblyV1:
    """Retain every raw outcome, even when a bounded document cannot be rendered.

    A digest is a future cache identity, not evidence of a hit or permission to
    persist/reuse the result. The caller supplies the actual adapter prompt
    version. Cached artifacts must later be checked against authorized originals.
    """
    try:
        frozen = RecognitionWorkflowReadV1.model_validate(raw.model_dump(warnings=False))
        document, error = _build_document(frozen, prompt_version)
        return RecognitionAssemblyV1(raw=frozen, prompt_version=prompt_version, document=document, safe_error_code=error)
    except (ValidationError, AttributeError, TypeError):
        raise RecognitionError("recognition_response_invalid") from None
