"""Provider-neutral transcription port; adapters own provider-specific requests.

Payloads are ephemeral, not serializable evidence. Authorization, inspection,
budgets and cancellation are enforced by the executor before invoking this port.
No provider registry or credential discovery belongs in an engine adapter.
"""
from __future__ import annotations

from typing import Annotated, Literal, Protocol

from pydantic import Field, ValidationError, model_validator

from backend.domain.errors import RecognitionError

from backend.recognition.models import (
    EvidenceModel,
    NormalizedRegionV1,
    Purpose,
    RecognitionCandidateV1,
)
from backend.recognition.planner import EngineCapabilitiesV1, VisualInput
from backend.recognition.repair_response import RepairContextV1


class EngineReadInputV1(EvidenceModel):
    purpose: Purpose
    input_mode: VisualInput
    page_number: int = Field(ge=1, le=10000)
    region: NormalizedRegionV1 = Field(default_factory=NormalizedRegionV1)
    content_type: Literal["application/pdf", "image/png", "image/jpeg", "image/webp"]
    payload: bytes = Field(min_length=1, max_length=10 * 1024 * 1024, repr=False, exclude=True)
    max_output_tokens: int = Field(default=4096, ge=1, le=32768)
    # Input mapping, not a claim that the returned Markdown has page boundaries.
    document_pages: list[int] = Field(default_factory=list, max_length=24)

    @model_validator(mode="after")
    def input_shape(self):
        if (self.input_mode == "document") != (self.content_type == "application/pdf"):
            raise ValueError("input mode and media type disagree")
        if self.input_mode == "document" and self.region != NormalizedRegionV1():
            raise ValueError("document input must represent a full mapped page")
        if self.document_pages and (
            self.input_mode != "document" or self.document_pages != sorted(set(self.document_pages))
            or self.document_pages[0] != self.page_number
            or any(type(page) is not int or not 1 <= page <= 10000 for page in self.document_pages)
        ):
            raise ValueError("document pages must preserve ordered original page identities")
        return self


class RecognitionEngine(Protocol):
    @property
    def capabilities(self) -> EngineCapabilitiesV1: ...

    async def recognize(self, request: EngineReadInputV1) -> RecognitionCandidateV1:
        """Transcribe one inspected call unit without solving or structuring it.

        Preserve the raw response as a candidate. Return empty explicitly; raise
        classified transport errors instead of embedding response bodies in text.
        This method must not fall back, repair, retry or invoke another engine.
        """
        ...


class EngineRepairInputV1(EngineReadInputV1):
    input_mode: Literal["page_image"] = "page_image"
    max_output_tokens: int = Field(default=2048, strict=True, ge=1, le=2048)
    repair_context: RepairContextV1

    @model_validator(mode="after")
    def repair_scope(self):
        context = self.repair_context
        if self.document_pages or self.purpose != context.purpose or self.page_number != context.page_number:
            raise ValueError("repair must refer to one original page and purpose")
        if not (self.region.x0 <= context.region.x0 < context.region.x1 <= self.region.x1
                and self.region.y0 <= context.region.y0 < context.region.y1 <= self.region.y1):
            raise ValueError("repair image must include its complete target region")
        return self


class SemanticRepairEngine(RecognitionEngine, Protocol):
    async def repair(self, request: EngineRepairInputV1) -> RecognitionCandidateV1:
        """One explicit image recheck; never solve, score, loop or use another engine."""
        ...


class LocatorImageV1(EvidenceModel):
    page_numbers: list[Annotated[int, Field(strict=True, ge=1, le=10000)]] = Field(min_length=1, max_length=8)
    payload: bytes = Field(min_length=1, max_length=10 * 1024 * 1024, repr=False, exclude=True)
    content_type: Literal["image/png"] = "image/png"

    @model_validator(mode="after")
    def ordered_pages(self):
        if self.page_numbers != sorted(set(self.page_numbers)):
            raise ValueError("locator images require unique ordered source page labels")
        return self


class EngineLocateInputV1(EvidenceModel):
    purpose: Purpose
    input_mode: Literal["page_image"] = "page_image"
    targets: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(min_length=1, max_length=64)
    images: list[LocatorImageV1] = Field(min_length=1, max_length=2)
    max_output_tokens: int = Field(default=4096, ge=1, le=32768)

    @model_validator(mode="after")
    def bounded_scope(self):
        if len(self.targets) != len(set(self.targets)) or any(
            not target.strip() or any(char in target for char in "\r\n\x00") for target in self.targets
        ):
            raise ValueError("locator targets must be unique single-line identifiers")
        pages = [number for image in self.images for number in image.page_numbers]
        if len(pages) != len(set(pages)):
            raise ValueError("locator sheets cannot repeat source pages")
        return self


def freeze_engine_input(request: EngineReadInputV1 | EngineLocateInputV1 | EngineRepairInputV1):
    """Revalidate a mutable input, retaining only its explicit ephemeral payloads."""
    try:
        if isinstance(request, EngineRepairInputV1):
            return EngineRepairInputV1.model_validate({**request.model_dump(warnings=False), "payload": request.payload})
        if isinstance(request, EngineReadInputV1):
            return EngineReadInputV1.model_validate({**request.model_dump(warnings=False), "payload": request.payload})
        if isinstance(request, EngineLocateInputV1):
            return EngineLocateInputV1.model_validate({
                **request.model_dump(warnings=False),
                "images": [{**image.model_dump(warnings=False), "payload": image.payload} for image in request.images],
            })
    except (ValidationError, AttributeError, TypeError):
        pass
    raise RecognitionError("recognition_request_invalid") from None


class TargetLocatorEngine(RecognitionEngine, Protocol):
    async def locate(self, request: EngineLocateInputV1) -> RecognitionCandidateV1:
        """Return a location candidate over supplied sheets only; never transcribe from it."""
        ...
