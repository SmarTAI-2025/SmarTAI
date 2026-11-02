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
from time import perf_counter
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from backend.models import ExpertResult, GradingConfidence, StepScore
from backend.skills.base import (
    GradingSkill, authoritative_score_instruction, build_system_prompt,
    classify_skill_error,
)
from backend.tools import knowledge as kb_tool
from backend.tools.structured_llm import PermanentLLMError, TransientLLMError, structured_llm_call
from backend.tools.structured_llm import StructuredOutputInvalidError
from backend.services.math_source_evidence import SourceMatch, match_source_quote, source_catalog
from backend.services.math_proof_audit import inspect_audit, read_appendix, REVIEW_INSTRUCTION, OUTPUT_INSTRUCTION
from backend.services.math_strictness import Adjustment, LEDGER_INSTRUCTION
from backend.services.binary_scoring import frozen_binary_rule

TRACE_VERSION = "math_evidence_v2"
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
    kind: Literal["statement", "omission"] = "statement"
    quote: str = ""
    # A missing obligation is grounded in the question/rubric, never fabricated
    # as text supposedly written by the student.
    source_quote: str = ""
    source_id: str = Field(default="", description=(
        "Prefer an ID from source_catalog over retyping LaTeX. For a statement use student:*, "
        "leave quote empty and the backend copies that original line. For an omission use "
        "question:* or rubric:*, leave source_quote and quote empty. "
        "If also supplying an excerpt, it must match within that indexed line. "
        "Never invent IDs or combine noncontiguous lines into a quotation."
    ))
    source_span: tuple[int, int] | None = None  # computed locally, never trusted from model
    depends_on: list[str] = Field(default_factory=list)
    condition: str = ""
    item_ids: list[str] = Field(default_factory=list)
    verdict: Literal["supported", "refuted", "unknown"] = Field(description=(
        "Whether the student's assertion or required obligation is satisfied. "
        "For kind=omission use refuted when the obligation is missing, or unknown "
        "when uncertain; never supported merely because you identified an omission."
    ))
    reason: str


class Verification(BaseModel):
    claim_id: str
    # Conditions are proposed by the model, never asserted to be source facts
    # merely because a CAS accepted them.
    context_quote: str = Field(min_length=1)
    request: dict


_ITEM_FEEDBACK_DESCRIPTION = (
    "Concise, self-contained feedback for this scoring item: explain what the student's "
    "work establishes or misses and why it earns this credit or deduction, in one or two sentences. "
    "Do not restate the rubric, paste evidence quotations, or repeat the same point. "
    "Keep exact source evidence in claims."
)


class Award(BaseModel):
    item_id: str
    score: float = Field(ge=0, allow_inf_nan=False)
    claim_ids: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1, description=_ITEM_FEEDBACK_DESCRIPTION)
    adjustment: Adjustment | None = None
    conditional_credit: float = Field(default=0, ge=0, allow_inf_nan=False)
    caused_by: str | None = Field(default=None, description=(
        "For adjustment=follow_through, the refuted ancestor claim.id of the original error."
    ))


class Deduction(BaseModel):
    error_id: str
    rule_id: str
    claim_id: str
    occurrences: int = Field(default=1, ge=1, le=100)
    caused_by: str | None = Field(default=None, description=(
        "The originating claim.id, never a deduction.error_id. It must name a "
        "refuted ancestor in this claim's depends_on chain. Null for an independent error."
    ))
    exemption_id: str | None = None
    reason: str = Field(min_length=1, description=_ITEM_FEEDBACK_DESCRIPTION)
    adjustment: Adjustment | None = None


class EvidenceJudgment(BaseModel):
    evidence_id: str
    applicability: Literal["applies", "partial", "not_applicable", "uncertain"]
    reason: str = Field(min_length=1)


class MathAssessment(BaseModel):
    route_id: str
    route_reason: str
    claims: list[Claim] = Field(default_factory=list, max_length=80)
    checks: list[Verification] = Field(default_factory=list, max_length=8)
    awards: list[Award] = Field(default_factory=list, max_length=80)
    deductions: list[Deduction] = Field(default_factory=list, max_length=80)
    score: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    binary_correct: bool | None = Field(default=None, strict=True, description=(
        "For an explicit whole-question zero/full rule only: whether the entire final answer "
        "satisfies that rule. Judge correctness independently of the proposed numerical score."
    ))
    confidence: GradingConfidence = 0.0
    rationale: str = ""  # Detailed evidence for holistic scoring; comment stays concise.
    comment: str = Field(description=(
        "Overall conclusion and useful next-step guidance, following the configured feedback "
        "tone and length. Do not enumerate the scoring items or repeat the detailed rationale, "
        "item explanations, point totals, source quotations or rubric text."
    ))
    needs_review: bool = False
    evidence_judgments: list[EvidenceJudgment] = Field(default_factory=list, max_length=8)


class DirectMathGrade(BaseModel):
    """Last resort: a genuine model score with an explicit, usable rationale."""
    score: float = Field(ge=0, allow_inf_nan=False)
    binary_correct: bool | None = Field(default=None, strict=True, description=(
        "Required when binary_rule is supplied: whether the final answer satisfies that exact rule."
    ))
    confidence: GradingConfidence = 0.0
    rationale: str = Field(min_length=1)
    comment: str

    @field_validator("rationale")
    @classmethod
    def require_scoring_evidence(cls, value):
        if not value.strip():
            raise ValueError("direct_grade_requires_scoring_evidence")
        return value


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


def _binary_score(correct, plan, answer, *, claims=None):
    if not answer.strip():
        return 0.0
    # Reuse an unambiguous existing verdict only if it judges the WHOLE
    # submitted answer. A correct intermediate statement cannot imply full marks.
    whole = [claim for claim in (claims or {}).values()
             if claim.kind == "statement" and claim.quote.strip() == answer.strip()]
    verdicts = {claim.verdict for claim in whole}
    if verdicts and (len(verdicts) != 1 or "unknown" in verdicts):
        raise ValueError("binary_rule_requires_consistent_answer_verdict")
    if len(verdicts) == 1 and "unknown" not in verdicts:
        grounded = next(iter(verdicts)) == "supported"
        if correct is not None and correct != grounded:
            raise ValueError("binary_correct_conflicts_with_answer_verdict")
        correct = grounded
    if correct is None:
        raise ValueError("binary_rule_requires_final_answer_judgment")
    return float(plan.max_score) if correct else 0.0


def _locate_claim(claim, answer, plan, problem_text, catalog):
    if claim.source_id:
        entry = catalog.get(claim.source_id)
        allowed = (claim.source_id.startswith("student:") if claim.kind == "statement" else
                   claim.source_id.startswith(("question:", "rubric:")))
        if entry is None or not allowed:
            raise ValueError("invalid_claim_source_id")
        text, start, end = entry
        excerpt = claim.quote if claim.kind == "statement" else claim.source_quote
        if excerpt:
            found = match_source_quote(text, excerpt)
            if found is None:
                raise ValueError("claim_excerpt_conflicts_with_source_id")
            return claim.source_id.split(":", 1)[0], SourceMatch(
                found.text, start + found.start, start + found.end, found.method)
        return claim.source_id.split(":", 1)[0], SourceMatch(text, start, end, "exact")
    if claim.kind == "statement":
        found = match_source_quote(answer, claim.quote)
        return ("student", found) if found else None
    return next(((document, found) for document, source in (
        ("question", problem_text), ("rubric", plan.original_criterion),
        ("effective_rubric", plan.effective_criterion))
        if (found := match_source_quote(source, claim.source_quote))), None)


def _claim_map(assessment: MathAssessment, answer: str, plan, *, problem_text="") -> dict[str, Claim]:
    claims = {claim.id: claim for claim in assessment.claims}
    catalog = source_catalog(answer, problem_text, plan.original_criterion)
    if len(claims) != len(assessment.claims) or "" in claims:
        raise ValueError("duplicate_or_empty_claim_id")
    item_ids = {item.id for route in plan.routes if route.id == assessment.route_id for item in (*route.items, *route.exemptions)}
    holistic = any(route.id == assessment.route_id and route.mode == "holistic" for route in plan.routes)
    for claim in claims.values():
        if holistic:
            # Holistic routes have no executable item IDs. Models may label
            # prose criteria here; those unused labels must not discard valid
            # sourced evidence. Numeric ledgers retain strict item validation.
            claim.item_ids = []
        if not claim.item_ids:
            # The ledger itself is an explicit many-to-many link. Materialize
            # omitted reverse links; never overwrite a contradictory mapping
            # or infer a score from the presence of a claim.
            claim.item_ids = list(dict.fromkeys([
                *(award.item_id for award in assessment.awards if claim.id in award.claim_ids),
                *(deduction.rule_id for deduction in assessment.deductions if claim.id == deduction.claim_id),
            ]))
        if not claim.reason.strip():
            raise ValueError("claim_requires_reason")
        if not set(claim.item_ids) <= item_ids:
            raise ValueError("claim_not_in_student_answer_or_rubric")
        if claim.kind == "omission":
            claim.source_span = None
            # The same obligation may appear in both the original and frozen
            # rubric. That is not ambiguous within either actual document.
            location = _locate_claim(claim, answer, plan, problem_text, catalog)
            if claim.quote or location is None or claim.verdict == "supported":
                raise ValueError("omission_requires_original_obligation")
            claim.source_quote = location[1].text
        else:
            location = _locate_claim(claim, answer, plan, problem_text, catalog)
            if location is None:
                raise ValueError("claim_not_in_student_answer_or_rubric")
            located = location[1]
            claim.quote = located.text
            claim.source_span = (located.start, located.end)
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


def _error_ancestors(claims, targets):
    ancestors, pending = set(), [key for target in targets for key in claims[target].depends_on]
    while pending:
        key = pending.pop()
        if key not in ancestors:
            ancestors.add(key)
            pending.extend(claims[key].depends_on)
    return {key for key in ancestors if claims[key].verdict == "refuted"}


def _complete_origin(kind, origin, claims, targets):
    # Only complete redundant metadata when the model explicitly classified
    # follow-through and its already validated graph names one unique error.
    if kind == "follow_through" and origin is None:
        candidates = _error_ancestors(claims, targets)
        if len(candidates) == 1:
            return next(iter(candidates))
    return origin


def _default_fraction(kind, item, claims, targets, origin, plan):
    """Validate eligibility from the frozen plan, never from answer keywords."""
    if kind not in item.default_adjustments or not targets:
        raise ValueError("default_adjustment_not_in_plan")
    if kind == "minor_omission":
        if not any(claims[key].kind == "omission" and claims[key].verdict == "refuted" for key in targets):
            raise ValueError("minor_adjustment_requires_omission")
        if origin is not None:
            raise ValueError("minor_adjustment_is_not_follow_through")
    elif (not any(claims[key].kind == "statement" for key in targets)
          or origin not in _error_ancestors(claims, targets)):
        raise ValueError("follow_through_requires_error_ancestor")
    return _points(plan.strictness) / 100 if plan.allow_partial_credit else Decimal(1)


def score_assessment(assessment: MathAssessment, plan, answer: str, *, problem_text="") -> tuple[float, list[StepScore]]:
    """Validate and sum one selected rubric route; no CAS-result score rules."""
    claims = _claim_map(assessment, answer, plan, problem_text=problem_text)
    if answer.strip() and not claims:
        raise ValueError("detailed_assessment_requires_claims")
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
            if (award.score > item.points or not set(award.claim_ids) <= claims.keys()
                    or not award.reason.strip() or (award.score > 0 and not award.claim_ids)):
                raise ValueError("invalid_award")
            if any(item_id not in claims[key].item_ids for key in award.claim_ids):
                raise ValueError("award_claim_not_mapped_to_item")
            if award.score > 0 and all(claims[key].kind == "omission" for key in award.claim_ids):
                raise ValueError("omission_is_not_evidence_of_credit")
            amount = _points(award.score)
            formula = ""
            if award.adjustment is not None:
                award.caused_by = _complete_origin(award.adjustment, award.caused_by, claims, award.claim_ids)
                fraction = _default_fraction(award.adjustment, item, claims, award.claim_ids, award.caused_by, plan)
                conditional = _points(award.conditional_credit)
                if not answer.strip() or amount + conditional > _points(item.points):
                    raise ValueError("conditional_credit_exceeds_item")
                amount += conditional * (1 - fraction)
                formula = f"\n{award.score:g} + {conditional} × {1 - fraction} = {amount}"
            elif award.conditional_credit or award.caused_by is not None:
                raise ValueError("conditional_credit_requires_policy")
            # The rubric and exact sources already live in the plan/claim graph.
            # Display the assessment once, not rubric + source + its paraphrase.
            desc = f"{award.reason}{formula}".strip()
            descriptions.append((desc, amount, amount == _points(item.points)))
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
        exemptions = {item.id: item for item in route.exemptions}
        total = _points(plan.max_score)
        reasons = []
        charges = []
        for deduction in assessment.deductions:
            if not deduction.reason.strip():
                raise ValueError("deduction_requires_scoring_evidence")
            if deduction.error_id in seen_errors or not deduction.error_id:
                raise ValueError("duplicate_deduction")
            seen_errors.add(deduction.error_id)
            if deduction.rule_id not in items.keys() | exemptions.keys() or deduction.claim_id not in claims:
                raise ValueError("ungrounded_deduction")
            application = (deduction.rule_id, deduction.claim_id)
            if application in seen_applications:
                raise ValueError("duplicate_rule_application")
            seen_applications.add(application)
            if deduction.rule_id in exemptions:
                exemption = exemptions[deduction.rule_id]
                if exemption.id not in claims[deduction.claim_id].item_ids:
                    raise ValueError("deduction_claim_not_mapped_to_item")
                reasons.append(f"−0: {deduction.reason}\n{exemption.description}")
                continue
            rule = items[deduction.rule_id]
            if rule.id not in claims[deduction.claim_id].item_ids:
                raise ValueError("deduction_claim_not_mapped_to_item")
            deduction.caused_by = _complete_origin(deduction.adjustment, deduction.caused_by,
                                                   claims, [deduction.claim_id])
            if deduction.caused_by is not None and deduction.caused_by not in claims:
                raise ValueError("missing_error_origin")
            if deduction.caused_by is not None:
                if claims[deduction.caused_by].verdict != "refuted":
                    raise ValueError("error_origin_must_identify_an_error")
                ancestors, pending = set(), list(claims[deduction.claim_id].depends_on)
                while pending:
                    key = pending.pop()
                    if key not in ancestors:
                        ancestors.add(key)
                        pending.extend(claims[key].depends_on)
                if deduction.caused_by not in ancestors:
                    raise ValueError("error_origin_not_a_dependency")
            if deduction.exemption_id is not None and deduction.exemption_id not in exemptions:
                raise ValueError("unknown_deduction_exemption")
            consequential = deduction.caused_by is not None and route.error_policy == "no_repeat_deduction"
            count = 0 if consequential or deduction.exemption_id else deduction.occurrences
            unit_loss = _points(rule.points)
            if deduction.adjustment is not None and count:
                unit_loss *= _default_fraction(deduction.adjustment, rule, claims,
                    [deduction.claim_id], deduction.caused_by, plan)
            policy = (exemptions[deduction.exemption_id].description if deduction.exemption_id else
                      route.error_policy_source_quote if consequential else "")
            charges.append((unit_loss, deduction, rule, count, policy, len(reasons)))
            reasons.append("")
        # Apply count caps to the largest eligible per-occurrence losses, not
        # whichever error the model lists first. Keep explanations in source
        # order and validate every charge before allocating the allowance.
        for unit_loss, deduction, rule, count, policy, index in sorted(
            charges, key=lambda charge: (-charge[0], charge[1].error_id),
        ):
            if rule.max_occurrences is not None:
                count = min(count, max(0, rule.max_occurrences - used_rules.get(rule.id, 0)))
            amount = unit_loss * count
            if amount:
                used_rules[rule.id] = used_rules.get(rule.id, 0) + count
            total -= amount
            reasons[index] = f"−{amount}: {deduction.reason}" + (f"\n{policy}" if policy else "")
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
    if any(not desc.strip() for desc, _, _ in descriptions):
        raise ValueError("missing_scoring_evidence")
    floor = _points(route.minimum_score) if (route.minimum_condition == "always" or answer.strip()) else Decimal(0)
    total = max(total, floor, Decimal(0))
    rule = frozen_binary_rule(plan)
    if rule:
        score = _binary_score(assessment.binary_correct, plan, answer, claims=claims)
        desc = "\n\n".join(d for d, _, _ in descriptions)
        return score, [StepScore(step_no=1, desc=f"{desc}\n{rule}: {score:g}/{plan.max_score:g}",
                                score=score, is_correct=score == plan.max_score)]
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


def _deduction_signature(assessment: MathAssessment) -> tuple:
    """Compare actual error locations/rules, not model-local IDs or wording."""
    quotes = {claim.id: claim.quote for claim in assessment.claims}
    return tuple(sorted((item.rule_id, item.exemption_id or "", quotes.get(item.claim_id, ""), item.occurrences,
                         quotes.get(item.caused_by, ""), item.adjustment or "")
                        for item in assessment.deductions))


def _award_signatures(assessment):
    quotes = {claim.id: claim.quote for claim in assessment.claims}
    return {award.item_id: (award.score, award.conditional_credit, award.adjustment,
                           quotes.get(award.caused_by, "")) for award in assessment.awards}


def math_scoring_disagreement(experts: list[ExpertResult]) -> bool:
    """Residual item disagreements remain advice even when totals coincide."""
    signatures = set()
    for expert in experts:
        trace = read_math_trace(expert)
        if expert.error_kind or not trace or not isinstance(trace.get("assessment"), dict):
            continue
        assessment = MathAssessment.model_validate(trace["assessment"])
        signatures.add((trace.get("plan_id"), assessment.route_id, expert.score,
                        tuple(sorted(_award_signatures(assessment).items())),
                        _deduction_signature(assessment)))
    scores = {expert.score for expert in experts if not expert.error_kind and read_math_trace(expert)}
    return len(signatures) > 1 or len(scores) > 1


def _evidence_pending(assessment, evidence):
    decisions = {item.evidence_id: item for item in assessment.evidence_judgments}
    claims = {claim.id: claim for claim in assessment.claims}
    for item in evidence:
        if item.get("status") not in {"supported", "refuted"}:
            continue
        decision = decisions.get(item.get("evidence_id"))
        if decision is None or decision.applicability == "uncertain":
            return True
        # A check of only one component may be marked partial with an explicit
        # explanation. Claim-wide applicability must agree with that verdict;
        # neither choice dictates the score or proves the entire argument.
        claim = claims.get(item.get("claim_id"))
        if decision.applicability == "applies" and (claim is None or claim.verdict != item["status"]):
            return True
    return False


def _with_pending_advice(expert, language):
    """Project a real unresolved contradiction through existing evidence text."""
    trace = read_math_trace(expert)
    if not trace:
        return expert
    proof_concerns = trace.get("proof_audit", {}).get("review_concerns", [])
    if not trace.get("review_pending") and not proof_concerns:
        return expert
    label = ("工具核验与当前判断仍有待核对之处：" if language == "zh" else
             "Tool verification remains unresolved: ")
    reasons = [item.get("reason", "") for item in trace.get("evidence", [])
               if item.get("status") in {"supported", "refuted"}]
    advice = []
    if trace.get("review_pending"):
        advice.append((label, label + ("; ".join(reasons) or (
            "核验反馈未完成。" if language == "zh" else "Evidence feedback did not complete."))))
    if proof_concerns:
        label = "证明依据核对建议：" if language == "zh" else "Proof evidence to check: "
        advice.append((label, label + "; ".join(item["reason"] for item in proof_concerns)))
    steps = list(expert.steps)
    if steps:
        additions = [text for label, text in advice if label not in steps[0].desc]
        if additions:
            steps[0] = steps[0].model_copy(update={"desc": steps[0].desc + "\n" + "\n".join(additions)})
    trace["review_advisory"] = True
    return expert.model_copy(update={"steps": steps, "raw_output": _json(trace)})


def _claim_identity(claim: Claim, location):
    # Identity is the trusted source location, not the model's address syntax.
    # Unique quote <-> line ID may change; another occurrence of the same text
    # or a different source document may not. Never trust supplied source_span.
    if location is None:
        return None
    document, found = location
    return claim.kind, document, found.text, found.start, found.end


def _validate_claim_identity(original: MathAssessment, revised: MathAssessment, *, answer, plan, problem_text):
    """A tool's claim reference must keep pointing to the same submitted text."""
    catalog = source_catalog(answer, problem_text, plan.original_criterion)
    def identity(claim):
        return _claim_identity(claim, _locate_claim(claim, answer, plan, problem_text, catalog))
    quotes = {claim.id: identity(claim) for claim in revised.claims}
    original_quotes = {claim.id: identity(claim) for claim in original.claims}
    if any(value is None or quotes.get(key) != value for key, value in original_quotes.items()):
        raise ValueError("review_changed_claim_identity")


def _assessment_validator(plan, answer, problem, *, original=None, diagnostic=None, evidence=None):
    """Repair local validation failures within the parser's ONE repair budget.

    Matching excerpts are fixed before feedback and must survive a repair. A
    bad excerpt is never simply dropped (which could erase a valid deduction).
    """
    preserved = {}
    catalog = source_catalog(answer.content or "", problem.stem, plan.original_criterion)

    def validate(draft):
        if diagnostic is not None:
            diagnostic.setdefault("first_draft", draft.model_dump(mode="json"))
            diagnostic["draft"] = draft.model_dump(mode="json")
        unresolved = []
        current = {}
        try:
            for claim in draft.claims:
                try:
                    location = _locate_claim(claim, answer.content or "", plan, problem.stem, catalog)
                except ValueError as exc:
                    unresolved.append({"claim_id": claim.id, "reason": str(exc)})
                    continue
                if location:
                    found = location[1]
                    if claim.kind == "statement":
                        claim.quote = found.text
                    else:
                        claim.source_quote = found.text
                    if claim.kind == "statement" or not claim.quote:
                        current[claim.id] = _claim_identity(claim, location)
                else:
                    unresolved.append({"claim_id": claim.id, "reason": "source_not_located"})
            if any(current.get(key) != identity for key, identity in preserved.items()):
                raise ValueError("repair_changed_valid_source_claim")
            if original is not None:
                _validate_claim_identity(original, draft, answer=answer.content or "", plan=plan,
                                         problem_text=problem.stem)
            score_assessment(draft, plan, answer.content or "", problem_text=problem.stem)
            if evidence is not None:
                required = {item["evidence_id"] for item in evidence if item.get("status") in {"supported", "refuted"}}
                ids = [item.evidence_id for item in draft.evidence_judgments]
                if len(ids) != len(set(ids)) or set(ids) != required:
                    raise ValueError("review_must_address_each_scoped_evidence_result")
        except ValueError as exc:
            if diagnostic is not None:
                diagnostic.setdefault("validation_failures", []).append(str(exc))
            if not preserved:
                preserved.update(current)
            error = StructuredOutputInvalidError("Invalid mathematical evidence or ledger.")
            hint = {
                "omission_requires_original_obligation": "For kind=omission, quote must be empty, "
                    "source_quote must locate the required obligation, and verdict must be refuted or unknown, "
                    "not supported. Do not replace a valid source quote merely to repair its verdict.",
                "missing_error_origin": "caused_by must name the originating claim.id, not deduction.error_id; "
                    "the claim must be a refuted ancestor. Preserve the actual dependency and error judgment.",
            }.get(str(exc), "")
            error.repair_context = _json({"validation_error": str(exc), "validation_hint": hint,
                "unresolved_sources": unresolved,
                "preserve_claim_quotes": preserved, "draft": draft.model_dump(mode="json"),
                "repair_scope": "Fix invalid source citations or ledger links using the original input. "
                    "Preserve all located claim IDs and quotes. Do not invent omitted student work. "
                    "Use source_catalog IDs and empty quote fields for unresolved citations instead of "
                    "retyping or rewriting LaTeX. Do not drop claims merely to pass validation. "
                    "Use omission evidence with a question/rubric source for a missing obligation."})
            raise error from exc
    return validate


def _critical_failure(error: BaseException) -> bool:
    from sqlalchemy.exc import SQLAlchemyError
    from backend.domain.errors import DomainError
    from backend.services.grading_checkpoints import CheckpointWriteError
    from backend.services.math_grading_context import _has_critical_plan_failure
    return _has_critical_plan_failure(error, (SQLAlchemyError, DomainError, CheckpointWriteError))


def _schedule_checks(draft, problem, answer, plan):
    """Give grounded, distinct checks the limited slots, not invalid proposals."""
    from backend.services.math_tool_planning import automatic_checks, binding_error
    from backend.tools.math_verification import MathVerificationRequest
    claims = {claim.id: claim for claim in draft.claims}
    scheduled, rejected, seen = [], [], set()
    candidates = [*draft.checks, *(Verification.model_validate(check) for check in
        automatic_checks(draft.claims, problem, plan, student_answer=answer.content or ""))]
    for check in candidates:
        try:
            request = MathVerificationRequest.model_validate(check.request)
            claim = claims.get(check.claim_id)
            issue = ("missing_student_claim" if claim is None or claim.kind != "statement" else
                binding_error(request, claim.quote, check.context_quote, problem=problem, plan=plan))
            if issue:
                rejected.append({"claim_id": check.claim_id, "reason": issue})
                continue
            canonical = request.model_dump(mode="json")
            key = check.claim_id, _json(canonical)
            if key not in seen and len(scheduled) < 8:
                scheduled.append(check.model_copy(update={"request": canonical}))
                seen.add(key)
        except ValueError:
            rejected.append({"claim_id": check.claim_id, "reason": "invalid_verification_request"})
    return scheduled, rejected


async def review_math_experts(problem, answer, experts, provider, *, language="en", grading_setup=None):
    """Resolve concrete disagreements once, retaining weights and provenance."""
    from backend.services.math_grading_context import get_math_plan
    from backend.services.math_rubric_plan import fallback_math_rubric_plan
    traces = [read_math_trace(expert) for expert in experts]
    if not all(trace and ("assessment" in trace or "direct" in trace) for trace in traces):
        return experts
    plan = get_math_plan(problem) or fallback_math_rubric_plan(problem, grading_setup=grading_setup)
    if any(trace["plan_id"] != plan.plan_id for trace in traces):
        return experts
    assessments = [MathAssessment.model_validate(trace["assessment"]) if "assessment" in trace else None for trace in traces]
    route_ids = {assessment.route_id for assessment in assessments if assessment is not None}
    signatures = [_award_signatures(assessment) if assessment is not None else {}
                  for assessment in assessments]
    item_ids = set().union(*(signature.keys() for signature in signatures))
    disputed = {key for key in item_ids
                if len({signature.get(key) for signature in signatures}) > 1}
    other_dispute = (len(route_ids) > 1 or
                     len({expert.score for expert in experts}) > 1 or
                     len({_deduction_signature(a) for a in assessments if a is not None}) > 1)
    requested_review = any(a.needs_review for a in assessments if a is not None) or any(
        trace.get("review_pending") or trace.get("proof_audit", {}).get("review_concerns") for trace in traces)
    if not (disputed or other_dispute or requested_review) or not take_review_budget():
        return experts

    def validate_review(review):
        try:
            indices = [revision.expert_index for revision in review.revisions]
            if len(indices) != len(set(indices)):
                raise ValueError("duplicate_expert_revision")
            for revision in review.revisions:
                index = revision.expert_index
                if index >= len(experts) or match_source_quote(answer.content or "", revision.source_quote) is None:
                    raise ValueError("review_source_or_expert_not_found")
                old, candidate = assessments[index], revision.assessment
                if len(route_ids) == 1 and old is not None and old.awards and not requested_review:
                    new_awards = _award_signatures(candidate)
                    if any(new_awards.get(key) != value for key, value in signatures[index].items() if key not in disputed):
                        raise ValueError("review_changed_undisputed_award")
                _assessment_validator(plan, answer, problem, original=old,
                    evidence=traces[index].get("evidence", []))(candidate)
        except (ValueError, StructuredOutputInvalidError) as exc:
            error = StructuredOutputInvalidError("Invalid targeted mathematical review.")
            error.repair_context = _json({"validation_error": str(exc),
                "detail": getattr(exc, "repair_context", None),
                "review": review.model_dump(mode="json"),
                "repair_scope": "Correct this review using the original experts and source. "
                    "Keep established claim IDs/quotes and undisputed awards; do not invent student work."})
            raise error from exc

    try:
        review, response = await structured_llm_call(
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
                "Also resolve pending tool evidence, even when experts agree on the score. "
                "A direct grade is a valid vote, not a reason to skip a dispute. "
                "Include a revision even if its score stays the same when resolving evidence. "
                "In its assessment return evidence_judgments for every supplied supported/refuted "
                "evidence_id with applicability (applies/partial/not_applicable/uncertain) and source-based reason. "
                "Use applies only when the result covers this assertion and agrees with its verdict; "
                "use partial with an explanation when it covers only part of the claim. "
                "Explain concrete residual disagreement; produce no mandatory-review instructions."
                " For explicit zero/full rules, include binary_correct judging the entire final answer."
                + ("\n" + REVIEW_INSTRUCTION + " After the complete review JSON, append PROOF_AUDIT "
                   "followed by a separate JSON object mapping each revised proof expert's index "
                   "(string) to an audit object with the same steps/context_source_ids shape as its trace. "
                   "This optional appendix must never be placed inside a revision or assessment."
                   if any("proof_audit" in trace for trace in traces) else "")
                + authoritative_score_instruction(problem.max_score)
                + "\nJSON Schema:\n" + _json(ExpertReview.model_json_schema()), language, grading_setup,
                **({"output_instruction": OUTPUT_INSTRUCTION} if any("proof_audit" in trace for trace in traces) else {})),
            user_prompt=_json({"problem": problem.stem, "reference": problem.reference_answer,
                               "answer": answer.content, "plan": plan.model_dump(mode="json"),
                               "source_catalog": {key: value[0] for key, value in
                                   source_catalog(answer.content or "", problem.stem, plan.original_criterion).items()},
                               "disputed_item_ids": sorted(disputed), "experts": traces}),
            validate_output=validate_review,
        )
        # Mock callers may bypass structured_llm_call's validator; this is also
        # a final guard before applying any revision to the valid votes.
        validate_review(review)
        revised = list(experts)
        for revision in review.revisions:
            index = revision.expert_index
            old, candidate = assessments[index], revision.assessment
            score, steps = score_assessment(candidate, plan, answer.content or "", problem_text=problem.stem)
            trace = dict(traces[index])
            if "assessment" in trace:
                trace["original_assessment"] = trace["assessment"]
            else:
                trace["original_direct"] = trace.pop("direct")
            trace["assessment"] = candidate.model_dump(mode="json")
            trace["review_pending"] = _evidence_pending(candidate, trace.get("evidence", []))
            if "proof_audit" in trace:
                trace["proof_audit"] = inspect_audit(
                    read_appendix(getattr(response, "content", None), expert_index=index), candidate,
                    answer.content or "", problem.stem, plan, previous=trace["proof_audit"])
            trace.pop("review_advisory", None)
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
        return [_with_pending_advice(expert, language) for expert in revised]
    except Exception as exc:
        if _critical_failure(exc):
            raise
        retained = []
        for expert, trace in zip(experts, traces):
            trace = dict(trace, review_failure=type(exc).__name__)
            retained.append(_with_pending_advice(expert.model_copy(update={"raw_output": _json(trace)}), language))
        return retained  # A failed optional review never erases valid grades.


def weighted_math_steps(experts, weights, maximum, score):
    """Project evidence without changing public StepScore or losing the sum."""
    from backend.services.grading_feedback import join_expert_feedback
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
            desc = join_expert_feedback((expert.provider, expert.steps[index].desc) for expert in experts)
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
            from backend.services.math_grading_context import report_math_progress
            await report_math_progress(self.reporter.substep, unit, name)

    def _prompt(self, problem, answer, plan, context):
        # There is one maintained prompt per specialization, no embedded old
        # prompt or legacy scoring branch to become a silent fallback.
        template = (Path(__file__).resolve().parents[1] / "prompts" /
                    f"{self.template_name}.txt").read_text(encoding="utf-8")
        return template + "\n" + LEDGER_INSTRUCTION + "\nINPUT DATA:\n" + _json({
            "problem": problem.stem, "reference_answer": problem.reference_answer,
            "student_answer": answer.content, "rubric_plan": plan.model_dump(mode="json"),
            "binary_rule": frozen_binary_rule(plan),
            "source_catalog": {key: value[0] for key, value in
                source_catalog(answer.content or "", problem.stem, plan.original_criterion).items()},
            "retrieved_material": context,
        })

    async def _verify(self, draft, problem, answer, plan):
        from backend.tools.math_verification import MathVerificationRequest, verify_math
        from backend.services.math_tool_planning import binding_error
        claims = _claim_map(draft, answer.content or "", plan, problem_text=problem.stem)
        evidence = []
        deadline = asyncio.get_running_loop().time() + VERIFICATION_BUDGET_SECONDS
        for index, check in enumerate(draft.checks):
            base = {"evidence_id": f"v{index + 1}", "claim_id": check.claim_id,
                    "context_quote": check.context_quote, "request": check.request}
            try:
                request = MathVerificationRequest.model_validate(check.request)
                claim = claims.get(check.claim_id)
                issue = ("missing_student_claim" if claim is None or claim.kind != "statement" else
                    binding_error(request, claim.quote, check.context_quote, problem=problem, plan=plan))
                if issue:
                    evidence.append({**base, "status": "unknown", "method": "source_binding",
                        "binding_issue": issue,
                        "reason": "The proposed relation or conditions could not be grounded in the submitted sources."})
                    continue
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError
                result = await asyncio.wait_for(verify_math(request), timeout=min(12, remaining))
                evidence.append({**base, "source_binding": "explicit_syntax",
                                 "request": request.model_dump(mode="json"),
                                 **result.model_dump(mode="json")})
            except Exception as exc:
                if _critical_failure(exc):
                    raise
                evidence.append({**base, "status": "unknown",
                                 "reason": "This relation could not be verified by the tool."})
        return evidence

    async def grade(self, problem, answer, *, student_id="") -> ExpertResult:
        started = perf_counter()
        from backend.services.math_grading_context import get_math_plan
        from backend.services.math_rubric_plan import fallback_math_rubric_plan
        plan = get_math_plan(problem) or fallback_math_rubric_plan(problem, grading_setup=self.grading_setup)
        unit, cm = None, None
        if self.reporter:
            from backend.services.math_grading_context import report_math_progress
            cm = self.reporter.step(student_id, problem.q_id, self.name, self.provider.provider_id)
            unit = await report_math_progress(cm.__aenter__)
        chunks, evidence, draft, duration = [], [], None, 0.0
        diagnostic = {}
        try:
            await self._substep(unit, "retrieve_knowledge")
            try:
                chunks = await kb_tool.retrieve_for_grading(
                    problem.stem, k=5 if self.template_name == "concept" else 3,
                    scope=self.task_id, provider=self.provider, reporter=self.reporter)
            except Exception as exc:
                if _critical_failure(exc):
                    raise
                # Reference retrieval is optional evidence, not a grade gate.
                chunks = []
            prompt = self._prompt(problem, answer, plan, kb_tool.context_text(chunks))
            if not (answer.content or "").strip():
                # There are no student claims to build. Preserve any explicit
                # teacher blank-answer policy with one direct grading call.
                return await self._direct(problem, answer, plan, prompt, unit, chunks, started=started)
            system = build_system_prompt(
                "Grade only the submitted work under the frozen rubric. "
                "Do not reconstruct missing student steps. Give concise checkable scoring evidence. "
                "The tool may verify only a limited mathematical relation, never a whole answer. "
                "Return MathAssessment with the selected route, actual source claims and item ledger. "
                "Prefer source_catalog IDs for evidence: student:* for statements, question:* or rubric:* "
                "for omissions, with quote/source_quote empty so the backend copies the exact source. "
                "Each ID is one original line, not proof that a claim is true. If no suitable ID exists, "
                "copy an exact excerpt; do not join noncontiguous lines or rewrite the mathematics."
                " If binary_rule is supplied, judge the entire final answer in binary_correct; "
                "do not infer correctness from your proposed score or a correct intermediate step."
                + authoritative_score_instruction(problem.max_score)
                + "\nJSON Schema:\n" + _json(MathAssessment.model_json_schema()), self.language, self.grading_setup,
                **({"output_instruction": OUTPUT_INSTRUCTION} if self.template_name == "proof" else {}))
            try:
                await self._substep(unit, "llm_grade")
                draft, response = await structured_llm_call(
                    self.provider, system_prompt=system, user_prompt=prompt,
                    output_model=MathAssessment,
                    validate_output=_assessment_validator(plan, answer, problem, diagnostic=diagnostic))
                duration += response.duration_ms or 0
                score, steps = score_assessment(draft, plan, answer.content or "", problem_text=problem.stem)
            except Exception as exc:
                # A malformed detailed graph must not stop an otherwise
                # gradeable answer. This is one new direct grading interaction.
                kind, _ = classify_skill_error(exc)
                if _critical_failure(exc) or isinstance(exc, (PermanentLLMError, TransientLLMError)) or kind in {
                    "zero_quota", "daily_quota_exhausted", "quota_exhausted", "transient_llm"
                }:
                    raise
                return await self._direct(problem, answer, plan, prompt, unit, chunks,
                    started=started,
                    fallback={"reason": "invalid_detailed_assessment", "error_type": type(exc).__name__,
                              "draft": draft.model_dump(mode="json") if draft else diagnostic.get("draft")})
            proof_audit = (inspect_audit(read_appendix(getattr(response, "content", None)), draft, answer.content or "", problem.stem, plan)
                           if self.template_name == "proof" else None)
            scheduled, rejected_checks = _schedule_checks(draft, problem, answer, plan)
            draft.checks = scheduled
            tool_applicability = "applicable" if scheduled else "not_applicable"
            if draft.checks:
                await self._substep(unit, "verify_result")
                try:
                    # The verifier preserves completed evidence if later
                    # checks exhaust the shared deadline.
                    evidence = await self._verify(draft, problem, answer, plan)
                except Exception as exc:
                    if _critical_failure(exc):
                        raise
                    evidence = [{"claim_id": check.claim_id, "status": "unknown",
                                 "reason": "Verification did not complete within its resource budget."}
                                for check in draft.checks]
            claims = {claim.id: claim for claim in draft.claims}
            for index, item in enumerate(evidence):
                item.setdefault("evidence_id", f"v{index + 1}")
            # A successful CAS check still assumes the model extracted the
            # right relation and conditions. Feed it back before accepting it
            # as scoring evidence, even if it agrees with the draft. This is
            # the evidence-feedback stage, not an unbounded expert debate.
            usable_evidence = any(item["status"] in {"supported", "refuted"}
                                  and item["claim_id"] in claims for item in evidence)
            review_failure = None
            review_pending = usable_evidence
            proof_concerns = (proof_audit or {}).get("review_concerns", [])
            if (usable_evidence or draft.needs_review or proof_concerns) and take_review_budget(local=True):
                await self._substep(unit, "llm_grade")
                try:
                    reviewed, response = await structured_llm_call(
                        self.provider, system_prompt=system,
                        user_prompt=prompt + "\nAPPLY SCOPED EVIDENCE (one pass):\n" + _json({
                            "draft": draft.model_dump(mode="json"), "evidence": evidence,
                            **({"proof_concerns": proof_concerns, "proof_instruction": REVIEW_INSTRUCTION}
                               if proof_audit is not None else {}),
                            "instruction": "Check affected scoring points against original text and rubric. "
                            "Check that the tool's assumed conditions really hold. Unknown is not incorrect. "
                            "Return a complete ledger with a concrete score; retain supported credit. "
                            "Do not extend the tool result to a different relation or the entire answer. "
                            "Do not propose further tools or reconstruct missing student work. "
                            "Preserve all existing claim IDs and their exact original quotes; "
                            "add new IDs only for newly identified original text. "
                            "For every supplied supported/refuted result return evidence_judgments with "
                            "evidence_id, applicability (applies/partial/not_applicable/uncertain), reason. "
                            "Use applies only for a claim-wide result consistent with its verdict; partial "
                            "requires explaining the limited scope. Explain any decision not to apply a result using the original conditions. "
                            "State a specific residual concern only when teacher attention would help.",
                        }), output_model=MathAssessment,
                        validate_output=_assessment_validator(plan, answer, problem, original=draft, evidence=evidence))
                    _claim_map(reviewed, answer.content or "", plan, problem_text=problem.stem)
                    _validate_claim_identity(draft, reviewed, answer=answer.content or "", plan=plan,
                                             problem_text=problem.stem)
                    reviewed_score, reviewed_steps = score_assessment(reviewed, plan, answer.content or "", problem_text=problem.stem)
                    draft, score, steps = reviewed, reviewed_score, reviewed_steps
                    if proof_audit is not None:
                        proof_audit = inspect_audit(read_appendix(getattr(response, "content", None)), draft,
                            answer.content or "", problem.stem, plan, previous=proof_audit)
                    review_pending = _evidence_pending(draft, evidence)
                    duration += response.duration_ms or 0
                except Exception as exc:
                    if _critical_failure(exc):
                        raise
                    # The existing valid grade remains useful; do not invent
                    # a failure or another model loop because review failed.
                    review_failure = type(exc).__name__
            result = ExpertResult(
                provider=self.provider.provider_id, score=score, max_score=problem.max_score,
                confidence=draft.confidence, comment=draft.comment, steps=steps,
                knowledge_citations=kb_tool.citations(chunks), duration_ms=max(duration, (perf_counter() - started) * 1000),
                raw_output=_json({"version": TRACE_VERSION, "plan_id": plan.plan_id,
                                  "assessment": draft.model_dump(mode="json"), "evidence": evidence,
                                  "initial_assessment": diagnostic.get("first_draft"),
                                  "assessment_validation_failures": diagnostic.get("validation_failures", []),
                                  "tool_applicability": tool_applicability,
                                  "rejected_checks": rejected_checks,
                                  "tool_applicability_reason": "explicit_relations_scheduled" if scheduled else "no_supported_relation_identified",
                                  "review_pending": review_pending,
                                  **({"proof_audit": proof_audit} if proof_audit is not None else {}),
                                  **({"review_failure": review_failure} if review_failure else {})}),
            )
            return _with_pending_advice(result, self.language)
        except Exception as exc:
            if _critical_failure(exc):
                raise
            kind, friendly = classify_skill_error(exc)
            if isinstance(exc, TransientLLMError) and kind == "general":
                kind = "transient_llm"
            return self._blank_result(problem.q_id, problem.max_score, friendly, error_kind=kind)
        finally:
            if cm:
                await report_math_progress(cm.__aexit__, None, None, None)

    async def _direct(self, problem, answer, plan, prompt, unit, chunks, *, fallback=None, started=None):
        await self._substep(unit, "llm_grade")
        rule = frozen_binary_rule(plan)
        def validate(result):
            if rule:
                try:
                    _binary_score(result.binary_correct, plan, answer.content or "")
                except ValueError as exc:
                    raise StructuredOutputInvalidError(str(exc)) from exc
        result, response = await structured_llm_call(
            self.provider,
            system_prompt=build_system_prompt(
                "Directly grade the student's actual work under the supplied frozen rubric. "
                "Return score, confidence, rationale (specific awarded/lost points), and a concise comment. "
                "No tool verification was completed: do not claim otherwise. Preserve teacher rules, "
                "alternative valid solutions and error follow-through; never impose a generic percentage cap."
                " If binary_rule is supplied, include binary_correct judging the entire final answer "
                "independently of your numerical score."
                + authoritative_score_instruction(problem.max_score), self.language, self.grading_setup),
            user_prompt=prompt, output_model=DirectMathGrade, validate_output=validate)
        if result.score > problem.max_score:
            raise ValueError("direct_score_out_of_bounds")
        score = _rounded(_points(result.score), problem.max_score)
        if rule:
            score = _binary_score(result.binary_correct, plan, answer.content or "")
        rationale = result.rationale + (f"\n{rule}: {score:g}/{plan.max_score:g}" if rule else "")
        return ExpertResult(
            provider=self.provider.provider_id, score=score, max_score=problem.max_score,
            confidence=result.confidence, comment=result.comment,
            steps=[StepScore(step_no=1, desc=rationale, score=score,
                             is_correct=score == problem.max_score)],
            knowledge_citations=kb_tool.citations(chunks), duration_ms=max(response.duration_ms or 0,
                (perf_counter() - started) * 1000 if started is not None else 0),
            raw_output=_json({"version": TRACE_VERSION, "plan_id": plan.plan_id,
                              "direct": result.model_dump(mode="json"), "evidence": [],
                              **({"fallback": fallback} if fallback else {})}),
        )
