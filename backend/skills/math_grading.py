"""Shared evidence and accounting pipeline for mathematical written answers.

All mathematical judgments remain scoped to the student's actual text. Tools
verify individual relations; only the frozen rubric determines point budgets.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from backend.models import ExpertResult, GradingConfidence, StepScore
from backend.skills.base import (
    GradingSkill, authoritative_score_instruction, build_system_prompt,
    classify_skill_error,
)
from backend.tools import knowledge as kb_tool
from backend.tools.structured_llm import PermanentLLMError, TransientLLMError, structured_llm_call

TRACE_VERSION = "math_evidence_v1"
VERIFICATION_BUDGET_SECONDS = 20.0
_review_budget = ContextVar("math_review_budget", default=None)


@contextmanager
def math_review_scope():
    """One review interaction across all experts for this student's question."""
    token = _review_budget.set({"used": False, "local": True})
    try:
        yield
    finally:
        _review_budget.reset(token)


def defer_local_review(defer: bool):
    budget = _review_budget.get()
    if budget is not None:
        budget["local"] = not defer


def take_review_budget(*, local=False) -> bool:
    budget = _review_budget.get()
    if budget is None:
        return True  # Direct skill invocation has only its own one-pass review.
    if budget["used"] or (local and not budget["local"]):
        return False
    budget["used"] = True
    return True


class Claim(BaseModel):
    id: str
    quote: str = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)
    condition: str = ""
    item_ids: list[str] = Field(default_factory=list)
    verdict: Literal["supported", "refuted", "unknown"]
    reason: str


class Verification(BaseModel):
    claim_id: str
    # Conditions are proposed by the model, never asserted to be source facts
    # merely because a CAS accepted them.
    context_quote: str = Field(min_length=1)
    request: dict


class Award(BaseModel):
    item_id: str
    score: float = Field(ge=0, allow_inf_nan=False)
    claim_ids: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)


class Deduction(BaseModel):
    error_id: str
    rule_id: str
    claim_id: str
    occurrences: int = Field(default=1, ge=1, le=100)
    caused_by: str | None = None
    reason: str = Field(min_length=1)


class MathAssessment(BaseModel):
    route_id: str
    route_reason: str
    claims: list[Claim] = Field(default_factory=list, max_length=80)
    checks: list[Verification] = Field(default_factory=list, max_length=8)
    awards: list[Award] = Field(default_factory=list, max_length=80)
    deductions: list[Deduction] = Field(default_factory=list, max_length=80)
    score: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    confidence: GradingConfidence = 0.0
    rationale: str = ""  # Detailed evidence for holistic scoring; comment stays concise.
    comment: str
    needs_review: bool = False


class DirectMathGrade(BaseModel):
    """Last resort: a genuine model score with an explicit, usable rationale."""
    score: float = Field(ge=0, allow_inf_nan=False)
    confidence: GradingConfidence = 0.0
    rationale: str = Field(min_length=1)
    comment: str


class ExpertRevision(BaseModel):
    expert_index: int = Field(ge=0)
    reason: str = Field(min_length=1)
    source_quote: str = Field(min_length=1)
    assessment: MathAssessment


class ExpertReview(BaseModel):
    revisions: list[ExpertRevision] = Field(default_factory=list, max_length=10)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _points(value) -> Decimal:
    return Decimal(str(value))


def _rounded(value: Decimal, maximum: float) -> float:
    return float(min(_points(maximum), max(Decimal(0), value.quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP))))


def _claim_map(assessment: MathAssessment, answer: str, plan) -> dict[str, Claim]:
    claims = {claim.id: claim for claim in assessment.claims}
    if len(claims) != len(assessment.claims) or "" in claims:
        raise ValueError("duplicate_or_empty_claim_id")
    item_ids = {item.id for route in plan.routes for item in route.items}
    for claim in claims.values():
        if claim.quote not in answer or not set(claim.item_ids) <= item_ids:
            raise ValueError("claim_not_in_student_answer_or_rubric")
        if not set(claim.depends_on) <= claims.keys():
            raise ValueError("missing_claim_dependency")
    visited, visiting = set(), set()

    def visit(key):
        if key in visiting:
            raise ValueError("cyclic_evidence_graph")
        if key in visited:
            return
        visiting.add(key)
        for dependency in claims[key].depends_on:
            visit(dependency)
        visiting.remove(key)
        visited.add(key)

    for key in claims:
        visit(key)
    return claims


def score_assessment(assessment: MathAssessment, plan, answer: str) -> tuple[float, list[StepScore]]:
    """Validate and sum one selected rubric route; no CAS-result score rules."""
    claims = _claim_map(assessment, answer, plan)
    routes = {route.id: route for route in plan.routes}
    if assessment.route_id not in routes:
        raise ValueError("unknown_rubric_route")
    route = routes[assessment.route_id]
    items = {item.id: item for item in route.items}
    descriptions: list[tuple[str, Decimal, bool]] = []
    if route.mode == "points":
        awards = {award.item_id: award for award in assessment.awards}
        if len(awards) != len(assessment.awards) or awards.keys() != items.keys() or assessment.deductions:
            raise ValueError("missing_duplicate_or_mixed_awards")
        for item_id, item in items.items():
            award = awards[item_id]
            if award.score > item.points or not set(award.claim_ids) <= claims.keys():
                raise ValueError("invalid_award")
            quotes = " / ".join(claims[key].quote for key in award.claim_ids)
            desc = f"{item.description}\n{quotes}\n{award.reason}".strip()
            descriptions.append((desc, _points(award.score), award.score == item.points))
        total = sum((score for _, score, _ in descriptions), Decimal(0))
    elif route.mode == "deductions":
        if not answer.strip():
            # No quoted mistakes in an empty submission is not evidence of
            # full credit. Ask the model for the rubric's blank-answer policy.
            raise ValueError("blank_deduction_requires_direct_grade")
        if assessment.awards:
            raise ValueError("mixed_scoring_modes")
        seen_errors: set[str] = set()
        seen_applications: set[tuple[str, str]] = set()
        used_rules: dict[str, int] = {}
        total = _points(plan.max_score)
        reasons = []
        for deduction in assessment.deductions:
            if deduction.error_id in seen_errors or not deduction.error_id:
                raise ValueError("duplicate_deduction")
            seen_errors.add(deduction.error_id)
            if deduction.rule_id not in items or deduction.claim_id not in claims:
                raise ValueError("ungrounded_deduction")
            application = (deduction.rule_id, deduction.claim_id)
            if application in seen_applications:
                raise ValueError("duplicate_rule_application")
            seen_applications.add(application)
            rule = items[deduction.rule_id]
            used_rules[rule.id] = used_rules.get(rule.id, 0) + deduction.occurrences
            if rule.max_occurrences is not None and used_rules[rule.id] > rule.max_occurrences:
                raise ValueError("deduction_rule_limit")
            if deduction.caused_by is not None and deduction.caused_by not in claims:
                raise ValueError("missing_error_origin")
            if deduction.caused_by is not None:
                ancestors, pending = set(), list(claims[deduction.claim_id].depends_on)
                while pending:
                    key = pending.pop()
                    if key not in ancestors:
                        ancestors.add(key)
                        pending.extend(claims[key].depends_on)
                if deduction.caused_by not in ancestors:
                    raise ValueError("error_origin_not_a_dependency")
            amount = _points(rule.points) * deduction.occurrences
            total -= amount
            reasons.append(f"{rule.description}: −{amount}; "
                           f"{claims[deduction.claim_id].quote}\n{deduction.reason}")
        desc = "\n\n".join(reasons) or assessment.rationale or assessment.comment
        descriptions = [(desc, total, not assessment.deductions)]
    else:
        if assessment.score is None or assessment.awards or assessment.deductions:
            raise ValueError("invalid_holistic_ledger")
        total = _points(assessment.score)
        descriptions = [(assessment.rationale or assessment.comment, total,
                         total == _points(plan.max_score))]
    if total > _points(plan.max_score):
        raise ValueError("score_exceeds_question")
    floor = _points(route.minimum_score) if (route.minimum_condition == "always" or answer.strip()) else Decimal(0)
    total = max(total, floor, Decimal(0))
    score = _rounded(total, plan.max_score)
    # Keep the existing nonnegative step scores exactly additive. Apply floors
    # at question level, not by inventing credit for a particular deduction.
    if route.mode != "points" or sum((v for _, v, _ in descriptions), Decimal(0)) != total:
        desc = "\n\n".join(d for d, _, _ in descriptions)
        if floor and total == floor:
            desc += f"\n{route.description}: {route.minimum_score:g}/{plan.max_score:g}"
        return score, [StepScore(step_no=1, desc=desc, score=score, is_correct=score == plan.max_score)]
    # Round cumulative subtotals; the displayed items telescope to the total.
    steps, subtotal, previous = [], Decimal(0), Decimal(0)
    for desc, amount, correct in descriptions:
        subtotal += amount
        current = _points(_rounded(subtotal, plan.max_score))
        steps.append(StepScore(step_no=len(steps) + 1, desc=desc,
                               score=float(current - previous), is_correct=correct))
        previous = current
    return score, steps


def read_math_trace(expert: ExpertResult) -> dict | None:
    try:
        trace = json.loads(expert.raw_output or "")
        if isinstance(trace, dict) and trace.get("version") == TRACE_VERSION:
            return trace
    except (ValueError, TypeError):
        pass
    return None


def _validate_claim_identity(original: MathAssessment, revised: MathAssessment):
    """A tool's claim reference must keep pointing to the same submitted text."""
    quotes = {claim.id: claim.quote for claim in revised.claims}
    if any(quotes.get(claim.id) != claim.quote for claim in original.claims):
        raise ValueError("review_changed_claim_identity")


def _critical_failure(error: BaseException) -> bool:
    from sqlalchemy.exc import SQLAlchemyError
    from backend.domain.errors import DomainError
    from backend.services.grading_checkpoints import CheckpointWriteError
    from backend.services.math_grading_context import _has_critical_plan_failure
    return _has_critical_plan_failure(error, (SQLAlchemyError, DomainError, CheckpointWriteError))


async def review_math_experts(problem, answer, experts, provider, *, language="en", grading_setup=None):
    """Resolve concrete disagreements once, retaining weights and provenance."""
    from backend.services.math_grading_context import get_math_plan
    from backend.services.math_rubric_plan import fallback_math_rubric_plan
    traces = [read_math_trace(expert) for expert in experts]
    if not all(trace and "assessment" in trace for trace in traces):
        return experts
    plan = get_math_plan(problem) or fallback_math_rubric_plan(problem)
    if any(trace["plan_id"] != plan.plan_id for trace in traces):
        return experts
    assessments = [MathAssessment.model_validate(trace["assessment"]) for trace in traces]
    route_ids = {assessment.route_id for assessment in assessments}
    signatures = [{award.item_id: award.score for award in assessment.awards}
                  for assessment in assessments]
    item_ids = set().union(*(signature.keys() for signature in signatures))
    disputed = {key for key in item_ids
                if len({signature.get(key) for signature in signatures}) > 1}
    other_dispute = (len(route_ids) > 1 or
                     len({expert.score for expert in experts}) > 1 or
                     len({_json([d.model_dump() for d in a.deductions]) for a in assessments}) > 1)
    requested_review = any(a.needs_review for a in assessments)
    if not (disputed or other_dispute or requested_review) or not take_review_budget():
        return experts
    try:
        review, _ = await structured_llm_call(
            provider, output_model=ExpertReview,
            system_prompt=build_system_prompt(
                "Review only the disputed mathematical scoring points against original student text, "
                "frozen rubric and supplied scoped tool evidence. This is the only review pass. "
                "Return revisions only when a specific error in an expert's judgment can be supported "
                "by the original text; source_quote must be verbatim from the student answer. "
                "A revision contains expert_index (zero-based), reason, source_quote, and complete "
                "MathAssessment. Preserve undisputed awards unless an explicit needs_review concern "
                "provides source evidence for correcting them. Keep existing claim IDs and their "
                "exact quotes unchanged; add new IDs only for newly identified original text. "
                "Do not invent consensus or change the "
                "rubric. Unknown tools are not evidence of an error. Empty revisions is valid. "
                "Explain concrete residual disagreement; produce no mandatory-review instructions."
                + authoritative_score_instruction(problem.max_score), language, grading_setup),
            user_prompt=_json({"problem": problem.stem, "reference": problem.reference_answer,
                               "answer": answer.content, "plan": plan.model_dump(mode="json"),
                               "disputed_item_ids": sorted(disputed), "experts": traces}),
        )
        revised = list(experts)
        indices = [revision.expert_index for revision in review.revisions]
        if len(indices) != len(set(indices)):
            return experts
        for revision in review.revisions:
            index = revision.expert_index
            if index >= len(experts) or revision.source_quote not in (answer.content or ""):
                continue
            old, candidate = assessments[index], revision.assessment
            if len(route_ids) == 1 and old.awards and not requested_review:
                new_awards = {award.item_id: award.score for award in candidate.awards}
                if any(new_awards.get(key) != value
                       for key, value in signatures[index].items() if key not in disputed):
                    continue
            try:
                _validate_claim_identity(old, candidate)
                score, steps = score_assessment(candidate, plan, answer.content or "")
            except ValueError:
                continue
            trace = dict(traces[index])
            trace["original_assessment"] = trace["assessment"]
            trace["assessment"] = candidate.model_dump(mode="json")
            trace["targeted_review"] = {"provider": provider.provider_id, "reason": revision.reason}
            if steps:
                label = "定向复核依据：" if language == "zh" else "Targeted review: "
                steps[0] = steps[0].model_copy(update={"desc": steps[0].desc + "\n" + label + revision.reason})
            # The original vote remains in the private trace. Make the review
            # reason visible; confidence/configured weights remain unchanged.
            revised[index] = experts[index].model_copy(update={
                "score": score, "steps": steps, "comment": candidate.comment,
                "raw_output": _json(trace),
            })
        return revised
    except Exception as exc:
        if _critical_failure(exc):
            raise
        return experts  # A failed optional review never erases valid grades.


def weighted_math_steps(experts, weights, maximum, score):
    """Project evidence without changing public StepScore or losing the sum."""
    traces = [read_math_trace(expert) for expert in experts]
    if not all(traces):
        return []
    same_route = len({trace.get("assessment", {}).get("route_id") for trace in traces}) == 1
    keys = [[award["item_id"] for award in trace.get("assessment", {}).get("awards", [])]
            for trace in traces]
    if same_route and keys[0] and all(key == keys[0] for key in keys) and all(
            len(expert.steps) == len(keys[0]) for expert in experts):
        result, subtotal, previous = [], Decimal(0), Decimal(0)
        for index in range(len(keys[0])):
            amount = sum(_points(expert.steps[index].score) * weight
                         for expert, weight in zip(experts, weights)) / sum(weights)
            subtotal += amount
            current = _points(_rounded(subtotal, maximum))
            desc = "\n\n".join(f"{expert.provider}: {expert.steps[index].desc}" for expert in experts)
            result.append(StepScore(step_no=index + 1, desc=desc, score=float(current - previous),
                                    is_correct=all(expert.steps[index].is_correct for expert in experts)))
            previous = current
        if _points(score) == previous:
            return result
    desc = "\n\n".join(f"{expert.provider} ({expert.score:g}/{maximum:g}):\n" +
                       "\n".join(step.desc for step in expert.steps) for expert in experts)
    return [StepScore(step_no=1, desc=desc, score=score, is_correct=score == maximum)]


class MathGradingSkill(GradingSkill):
    template_name = "concept"
    uses_math_evidence = True

    async def _substep(self, unit, name):
        if self.reporter and unit:
            await self.reporter.substep(unit, name)

    def _prompt(self, problem, answer, plan, context):
        # There is one maintained prompt per specialization, no embedded old
        # prompt or legacy scoring branch to become a silent fallback.
        template = (Path(__file__).resolve().parents[1] / "prompts" /
                    f"{self.template_name}.txt").read_text(encoding="utf-8")
        return template + "\nINPUT DATA:\n" + _json({
            "problem": problem.stem, "reference_answer": problem.reference_answer,
            "student_answer": answer.content, "rubric_plan": plan.model_dump(mode="json"),
            "retrieved_material": context,
        })

    async def _verify(self, draft, problem, answer, plan):
        from backend.tools.math_verification import MathVerificationRequest, verify_math
        claims = _claim_map(draft, answer.content or "", plan)
        context = "\n".join((problem.stem, problem.reference_answer or "", plan.effective_criterion))
        evidence = []
        deadline = asyncio.get_running_loop().time() + VERIFICATION_BUDGET_SECONDS
        for check in draft.checks:
            if check.claim_id not in claims or check.context_quote not in context:
                evidence.append({"claim_id": check.claim_id, "status": "unknown",
                                 "reason": "The proposed check has no matching source context."})
                continue
            try:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError
                request = MathVerificationRequest.model_validate(check.request)
                result = await asyncio.wait_for(verify_math(request), timeout=min(12, remaining))
                evidence.append({"claim_id": check.claim_id, "context_quote": check.context_quote,
                                 "request": request.model_dump(mode="json"),
                                 **result.model_dump(mode="json")})
            except Exception:
                evidence.append({"claim_id": check.claim_id, "status": "unknown",
                                 "reason": "This relation could not be verified by the tool."})
        return evidence

    async def grade(self, problem, answer, *, student_id="") -> ExpertResult:
        from backend.services.math_grading_context import get_math_plan
        from backend.services.math_rubric_plan import fallback_math_rubric_plan
        plan = get_math_plan(problem) or fallback_math_rubric_plan(problem)
        unit, cm = None, None
        if self.reporter:
            cm = self.reporter.step(student_id, problem.q_id, self.name, self.provider.provider_id)
            unit = await cm.__aenter__()
        chunks, evidence, draft, duration = [], [], None, 0.0
        try:
            await self._substep(unit, "retrieve_knowledge")
            try:
                chunks = await kb_tool.retrieve_for_grading(
                    problem.stem, k=5 if self.template_name == "concept" else 3,
                    scope=self.task_id, provider=self.provider, reporter=self.reporter)
            except Exception:
                # Reference retrieval is optional evidence, not a grade gate.
                chunks = []
            prompt = self._prompt(problem, answer, plan, kb_tool.context_text(chunks))
            if not (answer.content or "").strip():
                # There are no student claims to build. Preserve any explicit
                # teacher blank-answer policy with one direct grading call.
                return await self._direct(problem, answer, plan, prompt, unit, chunks)
            system = build_system_prompt(
                "Grade only the submitted work under the frozen rubric. "
                "Do not reconstruct missing student steps. Give concise checkable scoring evidence. "
                "The tool may verify only a limited mathematical relation, never a whole answer. "
                "Return MathAssessment with the selected route, actual quoted claims and item ledger."
                + authoritative_score_instruction(problem.max_score), self.language, self.grading_setup)
            try:
                await self._substep(unit, "llm_grade")
                draft, response = await structured_llm_call(
                    self.provider, system_prompt=system, user_prompt=prompt,
                    output_model=MathAssessment)
                duration += response.duration_ms or 0
                score, steps = score_assessment(draft, plan, answer.content or "")
            except Exception as exc:
                # A malformed detailed graph must not stop an otherwise
                # gradeable answer. This is one new direct grading interaction.
                kind, _ = classify_skill_error(exc)
                if _critical_failure(exc) or isinstance(exc, (PermanentLLMError, TransientLLMError)) or kind in {
                    "daily_quota_exhausted", "quota_exhausted", "transient_llm"
                }:
                    raise
                return await self._direct(problem, answer, plan, prompt, unit, chunks)
            if draft.checks:
                await self._substep(unit, "verify_result")
                try:
                    # The verifier preserves completed evidence if later
                    # checks exhaust the shared deadline.
                    evidence = await self._verify(draft, problem, answer, plan)
                except Exception:
                    evidence = [{"claim_id": check.claim_id, "status": "unknown",
                                 "reason": "Verification did not complete within its resource budget."}
                                for check in draft.checks]
            claims = {claim.id: claim for claim in draft.claims}
            # A successful CAS check still assumes the model extracted the
            # right relation and conditions. Feed it back before accepting it
            # as scoring evidence, even if it agrees with the draft. This is
            # the evidence-feedback stage, not an unbounded expert debate.
            usable_evidence = any(item["status"] in {"supported", "refuted"}
                                  and item["claim_id"] in claims for item in evidence)
            if usable_evidence or (draft.needs_review and take_review_budget(local=True)):
                await self._substep(unit, "llm_grade")
                try:
                    reviewed, response = await structured_llm_call(
                        self.provider, system_prompt=system,
                        user_prompt=prompt + "\nAPPLY SCOPED EVIDENCE (one pass):\n" + _json({
                            "draft": draft.model_dump(mode="json"), "evidence": evidence,
                            "instruction": "Check affected scoring points against original text and rubric. "
                            "Check that the tool's assumed conditions really hold. Unknown is not incorrect. "
                            "Return a complete ledger with a concrete score; retain supported credit. "
                            "Do not extend the tool result to a different relation or the entire answer. "
                            "Do not propose further tools or reconstruct missing student work. "
                            "Preserve all existing claim IDs and their exact original quotes; "
                            "add new IDs only for newly identified original text. "
                            "State a specific residual concern only when teacher attention would help.",
                        }), output_model=MathAssessment)
                    _validate_claim_identity(draft, reviewed)
                    reviewed_score, reviewed_steps = score_assessment(reviewed, plan, answer.content or "")
                    draft, score, steps = reviewed, reviewed_score, reviewed_steps
                    duration += response.duration_ms or 0
                except Exception as exc:
                    if _critical_failure(exc):
                        raise
                    # The existing valid grade remains useful; do not invent
                    # a failure or another model loop because review failed.
                    pass
            return ExpertResult(
                provider=self.provider.provider_id, score=score, max_score=problem.max_score,
                confidence=draft.confidence, comment=draft.comment, steps=steps,
                knowledge_citations=kb_tool.citations(chunks), duration_ms=duration,
                raw_output=_json({"version": TRACE_VERSION, "plan_id": plan.plan_id,
                                  "assessment": draft.model_dump(mode="json"), "evidence": evidence}),
            )
        except Exception as exc:
            if _critical_failure(exc):
                raise
            kind, friendly = classify_skill_error(exc)
            if isinstance(exc, TransientLLMError) and kind == "general":
                kind = "transient_llm"
            return self._blank_result(problem.q_id, problem.max_score, friendly, error_kind=kind)
        finally:
            if cm:
                await cm.__aexit__(None, None, None)

    async def _direct(self, problem, answer, plan, prompt, unit, chunks):
        await self._substep(unit, "llm_grade")
        result, response = await structured_llm_call(
            self.provider,
            system_prompt=build_system_prompt(
                "Directly grade the student's actual work under the supplied frozen rubric. "
                "Return score, confidence, rationale (specific awarded/lost points), and a concise comment. "
                "No tool verification was completed: do not claim otherwise. Preserve teacher rules, "
                "alternative valid solutions and error follow-through; never impose a generic percentage cap."
                + authoritative_score_instruction(problem.max_score), self.language, self.grading_setup),
            user_prompt=prompt, output_model=DirectMathGrade)
        if result.score > problem.max_score:
            raise ValueError("direct_score_out_of_bounds")
        score = _rounded(_points(result.score), problem.max_score)
        return ExpertResult(
            provider=self.provider.provider_id, score=score, max_score=problem.max_score,
            confidence=result.confidence, comment=result.comment,
            steps=[StepScore(step_no=1, desc=result.rationale, score=score,
                             is_correct=score == problem.max_score)],
            knowledge_citations=kb_tool.citations(chunks), duration_ms=response.duration_ms,
            raw_output=_json({"version": TRACE_VERSION, "plan_id": plan.plan_id,
                              "direct": result.model_dump(mode="json"), "evidence": []}),
        )
