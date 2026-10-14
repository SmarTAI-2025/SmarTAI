"""Shared precision for AI grades; teacher overrides retain their entered value."""
from decimal import Decimal, ROUND_HALF_UP


def round_ai_score(score: float | None, max_score: float) -> float | None:
    if score is None:
        return None
    return min(float(Decimal(str(score)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)), max_score)
