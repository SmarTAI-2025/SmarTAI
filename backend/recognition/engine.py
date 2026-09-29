"""Provider-neutral transcription port; adapters own provider-specific requests.

Payloads are ephemeral, not serializable evidence. Authorization, inspection,
budgets and cancellation are enforced by the executor before invoking this port.
No provider registry or credential discovery belongs in an engine adapter.
"""
from __future__ import annotations

from typing import Literal, Protocol

from pydantic import Field, model_validator

from backend.recognition.models import (
    EvidenceModel,
    NormalizedRegionV1,
    Purpose,
    RecognitionCandidateV1,
)
from backend.recognition.planner import EngineCapabilitiesV1, VisualInput


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
