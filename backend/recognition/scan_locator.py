"""Parse bounded model page suggestions, never verified question locations.

Only the supplied global PDF pages are in scope. Not-visible output is not proof
that a target is absent from the book; evidence and target labels remain data.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Annotated, Literal

from pydantic import Field, ValidationError, model_validator

from backend.domain.errors import RecognitionError
from backend.recognition.models import Code, EvidenceModel, RecognitionCandidateV1

MAX_RESPONSE_BYTES = 32 * 1024
PageNumber = Annotated[int, Field(strict=True, ge=1, le=10000)]
TargetId = Annotated[str, Field(strict=True, min_length=1, max_length=80)]
ParseStatus = Literal["ok", "invalid", "empty", "error", "refused", "truncated"]
_JSON_FENCE = re.compile(r"```json[ \t]*\r?\n(?P<body>.*)\r?\n```", re.DOTALL)


class _Scope(EvidenceModel):
    requested_targets: list[TargetId] = Field(min_length=1, max_length=64)
    inspected_pages: list[PageNumber] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def unique_scope(self):
        if len(set(self.requested_targets)) != len(self.requested_targets) or any(
            not target.strip() or any(char in target for char in "\r\n\x00") for target in self.requested_targets
        ):
            raise ValueError("targets must be unique nonempty single-line identifiers")
        if len(set(self.inspected_pages)) != len(self.inspected_pages):
            raise ValueError("inspected pages must be unique global page numbers")
        for target in self.requested_targets:
            target.encode("utf-8")
        return self


class ScanTargetLocationV1(EvidenceModel):
    target: TargetId
    status: Literal["candidate", "not_visible", "uncertain"]
    pages: list[PageNumber] = Field(max_length=16)
    evidence: str = Field(strict=True, max_length=240)

    @model_validator(mode="after")
    def conditional_evidence(self):
        if len(set(self.pages)) != len(self.pages):
            raise ValueError("candidate pages must be distinct")
        if self.status == "candidate" and (not self.pages or not self.evidence.strip()):
            raise ValueError("a page candidate requires visible clue text")
        if self.status == "not_visible" and self.pages:
            raise ValueError("not-visible responses cannot select pages")
        self.evidence.encode("utf-8")
        return self


class _Response(EvidenceModel):
    locations: list[ScanTargetLocationV1] = Field(min_length=1, max_length=64)


@dataclass
class _Parsed:
    status: ParseStatus
    locations: list[ScanTargetLocationV1]
    selected_pages: list[int]
    reasons: list[str]


def _rejected(status: ParseStatus, reason: str) -> _Parsed:
    return _Parsed(status, [], [], [reason])


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("nonfinite JSON constant")


def _interpret(candidate: RecognitionCandidateV1, scope: _Scope) -> _Parsed:
    if candidate.finish_reason == "refused" or "provider_refused" in candidate.warning_codes:
        return _rejected("refused", "scan_response_refused")
    if candidate.finish_reason == "length" or "output_truncated" in candidate.warning_codes:
        return _rejected("truncated", "scan_response_truncated")
    if candidate.status in {"error", "not_run"}:
        return _rejected("error", "scan_response_error" if candidate.status == "error" else "scan_response_not_run")
    if candidate.status == "empty":
        return _rejected("empty", "scan_response_empty")
    try:
        encoded_size = len(candidate.text.encode("utf-8"))
    except UnicodeEncodeError:
        return _rejected("invalid", "scan_response_invalid_encoding")
    if encoded_size > MAX_RESPONSE_BYTES:
        return _rejected("invalid", "scan_response_too_large")
    text = candidate.text.strip()
    fence = _JSON_FENCE.fullmatch(text)
    if fence:
        text = fence.group("body")
    try:
        payload = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return _rejected("invalid", "scan_json_invalid")
    try:
        response = _Response.model_validate(payload)
    except (ValidationError, ValueError):
        return _rejected("invalid", "scan_schema_invalid")
    targets = [location.target for location in response.locations]
    if len(targets) != len(set(targets)) or set(targets) != set(scope.requested_targets):
        return _rejected("invalid", "scan_target_mismatch")
    if any(set(location.pages) - set(scope.inspected_pages) for location in response.locations):
        return _rejected("invalid", "scan_page_out_of_scope")
    by_target = {location.target: location for location in response.locations}
    locations = [by_target[target] for target in scope.requested_targets]
    selected = sorted({page for location in locations if location.status == "candidate" for page in location.pages})
    return _Parsed("ok", locations, selected, ["scan_locations_unverified", "scan_inspected_scope_only"])


class ScanLocatorResultV1(_Scope):
    contract: Literal["smartai.recognition.scan_locator"] = "smartai.recognition.scan_locator"
    schema_version: Literal[1] = 1
    candidate: RecognitionCandidateV1 = Field(repr=False)
    parse_status: ParseStatus
    locations: list[ScanTargetLocationV1] = Field(max_length=64)
    selected_pages: list[PageNumber] = Field(max_length=16)
    reason_codes: list[Code] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def exact_candidate_derivation(self):
        self.candidate = RecognitionCandidateV1.model_validate(self.candidate.model_dump(warnings=False))
        expected = _interpret(self.candidate, _Scope(requested_targets=self.requested_targets,
                                                    inspected_pages=self.inspected_pages))
        if (self.parse_status != expected.status or self.locations != expected.locations
                or self.selected_pages != expected.selected_pages or self.reason_codes != expected.reasons):
            raise ValueError("scan locations must match the raw response and inspected scope")
        return self


def parse_scan_locations(
    candidate: RecognitionCandidateV1,
    *,
    requested_targets: list[str],
    inspected_pages: list[int],
) -> ScanLocatorResultV1:
    """Keep raw evidence; malformed model output becomes a safe non-selection.

Invalid caller DTOs or scope raise a safe request error. A valid JSON response
may suggest multiple pages but never establishes continuation or completeness.
"""
    try:
        scope = _Scope(requested_targets=requested_targets, inspected_pages=inspected_pages)
        candidate = RecognitionCandidateV1.model_validate(candidate.model_dump(warnings=False))
    except (ValidationError, ValueError, TypeError, AttributeError):
        raise RecognitionError("recognition_request_invalid") from None
    parsed = _interpret(candidate, scope)
    return ScanLocatorResultV1(
        requested_targets=scope.requested_targets, inspected_pages=scope.inspected_pages,
        candidate=candidate, parse_status=parsed.status, locations=parsed.locations,
        selected_pages=parsed.selected_pages, reason_codes=parsed.reasons,
    )
