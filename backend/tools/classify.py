"""
Problem classification tool.

Uses an LLM to decide the type of a problem (概念题/计算题/编程题/证明题/推理题/其他).
Classification is a TOOL, not a skill — skills consume a type that's already
been decided.

Called by:
  - backend/agents/ingest_agent.py when parsing uploaded assignments
  - re-classification after human edits to a problem stem
"""
from __future__ import annotations

import logging
from typing import Literal, Optional

from pydantic import BaseModel, Field

from backend.llm.providers import BaseProvider
from backend.tools.structured_llm import structured_llm_call

logger = logging.getLogger(__name__)


PROBLEM_TYPES = ("概念题", "计算题", "编程题", "证明题", "推理题", "其他")


class ClassificationResult(BaseModel):
    type: Literal["概念题", "计算题", "编程题", "证明题", "推理题", "其他"] = Field(
        description="The problem type. Choose exactly one."
    )
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="Confidence in the classification, 0.0 to 1.0"
    )
    rationale: str = Field(description="Short explanation (1-2 sentences) for the classification")


_CLASSIFY_SYSTEM_PROMPT = """You are an AI teaching assistant classifying higher-education STEM problems.

Classify the given problem into exactly ONE of these Chinese types:
- 概念题: Answer is determined or close-meaning; tests understanding of definitions/concepts.
- 计算题: Requires numerical or symbolic calculation to verify.
- 编程题: Contains code snippets or asks student to write code.
- 证明题: Asks student to prove a stated conclusion by logical derivation.
- 推理题: Asks student to derive a conclusion NOT given in the stem.
- 其他: None of the above.

Return a JSON object with fields: type, confidence (0-1), rationale (1-2 sentences).
"""


async def classify_problem(
    provider: BaseProvider,
    stem: str,
    *,
    existing_type: Optional[str] = None,
) -> ClassificationResult:
    """
    Classify a problem by its stem.

    Args:
        provider: LLM to use.
        stem: The problem text.
        existing_type: If the problem already has a type hint, pass it for reference
                       (classifier may override if hint seems wrong).

    Returns:
        ClassificationResult with type, confidence, rationale.
    """
    user_prompt = f"Problem stem:\n---\n{stem}\n---\n"
    if existing_type:
        user_prompt += f"\n(Previous type hint: {existing_type} — verify or correct.)"

    try:
        result, _ = await structured_llm_call(
            provider,
            system_prompt=_CLASSIFY_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            output_model=ClassificationResult,
        )
        logger.info(f"Classified problem as {result.type} (confidence={result.confidence:.2f})")
        return result
    except Exception as e:
        logger.error(f"Classification failed: {e}")
        # Fallback: 其他 with low confidence
        return ClassificationResult(
            type="其他",
            confidence=0.0,
            rationale=f"Classification error: {e}",
        )
