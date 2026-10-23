"""New production grade_batch replay; fixtures replace model boundaries only."""
from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from .candidate_fixtures import assessment_reply, plan_reply
from .reliability_v2 import feedback_v2


def registry_for(providers):
    return SimpleNamespace(list_available=lambda: providers, pick_default=lambda: providers[0],
                           get=lambda key: next((p for p in providers if p.provider_id == key), None))


def problem_data(case):
    data = deepcopy(case["problem"])
    # These are three different students answering exactly the same question.
    # Corpus case IDs remain separate observation IDs, not distinct q_ids.
    if case.get("shared_group"):
        data["q_id"], data["number"] = case["shared_group"], "1"
    return data


async def evaluate(case, provider=None):
    return (await evaluate_group([case], provider=provider))[0]


async def evaluate_group(cases, provider=None, *, student_names=None):
    from backend.agents.grading_agent import grade_batch
    from backend.domain.grading_review import score as effective_score, review_state
    from backend.services import math_rubric_plan as rubric
    from backend.skills import math_grading
    from backend.tools import math_verification
    from backend.tools.structured_llm import PermanentLLMError, StructuredOutputInvalidError

    live = provider is not None
    async def no_provider(*args, **kwargs):
        raise AssertionError("fixed replay cannot invoke an upstream provider")
    provider = provider or SimpleNamespace(provider_id="fixed:candidate-v1", ainvoke=no_provider)
    calls, plans, prompts, events = [], {}, [], []
    by_answer = {case["answer"]: case for case in cases}
    by_qid = {problem_data(case)["q_id"]: case for case in cases}
    actual_prepare = rubric.prepare_math_rubric_plan

    async def capture_plan(problem, *args, **kwargs):
        result = await actual_prepare(problem, *args, **kwargs)
        plans[problem.q_id] = result
        events.append({"stage": "plan_ready", "qid": problem.q_id, "plan_id": result.plan_id})
        return result

    async def fixed_plan(_provider, *, output_model, user_prompt, system_prompt, **kwargs):
        question = json.loads(user_prompt)["question"]
        case = by_qid[question["q_id"]]
        assert "student" not in question and "answer" not in question
        calls.append({"stage": "plan", "id": case["id"]})
        prompts.append({"stage": "plan", "system": system_prompt, "user": user_prompt})
        return output_model.model_validate(plan_reply(case)), SimpleNamespace(duration_ms=0, content="{}")

    async def fixed_grade(_provider, *, output_model, user_prompt, system_prompt, **kwargs):
        # Parse the actual prompt rather than assuming invocation order.
        input_data = user_prompt.split("\nINPUT DATA:\n", 1)[1].split("\nAPPLY SCOPED EVIDENCE", 1)[0]
        payload = json.loads(input_data)
        case = by_answer[payload["student_answer"]]
        calls.append({"stage": output_model.__name__, "id": case["id"]})
        events.append({"stage": "grade", "id": case["id"],
                       "plan_id": payload["rubric_plan"]["plan_id"]})
        prompts.append({"stage": output_model.__name__, "system": system_prompt, "user": user_prompt})
        if case["replay"]["model_fault"] == "permanent":
            raise PermanentLLMError("synthetic provider failure")
        if case["replay"]["model_fault"] == "invalid_output":
            raise StructuredOutputInvalidError("synthetic invalid output")
        if output_model is math_grading.DirectMathGrade:
            if case["id"] == "C07":
                return output_model(score=0, confidence=.95,
                                    rationale="原标准明确空答0分，不适用非空最低6分。",
                                    comment="未作答，得0分。"), SimpleNamespace(duration_ms=0, content="{}")
            # No happy-path direct fallback fixture: an invalid detailed
            # fixture must be visible as failure, not hidden by oracle credit.
            raise AssertionError("unexpected direct fallback in fixed valid corpus")
        reply = assessment_reply(case)
        if "APPLY SCOPED EVIDENCE" in user_prompt:
            evidence = json.loads(user_prompt.split("\nAPPLY SCOPED EVIDENCE (one pass):\n", 1)[1])["evidence"]
            reply = feedback_v2(reply, evidence)
        parsed = output_model.model_validate(reply)
        if kwargs.get("validate_output"):
            kwargs["validate_output"](parsed)
        return parsed, SimpleNamespace(duration_ms=0, content="{}")

    actual_verify = math_verification.verify_math
    async def verification(request):
        faults = {c["replay"]["tool_fault"] for c in cases}
        if "unknown" in faults:
            return math_verification.MathVerificationEvidence(status="unknown", method="injected",
                                                              reason="Synthetic unsupported tool.")
        if "timeout" in faults:
            raise TimeoutError("synthetic tool timeout")
        return await actual_verify(request)

    problem_store = {problem_data(c)["q_id"]: problem_data(c) for c in cases}
    students = {
        c["id"]: {"stu_id": c["id"], "stu_name": (student_names or {}).get(c["id"], "Synthetic"),
                  "stu_ans": [dict(q_id=problem_data(c)["q_id"], number=problem_data(c)["number"],
                                   type=c["problem"]["type"], content=c["answer"])]}
        for c in cases}
    start = time.perf_counter()
    before_calls = getattr(provider, "calls", 0)
    with ExitStack() as stack:
        stack.enter_context(patch.object(rubric, "prepare_math_rubric_plan", capture_plan))
        stack.enter_context(patch("backend.tools.knowledge.retrieve_for_grading", AsyncMock(return_value=[])))
        if not live:
            def no_network(*args, **kwargs):
                raise AssertionError("candidate replay forbids network")
            stack.enter_context(patch("socket.socket.connect", no_network))
            stack.enter_context(patch("socket.create_connection", no_network))
            stack.enter_context(patch.object(rubric, "structured_llm_call", fixed_plan))
            stack.enter_context(patch.object(math_grading, "structured_llm_call", fixed_grade))
            stack.enter_context(patch.object(math_verification, "verify_math", verification))
        batch = await grade_batch(student_store=students, problem_store=problem_store,
                                  registry=registry_for([provider]), language="zh",
                                  multi_sample_n=1, aggregation_method="single")
    elapsed = (time.perf_counter() - start) * 1000
    rows = []
    for student in batch:
        case = next(c for c in cases if c["id"] == student["student_id"])
        correction = student["corrections"][0]
        public = correction.model_dump(mode="json")
        value = effective_score(public)
        attention, _ = review_state(public)
        expert = correction.expert_results[0] if correction.expert_results else None
        trace = math_grading.read_math_trace(expert) if expert else None
        assessment = (trace or {}).get("assessment") or {}
        plan = plans[problem_data(case)["q_id"]]
        row = {
            "id": case["id"], "score": value, "raw_score": correction.score,
            "max_score": correction.max_score, "comment": correction.comment,
            "steps": public["steps"], "correction": public, "trace": trace,
            "plan_id": plan.plan_id, "plan": plan.model_dump(mode="json"),
            "plan_modes": [r.mode for r in plan.routes],
            "claims": assessment.get("claims"),
            "advisory": attention and value is not None, "required_handling": value is None,
            "forced_review_wording": "需人工复核" in correction.comment,
            "duration_ms": elapsed / len(cases), "duration_scope": "batch amortized per case",
            "fixed_response_calls": sum(c["id"] == case["id"] for c in calls),
            "provider_entry_calls": (getattr(provider, "calls", 0) - before_calls) / len(cases),
            "captured_prompts": prompts, "call_stages": calls, "execution_events": events,
        }
        if any(route.mode == "deductions" for route in plan.routes):
            row["deductions"] = assessment.get("deductions")
        # No model claims of tool verification are authored here. Preserve the
        # actual evidence; do not infer semantic truth from quote containment.
        row["verification_claims"] = None
        rows.append(row)
    return rows
