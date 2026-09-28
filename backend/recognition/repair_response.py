"""Strict, bounded repair proposals; parsing never proves source fidelity.

Original mistakes, code and instructions remain untrusted transcription data.
This boundary cannot infer whether newly recovered words were visible in an
image. A replacement therefore remains unverified and requires final review.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from backend.domain.errors import RecognitionError
from backend.recognition.models import (
    Code, EvidenceModel, NormalizedRegionV1, Purpose, RecognitionCandidateV1,
)

MAX_RESPONSE_CHARS = 8000
MAX_RESPONSE_BYTES = 32 * 1024
ParseStatus = Literal["ok", "invalid", "empty", "error", "refused", "truncated"]
RepairDecision = Literal["keep_native", "keep_visual", "replace", "still_unknown"]
_JSON_FENCE = re.compile(r"```json[ \t]*\r?\n(?P<body>.*)\r?\n```", re.DOTALL)


class RepairContextV1(EvidenceModel):
    purpose: Purpose
    span_id: str = Field(strict=True, pattern=r"^p\d{4,6}-s\d{4,6}$")
    page_number: int = Field(strict=True, ge=1, le=10000)
    region: NormalizedRegionV1
    native_text: str = Field(strict=True, max_length=6000, repr=False)
    visual_text: str = Field(strict=True, max_length=6000, repr=False)
    before_text: str = Field(strict=True, max_length=6000, repr=False)
    issue_codes: list[Code] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def preserve_source_context(self):
        if int(self.span_id.split("-")[0][1:]) != self.page_number:
            raise ValueError("repair span belongs to another page")
        if self.before_text:
            if not self.before_text.strip() or self.before_text not in {self.native_text, self.visual_text}:
                raise ValueError("before text must preserve a nonempty raw candidate")
        elif self.native_text or self.visual_text:
            raise ValueError("empty before text requires empty candidates")
        for text in (self.native_text, self.visual_text, self.before_text):
            text.encode("utf-8")
        return self


class _Response(EvidenceModel):
    decision: RepairDecision
    text: str = Field(strict=True, max_length=8000)
    source_evidence: str = Field(strict=True, max_length=240)

    @model_validator(mode="after")
    def valid_encoding(self):
        self.text.encode("utf-8")
        self.source_evidence.encode("utf-8")
        return self


@dataclass(frozen=True)
class _Parsed:
    status: ParseStatus
    decision: RepairDecision
    proposed_text: str
    final_text: str
    source_evidence: str
    reasons: list[str]


def _rejected(context: RepairContextV1, status: ParseStatus, reason: str) -> _Parsed:
    return _Parsed(status, "still_unknown", "", context.before_text, "", [reason])


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("nonfinite JSON constant")


def _interpret(candidate: RecognitionCandidateV1, context: RepairContextV1) -> _Parsed:
    if candidate.finish_reason == "refused" or "provider_refused" in candidate.warning_codes:
        return _rejected(context, "refused", "repair_response_refused")
    if candidate.finish_reason == "length" or "output_truncated" in candidate.warning_codes:
        return _rejected(context, "truncated", "repair_response_truncated")
    if candidate.status in {"error", "not_run"} or candidate.safe_error_code is not None:
        reason = "repair_response_not_run" if candidate.status == "not_run" else "repair_response_error"
        return _rejected(context, "error", reason)
    if candidate.status == "empty":
        return _rejected(context, "empty", "repair_response_empty")
    try:
        size = len(candidate.text.encode("utf-8"))
    except UnicodeEncodeError:
        return _rejected(context, "invalid", "repair_response_invalid_encoding")
    if len(candidate.text) > MAX_RESPONSE_CHARS or size > MAX_RESPONSE_BYTES:
        return _rejected(context, "invalid", "repair_response_too_large")
    text = candidate.text.strip()
    fence = _JSON_FENCE.fullmatch(text)
    if fence:
        text = fence.group("body")
    try:
        payload = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return _rejected(context, "invalid", "repair_json_invalid")
    try:
        response = _Response.model_validate(payload)
    except (ValidationError, ValueError):
        return _rejected(context, "invalid", "repair_schema_invalid")

    if response.decision in {"keep_native", "keep_visual"}:
        original = context.native_text if response.decision == "keep_native" else context.visual_text
        if not original.strip() or response.text != original:
            return _rejected(context, "invalid", "repair_keep_mismatch")
        return _Parsed("ok", response.decision, response.text, original, response.source_evidence,
                       ["repair_candidate_preserved", "repair_fidelity_unverified"])
    if response.decision == "still_unknown":
        if response.text != "":
            return _rejected(context, "invalid", "repair_unknown_requires_empty_text")
        return _Parsed("ok", "still_unknown", "", context.before_text, response.source_evidence,
                       ["repair_still_unknown", "repair_fidelity_unverified"])
    if not response.text.strip():
        return _rejected(context, "invalid", "repair_replace_requires_text")
    if not response.source_evidence.strip():
        return _rejected(context, "invalid", "repair_replace_requires_evidence")
    baseline = max(len(context.native_text), len(context.visual_text))
    if baseline and len(response.text) > baseline * 2 + 256:
        return _rejected(context, "invalid", "repair_expansion_limit")
    return _Parsed("ok", "replace", response.text, response.text, response.source_evidence,
                   ["repair_fidelity_unverified", "repair_requires_review"])


class RepairResponseV1(EvidenceModel):
    contract: Literal["smartai.recognition.repair_response"] = "smartai.recognition.repair_response"
    schema_version: Literal[1] = 1
    candidate: RecognitionCandidateV1 = Field(repr=False)
    context: RepairContextV1 = Field(repr=False)
    parse_status: ParseStatus
    decision: RepairDecision
    proposed_text: str = Field(strict=True, max_length=8000, repr=False)
    final_text: str = Field(strict=True, max_length=8000, repr=False)
    source_evidence: str = Field(strict=True, max_length=240, repr=False)
    reason_codes: list[Code] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def exact_candidate_derivation(self):
        self.candidate = RecognitionCandidateV1.model_validate(self.candidate.model_dump(warnings=False))
        self.context = RepairContextV1.model_validate(self.context.model_dump(warnings=False))
        if self.candidate.kind == "native":
            raise ValueError("source text is not a model repair response")
        expected = _interpret(self.candidate, self.context)
        if (self.parse_status != expected.status or self.decision != expected.decision
                or self.proposed_text != expected.proposed_text or self.final_text != expected.final_text
                or self.source_evidence != expected.source_evidence or self.reason_codes != expected.reasons):
            raise ValueError("repair proposal must match its raw response and original context")
        return self


def parse_repair_response(candidate: RecognitionCandidateV1, context: RepairContextV1) -> RepairResponseV1:
    """Return a bounded proposal, retaining raw evidence on every parse outcome.

    Invalid caller DTOs raise a safe error. Content is never solved, normalized,
    executed, or completed here; legal JSON is not proof of a faithful repair.
    """
    try:
        context = RepairContextV1.model_validate(context.model_dump(warnings=False))
        candidate = RecognitionCandidateV1.model_validate(candidate.model_dump(warnings=False))
        if candidate.kind == "native":
            raise ValueError("source text is not a model repair response")
    except (ValidationError, ValueError, TypeError, AttributeError):
        raise RecognitionError("recognition_request_invalid") from None
    parsed = _interpret(candidate, context)
    return RepairResponseV1(
        candidate=candidate, context=context, parse_status=parsed.status, decision=parsed.decision,
        proposed_text=parsed.proposed_text, final_text=parsed.final_text, source_evidence=parsed.source_evidence,
        reason_codes=parsed.reasons,
    )
