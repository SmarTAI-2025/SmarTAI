"""One numeric confidence policy; evidence failures remain independent gates."""
from __future__ import annotations

import math
from typing import Any

LOW_CONFIDENCE_THRESHOLD = 0.60


def confidence_value(value: Any) -> float | None:
    # Qualitative recognition labels are not calibrated probabilities.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and 0 <= value <= 1 else None


def confidence_needs_attention(value: Any) -> bool:
    confidence = confidence_value(value)
    return confidence is not None and confidence < LOW_CONFIDENCE_THRESHOLD


def confidence_requires_review(value: Any) -> bool:
    # Numeric uncertainty is advisory; only missing/invalid evidence can block.
    return False


def question_issue_confidence(issue, presentation):
    value = confidence_value((issue.get("details") or {}).get("confidence"))
    if value is not None:
        return value
    field = {"answer": "reference_answer", "rubric": "criterion", "programming_tests": "test_cases"}.get(issue.get("field"))
    return confidence_value(((presentation.get("material_provenance") or {}).get(field) or {}).get("confidence"))


def question_issue_blocks_progress(issue, presentation):
    if issue.get("status") != "open" or issue.get("field") == "answer":
        return False
    return (issue.get("severity") == "blocking"
            or issue.get("code") in {"source_conflict", "ambiguous_question_match"}
            or (issue.get("code") == "manual_completion_required"
                and set((issue.get("details") or {}).get("required_fields", [])) & {"number", "type", "stem", "criterion"})
            or confidence_requires_review(question_issue_confidence(issue, presentation)))
