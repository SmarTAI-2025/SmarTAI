"""Versioned evidence contracts; recognition never owns scoring identities."""
from __future__ import annotations

from typing import Annotated, Literal
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, model_validator

Purpose = Literal["problems", "reference", "submissions", "rubric", "test_cases", "knowledge"]
Confidence = Literal["high", "medium", "low"]
Code = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,119}$")]


class EvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, hide_input_in_errors=True)


def _formatting_payload(text: str) -> str:
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n")
    for opening, closing in (("$$", "$$"), ("\\(", "\\)"), ("\\[", "\\]"), ("$", "$")):
        if len(text) >= len(opening) + len(closing) and text.startswith(opening) and text.endswith(closing):
            inner = text[len(opening):-len(closing)]
            if opening.startswith("$"):
                return inner if "$" not in inner else text
            return inner if opening not in inner and closing not in inner else text
    return text


class NormalizedRegionV1(EvidenceModel):
    x0: float = Field(default=0, ge=0, le=1)
    y0: float = Field(default=0, ge=0, le=1)
    x1: float = Field(default=1, ge=0, le=1)
    y1: float = Field(default=1, ge=0, le=1)

    @model_validator(mode="after")
    def positive_area(self):
        if self.x0 >= self.x1 or self.y0 >= self.y1:
            raise ValueError("region must have positive area")
        return self

    def as_tuple(self) -> tuple[float, float, float, float]:
        return self.x0, self.y0, self.x1, self.y1


class RecognitionSourceRefV1(EvidenceModel):
    owner_id: str = Field(min_length=1, max_length=240)
    scope: Literal["assignment_source", "submission_source", "knowledge_document"]
    business_id: str = Field(min_length=1, max_length=240)
    stored_file_id: str | None = Field(default=None, max_length=240)
    original_name: str = Field(default="upload", max_length=512)
    content_type: str = Field(max_length=100)
    input_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class RecognitionCandidateV1(EvidenceModel):
    kind: Literal["native", "vision", "ocr"]
    status: Literal["not_run", "ok", "empty", "error"]
    text: str = Field(default="", max_length=400_000)
    normalized_text: str | None = Field(default=None, max_length=400_000)
    provider_route_id: str | None = Field(default=None, max_length=240)
    model: str | None = Field(default=None, max_length=240)
    duration_ms: float | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    finish_reason: Literal["stop", "length", "refused", "unknown"] | None = None
    warning_codes: list[Code] = Field(default_factory=list, max_length=32)
    safe_error_code: Code | None = None

    @model_validator(mode="after")
    def error_is_not_content(self):
        if self.status in {"error", "not_run", "empty"} and (self.text or self.normalized_text is not None):
            raise ValueError("failed candidates cannot contain response bodies")
        if self.status == "ok" and not self.text.strip():
            raise ValueError("successful candidates require content")
        if self.normalized_text is not None and _formatting_payload(self.text) != _formatting_payload(self.normalized_text):
            raise ValueError("normalization cannot rewrite transcription content")
        return self


class RecognitionPatchV1(EvidenceModel):
    status: Literal["not_needed", "not_available", "applied", "rejected", "still_unknown"] = "not_needed"
    attempt_count: int = Field(default=0, ge=0, le=1)
    decision: Literal["keep_native", "keep_visual", "replace", "still_unknown", "none"] = "none"
    before_text: str = Field(default="", max_length=400_000)
    proposed_text: str = Field(default="", max_length=8000)
    final_text: str = Field(default="", max_length=400_000)
    unresolved_codes: list[Code] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def valid_attempt(self):
        if self.status in {"not_needed", "not_available"}:
            if self.attempt_count or self.decision != "none" or self.proposed_text:
                raise ValueError("an unattempted patch cannot contain a decision")
        elif self.attempt_count != 1:
            raise ValueError("patch result requires one attempt")
        if self.status == "applied" and (self.decision in {"none", "still_unknown"} or not self.final_text.strip()):
            raise ValueError("applied patch requires a final candidate")
        return self


class RecognitionSpanV1(EvidenceModel):
    span_id: str = Field(pattern=r"^p\d{4,6}-s\d{4,6}$")
    order_index: int = Field(ge=0)
    kind: Literal["question", "paragraph", "formula", "table", "code", "identity", "other"] = "other"
    question_id_hint: str | None = Field(default=None, max_length=120)
    subpart_label_hint: str | None = Field(default=None, max_length=120)
    region: NormalizedRegionV1 = Field(default_factory=NormalizedRegionV1)
    native_char_start: int | None = Field(default=None, ge=0)
    native_char_end: int | None = Field(default=None, ge=0)
    native: RecognitionCandidateV1 | None = None
    visual: RecognitionCandidateV1 | None = None
    final_text: str = Field(default="", max_length=400_000)
    adopted_from: Literal["native", "vision", "ocr", "fused", "none"] = "none"
    confidence: Confidence = "low"
    confidence_reasons: list[Code] = Field(default_factory=list, max_length=32)
    issues: list[Code] = Field(default_factory=list, max_length=32)
    patch: RecognitionPatchV1 = Field(default_factory=RecognitionPatchV1)

    @model_validator(mode="after")
    def valid_evidence(self):
        if not self.final_text.strip() and (self.adopted_from != "none" or self.confidence != "low"):
            raise ValueError("empty spans must remain unconfirmed")
        if self.final_text.strip() and self.adopted_from == "none":
            raise ValueError("content requires attribution")
        if self.native is not None and self.native.kind != "native":
            raise ValueError("native slot requires native evidence")
        if self.visual is not None and self.visual.kind not in {"vision", "ocr"}:
            raise ValueError("visual slot requires visual evidence")
        if self.adopted_from == "native" and (self.native is None or self.native.status != "ok"):
            raise ValueError("adopted native content requires evidence")
        if self.adopted_from in {"vision", "ocr"} and (
            self.visual is None or self.visual.kind != self.adopted_from or self.visual.status != "ok"
        ):
            raise ValueError("adopted visual content requires evidence")
        if self.adopted_from == "fused" and (
            self.native is None or self.visual is None or self.native.status != "ok" or self.visual.status != "ok"
        ):
            raise ValueError("fusion requires both candidates")
        candidates = []
        if self.adopted_from in {"native", "fused"}:
            candidates.append(self.native)
        if self.adopted_from in {"vision", "ocr", "fused"}:
            candidates.append(self.visual)
        supported_texts = {
            text for candidate in candidates if candidate is not None
            for text in (candidate.text, candidate.normalized_text) if text is not None
        }
        if self.patch.status == "applied":
            if self.patch.final_text != self.final_text:
                raise ValueError("patch must explain adopted text")
            if self.patch.decision == "replace" and self.patch.final_text != self.patch.proposed_text:
                raise ValueError("replacement must match the recorded proposal")
            if self.patch.decision == "keep_native":
                candidate = self.native
            elif self.patch.decision == "keep_visual":
                candidate = self.visual
            else:
                candidate = None
            if self.patch.decision in {"keep_native", "keep_visual"} and (
                candidate is None or self.final_text not in {candidate.text, candidate.normalized_text}
            ):
                raise ValueError("keep decision must preserve that candidate")
        elif self.final_text and self.final_text not in supported_texts:
            raise ValueError("span text must retain a candidate or an explicit applied patch")
        if (self.native_char_start is None) != (self.native_char_end is None):
            raise ValueError("native offsets must occur together")
        if self.native_char_start is not None and self.native_char_end < self.native_char_start:
            raise ValueError("invalid native interval")
        return self


class RecognitionPageV1(EvidenceModel):
    page_index: int = Field(ge=0, le=9999)
    page_number: int = Field(ge=1, le=10000)
    geometry_unit: Literal["points", "pixels"] = "points"
    width_points: float | None = Field(default=None, gt=0)
    height_points: float | None = Field(default=None, gt=0)
    width_pixels: int | None = Field(default=None, strict=True, gt=0)
    height_pixels: int | None = Field(default=None, strict=True, gt=0)
    native_text: str = Field(default="", max_length=500_000)
    verified_blank: bool = False
    selected_for_visual: bool = False
    selection_reasons: list[Code] = Field(default_factory=list, max_length=32)
    spans: list[RecognitionSpanV1] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def page_identity(self):
        if self.geometry_unit == "points":
            if self.width_points is None or self.height_points is None or self.width_pixels is not None or self.height_pixels is not None:
                raise ValueError("PDF pages require physical geometry only")
        elif self.width_pixels is None or self.height_pixels is None or self.width_points is not None or self.height_points is not None:
            raise ValueError("image pages require pixel geometry only")
        if self.page_number != self.page_index + 1:
            raise ValueError("page number/index mismatch")
        if len({span.span_id for span in self.spans}) != len(self.spans):
            raise ValueError("duplicate span")
        if self.verified_blank and (self.native_text or self.spans):
            raise ValueError("blank pages cannot contain evidence")
        if [span.order_index for span in self.spans] != sorted({span.order_index for span in self.spans}):
            raise ValueError("spans must have unique ascending order")
        for span in self.spans:
            if int(span.span_id.split("-")[0][1:]) != self.page_number:
                raise ValueError("span belongs to another page")
            if span.native_char_end is not None and span.native_char_end > len(self.native_text):
                raise ValueError("native offset exceeds page")
            if span.native_char_start is not None and span.native is not None and span.native.status == "ok":
                if span.native.text != self.native_text[span.native_char_start:span.native_char_end]:
                    raise ValueError("native candidate must match its source interval")
        return self


class RecognitionCoverageV1(EvidenceModel):
    scope: Literal["targets", "pages", "document"]
    total_pages: int = Field(ge=1, le=10000)
    requested_pages: list[int] = Field(default_factory=list, max_length=10000)
    processed_pages: list[int] = Field(default_factory=list, max_length=10000)
    failed_pages: list[int] = Field(default_factory=list, max_length=10000)
    unprocessed_pages: list[int] = Field(default_factory=list, max_length=10000)
    requested_targets: list[str] = Field(default_factory=list, max_length=256)
    missing_targets: list[str] = Field(default_factory=list, max_length=256)
    unverified_targets: list[str] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def disjoint_partition(self):
        names = ("requested_pages", "processed_pages", "failed_pages", "unprocessed_pages")
        for name in names:
            pages = getattr(self, name)
            if len(pages) != len(set(pages)) or any(p < 1 or p > self.total_pages for p in pages):
                raise ValueError("invalid coverage pages")
        states = self.processed_pages + self.failed_pages + self.unprocessed_pages
        if len(states) != len(set(states)) or set(states) != set(self.requested_pages):
            raise ValueError("coverage states must partition requested pages")
        if self.scope == "document" and set(self.requested_pages) != set(range(1, self.total_pages + 1)):
            raise ValueError("document scope requires every page")
        for targets in (self.missing_targets, self.unverified_targets):
            if len(targets) != len(set(targets)) or not set(targets).issubset(self.requested_targets):
                raise ValueError("invalid unresolved targets")
        if self.scope == "targets" and not self.requested_targets:
            raise ValueError("target scope requires target identities")
        if len(set(self.requested_targets)) != len(self.requested_targets):
            raise ValueError("duplicate requested target")
        return self

    @property
    def complete(self) -> bool:
        return bool(self.requested_pages) and not (
            self.failed_pages or self.unprocessed_pages or self.missing_targets or self.unverified_targets
        )


class RecognitionUsageV1(EvidenceModel):
    locator_calls: int = Field(default=0, ge=0)
    initial_calls: int = Field(default=0, ge=0)
    empty_recovery_calls: int = Field(default=0, ge=0)
    patch_calls: int = Field(default=0, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    duration_ms: float = Field(default=0, ge=0)
    usage_complete: bool = True
    cache_hits: int = Field(default=0, ge=0)
    cache_misses: int = Field(default=0, ge=0)

    @property
    def total_calls(self) -> int:
        return self.locator_calls + self.initial_calls + self.empty_recovery_calls + self.patch_calls

    @model_validator(mode="after")
    def unknown_usage_is_not_zero(self):
        if self.total_calls and self.usage_complete and (self.input_tokens is None or self.output_tokens is None):
            raise ValueError("missing token usage must be marked incomplete")
        return self


class RecognitionPolicyV1(EvidenceModel):
    version: str = "faithful-v2"
    max_locator_calls: int = Field(default=12, ge=0, le=12)
    locator_seconds: float = Field(default=360, gt=0, le=360)
    read_seconds: float = Field(default=600, gt=0, le=600)
    max_detail_pages: int = Field(default=24, ge=1, le=24)
    max_regions: int = Field(default=24, ge=1, le=24)
    max_initial_calls: int = Field(default=12, ge=0, le=12)
    max_calls: int = Field(default=18, ge=0, le=18)
    max_empty_recoveries: int = Field(default=2, ge=0, le=2)
    max_patches: int = Field(default=4, ge=0, le=4)
    per_call_seconds: float = Field(default=120, gt=0, le=120)
    total_seconds: float = Field(default=600, gt=0, le=900)
    max_output_tokens: int = Field(default=32768, ge=1, le=32768)
    enable_repair: bool = True
    force_visual: bool = False


class RecognitionUnalignedUnitV1(EvidenceModel):
    """Raw visual output whose input mapping does not prove text placement."""

    unit_id: str = Field(pattern=r"^u\d{4}$")
    page_numbers: list[int] = Field(min_length=1, max_length=24)
    region: NormalizedRegionV1
    candidate: RecognitionCandidateV1
    reason: Literal["document_page_alignment_unverified", "region_composition_unverified"]
    submission_may_exist: bool = False

    @model_validator(mode="after")
    def valid_scope(self):
        if self.page_numbers != sorted(set(self.page_numbers)) or any(not 1 <= p <= 10000 for p in self.page_numbers):
            raise ValueError("invalid unaligned unit pages")
        if self.candidate.kind == "native":
            raise ValueError("unaligned units require visual evidence")
        if self.reason == "document_page_alignment_unverified" and len(self.page_numbers) < 2:
            raise ValueError("document alignment requires multiple pages")
        return self


class RecognitionDocumentV1(EvidenceModel):
    contract: Literal["smartai.recognition.document"] = "smartai.recognition.document"
    schema_version: Literal[1] = 1
    policy_version: str = Field(max_length=120)
    prompt_version: str = Field(max_length=120)
    source: RecognitionSourceRefV1
    purpose: Purpose
    provider_fingerprint: str = Field(min_length=1, max_length=256)
    pages: list[RecognitionPageV1] = Field(default_factory=list, max_length=24)
    unaligned_units: list[RecognitionUnalignedUnitV1] = Field(default_factory=list, max_length=24)
    final_markdown: str = Field(default="", max_length=400_000)
    coverage: RecognitionCoverageV1
    confidence: Confidence = "low"
    confidence_reasons: list[Code] = Field(default_factory=list, max_length=32)
    warning_codes: list[Code] = Field(default_factory=list, max_length=64)
    usage: RecognitionUsageV1 = Field(default_factory=RecognitionUsageV1)
    result_cache_key: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def evidence_agrees_with_summary(self):
        numbers = [page.page_number for page in self.pages]
        if numbers != sorted(set(numbers)):
            raise ValueError("pages must be unique and ascending")
        if not set(self.coverage.processed_pages).issubset(numbers):
            raise ValueError("processed pages need evidence")
        if not set(numbers).issubset(self.coverage.requested_pages):
            raise ValueError("evidence belongs outside requested scope")
        if len({unit.unit_id for unit in self.unaligned_units}) != len(self.unaligned_units):
            raise ValueError("duplicate unaligned evidence")
        for unit in self.unaligned_units:
            if not set(unit.page_numbers).issubset(numbers) or set(unit.page_numbers) & set(self.coverage.processed_pages):
                raise ValueError("unaligned output cannot prove processed pages")
        if self.unaligned_units and self.confidence != "low":
            raise ValueError("unaligned evidence requires review")
        for page in self.pages:
            if page.page_number in self.coverage.processed_pages and not (page.spans or page.verified_blank):
                raise ValueError("processed pages require spans or verified blank evidence")
        if not self.coverage.complete and self.confidence != "low":
            raise ValueError("incomplete results must remain unconfirmed")
        ranks = {"low": 0, "medium": 1, "high": 2}
        if any(ranks[span.confidence] < ranks[self.confidence] for page in self.pages for span in page.spans):
            raise ValueError("unconfirmed spans must not become confirmed documents")
        expected_text = "\n\n".join(
            span.final_text for page in self.pages for span in page.spans if span.final_text
        )
        if self.final_markdown != expected_text:
            raise ValueError("document text must faithfully render its ordered spans")
        return self
