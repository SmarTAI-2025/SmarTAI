"""Deterministic page candidates from bounded native PDF evidence, without I/O.

A location is not a claim that a whole question or its continuation is complete.
Missing targets mean unlocated in the supplied evidence, never absent from a book.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Annotated, Literal

from pydantic import Field, model_validator

from backend.recognition.models import EvidenceModel, NormalizedRegionV1, RecognitionSourceRefV1
from backend.tools.pdf_evidence import PdfDetailPage, PdfIndexPage

PageNumber = Annotated[int, Field(strict=True, ge=1, le=10000)]
TargetId = Annotated[str, Field(min_length=1, max_length=80)]
MAX_MATCHES_PER_TARGET = 64
_NUMBER = r"\d+(?:[ \t]*\.[ \t]*\d+)*"
_CONTEXT = re.compile(
    r"(?im)^[ \t]*(?P<label>sec(?:tion)?\.?|chap(?:ter)?\.?|ch\.?)"
    r"[ \t]*(?P<number>" + _NUMBER + r")(?![\w.]|[ \t]*\.\d)"
)
_CHINESE_CONTEXT = re.compile(r"(?m)^[ \t]*第[ \t]*(?P<number>" + _NUMBER + r")[ \t]*(?P<label>章|节)")


class NativeLocatorRequestV1(EvidenceModel):
    source: RecognitionSourceRefV1
    total_pages: int = Field(strict=True, ge=1, le=10000)
    targets: list[TargetId] = Field(min_length=1, max_length=64)
    index_pages: list[PdfIndexPage] = Field(default_factory=list, max_length=500)
    detail_pages: list[PdfDetailPage] = Field(default_factory=list, max_length=24)
    page_hints: dict[TargetId, list[PageNumber]] = Field(default_factory=dict, max_length=64)

    @model_validator(mode="after")
    def consistent_scope(self):
        if len(set(self.targets)) != len(self.targets) or any(
            not target.strip() or any(char in target for char in "\r\n\x00") for target in self.targets
        ):
            raise ValueError("targets must be distinct single-line identifiers")
        if set(self.page_hints) - set(self.targets):
            raise ValueError("page hints must name requested targets")
        for pages in (self.index_pages, self.detail_pages):
            numbers = [page.page_number for page in pages]
            if numbers != sorted(set(numbers)) or any(number > self.total_pages for number in numbers):
                raise ValueError("inspected pages must be ordered, unique and in source scope")
        for numbers in self.page_hints.values():
            if (not numbers or numbers != sorted(set(numbers)) or len(numbers) > 24
                    or any(number > self.total_pages for number in numbers)):
                raise ValueError("page hints must be bounded, ordered source pages")
        if len({number for numbers in self.page_hints.values() for number in numbers}) > 24:
            raise ValueError("explicit hints exceed the selection budget")
        index = {page.page_number: page for page in self.index_pages}
        for page in self.detail_pages:
            if page.target_matches:
                raise ValueError("detail evidence cannot assert index-only target matches")
            if page.page_number in index:
                summary = index[page.page_number]
                if any(getattr(page, field) != getattr(summary, field) for field in
                       ("width_points", "height_points", "rotation", "observation")):
                    raise ValueError("index and detail snapshots disagree")
        allowed_terms = set(self.targets) | {target.rsplit(".", 1)[0] for target in self.targets
                                            if re.fullmatch(r"\d+(?:\.\d+)+", target)}
        if any(set(page.target_matches) - allowed_terms for page in self.index_pages):
            raise ValueError("index candidates must belong to the requested targets")
        return self


class NativeMatchV1(EvidenceModel):
    page_number: PageNumber
    kind: Literal["literal", "section", "chapter", "local_label", "continuation"]
    native_char_start: int = Field(ge=0, le=500000)
    native_char_end: int = Field(gt=0, le=500000)
    matched_text: str = Field(min_length=1, max_length=240)
    block_order_indices: list[int] = Field(min_length=1, max_length=240)
    region: tuple[float, float, float, float]

    @model_validator(mode="after")
    def interval(self):
        if self.native_char_end - self.native_char_start != len(self.matched_text):
            raise ValueError("matched raw text must equal its codepoint interval")
        if self.block_order_indices != sorted(set(self.block_order_indices)) or min(self.block_order_indices) < 0:
            raise ValueError("matched block identities must be ordered and unique")
        NormalizedRegionV1(**dict(zip(("x0", "y0", "x1", "y1"), self.region)))
        return self


class NativeTargetLocationV1(EvidenceModel):
    target: TargetId
    status: Literal["located", "hinted", "needs_detail", "unlocated", "ambiguous", "needs_hint"]
    resolved_scope: Literal["none", "page_only"] = "none"
    selected_pages: list[PageNumber] = Field(default_factory=list, max_length=24)
    candidate_pages: list[PageNumber] = Field(default_factory=list, max_length=524)
    evidence: list[NativeMatchV1] = Field(default_factory=list, max_length=MAX_MATCHES_PER_TARGET * 4)
    reason_codes: list[str] = Field(default_factory=list, max_length=12)


class NativeLocatorResultV1(EvidenceModel):
    contract: Literal["smartai.recognition.native_locator"] = "smartai.recognition.native_locator"
    schema_version: Literal[1] = 1
    source: RecognitionSourceRefV1
    total_pages: int = Field(ge=1, le=10000)
    requested_targets: list[TargetId]
    selected_pages: list[PageNumber] = Field(max_length=24)
    inspected_index_pages: list[PageNumber] = Field(max_length=500)
    inspected_detail_pages: list[PageNumber] = Field(max_length=24)
    locations: list[NativeTargetLocationV1] = Field(max_length=64)
    missing_targets: list[TargetId] = Field(description="Unlocated in supplied native evidence, not absent from the source")
    ambiguous_targets: list[TargetId]
    needs_detail_targets: list[TargetId]
    selection_limit_exceeded: bool = False
    reason_codes: list[str] = Field(default_factory=list, max_length=8)


@dataclass
class _Anchor:
    page_number: int
    evidence: list[NativeMatchV1]
    continued: bool = False
    uncertain_context: bool = False
    weak_literal: bool = False


def _proof(page: PdfDetailPage, start: int, end: int, kind: str) -> NativeMatchV1:
    blocks = [block for block in page.blocks if block.kind == "text"
              and block.native_char_start < end and start < block.native_char_end]
    region = (min(block.region[0] for block in blocks), min(block.region[1] for block in blocks),
              max(block.region[2] for block in blocks), max(block.region[3] for block in blocks))
    return NativeMatchV1(page_number=page.page_number, kind=kind, native_char_start=start,
                         native_char_end=end, matched_text=page.native_text[start:end],
                         block_order_indices=[block.order_index for block in blocks], region=region)


def _literal_pattern(target: str) -> re.Pattern:
    if re.fullmatch(r"[A-Za-z]*\d+(?:\.\d+)*", target):
        return re.compile(r"(?<![A-Za-z0-9_.])" + re.escape(target) + r"(?![A-Za-z0-9_])(?!\.\d)")
    return re.compile(r"(?<!\w)" + re.escape(target) + r"(?!\w)")


def _continuation(page: PdfDetailPage, start: int, end: int) -> NativeMatchV1 | None:
    line_start = page.native_text.rfind("\n", 0, start) + 1
    prefix = page.native_text[line_start:start].strip()
    if prefix and not re.fullmatch(r"(?i)(?:exercise|problem|question|ex\.?|q\.?)", prefix):
        return None
    suffix = page.native_text[end:page.native_text.find("\n", end) if "\n" in page.native_text[end:] else len(page.native_text)]
    marker = re.match(r"(?i)^[ \t.)：:\-(（]*(?P<marker>continued\b|cont\.|续)", suffix)
    if marker:
        return _proof(page, end + marker.start("marker"), end + marker.end("marker"), "continuation")
    return None


def _question_context(page: PdfDetailPage, target: str, start: int) -> bool:
    if not re.fullmatch(r"[A-Za-z]*\d+(?:\.\d+)*", target):
        return True
    line_start = page.native_text.rfind("\n", 0, start) + 1
    prefix = page.native_text[line_start:start].strip()
    return not prefix or bool(re.fullmatch(r"(?i)(?:exercise|problem|question|ex\.?|q\.?|题)", prefix))


def _contexts(page: PdfDetailPage):
    found = []
    for pattern in (_CONTEXT, _CHINESE_CONTEXT):
        for match in pattern.finditer(page.native_text):
            label = match.group("label").lower()
            kind = "section" if label.startswith("sec") or label == "节" else "chapter"
            number = re.sub(r"[ \t]", "", match.group("number"))
            start = match.start("number") if match.end() - match.start() > 240 else match.start()
            if match.end() - start <= 240:
                found.append((number, kind, _proof(page, start, match.end(), kind)))
    return found


def _composite_context(page: PdfDetailPage, prefix: str) -> tuple[list[NativeMatchV1], bool]:
    contexts = _contexts(page)
    sections = [item for item in contexts if item[1] == "section"]
    chapters = [item for item in contexts if item[1] == "chapter"]
    if sections:
        direct = [item[2] for item in sections if item[0] == prefix]
        if direct:
            return [direct[0]], len({item[0] for item in sections}) > 1
        if "." in prefix:
            chapter, section = prefix.rsplit(".", 1)
            left = [item[2] for item in chapters if item[0] == chapter]
            right = [item[2] for item in sections if item[0] == section]
            if left and right:
                return [left[0], right[0]], (len({item[0] for item in chapters}) > 1
                                            or len({item[0] for item in sections}) > 1)
        return [], False
    matching = [item[2] for item in chapters if item[0] == prefix]
    return matching[:1], len({item[0] for item in chapters}) > 1


def _anchors(page: PdfDetailPage, target: str) -> list[_Anchor]:
    anchors = []
    if re.fullmatch(r"\d+(?:\.\d+)+", target):
        prefix, local = target.rsplit(".", 1)
        context, uncertain = _composite_context(page, prefix)
        if context:
            pattern = re.compile(r"(?im)^[ \t]*(?:(?:exercise|problem|question|ex\.?|q\.?)[ \t]+)?"
                                 r"(?P<label>" + re.escape(local) + r"[ \t]{0,8}[.)、:：]|\(" + re.escape(local)
                                 + r"\)|（" + re.escape(local) + r"）)(?=[ \t]|$)")
            for match in pattern.finditer(page.native_text):
                start, end = match.span("label")
                continued = _continuation(page, start, end)
                anchors.append(_Anchor(page.page_number, [*context, _proof(page, start, end, "local_label")]
                                       + ([continued] if continued else []), bool(continued), uncertain))
                if len(anchors) > MAX_MATCHES_PER_TARGET:
                    return anchors
    context_intervals = [(item[2].native_char_start, item[2].native_char_end) for item in _contexts(page)]
    for match in _literal_pattern(target).finditer(page.native_text):
        if any(start <= match.start() and match.end() <= end for start, end in context_intervals):
            continue
        continued = _continuation(page, match.start(), match.end())
        anchors.append(_Anchor(page.page_number, [_proof(page, match.start(), match.end(), "literal")]
                               + ([continued] if continued else []), bool(continued),
                               weak_literal=not _question_context(page, target, match.start())))
        if len(anchors) > MAX_MATCHES_PER_TARGET:
            break
    return anchors


def _locate_one(request: NativeLocatorRequestV1, target: str) -> NativeTargetLocationV1:
    hints = request.page_hints.get(target, [])
    details = [page for page in request.detail_pages if not hints or page.page_number in hints]
    anchors = []
    for page in details:
        anchors.extend(_anchors(page, target))
        if len(anchors) > MAX_MATCHES_PER_TARGET:
            break
    overflow = len(anchors) > MAX_MATCHES_PER_TARGET
    anchors = anchors[:MAX_MATCHES_PER_TARGET]
    evidence = [proof for anchor in anchors for proof in anchor.evidence]
    # Explicit full-ID labels and composite labels are peers; inline mentions are weaker.
    starts = [anchor for anchor in anchors if not anchor.continued and not anchor.weak_literal]
    detail_numbers = {page.page_number for page in details}
    terms = {target}
    if re.fullmatch(r"\d+(?:\.\d+)+", target):
        terms.add(target.rsplit(".", 1)[0])
    indexed = {page.page_number for page in request.index_pages if terms & set(page.target_matches)
               and (not hints or page.page_number in hints)}
    pending = indexed - detail_numbers
    candidates = sorted(indexed | {anchor.page_number for anchor in anchors})
    values = dict(target=target, candidate_pages=candidates, evidence=evidence)
    if hints:
        reasons = ["explicit_page_hint"]
        if not anchors:
            reasons.append("hint_not_native_verified")
        if overflow or len(starts) > 1 or any(anchor.uncertain_context for anchor in starts):
            reasons.append("native_candidates_ambiguous_within_hint")
        return NativeTargetLocationV1(**values, status="hinted", resolved_scope="page_only",
                                      selected_pages=hints, reason_codes=reasons)
    if overflow or len(starts) > 1 or any(anchor.uncertain_context for anchor in starts):
        same_page = {anchor.page_number for anchor in starts}
        if not overflow and not pending and len(same_page) == 1:
            return NativeTargetLocationV1(**values, status="ambiguous", resolved_scope="page_only",
                                          selected_pages=sorted(same_page), reason_codes=[
                                              "native_candidates_ambiguous", "same_page_occurrences_require_full_page_context"])
        return NativeTargetLocationV1(**values, status="ambiguous", reason_codes=[
            "native_match_limit_exceeded" if overflow else "native_candidates_ambiguous", "target_location_needs_hint"])
    if pending:
        return NativeTargetLocationV1(**values, status="needs_detail", selected_pages=sorted(pending)[:24],
                                      reason_codes=["index_candidate_requires_detail"])
    if starts:
        start = starts[0]
        continuations = [anchor for anchor in anchors if anchor.continued and anchor.page_number > start.page_number]
        selected = sorted({start.page_number, *(anchor.page_number for anchor in continuations)})
        reasons = ["native_section_local_label" if any(proof.kind == "local_label" for proof in start.evidence)
                   else "native_literal_boundary"]
        if continuations:
            reasons.append("explicit_native_continuation")
        return NativeTargetLocationV1(**values, status="located", resolved_scope="page_only",
                                      selected_pages=selected, reason_codes=reasons)
    reasons = ["target_not_located_in_inspected_native_scope", "target_location_needs_hint"]
    if any(anchor.weak_literal for anchor in anchors):
        reasons.append("native_literal_requires_question_context")
    return NativeTargetLocationV1(**values, status="unlocated", reason_codes=reasons)


def locate_native_targets(request: NativeLocatorRequestV1) -> NativeLocatorResultV1:
    """Return bounded page candidates; this function never searches unseen pages."""
    request = NativeLocatorRequestV1.model_validate(request.model_dump(warnings=False))
    locations = [_locate_one(request, target) for target in request.targets]
    selected = sorted({number for location in locations for number in location.selected_pages})
    over_budget = len(selected) > 24 or any(
        location.status == "needs_detail" and len(location.candidate_pages) > 24 for location in locations
    )
    if over_budget:
        selected = []
        for location in locations:
            if location.selected_pages:
                location.selected_pages = []
                location.status = "needs_hint"
                location.resolved_scope = "none"
                location.reason_codes += ["target_selection_limit_exceeded", "target_location_needs_hint"]
    return NativeLocatorResultV1(
        source=request.source, total_pages=request.total_pages, requested_targets=request.targets,
        selected_pages=selected, inspected_index_pages=[page.page_number for page in request.index_pages],
        inspected_detail_pages=[page.page_number for page in request.detail_pages], locations=locations,
        missing_targets=[item.target for item in locations if item.status == "unlocated"],
        ambiguous_targets=[item.target for item in locations if item.status == "ambiguous"],
        needs_detail_targets=[item.target for item in locations if item.status == "needs_detail"],
        selection_limit_exceeded=over_budget,
        reason_codes=["target_selection_limit_exceeded", "target_location_needs_hint"] if over_budget else [],
    )
