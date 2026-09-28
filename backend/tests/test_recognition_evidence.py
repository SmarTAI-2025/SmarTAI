from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from backend.recognition.models import (
    NormalizedRegionV1,
    RecognitionCandidateV1,
    RecognitionCoverageV1,
    RecognitionDocumentV1,
    RecognitionPatchV1,
    RecognitionUsageV1,
)


def document_payload():
    return {
        "policy_version": "faithful-v2", "prompt_version": "transcribe-v1",
        "source": {
            "owner_id": "teacher-a", "scope": "submission_source", "business_id": "submission-1",
            "content_type": "application/pdf", "input_sha256": "a" * 64,
        },
        "purpose": "submissions", "provider_fingerprint": "frozen-route-v1",
        "pages": [{
            "page_index": 0, "page_number": 1, "width_points": 595, "height_points": 842,
            "native_text": "x = -2", "selected_for_visual": True,
            "spans": [{
                "span_id": "p0001-s0001", "order_index": 0,
                "native_char_start": 0, "native_char_end": 6,
                "native": {"kind": "native", "status": "ok", "text": "x = -2"},
                "visual": {"kind": "vision", "status": "ok", "text": "x = -2"},
                "final_text": "x = -2", "adopted_from": "vision", "confidence": "medium",
            }],
        }],
        "final_markdown": "x = -2",
        "coverage": {"scope": "document", "total_pages": 1, "requested_pages": [1], "processed_pages": [1]},
        "confidence": "medium", "result_cache_key": "b" * 64,
        "usage": {"initial_calls": 1, "usage_complete": False},
    }


def test_evidence_round_trip_preserves_source_wrong_answer_and_unknown_usage():
    original = RecognitionDocumentV1.model_validate(document_payload())
    restored = RecognitionDocumentV1.model_validate_json(original.model_dump_json())
    assert restored == original
    assert restored.pages[0].spans[0].final_text == "x = -2"
    assert restored.pages[0].native_text == "x = -2"
    assert restored.usage.input_tokens is None
    assert restored.usage.total_calls == 1
    assert restored.coverage.complete


@pytest.mark.parametrize("changes", [
    {"processed_pages": [], "unprocessed_pages": []},
    {"processed_pages": [1], "failed_pages": [1]},
    {"requested_pages": [1, 1]}, {"requested_pages": [0]},
    {"total_pages": 500}, {"missing_targets": ["not-requested"]},
])
def test_coverage_cannot_silently_omit_pages(changes):
    payload = document_payload()["coverage"] | changes
    with pytest.raises(ValidationError):
        RecognitionCoverageV1.model_validate(payload)


def test_partial_target_coverage_does_not_mean_full_book():
    coverage = RecognitionCoverageV1(
        scope="targets", total_pages=500, requested_pages=[8, 9], processed_pages=[8, 9],
        requested_targets=["1.2.16"],
    )
    assert coverage.complete
    assert coverage.scope == "targets"
    assert len(coverage.processed_pages) != coverage.total_pages


@pytest.mark.parametrize("field,changes", [
    ("page", {"page_number": 2}),
    ("page", {"spans": []}),
    ("page", {"verified_blank": True}),
    ("span", {"span_id": "p0002-s0001"}),
    ("span", {"native_char_end": 7}),
    ("span", {"native_char_start": None}),
    ("span", {"native_char_end": 0, "native_char_start": 1}),
    ("span", {"final_text": " "}),
    ("span", {"final_text": "x = 2"}),
    ("span", {"adopted_from": "none"}),
    ("span", {"visual": None}),
    ("span", {"adopted_from": "ocr"}),
    ("span", {"confidence": "low"}),
    ("span", {"native": {"kind": "vision", "status": "ok", "text": "wrong slot"}}),
    ("span", {"issues": ["raw provider error with a secret"]}),
])
def test_invalid_evidence_is_not_laundered_as_success(field, changes):
    payload = document_payload()
    target = payload["pages"][0]
    if field == "span":
        target = target["spans"][0]
    target.update(changes)
    with pytest.raises(ValidationError):
        RecognitionDocumentV1.model_validate(payload)


def test_document_cannot_promote_medium_span_to_high():
    payload = document_payload() | {"confidence": "high"}
    with pytest.raises(ValidationError):
        RecognitionDocumentV1.model_validate(payload)


@pytest.mark.parametrize("text", ["", "x = 2", "invented solution"])
def test_document_summary_cannot_drop_or_rewrite_recognized_content(text):
    with pytest.raises(ValidationError, match="faithfully render"):
        RecognitionDocumentV1.model_validate(document_payload() | {"final_markdown": text})


def test_target_coverage_cannot_claim_unspecified_targets_complete():
    with pytest.raises(ValidationError, match="target identities"):
        RecognitionCoverageV1(scope="targets", total_pages=1000, requested_pages=[1], processed_pages=[1])


def test_formatted_candidate_keeps_raw_evidence_without_untracked_rewrite():
    payload = document_payload()
    span = payload["pages"][0]["spans"][0]
    span["visual"]["normalized_text"] = "$x = -2$"
    span["final_text"] = payload["final_markdown"] = "$x = -2$"
    result = RecognitionDocumentV1.model_validate(payload)
    assert result.pages[0].spans[0].visual.text == "x = -2"


@pytest.mark.parametrize("normalized", ["x = 2", "x = -3", "x=-2", "x = -2 and therefore solved"])
def test_normalization_is_not_a_backdoor_to_changing_the_candidate(normalized):
    with pytest.raises(ValidationError, match="cannot rewrite"):
        RecognitionCandidateV1(kind="vision", status="ok", text="x = -2", normalized_text=normalized)


def test_normalization_can_unify_line_endings_and_unicode_without_losing_raw():
    raw = "Cafe\u0301\r\nx = -2"
    result = RecognitionCandidateV1(kind="vision", status="ok", text=raw, normalized_text="Caf\u00e9\nx = -2")
    assert result.text == raw


@pytest.mark.parametrize("raw,normalized", [
    ("$a$ + $b$", "a$ + $b"),
    ("\\(a\\) + \\(b\\)", "a\\) + \\(b"),
])
def test_normalization_does_not_strip_separate_formulas_as_one_outer_wrapper(raw, normalized):
    with pytest.raises(ValidationError, match="cannot rewrite"):
        RecognitionCandidateV1(kind="vision", status="ok", text=raw, normalized_text=normalized)


def test_span_patch_must_record_the_exact_adopted_proposal():
    payload = document_payload()
    span = payload["pages"][0]["spans"][0]
    span["final_text"] = payload["final_markdown"] = "x = -3"
    span["patch"] = {
        "status": "applied", "attempt_count": 1, "decision": "replace",
        "proposed_text": "x = -3", "final_text": "x = -3", "before_text": "x = -2",
    }
    assert RecognitionDocumentV1.model_validate(payload).final_markdown == "x = -3"
    span["patch"]["proposed_text"] = "x = -4"
    with pytest.raises(ValidationError, match="recorded proposal"):
        RecognitionDocumentV1.model_validate(payload)


def test_duplicate_or_unordered_spans_rejected():
    for order in [0, -1]:
        payload = document_payload()
        span = copy.deepcopy(payload["pages"][0]["spans"][0])
        span.update(span_id="p0001-s0002", order_index=order)
        payload["pages"][0]["spans"].append(span)
        with pytest.raises(ValidationError):
            RecognitionDocumentV1.model_validate(payload)


def test_unprocessed_page_forces_low_confidence():
    payload = document_payload()
    payload["coverage"].update(total_pages=2, requested_pages=[1, 2], unprocessed_pages=[2])
    with pytest.raises(ValidationError):
        RecognitionDocumentV1.model_validate(payload)
    payload["confidence"] = "low"
    assert not RecognitionDocumentV1.model_validate(payload).coverage.complete


def test_verified_blank_can_be_processed_without_invented_text():
    payload = document_payload()
    payload["pages"][0].update(native_text="", spans=[], verified_blank=True)
    payload["final_markdown"] = ""
    assert RecognitionDocumentV1.model_validate(payload).coverage.complete


@pytest.mark.parametrize("status,text", [("error", "raw secret response"), ("empty", "text"), ("not_run", "text"), ("ok", " ")])
def test_error_candidate_never_contains_response_body(status, text):
    with pytest.raises(ValidationError):
        RecognitionCandidateV1(kind="vision", status=status, text=text)


def test_unknown_usage_is_never_silently_zero():
    with pytest.raises(ValidationError):
        RecognitionUsageV1(initial_calls=1)
    assert RecognitionUsageV1(initial_calls=1, usage_complete=False).input_tokens is None
    assert RecognitionUsageV1(initial_calls=1, input_tokens=0, output_tokens=0).total_calls == 1


@pytest.mark.parametrize("changes", [
    {"x0": 1, "x1": 0}, {"y0": 0.5, "y1": 0.5}, {"x1": 2}, {"y0": float("nan")},
])
def test_regions_must_be_finite_nonempty_and_normalized(changes):
    with pytest.raises(ValidationError):
        NormalizedRegionV1(**changes)


@pytest.mark.parametrize("changes", [
    {"attempt_count": 2}, {"attempt_count": 1},
    {"status": "applied", "attempt_count": 0},
    {"status": "applied", "attempt_count": 1, "decision": "replace"},
])
def test_patch_contract_is_bounded_and_requires_real_decision(changes):
    with pytest.raises(ValidationError):
        RecognitionPatchV1(**changes)


def test_patch_history_keeps_original_candidate():
    patch = RecognitionPatchV1(
        status="applied", attempt_count=1, decision="keep_visual", before_text="x = 2",
        proposed_text="x = -2", final_text="x = -2",
    )
    assert patch.before_text == "x = 2"
    assert patch.final_text == "x = -2"


def test_evidence_schema_does_not_own_grading_identity_or_scores():
    payload = document_payload()
    payload["pages"][0]["spans"][0]["q_id"] = "Q1"
    with pytest.raises(ValidationError):
        RecognitionDocumentV1.model_validate(payload)
