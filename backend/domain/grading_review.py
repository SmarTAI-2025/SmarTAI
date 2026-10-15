"""Shared grading review policy for report exports and result queries.

The frontend queue is checked against the same contract cases in CI.
"""
from __future__ import annotations

import math
from typing import Any


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
    if correction.get("synthesis_method") in {"all_failed", "quota_exhausted"} or reasons.intersection({
        "llm_failed", "quota_exhausted", "invalid_score_scale", "missing_correction", "missing_student_result",
    }):
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
    expert_scores = [v for expert in correction.get("expert_results") or []
                     if not expert.get("error_kind") and (v := number(expert.get("score"))) is not None]
    reasons = {part.strip() for reason in [*(correction.get("review_reasons") or []),
                                          *(correction.get("initial_review_reasons") or [])]
               for part in reason.split(",")}
    disagreement = bool(reasons.intersection({"high_indecisiveness", "score_spread_high"})
                        or (len(expert_scores) > 1 and maximum is not None
                            and max(expert_scores) - min(expert_scores) > max(1, maximum * .25)))
    conf = number(correction.get("confidence"))
    pending = blocked or (not reviewed and (disagreement or (conf is not None and 0 <= conf <= .35)))
    return pending, reviewed

