"""Shared grading review policy for report exports and result queries.

The frontend queue is checked against the same contract cases in CI.
"""
from __future__ import annotations

import math
from typing import Any

from backend.domain.confidence_policy import confidence_needs_attention

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
    original = correction.get("provisional_score", correction.get("score"))
    blocked = not valid(score(correction)) or (not valid(original) and not confirmed)
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
    pending = blocked or (not reviewed and (low_confidence or (not expert_scores and confidence_needs_attention(conf))
                                            or disagreement or partial or bool(reasons & {"partial_experts", "degraded_to_single"})))
    return pending, reviewed

