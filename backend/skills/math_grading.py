"""Shared evidence and accounting pipeline for mathematical written answers.

All mathematical judgments remain scoped to the student's actual text. Tools
verify individual relations; only the frozen rubric determines point budgets.
"""
from __future__ import annotations

import json
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from time import perf_counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SkipValidation, TypeAdapter, ValidationError, field_validator, model_validator

from backend.services.part_rubric import supplied_notation
from backend.skills.part_scoring import INSTRUCTION as PART_SCORING_INSTRUCTION, PROOF_INSTRUCTION, PartDecision, ReasonedDecision, settle_parts
from backend.models import ExpertResult, GradingConfidence, StepScore
from backend.skills.base import (
    GradingSkill, authoritative_score_instruction, build_system_prompt,
    classify_skill_error, format_deterministic_feedback,
)
from backend.tools import knowledge as kb_tool
from backend.tools.structured_llm import PermanentLLMError, TransientLLMError, structured_llm_call
from backend.tools.structured_llm import StructuredOutputInvalidError
from backend.services.math_source_evidence import SourceMatch, match_source_quote, source_catalog
from backend.services.math_rubric_plan import SUBPART_ALLOCATION
from backend.services.math_proof_audit import inspect_audit, read_appendix, REVIEW_INSTRUCTION, OUTPUT_INSTRUCTION
from backend.services.math_strictness import Adjustment, LEDGER_INSTRUCTION
from backend.services.binary_scoring import frozen_binary_rule

TRACE_VERSION = "math_evidence_v2"
_MAX_ASSESSMENT_CLAIMS = 80
_MAX_CITATIONS_PER_EDIT = 8
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


class EvidenceAssessment(BaseModel):
    claims: list[Claim] = Field(default_factory=list, max_length=_MAX_ASSESSMENT_CLAIMS)
    checks: list[Verification] = Field(default_factory=list, max_length=8)
    confidence: GradingConfidence = 0.0
    rationale: str = ""  # Detailed evidence for holistic scoring; comment stays concise.
    comment: str = Field(description=(
        "Overall conclusion and useful next-step guidance, following the configured feedback "
        "tone and length. Do not enumerate the scoring items or repeat the detailed rationale, "
        "item explanations, point totals, source quotations or rubric text."
    ))
    needs_review: bool = False
    evidence_judgments: list[EvidenceJudgment] = Field(default_factory=list, max_length=8)


class FrozenPartAssessment(EvidenceAssessment):
    """Wire shape for frozen written rubrics, including independent review."""
    model_config = ConfigDict(extra="forbid")
    part_decisions: list[PartDecision] = Field(min_length=1, max_length=100)

    def assessment(self):
        return MathAssessment(route_id="parts", route_reason="Frozen part rules", **self.model_dump())


class ProofPartAssessment(FrozenPartAssessment):
    part_decisions: list[ReasonedDecision] = Field(min_length=1, max_length=100)


class AddressedPartAssessment(FrozenPartAssessment):
    """On repair, select original line numbers instead of retyping their mathematics."""

    @classmethod
    def model_json_schema(cls, *args, **kwargs):
        schema = super().model_json_schema(*args, **kwargs)
        addresses = {
            "type": "array",
            "items": {"type": "integer", "minimum": 1, "maximum": 500000},
            "maxItems": 100,
            "description": "Original student line numbers, e.g. [1, 3].",
        }
        for name, field in (
            ("PartDecision", "student_quote"),
            ("StepDecision", "evidence"),
        ):
            schema["$defs"][name]["properties"][field] = addresses
        return schema

    @model_validator(mode="before")
    @classmethod
    def resolve_line_numbers(cls, value):
        if not isinstance(value, dict) or not isinstance(
            value.get("part_decisions"), list
        ):
            return value

        def addressed(item, key):
            if not isinstance(item, dict) or not isinstance(item.get(key), list):
                return item
            numbers = item[key]
            if len(numbers) > 100 or any(
                type(n) is not int or not 1 <= n <= 500000 for n in numbers
            ):
                raise ValueError("Source line numbers must be positive integers.")
            return {**item, key: ", ".join(f"student:{n}" for n in numbers)}

        parts = []
        for raw in value["part_decisions"]:
            part = addressed(raw, "student_quote")
            if isinstance(part, dict) and isinstance(part.get("steps"), list):
                part = {
                    **part,
                    "steps": [addressed(step, "evidence") for step in part["steps"]],
                }
            parts.append(part)
        return {**value, "part_decisions": parts}


class StepsOnlyRepair(AddressedPartAssessment):
    """The selected frozen routes have no independently chargeable deductions."""

    @classmethod
    def model_json_schema(cls, *args, **kwargs):
        schema = super().model_json_schema(*args, **kwargs)
        schema["$defs"]["PartDecision"]["properties"]["errors"] = {
            "type": "array",
            "maxItems": 0,
        }
        schema["$defs"].pop("ErrorEvent", None)
        return schema

    @model_validator(mode="after")
    def step_ledger_only(self):
        if any(d.errors for d in self.part_decisions):
            raise ValueError(
                "For step scoring put error explanations in the affected step reasons; leave errors empty."
            )
        return self


class MathAssessment(EvidenceAssessment):
    part_decisions: list[PartDecision] = Field(
        default_factory=list, max_length=100, exclude_if=lambda v: not v
    )
    route_id: str
    route_reason: str
    awards: list[Award] = Field(default_factory=list, max_length=80)
    deductions: list[Deduction] = Field(default_factory=list, max_length=80)
    score: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    binary_correct: bool | None = Field(default=None, strict=True, description=(
        "For an explicit whole-question zero/full rule only: whether the entire final answer "
        "satisfies that rule. Judge correctness independently of the proposed numerical score."
    ))


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


class SubpartGrade(BaseModel):
    """Minimal fallback for a known points ledger; a single proposed total is insufficient."""
    claims: list[Claim] = Field(default_factory=list, max_length=80)
    awards: list[Award] = Field(min_length=1, max_length=80)
    confidence: GradingConfidence

    def assessment(self, plan) -> MathAssessment:
        return MathAssessment(route_id=plan.routes[0].id, route_reason=plan.original_criterion,
                              claims=self.claims, awards=self.awards, confidence=self.confidence, comment="")


class ExpertRevision(BaseModel):
    expert_index: int = Field(ge=0)
    reason: str = Field(min_length=1)
    source_quote: str = Field(min_length=1)
    assessment: MathAssessment


class ReviewCitation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    verdict: Literal["supported", "refuted", "unknown"]


class PointAward(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str
    score: float = Field(ge=0, allow_inf_nan=False)
    reason: str = Field(min_length=1)
    citations: list[ReviewCitation] = Field(default_factory=list, max_length=_MAX_CITATIONS_PER_EDIT)


class PointRevision(BaseModel):
    """A sourced edit to one expert's disputed point awards."""
    model_config = ConfigDict(extra="forbid")

    expert_index: int = Field(ge=0, strict=True)
    reason: str = Field(min_length=1)
    comment: str = Field(min_length=1, description="Updated overall feedback for this vote.")
    awards: list[PointAward] = Field(min_length=1, max_length=80)


class PointReview(BaseModel):
    """Narrow generation schema when the entire dispute is ordinary point credit."""
    revisions: list[SkipValidation[PointRevision]] = Field(max_length=10)


class PartRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expert_index: int = Field(ge=0, strict=True)
    reason: str = Field(min_length=1)
    comment: str = Field(min_length=1)
    decisions: list[PartDecision] = Field(min_length=1, max_length=100)


class ProofPartRevision(PartRevision):
    decisions: list[ReasonedDecision] = Field(min_length=1, max_length=100)


class ProofPartReview(BaseModel):
    revisions: list[SkipValidation[ProofPartRevision]] = Field(max_length=10)


class PartReview(BaseModel):
    revisions: list[SkipValidation[PartRevision]] = Field(max_length=10)


_REVIEW_ENTRY = TypeAdapter(PointRevision | ExpertRevision)
_EXPERT_INDEX = TypeAdapter(int)


class ExpertReview(BaseModel):
    # Parse the envelope once, then validate each expert's independent revision.
    # Both the first request and a format repair advertise only complete revisions here.
    revisions: list[SkipValidation[ExpertRevision]] = Field(max_length=10)


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
    if plan.part_rubric is not None:
        if assessment.route_id != "parts" or assessment.awards or assessment.deductions:
            raise ValueError("Use the frozen parts only, without a second question-level ledger.")
        return settle_parts(plan.part_rubric, assessment.part_decisions, answer, problem_text)
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
            if (plan.compilation_reason == SUBPART_ALLOCATION and not plan.allow_partial_credit
                    and award.score not in (0, item.points)):
                raise ValueError("partial_credit_disabled")
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
    if plan.compilation_reason == SUBPART_ALLOCATION:
        # Keep trace IDs aligned with the displayed steps, even if the model returned them out of order.
        assessment.awards = [awards[item_id] for item_id in items]
        assessment.score = score
        assessment.comment = " + ".join(f"{step.score:g}" for step in steps) + f" = {score:g}"
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


def _part_outcome(part):
    method = "reference" if part.method == "minor_variant" else part.method
    # A complete decision has already passed settlement at the full route cap.
    # Explicit earned steps and the shorthand for a single earned step are identical.
    steps = () if part.relevance == "complete" else tuple(sorted((s.id, s.outcome) for s in part.steps))
    errors = tuple(sorted((e.rule_id, e.origin_quote) for e in part.errors))
    return part.relevance, method, steps, errors, tuple(sorted(part.condition_results.items()))


def _part_signature(assessment):
    return tuple(sorted((part.part_id, _part_outcome(part)) for part in assessment.part_decisions))


def _apply_part_revision(old, revision, review_parts):
    edits = {d.part_id: d for d in revision.decisions}
    if len(edits) != len(revision.decisions) or edits.keys() - review_parts:
        raise ValueError("review_changed_undisputed_award")
    decisions = [edits.get(part.part_id, part) for part in old.part_decisions]
    return old.model_copy(update={"part_decisions": decisions, "comment": revision.comment})


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
                        _part_signature(assessment),
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
    if trace.get("review_pending") and not trace.get("relevance_review_pending"):
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


def _assessment_for_model(draft, plan):
    """Project the persisted ledger back onto the schema used by this model call."""
    fields = set(FrozenPartAssessment.model_fields) if plan.part_rubric else None
    return draft.model_dump(mode="json", include=fields)


def _proof_review_observations(decision, rules):
    """Review a grouped proof's mathematics without repeating its old loss tier.

    This is an input projection, never a replacement for the persisted ledger.
    Numeric rules, source evidence and teacher conditions still come from the
    unchanged plan. Non-grouped and alternative decisions keep their protocol.
    """
    part = next((p for p in rules.parts if p.id == decision.part_id), None)
    if (part is None or part.steps or not part.deductions or not decision.errors
            or not all(rule.exclusive_group for rule in part.deductions)
            or decision.method not in {"reference", "minor_variant"}):
        return decision.model_dump(mode="json", exclude_defaults=True)
    observations = []
    if decision.goal_findings:
        observations = [finding.model_dump(
            mode="json", exclude={"repair"}, exclude_defaults=True
        ) for finding in decision.goal_findings]
    else:
        groups = {rule.id: rule.exclusive_group for rule in part.deductions}
        observations = [{"group": groups.get(event.rule_id), "evidence": event.evidence,
                         "origin_quote": event.origin_quote, "remaining": event.reason}
                        for event in decision.errors]
    return {
        "part_id": decision.part_id,
        "target_quote": decision.target_quote,
        "student_quote": decision.student_quote,
        "method": decision.method,
        "reference_method": decision.reference_method,
        "student_method": decision.student_method,
        "mathematical_observations": observations,
    }


def _assessment_validator(plan, answer, problem, *, original=None, diagnostic=None, evidence=None):
    """Repair local validation failures within the parser's ONE repair budget.

    Matching excerpts are fixed before feedback and must survive a repair. A
    bad excerpt is never simply dropped (which could erase a valid deduction).
    """
    from backend.services.count_witnesses import marked_counts, submitted_count_checks, require_consistent_count_grades
    count_checks = submitted_count_checks(answer.content or "", marked_counts(problem.stem))
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
            require_consistent_count_grades(count_checks, plan, draft, answer.content or "", problem.stem)
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
                "preserve_claim_quotes": preserved, "draft": _assessment_for_model(draft, plan),
                "repair_scope": ("Return the same part_decisions schema. Correct the indicated part or step, keeping "
                    "valid source IDs. For step.evidence and student_quote use a student line ID or exact "
                    "text, and for target_quote use part:<part_id>. Never supply a numeric score."
                    if plan.part_rubric else "Fix invalid source citations or ledger links using the original input. "
                    "Preserve all located claim IDs and quotes. Do not invent omitted student work. "
                    "Use source_catalog IDs and empty quote fields for unresolved citations instead of "
                    "retyping or rewriting LaTeX. Do not drop claims merely to pass validation. "
                    "Use omission evidence with a question/rubric source for a missing obligation.")})
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


def _apply_point_revision(revision, old, disputed, catalog):
    proposed = {award.item_id: award for award in revision.awards}
    existing = {award.item_id: award for award in old.awards}
    if len(proposed) != len(revision.awards) or not proposed.keys() <= existing.keys():
        raise ValueError("review_expected_unique_disputed_awards")
    for key in proposed.keys() - disputed:
        # Some providers echo context rows. Keep the original row (including
        # its reasons and evidence) and reject any change to its accounting.
        if existing[key].score != proposed[key].score:
            raise ValueError("review_changed_undisputed_award")
    edits = [award for award in revision.awards if award.item_id in disputed]
    if not edits:
        raise ValueError("review_expected_unique_disputed_awards")
    # The model cites immutable lines, not another expert's private claim IDs.
    # Construct scoped claims locally; the usual ledger validates source and credit.
    claims = []
    used_ids = {c.id for c in old.claims}
    replacements = {}
    for edit in edits:
        ids = []
        for citation in edit.citations:
            if citation.source_id not in catalog:
                raise ValueError("invalid_claim_source_id")
            claim_id = f"review_{len(used_ids)}"
            while claim_id in used_ids:
                claim_id += "_"
            used_ids.add(claim_id)
            kind = "statement" if citation.source_id.startswith("student:") else "omission"
            claims.append(Claim(id=claim_id, kind=kind, source_id=citation.source_id,
                                verdict=citation.verdict, reason=edit.reason, item_ids=[edit.item_id]))
            ids.append(claim_id)
        if not ids:
            raise ValueError("review_changed_award_requires_evidence")
        replacements[edit.item_id] = Award(item_id=edit.item_id, score=edit.score, reason=edit.reason, claim_ids=ids)
    data = old.model_dump()
    data.update(awards=[replacements.get(award.item_id, award).model_dump() for award in old.awards],
                claims=data["claims"] + [claim.model_dump() for claim in claims],
                comment=revision.comment)
    return revision.model_copy(update={"awards": edits}), MathAssessment.model_validate(data)


def _review_error_code(exc):
    if isinstance(exc, StructuredOutputInvalidError) and isinstance(exc.__cause__, ValueError):
        exc = exc.__cause__
    if isinstance(exc, ValidationError):
        return "revision_schema_invalid"
    # Never persist provider text or Pydantic's messages containing source data.
    code = str(exc)
    if code in {
        "duplicate_expert_revision", "review_source_or_expert_not_found", "review_changed_claim_identity",
        "review_requires_complete_assessment", "review_expected_unique_disputed_awards",
        "review_changed_award_requires_evidence",
        "review_changed_undisputed_award",
        "invalid_claim_source_id", "claim_excerpt_conflicts_with_source_id", "claim_not_in_student_answer_or_rubric",
        "award_claim_not_mapped_to_item", "invalid_award", "omission_is_not_evidence_of_credit",
        "omission_requires_original_obligation", "review_must_address_each_scoped_evidence_result",
        "duplicate_or_empty_claim_id", "conditional_credit_exceeds_item", "default_adjustment_not_in_plan",
    }:
        return code
    return "invalid_mathematical_ledger"


def _review_index(entry):
    value = entry.get("expert_index") if isinstance(entry, dict) else getattr(entry, "expert_index", None)
    try:
        return None if isinstance(value, bool) else _EXPERT_INDEX.validate_python(value)
    except ValidationError:
        return None


def _part_proof_evidence(problem, rules=None):
    if problem.type != "推理题":
        return ""
    correspondence = ""
    if supplied_notation(problem.stem):
        correspondence = (
            "Read the quoted notation convention in context. Use only correspondences the source actually "
            "grants; a conjecture, requested construction, prohibition or implication to prove is still "
            "a student obligation. Apply a granted correspondence to the student's last justified "
            "statement and compare with the target definition, including the direction(s) that must hold. "
            "For an identification of objects by defining conditions, establishing equivalence in both "
            "directions under the GIVEN correspondence establishes the identification itself. Award the "
            "identification step when this holds; write the short substitution in the scoring reason. "
            "Do not demand the student copy the supplied names. If withholding credit, identify the "
            "mathematical condition still missing after substitution or quote the explicit source demand "
            "for a separately written map. A one-way implication alone does not establish equivalence. "
        )
    # One proof can supply a premise to another independently scored goal.
    # Single-goal parts already use the ordinary source/implication checks below.
    cross_goal = ""
    if rules and any(
        len({rule.exclusive_group for rule in part.deductions if rule.exclusive_group}) > 1
        for part in rules.parts
    ):
        cross_goal = (
            "When a student explicitly states AND uses an intermediate fact, first check that fact on its own. "
            "Distinguish an auxiliary fact from the actual result the problem asks them to prove. "
            "Starting with the GIVEN laws, try to justify the stated fact: for a claimed form of an expression, "
            "apply those laws to an arbitrary expression, showing what is eliminated at each rewrite and why "
            "it terminates. For a standard theorem check its hypotheses, permitted method and actual use. "
            "If this verifies a routine auxiliary fact, accept it as a premise everywhere it is used. "
            "Apply the student's resulting general conclusion to the objects they use in the next goal. "
            "In the judgment record the short mathematical justification and what still needs new work AFTER it. "
            "Do not require a separate proof of an admissible intermediate fact merely because its justification "
            "was omitted. A claimed target, a circular citation, an impermissible method or a missing independent "
            "argument is still assessed under the frozen policy. Never invent a premise the student did not use. "
        )
    return "On the alternative route, use these proof checks to support the decision and independent errors only. " + correspondence + (
        "\nFor each scored proof obligation, check what follows from the cited student statements under "
        "the problem's hypotheses and definitions. A direct consequence of a definition, a substitution "
        "or a reversible algebraic equality may establish the goal without an extra sentence. "
    ) + cross_goal + (
        "For an existence or uniqueness goal where the student supplies a witness or construction, "
        "substitute that specific candidate into the defining conditions yourself over the ENTIRE "
        "stated domain. Record the resulting short calculation in the decision's progress or step "
        "reason. If it follows immediately with no new choice, lemma or case, mark the step earned. "
        "Naming the requested witness is evidence beyond asserting the theorem. If it fails at an "
        "admissible input or requires an unwritten substantive argument, identify that input or gap. "
        "An AI rubric saying 'verify' describes what must be true; require a written intermediate "
        "calculation only if the question or teacher explicitly asks for it. "
        "For each missed goal, name the substantive premise, invalid implication, unproved case or "
        "explicit teacher requirement that remains. A chain of equivalences can prove both implications. For a universal result, compare the domain "
        "actually covered by the cited argument to the original domain. A partial-domain example in an "
        "AI-authored step cannot discharge the missing cases. "
        "Do not count a single missing justification again when its conclusion follows independently "
        "from the student's written premises. A bare restatement of the claim, a circular argument or "
        "examples covering only a special case do not establish a general proof. If the transcription "
        "has conflicting spatial order or unreadable notation that changes this judgment, identify the "
        "affected step and request review. Preserve all explicit teacher requirements.\n"
    )


def _part_rule_projection(rules, part_ids=None):
    """Only executable rules and their sources belong in an expert's input."""
    data = rules.model_dump(mode="json", exclude={"teacher_gaps", "low_confidence"})
    if part_ids is not None:
        data["parts"] = [part for part in data["parts"] if part["id"] in part_ids]
    return data


def _expert_sources(catalog, plan):
    # AI-authored rubric prose is a rendering of the same JSON, not another
    # teacher instruction. Keep the original text and line IDs for supplied policies.
    generated = plan.part_rubric and plan.part_rubric.rubric_origin == "ai"
    return {key: entry[0] for key, entry in catalog.items()
            if not (generated and key.startswith("rubric:"))}


def _adopted_problem(problem, plan):
    if plan.adopted_material is None:
        return problem
    return problem.model_copy(update=plan.adopted_material.model_dump())


def _generated_proof_guidance(problem):
    from backend.services.math_rubric_plan import _question_input
    if problem.type != "推理题" or _question_input(problem)["criterion_source"] != "ai":
        return ""
    return (
        "\nThis proof rubric was AI-generated. Read the question's actual proof obligation first. "
        "Unless the question or teacher specifies a method, an item describing a reference step is one "
        "way to earn its allocated credit. For another route, identify the corresponding mathematical "
        "work (premise or witness, decisive inference, conclusion) in the student's text. A counterexample "
        "to a different necessary condition can prove the same negative claim. Under a per-part rubric, "
        "settle a different strategy using only its alternative policy. Keep independent "
        "subquestions, explicit instructions and real gaps intact. Give the precise missing implication "
        "if withholding credit. An unsupported conclusion alone does not earn the reasoning points.\n"
    )


def _point_review_evidence(assessment, trace, disputed):
    """Present disputed credit and its cited passages without model-local editing IDs."""
    claims = {claim.id: claim for claim in assessment.claims}
    awards = []
    for award in assessment.awards:
        if award.item_id not in disputed:
            continue
        evidence = []
        for key in award.claim_ids:
            claim = claims[key]
            evidence.append({"kind": claim.kind, "source_id": claim.source_id,
                             "quote": claim.quote if claim.kind == "statement" else claim.source_quote,
                             "verdict": claim.verdict, "reason": claim.reason})
        awards.append({"item_id": award.item_id, "score": award.score, "reason": award.reason,
                       "evidence": evidence})
    result = {"awards": awards, "comment": assessment.comment}
    if "proof_audit" in trace:
        result["proof_audit"] = trace["proof_audit"]
    return result


async def review_math_experts(problem, answer, experts, provider, *, language="en", grading_setup=None):
    """Resolve concrete disagreements once, retaining weights and provenance."""
    from backend.services.math_grading_context import scoring_plan
    traces = [read_math_trace(expert) for expert in experts]
    if not all(trace and ("assessment" in trace or "direct" in trace) for trace in traces):
        return experts
    plan = scoring_plan(problem, grading_setup)
    if any(trace["plan_id"] != plan.plan_id for trace in traces):
        return experts
    problem = _adopted_problem(problem, plan)
    assessments = [MathAssessment.model_validate(trace["assessment"]) if "assessment" in trace else None for trace in traces]
    route_ids = {assessment.route_id for assessment in assessments if assessment is not None}
    signatures = [_award_signatures(assessment) if assessment is not None else {}
                  for assessment in assessments]
    item_ids = set().union(*(signature.keys() for signature in signatures))
    disputed = {key for key in item_ids
                if len({signature.get(key) for signature in signatures}) > 1}
    other_dispute = (len({_part_signature(a) for a in assessments if a is not None}) > 1 or len(route_ids) > 1 or
                     len({expert.score for expert in experts}) > 1 or
                     len({_deduction_signature(a) for a in assessments if a is not None}) > 1)
    relevance_review = any(trace.get("relevance_review_pending") for trace in traces)
    evidence_review = any(
        (trace.get("review_pending") and not trace.get("relevance_review_pending"))
        or trace.get("proof_audit", {}).get("review_concerns") for trace in traces)
    requested_review = evidence_review or any(a.needs_review for a in assessments if a is not None)
    if not (disputed or other_dispute or requested_review or relevance_review) or not take_review_budget():
        return experts

    catalog = source_catalog(answer.content or "", problem.stem, plan.original_criterion)
    # Keep alternative route selection, explicit binary policies and
    # conditional/dependent credit on the complete-ledger review path.
    # A point edit appends one claim per citation. Use the complete format
    # when a permitted edit could overflow any vote's original ledger.
    max_added_claims = len(disputed) * _MAX_CITATIONS_PER_EDIT
    compact = bool(disputed and len(plan.routes) == 1 and plan.routes[0].mode == "points"
                   and plan.allow_partial_credit and not frozen_binary_rule(plan)
                   and not any(item.default_adjustments for item in plan.routes[0].items if item.id in disputed)
                   and all(a and a.awards and len(a.claims) + max_added_claims <= _MAX_ASSESSMENT_CLAIMS
                           for a in assessments)
                   and any(key.startswith("student:") for key in catalog)
                   and not requested_review and not any(t.get("evidence") for t in traces))
    part_outcomes = [dict(_part_signature(a)) if a is not None else {} for a in assessments]
    disputed_parts = {part.id for part in plan.part_rubric.parts
                      if len({outcomes.get(part.id) for outcomes in part_outcomes}) > 1} if plan.part_rubric else set()
    # A proof expert's own doubt concerns the judgment, not the identity of its
    # immutable source claims. Only actual pending evidence needs the full format.
    proof_judgment_review = (problem.type == "推理题" and requested_review and not evidence_review
                             and not any(t.get("evidence") for t in traces))
    review_parts = ({part.id for part in plan.part_rubric.parts}
                    if plan.part_rubric and (relevance_review or proof_judgment_review) else disputed_parts)
    compact_parts = bool(review_parts and all(a and a.part_decisions for a in assessments)
                         and (not requested_review or proof_judgment_review) and not any(t.get("evidence") for t in traces))
    part_revision = ProofPartRevision if problem.type == "推理题" else PartRevision
    accepted = {}
    validation_issues = []
    last_review = None
    rejected_indices = set()

    def validate_review(review):
        nonlocal last_review
        if review is last_review:
            return
        accepted.clear()
        rejected_indices.clear()
        indices = [_review_index(raw) for raw in review.revisions]
        counts = Counter(indices)
        issues = []
        repair_details = []
        for raw, index in zip(review.revisions, indices):
            revision = None
            try:
                revision = (part_revision.model_validate(raw) if compact_parts else _REVIEW_ENTRY.validate_python(raw))
                if index is None or not 0 <= index < len(experts):
                    raise ValueError("review_source_or_expert_not_found")
                if counts[index] != 1:
                    raise ValueError("duplicate_expert_revision")
                old, trace = assessments[index], traces[index]
                if isinstance(revision, PartRevision):
                    candidate = _apply_part_revision(old, revision, review_parts)
                elif isinstance(revision, PointRevision):
                    if not compact:
                        raise ValueError("review_requires_complete_assessment")
                    revision, candidate = _apply_point_revision(revision, old, disputed, catalog)
                else:
                    if match_source_quote(answer.content or "", revision.source_quote) is None:
                        raise ValueError("review_source_or_expert_not_found")
                    candidate = revision.assessment
                    if old and old.part_decisions and not (requested_review or relevance_review):
                        revised = dict(_part_signature(candidate))
                        if any(revised.get(key) != outcome for key, outcome in part_outcomes[index].items()
                               if key not in disputed_parts):
                            raise ValueError("review_changed_undisputed_award")
                    if len(route_ids) == 1 and old is not None and old.awards and not requested_review:
                        new_awards = _award_signatures(candidate)
                        if any(new_awards.get(key) != value for key, value in signatures[index].items() if key not in disputed):
                            raise ValueError("review_changed_undisputed_award")
                _assessment_validator(plan, answer, problem, original=old,
                    evidence=trace.get("evidence", []))(candidate)
                score, steps = score_assessment(candidate, plan, answer.content or "", problem_text=problem.stem)
                accepted[index] = revision, candidate, score, steps
            except ValueError as exc:
                issues.append({"expert_index": index if index is not None and 0 <= index < len(experts) else None,
                               "code": _review_error_code(exc)})
                # Give the model its rejected edit and detailed grounding context
                # on repair. Only the bounded error codes go to the private trace.
                if revision is not None:
                    repair_details.append({"revision": revision.model_dump(mode="json"),
                                           "detail": getattr(exc, "repair_context", None)})
                elif isinstance(exc, ValidationError):
                    repair_details.append({"fields": exc.errors(include_input=False, include_url=False,
                                                               include_context=False)})
                if index is not None:
                    rejected_indices.add(index)
        if issues:
            validation_issues.append(issues)
        if issues and not accepted:
            error = StructuredOutputInvalidError("Invalid targeted mathematical review.")
            error.repair_context = _json({"validation_errors": issues, "rejected": repair_details,
                "repair_scope": "Correct the listed revisions using the original votes, source catalog and "
                    "disputed items. Return only source-cited edits for the requested format and preserve undisputed awards."})
            raise error
        last_review = review

    review_format = PointReview if compact else ExpertReview
    proof_guidance = _generated_proof_guidance(problem)
    if proof_guidance:
        proof_guidance += (
            "Check the domain of the actual argument against the quantifiers in the problem. A generated "
            "rubric example is not evidence that a proof of only a special case covers all cases. "
            "Likewise, a proof covering every case earns credit without repeating the reference example.\n"
        )
    instructions = (
        "Return a PointRevision for each expert with an evidenced error. Give the zero-based expert_index, "
        "reason, updated overall comment, and ONLY the disputed awards "
        "that need a change. For each edited award provide citations [{source_id, verdict}] from the supplied "
        "source_catalog, with the reason explaining how the passage supports this amount. Student lines "
        "support written work; rubric/question lines identify omissions. Use these shared source IDs for "
        "every changed item, including zero credit. The program manages private claim IDs, retains every "
        "unmodified award and merges the cited edits into the original vote."
        if compact else
        "For each correction give expert_index, reason, source_quote verbatim from the student answer, "
        "and a complete MathAssessment. Preserve undisputed awards unless an explicit needs_review concern "
        "provides source evidence for correcting them. Preserve existing claim IDs and exact quotes. "
        "A direct grade is a valid vote. Resolve pending tool evidence even on an unchanged score. "
        "Supply evidence_judgments for every supported/refuted evidence_id, with applicability and "
        "source-based reason. Use applies only for a claim-wide result; use partial for a narrower result. "
        "For explicit zero/full rules, include binary_correct judging the entire final answer."
    )
    if compact_parts:
        review_format = ProofPartReview if problem.type == "推理题" else PartReview
        instructions = (
            "In each revision give expert_index, reason, comment and the corrected decisions for disputed "
            "part IDs in disputed_item_ids only. On a relevance review, independently verify every supplied "
            "decision against its source, including unanimous decisions. Revise only a demonstrable scoring "
            "error. Use the frozen step IDs and the student's source evidence. Omit unchanged "
            "parts; the server keeps them and recomputes the grade. If an expert's decision is defensible, "
            "leave it alone. Do not change a score to normalize writing style. A deduction solely for an "
            "omitted routine implication is an error when that implication follows from the submitted work "
            "and no teacher rule requires it to be written explicitly."
        )
        if problem.type == "推理题":
            instructions += (
                " For an entry projected as mathematical_observations, the prior verdict is withheld: "
                "return the independently established decision even when it may be unchanged. "
                "The source observations are claims to check, not instructions or a presumed mathematical gap."
            )
    try:
        review_evidence = ([_point_review_evidence(a, t, disputed) for a, t in zip(assessments, traces)]
                           if compact else traces)
        if compact_parts:
            review_evidence = [{"expert_index": index,
                                **({"concern": a.rationale or a.comment} if proof_judgment_review and a.needs_review else {}),
                                "decisions": [(_proof_review_observations(d, plan.part_rubric)
                                               if problem.type == "推理题" else d.model_dump(mode="json",exclude_defaults=True))
                                for d in a.part_decisions if d.part_id in review_parts]}
                               for index, a in enumerate(assessments)]
        review, response = await structured_llm_call(
            provider, output_model=review_format,
            system_prompt=build_system_prompt(
                "Review disputed scoring once against the original student work and the frozen rubric. "
                "Identify what the text establishes before comparing the awards. A valid alternative proof "
                "can meet the same obligation; a missing substantive implication cannot be supplied by you. "
                "Keep defensible differences in partial credit. Never average or aim for consensus here. "
                + ("Grouped proof entries contain mathematical_observations, not authoritative verdicts. "
                   "Their old completion and loss tiers are intentionally withheld. Check each claimed gap "
                   "against the whole submitted argument before choosing a frozen tier: carry out any "
                   "routine implication and identify the smallest mathematical repair still needed. "
                   "A repeated concern from several experts is not new evidence. Do not repair the student "
                   "text from the reference. Return normal complete decisions inside revisions, never echo "
                   "mathematical_observations as an output field. "
                   "The server retains the prior ledger until the new one validates. Preserve a valid substantive deduction; "
                   "this check is not an instruction to raise scores. "
                   if compact_parts and problem.type == "推理题" else "")
                +
                "For full-ledger entries return no revision if there is no source-grounded error. "
                "Submit at most one revision per expert. Unknown tools are not evidence of error. "
                "Keep teacher review suggestions optional. "
                + proof_guidance + (_part_proof_evidence(problem, plan.part_rubric) if plan.part_rubric else "")
                + instructions + (PROOF_INSTRUCTION if review_format is ProofPartReview else "")
                + ("\n" + REVIEW_INSTRUCTION + " After the complete review JSON, append PROOF_AUDIT "
                   "followed by a separate JSON object mapping each revised proof expert's index "
                   "(string) to an audit object with the same steps/context_source_ids shape as its trace. "
                   "This optional appendix must never be placed inside a revision or assessment."
                   if not compact_parts and any("proof_audit" in trace for trace in traces) else "")
                + (PART_SCORING_INSTRUCTION if plan.part_rubric else authoritative_score_instruction(problem.max_score))
                + "\nJSON Schema:\n" + _json(review_format.model_json_schema()), language, grading_setup,
                **({"output_instruction": OUTPUT_INSTRUCTION} if not compact_parts and any("proof_audit" in trace for trace in traces) else {})),
            user_prompt=_json({"problem": problem.stem, "reference": problem.reference_answer,
                               **({"source_supplied_correspondence": notation}
               if plan.part_rubric and problem.type == "推理题" and (notation := supplied_notation(problem.stem)) else {}),
                               "answer": answer.content,
                               "plan": (_part_rule_projection(plan.part_rubric, review_parts if compact_parts else None)
                                        if plan.part_rubric else plan.model_dump(mode="json")),
                               "source_catalog": _expert_sources(catalog, plan),
                               "disputed_item_ids": sorted(review_parts if compact_parts else disputed), "experts": review_evidence}),
            validate_output=validate_review,
        )
        # Mock callers may bypass structured_llm_call's validator; this is also
        # a final guard before applying any revision to the valid votes.
        validate_review(review)
        revised = list(experts)
        for index, (revision, candidate, score, steps) in accepted.items():
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
            if isinstance(revision, PartRevision):
                trace["targeted_review"]["part_ids"] = [d.part_id for d in revision.decisions]
            if isinstance(revision, PointRevision):
                trace["targeted_review"]["item_ids"] = [award.item_id for award in revision.awards]
            if steps:
                label = "定向复核依据：" if language == "zh" else "Targeted review: "
                steps[0] = steps[0].model_copy(update={"desc": steps[0].desc + "\n" + label + revision.reason})
            # The original vote remains in the private trace. Make the review
            # reason visible; confidence/configured weights remain unchanged.
            revised[index] = experts[index].model_copy(update={
                "score": score, "steps": steps, "comment": candidate.comment,
                "raw_output": _json(trace),
            })
        if validation_issues or (not accepted and (disputed or other_dispute)):
            for index, expert in enumerate(revised):
                trace = dict(read_math_trace(expert))
                status = "retained"
                if index in accepted:
                    status = "revised"
                elif index in rejected_indices:
                    status = "invalid_revision"
                trace["review_diagnostic"] = {"status": status, "validation_issues": validation_issues}
                revised[index] = expert.model_copy(update={"raw_output": _json(trace)})
        for index, expert in enumerate(revised):
            trace = read_math_trace(expert)
            if not trace.get("relevance_review_pending") or index in rejected_indices:
                continue
            # A valid abstention also completes an independent relevance check.
            # CAS evidence and unresolved proof concerns keep their own disposition.
            trace.pop("relevance_review_pending")
            current = MathAssessment.model_validate(trace["assessment"])
            trace["review_pending"] = _evidence_pending(current, trace.get("evidence", []))
            trace.pop("review_advisory", None)
            revised[index] = expert.model_copy(update={"raw_output": _json(trace)})
        return [_with_pending_advice(expert, language) for expert in revised]
    except Exception as exc:
        if _critical_failure(exc):
            raise
        retained = []
        failure_stage = "provider_or_runtime"
        if isinstance(exc, StructuredOutputInvalidError):
            failure_stage = ("revision_validation" if str(exc) == "Invalid targeted mathematical review."
                             else "response_format_or_attempt_limit")
        for expert, trace in zip(experts, traces):
            trace = dict(trace, review_failure=type(exc).__name__, review_diagnostic={
                "status": "failed", "stage": failure_stage, "error_type": type(exc).__name__,
                "validation_issues": validation_issues})
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
    if all(trace.get("assessment", {}).get("part_decisions") for trace in traces):
        keys = [[(step.part_id, step.item_id) for step in expert.steps] for expert in experts]
        if keys and any(group != keys[0] for group in keys):
            # A method-specific deduction may have one subtotal where another
            # expert has three steps. Compare the common frozen part IDs.
            parts = list(dict.fromkeys(step.part_id for expert in experts for step in expert.steps))
            reduced = []
            for expert in experts:
                totals = []
                for index, part_id in enumerate(parts, 1):
                    leaves = [step for step in expert.steps if step.part_id == part_id]
                    totals.append(StepScore(step_no=index, part_id=part_id, item_id=None,
                        score=float(sum((_points(step.score) for step in leaves), Decimal(0))),
                        desc="\n".join(step.desc for step in leaves) or expert.comment,
                        is_correct=bool(leaves) and all(step.is_correct for step in leaves)))
                reduced.append(expert.model_copy(update={"steps": totals}))
            experts = reduced
            keys = [parts for _ in experts]
    from backend.services.grading_scores import score_quantum
    quantum = score_quantum(experts[0].steps)
    if same_route and keys[0] and all(key == keys[0] for key in keys) and all(
            len(expert.steps) == len(keys[0]) for expert in experts):
        result, subtotal, previous = [], Decimal(0), Decimal(0)
        for index in range(len(keys[0])):
            amount = sum(_points(expert.steps[index].score) * weight
                         for expert, weight in zip(experts, weights)) / sum(weights)
            subtotal += amount
            current = min(_points(maximum), subtotal.quantize(quantum, rounding=ROUND_HALF_UP))
            desc = join_expert_feedback((expert.provider, expert.steps[index].desc) for expert in experts)
            result.append(StepScore(step_no=index + 1, desc=desc, score=float(current - previous),
                                    part_id=experts[0].steps[index].part_id, item_id=experts[0].steps[index].item_id,
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
        from backend.services.count_witnesses import marked_counts, submitted_count_checks
        from backend.services.growth_order import ordered_growth_classes
        checks = marked_counts(problem.stem)
        growth = ordered_growth_classes(problem.stem)
        template = (Path(__file__).resolve().parents[1] / "prompts" /
                    f"{self.template_name}.txt").read_text(encoding="utf-8")
        return ("" if plan.part_rubric else template + "\n" + LEDGER_INSTRUCTION) + _generated_proof_guidance(problem) + "\nINPUT DATA:\n" + _json({
            "problem": problem.stem, "reference_answer": problem.reference_answer,
            "student_answer": answer.content,
            **({"source_supplied_correspondence": notation}
               if plan.part_rubric and problem.type == "推理题" and (notation := supplied_notation(problem.stem)) else {}),
            "rubric_plan": _part_rule_projection(plan.part_rubric) if plan.part_rubric else plan.model_dump(mode="json"),
            "binary_rule": None if plan.part_rubric else frozen_binary_rule(plan),
            **({"frozen_targets": {f"part:{part.id}": part.stem or problem.stem
                                    for part in plan.part_rubric.parts}} if plan.part_rubric else {}),
            "source_catalog": _expert_sources(source_catalog(answer.content or "", problem.stem, plan.original_criterion), plan),
            **({"required_decisions": [{
                "part_id": part.id, "target_quote": f"part:{part.id}",
                "step_ids": [step.id for step in part.steps], "deduction_ids": [rule.id for rule in part.deductions],
                "alternative_error_id": "alternative",
            } for part in plan.part_rubric.parts]} if plan.part_rubric else {}),
            "retrieved_material": context,
            **({"source_growth_order": growth, "growth_order_instruction": "Compare the submitted order to these ordered classes of the original expressions. Terms within the same group have equal asymptotic growth."} if growth else {}),
            **({"source_execution_checks": checks, "submitted_count_checks": submitted_count_checks(answer.content, checks),
                "execution_check_instruction": "Grade each marked statement against its own source trace. A counterexample refutes a general answer for that part; matching finite samples alone is not a proof. The exact field is a proven count of an input-independent program."} if checks else {}),
        })

    async def _assess(self, plan, *, system_prompt, user_prompt, validator, part_output=FrozenPartAssessment):
        if plan.part_rubric:
            assessment, response = await structured_llm_call(self.provider,
                output_model=part_output, system_prompt=system_prompt, user_prompt=user_prompt,
                validate_output=lambda candidate: validator(candidate.assessment()))
            return assessment.assessment(), response
        return await structured_llm_call(self.provider, output_model=MathAssessment,
            system_prompt=system_prompt, user_prompt=user_prompt, validate_output=validator)

    async def _verify(self, draft, problem, answer, plan):
        from backend.tools.math_verification import MathVerificationRequest, verify_math
        from backend.services.math_tool_planning import binding_error
        claims = _claim_map(draft, answer.content or "", plan, problem_text=problem.stem)
        evidence = []
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
                result = await verify_math(request)
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
        from backend.services.math_grading_context import scoring_plan
        plan = scoring_plan(problem, self.grading_setup)
        problem = _adopted_problem(problem, plan)
        unit, cm = None, None
        if self.reporter:
            from backend.services.math_grading_context import report_math_progress
            cm = self.reporter.step(student_id, problem.q_id, self.name, self.provider.provider_id)
            unit = await report_math_progress(cm.__aenter__)
        chunks, evidence, draft, duration = [], [], None, 0.0
        diagnostic = {}
        try:
            if not (answer.content or "").strip():
                if any(flag in {"recognition_partial", "parse_failed", "recognition_failed", "recognition_manual_pending", "no_matching_answer"}
                       or flag.startswith(("recognition_failed_pages:", "recognition_unprocessed_pages:", "recognition_invalid_fields:"))
                       for flag in (answer.flag or [])):
                    raise ValueError("Unresolved recognition cannot be graded as blank.")
                return ExpertResult(provider=self.provider.provider_id, score=0, max_score=problem.max_score,
                    confidence=1, comment=format_deterministic_feedback(self.grading_setup,
                        zh_message="本题尚未作答，得0分。", en_message="No answer was submitted; this question receives 0 points.",
                        zh_suggestion="请补充作答。", en_suggestion="Add your answer to this question.",
                        legacy_message="空白作答 / Blank answer"), steps=[],
                    raw_output=_json({"version":TRACE_VERSION,"plan_id":plan.plan_id,"blank":True}))
            if plan.part_rubric and plan.expected_truth_by_part:
                from backend.services.true_false_answers import adopted_truth_key, submitted_truth_values
                choices = submitted_truth_values(answer.content, plan.part_rubric.parts, question_label=plan.part_rubric.label)
                key = adopted_truth_key(plan.part_rubric, problem.stem, problem.reference_answer)
                if choices is not None and key:
                    judgments = []
                    for part in plan.part_rubric.parts:
                        correct = choices[part.id] == key[part.id]
                        reason = ("判断正确" if correct else "判断错误") if self.language == "zh" else (
                            "Correct judgment" if correct else "Incorrect judgment")
                        judgments.append(PartDecision(part_id=part.id, target_quote=f"part:{part.id}",
                            student_quote=answer.content, progress=reason, method="reference",
                            relevance="complete" if correct else "incorrect"))
                    assessment = MathAssessment(route_id="parts", route_reason="Exact labelled judgments",
                        part_decisions=judgments, confidence=1, comment="逐项核对判断答案。" if self.language == "zh"
                        else "Compared each labelled judgment with the frozen answer key.")
                    score, steps = score_assessment(assessment, plan, answer.content, problem_text=problem.stem)
                    return ExpertResult(provider=self.provider.provider_id, score=score, max_score=problem.max_score,
                        confidence=1, steps=steps, comment=format_deterministic_feedback(self.grading_setup,
                            zh_message="已逐项核对判断答案。", en_message="Each answer has been checked against the answer key.",
                            zh_detail=f"本题得分：{score:g}/{problem.max_score:g}。", en_detail=f"Score: {score:g}/{problem.max_score:g}.",
                            legacy_message=assessment.comment, complete=score == problem.max_score),
                        raw_output=_json({"version": TRACE_VERSION, "plan_id": plan.plan_id,
                                          "assessment": assessment.model_dump(mode="json"), "exact_truth_match": True}))
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
            system = build_system_prompt(
                "Grade only the submitted work under the frozen rubric. "
                "For each point item identify the obligation, locate the student's relevant passage, "
                "then decide whether it establishes the obligation, makes partial progress, or contradicts it. "
                "Apply the same evidence threshold to a reference method and a valid alternative. "
                "Where a proof is implicit, distinguish a consequence of the written premises from a "
                "substantive missing premise. Award only what the text and rubric support. "
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
            part_output = ProofPartAssessment if problem.type == "推理题" else FrozenPartAssessment
            if plan.part_rubric:
                system = build_system_prompt(PART_SCORING_INSTRUCTION + _part_proof_evidence(problem, plan.part_rubric) + "\nUse the supplied part and student line IDs "
                    "for target_quote, student_quote and step evidence. Only include conventional claims when "
                    "requesting mathematical verification. All arithmetic is performed by the server. Schema:\n"
                    + (PROOF_INSTRUCTION if part_output is ProofPartAssessment else "")
                    + _json(part_output.model_json_schema()), self.language, self.grading_setup)
            try:
                await self._substep(unit, "llm_grade")
                draft, response = await self._assess(
                    plan, system_prompt=system, user_prompt=prompt, part_output=part_output,
                    validator=_assessment_validator(plan, answer, problem, diagnostic=diagnostic))
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
                    started=started, repair_context=getattr(exc, "repair_context", None),
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
                    # checks exhaust the shared execution budget.
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
            relevance_check = bool(plan.part_rubric and any(
                d.relevance != "unanswered"
                for d in draft.part_decisions))
            review_pending = usable_evidence or relevance_check
            proof_concerns = (proof_audit or {}).get("review_concerns", [])
            if (usable_evidence or relevance_check or draft.needs_review or proof_concerns) and take_review_budget(local=True):
                await self._substep(unit, "llm_grade")
                try:
                    reviewed, response = await self._assess(
                        plan, system_prompt=system, part_output=part_output,
                        user_prompt=prompt + "\nAPPLY SCOPED EVIDENCE (one pass):\n" + _json({
                            "draft": _assessment_for_model(draft, plan), "evidence": evidence,
                            **({"proof_concerns": proof_concerns, "proof_instruction": REVIEW_INSTRUCTION}
                               if proof_audit is not None else {}),
                            "instruction": "Independently verify relevance and method against each exact target. Unrelated true statements earn no academic credit. Check affected scoring points against original text and rubric. "
                            "Check that the tool's assumed conditions really hold. Unknown is not incorrect. "
                            + ("Return a complete part_decisions ledger; the server calculates the score. "
                               if plan.part_rubric else "Return a complete ledger with a concrete score; retain supported credit. ")
                            +
                            "Do not extend the tool result to a different relation or the entire answer. "
                            "Do not propose further tools or reconstruct missing student work. "
                            "Preserve all existing claim IDs and their exact original quotes; "
                            "add new IDs only for newly identified original text. "
                            "For every supplied supported/refuted result return evidence_judgments with "
                            "evidence_id, applicability (applies/partial/not_applicable/uncertain), reason. "
                            "Use applies only for a claim-wide result consistent with its verdict; partial "
                            "requires explaining the limited scope. Explain any decision not to apply a result using the original conditions. "
                            "State a specific residual concern only when teacher attention would help.",
                        }),
                        validator=_assessment_validator(plan, answer, problem, original=draft, evidence=evidence))
                    _claim_map(reviewed, answer.content or "", plan, problem_text=problem.stem)
                    _validate_claim_identity(draft, reviewed, answer=answer.content or "", plan=plan,
                                             problem_text=problem.stem)
                    reviewed_score, reviewed_steps = score_assessment(reviewed, plan, answer.content or "", problem_text=problem.stem)
                    draft, score, steps = reviewed, reviewed_score, reviewed_steps
                    if proof_audit is not None:
                        proof_audit = inspect_audit(read_appendix(getattr(response, "content", None)), draft,
                            answer.content or "", problem.stem, plan, previous=proof_audit)
                    review_pending = _evidence_pending(draft, evidence) or any(part.relevance == "uncertain" for part in draft.part_decisions)
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
                                  **({"relevance_review_pending": True} if relevance_check and review_pending
                                     and not usable_evidence else {}),
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

    async def _direct(self, problem, answer, plan, prompt, unit, chunks, *, fallback=None, started=None, repair_context=None):
        await self._substep(unit, "llm_grade")
        if plan.part_rubric is not None:
            # This fallback still uses the frozen decision schema. Carry forward
            # the failed validation and supply the exact addresses it can accept.
            output_model = (StepsOnlyRepair if all(
                p.steps and not p.deductions and p.alternative.deduct_per_error is None for p in plan.part_rubric.parts
            ) else AddressedPartAssessment)
            recovery = {
                "validation_feedback": repair_context,
                "student_line_ids": list(source_catalog(answer.content or "", "", "")),
                "parts": [{"part_id": p.id, "target_quote": f"part:{p.id}",
                           "step_ids": [step.id for step in p.steps],
                           "deduction_rule_ids": [rule.id for rule in p.deductions], "alternative_error_id": "alternative"}
                          for p in plan.part_rubric.parts],
                "instruction": "For student_quote and each step.evidence return an array of source line numbers, "
                               "e.g. [1,3] references student:1 and student:3. Use [] for an absent step. Keep any "
                               "paraphrase in progress/reason. On the points route put lost credit in "
                               "the corresponding missed step. Charge only explicitly listed deduction rules. For actual "
                               "deductions keep each independent error grounded at its source. For method=alternative, "
                               "leave steps empty and use only rule_id=alternative for any errors.",
            }
            prompt += "\nRepair the rejected decision from the original sources:\n" + _json(recovery)
            result, response = await self._assess(plan,
                system_prompt=build_system_prompt(
                    PART_SCORING_INSTRUCTION + _part_proof_evidence(problem, plan.part_rubric) + "\nRepair format: student_quote and step.evidence are arrays of original line numbers.\n" + _json(output_model.model_json_schema()),
                    self.language, self.grading_setup),
                user_prompt=prompt, validator=_assessment_validator(plan,answer,problem),part_output=output_model)
            score,steps=score_assessment(result,plan,answer.content,problem_text=problem.stem)
            return ExpertResult(provider=self.provider.provider_id,score=score,max_score=problem.max_score,
                confidence=result.confidence,comment=result.comment,steps=steps,duration_ms=response.duration_ms or 0,
                raw_output=_json({"version":TRACE_VERSION,"plan_id":plan.plan_id,"assessment":result.model_dump(mode="json"),
                                  "review_pending":True,"relevance_review_pending":True,"review_advisory":True,"evidence":[]}))
        if plan.compilation_reason == SUBPART_ALLOCATION:
            validate = _assessment_validator(plan, answer, problem)
            result, response = await structured_llm_call(
                self.provider, output_model=SubpartGrade,
                system_prompt=build_system_prompt(
                    "Grade each labelled subpart under its frozen allocation. Return one award for each item ID "
                    "with its score and a reason specific to that part. Link credit to the submitted work using "
                    "claims and source_catalog. Missing answers earn zero. The program computes the total.\n"
                    + _json(SubpartGrade.model_json_schema()), self.language, self.grading_setup),
                user_prompt=prompt, validate_output=lambda value: validate(value.assessment(plan)))
            draft = result.assessment(plan)
            score, steps = score_assessment(draft, plan, answer.content or "", problem_text=problem.stem)
            return ExpertResult(provider=self.provider.provider_id, score=score, max_score=problem.max_score,
                confidence=draft.confidence, comment=draft.comment, steps=steps,
                knowledge_citations=kb_tool.citations(chunks), duration_ms=max(response.duration_ms or 0,
                    (perf_counter() - started) * 1000 if started is not None else 0),
                raw_output=_json({"version": TRACE_VERSION, "plan_id": plan.plan_id,
                                  "assessment": draft.model_dump(mode="json"), "evidence": [],
                                  **({"fallback": fallback} if fallback else {})}))
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
