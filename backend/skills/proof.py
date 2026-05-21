"""
ProofSkill: grades proof and inference questions (证明题, 推理题).

Workflow (docs §4.2.4):
  1. Split the student answer into logical steps
  2. For each step, retrieve relevant knowledge for reasoning check
  3. Call LLM for step-by-step validation
  4. Optionally MapReduce-summarize if answer is long
  5. Return ExpertResult
"""
from __future__ import annotations

import logging
import os
from typing import Optional, List, TYPE_CHECKING

from pydantic import BaseModel, Field

from backend.skills.base import GradingSkill, build_system_prompt, register_skill
from backend.models import ExpertResult, ProblemInfo, StudentAnswerInfo, StepScore
from backend.llm.providers import BaseProvider
from backend.tools.structured_llm import structured_llm_call
from backend.tools import knowledge as kb_tool

if TYPE_CHECKING:
    from backend.progress.tracker import ProgressReporter

logger = logging.getLogger(__name__)


class ProofGradingOutput(BaseModel):
    score: float = Field(ge=0)
    max_score: float = Field(default=10.0)
    confidence: float = Field(ge=0, le=1)
    comment: str
    steps: List[dict] = Field(default_factory=list)


def _load_template() -> str:
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "prompts", "proof.txt")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return _DEFAULT_TEMPLATE


_DEFAULT_TEMPLATE = """You are a mathematics teacher grading a proof/inference problem.

Problem:
{problem}

Student's proof steps:
{steps}

Reference knowledge (from textbook):
{context}

Grading Rubric:
{rubric}

For each step, determine if the reasoning is sound, and note any gaps or errors.
Return JSON: score, max_score, confidence, comment, steps (list of {{step_no, desc, is_correct, score, comment}}).
"""


def _split_steps(answer: str) -> List[str]:
    """Split a proof into logical steps. Simple heuristic: by blank lines / numbered markers."""
    lines = [line.strip() for line in answer.split("\n") if line.strip()]
    # Just return non-empty lines; the LLM will figure out step boundaries.
    return lines


@register_skill("证明题", "推理题")
class ProofSkill(GradingSkill):
    name = "ProofSkill"
    problem_type = "证明题"

    def __init__(
        self,
        provider: BaseProvider,
        *,
        reporter: Optional["ProgressReporter"] = None,
        language: str = "en",
        task_id: Optional[str] = None,
    ):
        super().__init__(provider, reporter=reporter, language=language, task_id=task_id)
        self._template = _load_template()

    async def grade(
        self,
        problem: ProblemInfo,
        answer: StudentAnswerInfo,
        *,
        student_id: str = "",
    ) -> ExpertResult:
        logger.info(f"ProofSkill.grade start: q_id={problem.q_id}, provider={self.provider.provider_id}")

        active_unit = None
        step_ctx = None
        if self.reporter:
            step_ctx = self.reporter.step(student_id, problem.q_id, self.name, self.provider.provider_id)
            active_unit = await step_ctx.__aenter__()

        try:
            # Step 1: Split into steps
            if self.reporter and active_unit:
                await self.reporter.substep(active_unit, "split_steps")
            steps = _split_steps(answer.content or "")
            steps_text = "\n".join(f"Step {i+1}: {s}" for i, s in enumerate(steps))

            # Step 2: Retrieve reference knowledge (use problem stem as query)
            if self.reporter and active_unit:
                await self.reporter.substep(active_unit, "retrieve_knowledge")
            chunks = await kb_tool.retrieve(problem.stem, k=3, scope=self.task_id)
            context_str = "\n".join(f"[{c.source}] {c.content}" for c in chunks) if chunks else "(no reference knowledge)"

            # Step 3: Build prompt
            if self.reporter and active_unit:
                await self.reporter.substep(active_unit, "build_prompt")
            prompt = self._template
            prompt = prompt.replace("{problem}", problem.stem)
            prompt = prompt.replace("{steps}", steps_text or "(no steps extracted)")
            prompt = prompt.replace("{context}", context_str)
            prompt = prompt.replace("{rubric}", problem.criterion)

            # Step 4: LLM grading
            if self.reporter and active_unit:
                await self.reporter.substep(active_unit, "llm_grade")
            system_prompt = build_system_prompt(
                "You are a mathematics teacher grading a proof or inference problem. "
                "Walk through the 4-step reasoning workflow (Premises → Reasoning chain → "
                "Formal correctness → Conclusion) and produce a structured per-dimension score. "
                "A correct conclusion via a broken chain is NOT a correct proof.",
                self.language,
            )
            result, raw = await structured_llm_call(
                self.provider,
                system_prompt=system_prompt,
                user_prompt=prompt,
                output_model=ProofGradingOutput,
            )

            step_scores = []
            for s in result.steps:
                if isinstance(s, dict):
                    step_scores.append(StepScore(
                        step_no=s.get("step_no", len(step_scores) + 1),
                        desc=s.get("desc", s.get("comment", "")),
                        is_correct=bool(s.get("is_correct", True)),
                        score=float(s.get("score", 0.0)),
                    ))

            return ExpertResult(
                provider=self.provider.provider_id,
                score=max(0.0, min(result.score, result.max_score)),
                max_score=result.max_score,
                confidence=max(0.0, min(result.confidence, 1.0)),
                comment=result.comment,
                steps=step_scores,
                raw_output=raw.content,
                duration_ms=raw.duration_ms,
            )

        except Exception as e:
            logger.error(f"ProofSkill failed: {e}")
            from backend.skills.base import classify_skill_error
            kind, friendly = classify_skill_error(e)
            return self._blank_result(problem.q_id, 10.0, friendly, error_kind=kind)
        finally:
            if step_ctx:
                await step_ctx.__aexit__(None, None, None)
