"""Shared precision for AI grades; teacher overrides retain their entered value."""
from decimal import Decimal, ROUND_HALF_UP


def score_quantum(steps=()) -> Decimal:
    # Frozen part budgets can use hundredths. Preserve their additive precision
    # everywhere a result is synthesized, persisted or read back.
    return Decimal("0.01") if any(
        (step.get("part_id") if isinstance(step, dict) else getattr(step, "part_id", None))
        for step in (steps or ())
    ) else Decimal("0.1")


def round_ai_score(score: float | None, max_score: float, steps=()) -> float | None:
    if score is None:
        return None
    return min(float(Decimal(str(score)).quantize(score_quantum(steps), rounding=ROUND_HALF_UP)), max_score)
