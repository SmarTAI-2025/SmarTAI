"""Shared calculation fixtures; behavioral coverage is in test_math_grading."""
from backend.models import ProblemInfo, StudentAnswerInfo


def _make_problem(
    *,
    q_id: str = "q1",
    type_: str = "计算题",
    stem: str = "Compute 6 * 7.",
    criterion: str = "Final value must be 42.",
    reference_answer: str | None = None,
    max_score: float = 10.0,
) -> ProblemInfo:
    return ProblemInfo(
        q_id=q_id,
        number="1",
        type=type_,
        stem=stem,
        criterion=criterion,
        reference_answer=reference_answer,
        max_score=max_score,
    )


def _make_answer(content: str) -> StudentAnswerInfo:
    return StudentAnswerInfo(q_id="q1", number="1", type="计算题", content=content)
