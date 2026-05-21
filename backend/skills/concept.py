"""
ConceptSkill: grades concept-type questions (概念题).

Workflow (docs §4.2.4):
  1. Retrieve relevant knowledge from KB (tools/knowledge.py)
  2. Build prompt with context + problem + student answer + rubric
  3. Call LLM for structured grading result
  4. Return ExpertResult

This replaces the ~500-line backend/correct/concept.py.
"""
from __future__ import annotations

import logging
import os
from typing import Optional, TYPE_CHECKING

from pydantic import BaseModel, Field
from typing import List

from backend.skills.base import GradingSkill, build_system_prompt, register_skill
from backend.models import ExpertResult, ProblemInfo, StudentAnswerInfo, StepScore
from backend.llm.providers import BaseProvider
from backend.tools.structured_llm import structured_llm_call
from backend.tools import knowledge as kb_tool

if TYPE_CHECKING:
    from backend.progress.tracker import ProgressReporter, ActiveUnit

logger = logging.getLogger(__name__)


# ─── Structured LLM output for concept grading ──────────────────────────────

class ConceptGradingOutput(BaseModel):
    """Expected JSON output from the LLM when grading a concept question."""
    score: float = Field(ge=0, description="Score awarded, 0 to max_score")
    max_score: float = Field(default=10.0, description="Maximum possible score")
    confidence: float = Field(ge=0, le=1, description="Grading confidence")
    comment: str = Field(description="Overall feedback to the student")
    steps: List[dict] = Field(default_factory=list, description="Step-by-step scoring breakdown")
    hits: List[str] = Field(default_factory=list, description="Knowledge points matched")


# ─── Prompt template ─────────────────────────────────────────────────────────

def _load_template() -> str:
    """Load the concept prompt template, or use a built-in default."""
    template_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "prompts",
        "concept.txt",
    )
    try:
        with open(template_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        logger.warning(f"Concept prompt template not found at {template_path}, using default")
        return _DEFAULT_TEMPLATE


_DEFAULT_TEMPLATE = """You are a professional teacher who needs to grade a student's answer to a concept problem.

Relevant Knowledge:
{context}

Problem:
{problem}

Student Answer:
{answer}

Grading Rubric:
{rubric}

Please return the JSON result in the following format:
{{
    "score": Score between 0 and the rubric's max score,
    "max_score": The maximum score defined in the rubric,
    "confidence": Confidence between 0-1,
    "comment": "Detailed feedback",
    "steps": [
        {{
            "step_no": 1,
            "desc": "Step description",
            "is_correct": true/false,
            "score": Score for this step
        }}
    ],
    "hits": ["Knowledge Point 1", "Knowledge Point 2"]
}}
"""


# ─── Skill implementation ────────────────────────────────────────────────────

@register_skill("概念题", "其他", "其它")
class ConceptSkill(GradingSkill):
    name = "ConceptSkill"
    problem_type = "概念题"

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
        """Grade a concept question."""
        logger.info(f"ConceptSkill.grade start: q_id={problem.q_id}, provider={self.provider.provider_id}")

        # Track active unit for progress reporting
        active_unit = None
        if self.reporter:
            step_ctx = self.reporter.step(student_id, problem.q_id, self.name, self.provider.provider_id)
            active_unit_cm = await step_ctx.__aenter__()
            active_unit = active_unit_cm

        try:
            # Step 1: Retrieve relevant knowledge
            if self.reporter and active_unit:
                await self.reporter.substep(active_unit, "retrieve_knowledge")

            chunks = await kb_tool.retrieve(problem.stem, k=5, scope=self.task_id)
            context_str = "\n".join(
                f"[{c.source}] {c.content}" for c in chunks
            ) if chunks else "No reference knowledge available. Please use your own expertise."

            # Step 2: Build prompt
            if self.reporter and active_unit:
                await self.reporter.substep(active_unit, "build_prompt")

            prompt = self._template
            prompt = prompt.replace("{context}", context_str)
            prompt = prompt.replace("{problem}", problem.stem)
            prompt = prompt.replace("{answer}", answer.content or "(No answer provided)")
            prompt = prompt.replace("{rubric}", problem.criterion)

            # Step 3: Call LLM
            if self.reporter and active_unit:
                await self.reporter.substep(active_unit, "llm_grade")

            # Anti-jailbreak prefix is baked in by build_system_prompt — student
            # answer / rubric content cannot override the grader's role.
            system_prompt = build_system_prompt(
                "You are a professional teacher grading a concept question. "
                "Walk through the 4-step reasoning workflow specified in the user prompt "
                "and produce a structured per-dimension score.",
                self.language,
            )

            try:
                result, raw_response = await structured_llm_call(
                    self.provider,
                    system_prompt=system_prompt,
                    user_prompt=prompt,
                    output_model=ConceptGradingOutput,
                )

                # Build step scores
                step_scores = []
                for s in result.steps:
                    if isinstance(s, dict):
                        step_scores.append(StepScore(
                            step_no=s.get("step_no", len(step_scores) + 1),
                            desc=s.get("desc", s.get("comment", "")),
                            is_correct=bool(s.get("is_correct", True)),
                            score=float(s.get("score", 0.0)),
                        ))

                # Clamp score
                score = max(0.0, min(result.score, result.max_score))
                confidence = max(0.0, min(result.confidence, 1.0))

                return ExpertResult(
                    provider=self.provider.provider_id,
                    score=score,
                    max_score=result.max_score,
                    confidence=confidence,
                    comment=result.comment,
                    steps=step_scores,
                    hits=result.hits,
                    raw_output=raw_response.content,
                    duration_ms=raw_response.duration_ms,
                )

            except Exception as e:
                logger.error(f"ConceptSkill LLM call failed: {e}")
                from backend.skills.base import classify_skill_error
                kind, friendly = classify_skill_error(e)
                return self._blank_result(
                    problem.q_id, 10.0, friendly, error_kind=kind,
                )

        finally:
            if self.reporter and active_unit:
                await step_ctx.__aexit__(None, None, None)
