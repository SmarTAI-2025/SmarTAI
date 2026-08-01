"""Fake LLM provider for deterministic grading tests.

Returns a fixed ``Correction`` per question so the grading adapter, run
lifecycle, review, and release can be exercised end-to-end without a real
provider. The adapter/run code is unchanged; only the provider boundary is
swapped via dependency injection or test settings.
"""
from __future__ import annotations

from typing import Any

from backend.models import Correction, ExpertResult, StepScore


class FakeProvider:
    """A provider-shaped double that yields a passing correction for every q_id.

    Set ``fail_qids`` to force a confidence-0 / all_failed result for specific
    questions so review + release-blocking paths are covered deterministically.
    """

    def __init__(self, *, fail_qids: set[str] | None = None, score: float = 8.0,
                 confidence: float = 0.9) -> None:
        self.fail_qids = fail_qids or set()
        self.score = score
        self.confidence = confidence
        self.calls: list[str] = []

    async def grade(self, *, q_id: str, stem: str, answer: str, max_score: float = 10.0) -> Correction:
        self.calls.append(q_id)
        if q_id in self.fail_qids:
            return Correction(
                q_id=q_id, type="short", score=0.0, max_score=max_score,
                confidence=0.0, comment="all_failed", steps=[],
                synthesis_method="all_failed",
            )
        return Correction(
            q_id=q_id, type="short", score=self.score, max_score=max_score,
            confidence=self.confidence, comment="ok",
            steps=[StepScore(step_no=1, desc="s1", is_correct=True, score=self.score)],
            expert_results=[ExpertResult(
                provider="fake", score=self.score, max_score=max_score,
                confidence=self.confidence, comment="ok", steps=[],
            )],
            synthesis_method="single",
        )


async def fake_grade_batch(*, student_store: dict, problem_store: dict,
                          registry: Any = None, reporter: Any = None,
                          language: str = "en", task_id: str | None = None,
                          provider: FakeProvider | None = None,
                          **_kwargs: Any) -> list[dict[str, Any]]:
    """Drop-in replacement for agents.grading_agent.grade_batch.

    Builds the same per-student result shape the real grade_batch returns
    (``{student_id, corrections, ...}``) by calling the fake provider once per
    (student, question). No prompts, skills, or scoring code are touched.
    """
    provider = provider or FakeProvider()
    out: list[dict[str, Any]] = []
    for sid, sd in student_store.items():
        corrections: list[Correction] = []
        for q_id, problem in problem_store.items():
            answer_text = ""
            for a in sd.get("answers", sd.get("stu_ans", [])):
                if a.get("q_id") == q_id:
                    answer_text = a.get("content", "")
                    break
            corrections.append(await provider.grade(
                q_id=q_id, stem=problem.get("stem", ""), answer=answer_text,
                max_score=problem.get("max_score", 10.0),
            ))
        out.append({
            "student_id": sid,
            "student_name": sd.get("stu_name", sid),
            "corrections": corrections,
            "student_answers": sd.get("answers", sd.get("stu_ans", [])),
        })
    return out
