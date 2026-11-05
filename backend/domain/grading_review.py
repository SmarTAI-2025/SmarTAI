"""Shared grading review policy for report exports and result queries.

The frontend queue is checked against the same contract cases in CI.
"""
from __future__ import annotations

import math
from typing import Any

from backend.domain.confidence_policy import confidence_needs_attention
from backend.services.grading_scores import round_ai_score

EXPERT_DISAGREEMENT_THRESHOLD = 0.15


def expert_disagreement_signal(scores: list[float], maximum: float) -> tuple[float | None, bool]:
    """Population standard deviation / full marks; the only disagreement rule."""
    if len(scores) < 2 or not math.isfinite(maximum) or maximum <= 0:
        return None, False
    mean = sum(scores) / len(scores)
    normalized_std = (sum((value - mean) ** 2 for value in scores) / len(scores)) ** .5 / maximum
    # Avoid flagging an exact 15% boundary due to floating-point noise.
    return normalized_std, normalized_std > EXPERT_DISAGREEMENT_THRESHOLD + math.ulp(1.0)


def number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


TEACHER_CASE_SCORE_REASONS = ("programming_teacher_tests_passed_deduction", "programming_teacher_tests_failed_full_credit")
PROGRAMMING_REVIEW_REASONS = (*TEACHER_CASE_SCORE_REASONS, "programming_ai_only", "programming_code_review_only", "programming_interface_only")


def teacher_case_score_review_reason(evidence, score, maximum) -> str | None:
    """Compare an actual teacher-run observation with a normalized, publishable vote."""
    if (not isinstance(evidence, dict) or evidence.get("contract") != "smartai.programming_evidence"
            or evidence.get("score_status") not in {"final", "provisional"}
            or evidence.get("execution_status") != "executed" or evidence.get("mapping_status") != "valid"):
        return None
    tests = evidence.get("teacher_tests")
    if not isinstance(tests, dict):
        return None
    total, passed = tests.get("total"), tests.get("passed")
    score, maximum = number(score), number(maximum)
    if (type(total) is not int or type(passed) is not int or not 0 <= passed <= total or total == 0
            or score is None or maximum is None or not 0 <= score <= maximum or maximum <= 0):
        return None
    published = round_ai_score(score, maximum)
    if passed == total and score < maximum and not math.isclose(score, maximum, rel_tol=1e-12):
        return "programming_teacher_tests_passed_deduction"
    if passed < total and (score == maximum or published == maximum):
        return "programming_teacher_tests_failed_full_credit"
    return None


def programming_evidence_review_reason(evidence: dict) -> str:
    if evidence.get("score_review_reason") in TEACHER_CASE_SCORE_REASONS:
        return evidence["score_review_reason"]
    if evidence.get("execution_status") != "executed":
        return "programming_code_review_only"
    if ((evidence.get("teacher_tests") or {}).get("total", 0) > 0
            and evidence.get("primary_reason") in {"target_function_missing", "signature_mismatch"}):
        return "programming_interface_only"
    return "programming_ai_only"


def programming_review_reason(reasons, experts) -> str | None:
    """Read the frozen programming review requirement, including restored expert votes."""
    restored = set(reasons or ())
    for expert in experts or ():
        evidence = expert.get("programming_evidence") if isinstance(expert, dict) else None
        if (isinstance(evidence, dict) and not expert.get("error_kind")
                and evidence.get("contract") == "smartai.programming_evidence"
                and evidence.get("score_status") == "provisional"):
            restored.add(programming_evidence_review_reason(evidence))
    return next((reason for reason in PROGRAMMING_REVIEW_REASONS if reason in restored), None)


def teacher_resolved_programming_score(ai_score, teacher_score, maximum, confirmed) -> bool:
    value = number(teacher_score)
    if value is None or not 0 <= value <= maximum:
        return False
    return confirmed is True or value != round_ai_score(number(ai_score), maximum)


def result_programming_review_reason(result) -> str | None:
    return programming_review_reason(
        [*(getattr(result, "initial_review_reasons", None) or ()), *(getattr(result, "review_reasons", None) or ())],
        getattr(result, "ai_expert_results", None),
    )


def score(correction: dict) -> float | None:
    teacher = number(correction.get("teacher_score"))
    if teacher is not None:
        return teacher
    reasons = {part.strip() for reason in [*(correction.get("review_reasons") or []),
                                          *(correction.get("initial_review_reasons") or [])]
               for part in reason.split(",")}
    maximum = number(correction.get("max_score"))
    valid_expert = any(not expert.get("error_kind") and number(expert.get("score")) is not None
                       and maximum is not None and 0 <= expert["score"] <= maximum
                       for expert in correction.get("expert_results") or [])
    if reasons.intersection({"invalid_score_scale", "missing_correction", "missing_student_result"}) or (
        not valid_expert and (correction.get("synthesis_method") in {"all_failed", "quota_exhausted", "paused"}
                             or reasons.intersection({"llm_failed", "quota_exhausted"}))
    ):
        return None
    current = number(correction.get("score"))
    return current if current is not None else number(correction.get("provisional_score"))


def review_state(correction: dict) -> tuple[bool, bool]:
    """Match the grading review queue (resultsModel.correctionReviewAttention)."""
    maximum = number(correction.get("max_score"))

    def valid(value):
        value = number(value)
        return value is not None and maximum is not None and maximum > 0 and 0 <= value <= maximum

    confirmed = correction.get("review_status") == "confirmed"
    blocked = not valid(score(correction))
    reviewed = number(correction.get("teacher_score")) is not None and confirmed and not blocked
    conf = number(correction.get("confidence"))
    expert_scores = [
        number(expert.get("score"))
        for expert in correction.get("expert_results") or []
        if not expert.get("error_kind") and valid(expert.get("score"))
    ]
    _, disagreement = expert_disagreement_signal(expert_scores, maximum or 0)
    experts = correction.get("expert_results") or []
    low_confidence = any(confidence_needs_attention(expert.get("confidence"))
                         for expert in experts if not expert.get("error_kind") and valid(expert.get("score")))
    partial = bool(expert_scores) and len(expert_scores) < len(experts)
    reasons = {part.strip() for reason in [*(correction.get("review_reasons") or []),
                                          *(correction.get("initial_review_reasons") or [])]
               for part in reason.split(",")}
    programming = programming_review_reason(reasons, experts)
    program_pending = programming and not teacher_resolved_programming_score(
        correction.get("provisional_score"), correction.get("teacher_score"), maximum or 0, confirmed,
    )
    pending = blocked or bool(program_pending) or (not reviewed and (low_confidence or (not expert_scores and confidence_needs_attention(conf))
                                            or disagreement or partial or bool(reasons & {
                                                "partial_experts", "degraded_to_single", "math_scoring_uncertainty",
                                                "math_expert_disagreement", "math_verification_conflict",
                                                "math_proof_concern", "math_model_review",
                                            })))
    return pending, reviewed

