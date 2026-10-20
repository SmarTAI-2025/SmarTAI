"""Execute legacy production skills; replace external LLM/KB boundaries only.

No-reference examples explicitly simulate reference-generation failure. This
adapter is tied to base 0304ff2e; it is NOT an adapter for the new main chain.
"""
from __future__ import annotations

import json
import socket
import time
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from .metrics import finite

BASE_SHA = "0304ff2e0cc294ada51c99af8d5ab07540cb61a8"


def skill_for(kind):
    from backend.skills.calculation import CalculationSkill
    from backend.skills.concept import ConceptSkill
    from backend.skills.proof import ProofSkill
    return {"计算题": CalculationSkill, "证明题": ProofSkill,
            "推理题": ProofSkill, "概念题": ConceptSkill}[kind]


def arguments(case):
    from backend.models import ProblemInfo, StudentAnswerInfo
    problem = ProblemInfo(**case["problem"])
    answer = StudentAnswerInfo(q_id=problem.q_id, number=problem.number,
                               type=problem.type, content=case["answer"])
    return problem, answer


def observation(case, expert, duration_ms):
    # error_kind is the legacy skill failure contract, not an actual zero grade.
    valid = not expert.error_kind and finite(expert.score)
    return {
        "id": case["id"], "score": expert.score if valid else None,
        "raw_score": expert.score, "max_score": expert.max_score,
        "error_kind": expert.error_kind, "comment": expert.comment,
        "steps": [s.model_dump(mode="json") for s in expert.steps],
        "logs": expert.logs, "raw_output": expert.raw_output,
        # Skill-only boundary cannot assert the final report/UI review state.
        "advisory": None, "required_handling": None,
        "forced_review_wording": "需人工复核" in expert.comment,
        "duration_ms": duration_ms,
    }


async def legacy_case(case):
    from backend.skills import calculation
    if not hasattr(calculation, "SympyLoopResult"):
        raise RuntimeError("legacy replay requires the old production base; use candidate:evaluate")
    from backend.llm.providers import LLMResponse
    from backend.skills.calculation import SympyLoopResult
    from backend.tools.structured_llm import PermanentLLMError, StructuredOutputInvalidError

    skill_type = skill_for(case["problem"]["type"])
    boundary_calls, prompts = [], []

    async def fixed_call(provider, *, output_model, **kwargs):
        boundary_calls.append("structured_llm_call")
        prompts.append({"system": kwargs.get("system_prompt"), "user": kwargs.get("user_prompt")})
        fault = case["replay"]["model_fault"]
        if fault == "permanent":
            raise PermanentLLMError("synthetic evaluation failure")
        if fault == "invalid_output":
            raise StructuredOutputInvalidError("synthetic evaluation parse failure")
        payload = case["replay"]["response"]
        return output_model.model_validate(payload), LLMResponse(
            content=json.dumps(payload, ensure_ascii=False), provider="fixed",
            model="authored-fixture-v1", duration_ms=0)

    async def never_provider(*args, **kwargs):
        raise AssertionError("replay must never invoke a provider")

    def never_network(*args, **kwargs):
        raise AssertionError("network forbidden during replay")

    problem, answer = arguments(case)
    provider = SimpleNamespace(provider_id="fixed:authored-fixture-v1", ainvoke=never_provider)
    started = time.perf_counter()
    tool_fault = case["replay"]["tool_fault"]
    status = "timeout" if tool_fault == "timeout" else "generation_failed"
    with ExitStack() as stack:
        stack.enter_context(patch("socket.socket.connect", never_network))
        stack.enter_context(patch("socket.create_connection", never_network))
        stack.enter_context(patch(f"{skill_type.__module__}.structured_llm_call", fixed_call))
        stack.enter_context(patch("backend.tools.knowledge.retrieve_for_grading", AsyncMock(return_value=[])))
        # No arbitrary code generation or sandbox invocation in fixed replay.
        stack.enter_context(patch("backend.skills.calculation._run_sympy_loop",
                                  AsyncMock(return_value=SympyLoopResult(
                                      execution_id="fixed-replay", status=status, stop_reason=status))))
        if tool_fault == "unknown":
            stack.enter_context(patch("backend.tools.numerical.verify_equivalent", AsyncMock(return_value=None)))
            stack.enter_context(patch("backend.tools.numerical.verify_value", AsyncMock(return_value=None)))
        expert = await skill_type(provider, language="zh").grade(
            problem, answer, student_id="synthetic")
    result = observation(case, expert, (time.perf_counter() - started) * 1000)
    result.update(fixed_response_calls=len(boundary_calls), provider_entry_calls=0,
                  captured_prompts=prompts, tool_fault=tool_fault)
    return result


def opposite_experts_probe():
    from backend.agents.multi_expert import _compute_review_signal
    from backend.models import ExpertResult, StepScore
    experts = [ExpertResult(provider=f"fixed:{i}", score=5, max_score=10,
                            confidence=.95, comment="synthetic opposing item judgments",
                            steps=[StepScore(step_no=j + 1, desc=f"item{j + 1}",
                                             is_correct=p > 0, score=p)
                                   for j, p in enumerate(points)])
               for i, points in enumerate(([5, 0], [0, 5]))]
    signal, attention, reasons = _compute_review_signal(experts)
    return {"id": "same_total_opposite_items", "scores": [e.score for e in experts],
            "item_scores": [[s.score for s in e.steps] for e in experts],
            "legacy_review_signal": signal, "legacy_attention": attention,
            "legacy_reasons": reasons,
            "scope": "actual legacy review-signal function only; no synthesis or UI run"}
