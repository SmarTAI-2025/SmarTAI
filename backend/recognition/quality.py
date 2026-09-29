"""Conservative candidate labels, not OCR accuracy estimates or source correction.

Checks never rewrite text, solve mathematics, or establish page/span coverage.
The preferred candidate is provisional even when both transcriptions agree.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError

from backend.domain.errors import RecognitionError
from backend.recognition.models import Purpose, RecognitionCandidateV1, _formatting_payload

_PURPOSES = frozenset({"problems", "reference", "submissions", "rubric", "test_cases", "knowledge"})
_VISUAL_RISKS = frozenset({"math", "table", "diagram", "handwriting", "layout", "damaged_text"})
_KNOWN_WARNINGS = frozenset({"output_truncated", "provider_refused"})


@dataclass(frozen=True)
class CandidateAssessment:
    confidence: Literal["low", "medium"]
    reasons: tuple[str, ...]
    issues: tuple[str, ...]
    preferred: Literal["native", "visual", "none"]


def _validated(candidate, *, visual: bool) -> RecognitionCandidateV1 | None:
    if candidate is None:
        return None
    try:
        candidate = RecognitionCandidateV1.model_validate(candidate.model_dump(warnings=False))
        if candidate.kind not in ({"vision", "ocr"} if visual else {"native"}):
            raise ValueError
        return candidate
    except (ValidationError, ValueError, AttributeError, TypeError):
        raise RecognitionError("recognition_request_invalid") from None


def _text_issues(text: str) -> tuple[str, ...]:
    """Bounded lexical observations only; source mathematical syntax is untouched."""
    issues = []
    if "\ufffd" in text:
        issues.append("replacement_character")
    if any((ord(char) < 32 and char not in "\t\r\n") or 127 <= ord(char) <= 159 for char in text):
        issues.append("control_character")
    folded = text.lower()
    if any(marker in folded for marker in ("[unclear]", "[unreadable]", "[illegible]")):
        issues.append("source_form_uncertain")
    # Markdown fence balance is packaging, not a test of the enclosed code/math.
    fence = None
    for line in text.splitlines():
        stripped = line.lstrip(" ")
        if len(line) - len(stripped) > 3 or not stripped.startswith(("```", "~~~")):
            continue
        character = stripped[0]
        size = len(stripped) - len(stripped.lstrip(character))
        if fence is None:
            fence = character, size
        elif character == fence[0] and size >= fence[1] and not stripped[size:].strip():
            fence = None
    if fence is not None:
        issues.append("packaging_unclosed_fence")
    # Only explicit outer LaTeX wrappers are observed. Parentheses, braces,
    # dollar signs and source expressions are not interpreted as math errors.
    for opening, closing in (("\\(", "\\)"), ("\\[", "\\]")):
        if text.startswith(opening) and closing not in text[len(opening):]:
            issues.append("packaging_math_wrapper_uncertain")
            break
    return tuple(issues)


def _candidate_state(candidate, name: str, reasons: list[str], issues: list[str]) -> bool:
    if candidate is None:
        return False
    blocked = False
    if candidate.status == "error" or candidate.safe_error_code is not None:
        issues.append(name + "_error")
        blocked = True
    if candidate.finish_reason == "refused" or "provider_refused" in candidate.warning_codes:
        issues.append(name + "_refused")
        blocked = True
    if candidate.finish_reason == "length" or "output_truncated" in candidate.warning_codes:
        issues.append(name + "_truncated")
    if candidate.status != "ok":
        reasons.append(name + "_" + candidate.status)
        if name == "visual":
            issues.append(name + "_" + candidate.status)
        return False
    if blocked:
        return False
    if set(candidate.warning_codes) - _KNOWN_WARNINGS:
        issues.append(name + "_warning")
    observations = _text_issues(candidate.text)
    if observations:
        reasons.append(name + "_text_flags")
        issues.extend(observations)
    return True


def assess_candidates(
    native: RecognitionCandidateV1 | None,
    visual: RecognitionCandidateV1 | None,
    *,
    purpose: Purpose,
    page_risks: tuple[str, ...] = (),
    partial: bool = False,
) -> CandidateAssessment:
    """Select one raw candidate provisionally; never create hybrid transcription.

    Medium means no issue was found by these limited checks, not calibrated
    confidence, correctness, completeness, or permission for a repair/call.
    """
    if (type(purpose) is not str or purpose not in _PURPOSES or type(partial) is not bool
            or type(page_risks) is not tuple or len(page_risks) > 32
            or any(type(risk) is not str or not risk or len(risk) > 120 for risk in page_risks)):
        raise RecognitionError("recognition_request_invalid")
    native, visual = _validated(native, visual=False), _validated(visual, visual=True)
    reasons, issues = ["coverage_not_verified"], []
    if partial:
        issues.append("coverage_partial")
    if set(page_risks) - _VISUAL_RISKS:
        issues.append("unclassified_page_risk")
    native_ok = _candidate_state(native, "native", reasons, issues)
    visual_ok = _candidate_state(visual, "visual", reasons, issues)
    if native_ok and visual_ok:
        equivalent = _formatting_payload(native.text) == _formatting_payload(visual.text)
        if equivalent:
            reasons.append("raw_candidates_agree" if native.text == visual.text else "formatting_equivalent")
        else:
            issues.append("transcription_conflict")
            reasons.append("raw_candidates_disagree")
        if purpose == "submissions":
            preferred = "visual"
            reasons.append("submission_visual_preferred")
        elif page_risks:
            preferred = "visual"
            reasons.append("page_risk_visual_preferred")
        else:
            preferred = "native"
            reasons.append("native_preferred_without_page_risk")
    elif native_ok:
        preferred = "native"
        reasons.append("single_native_candidate")
        if purpose == "submissions" or page_risks:
            issues.append("visual_evidence_missing")
    elif visual_ok:
        preferred = "visual"
        reasons.append("single_visual_candidate")
    else:
        preferred = "none"
        issues.append("no_transcription_candidate")
    return CandidateAssessment(
        confidence="low" if issues else "medium", reasons=tuple(dict.fromkeys(reasons)),
        issues=tuple(dict.fromkeys(issues)), preferred=preferred,
    )
