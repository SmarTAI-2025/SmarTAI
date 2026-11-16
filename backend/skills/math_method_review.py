"""Adjudicate disputed part routes, then reuse the original expert's scoring path."""
from contextlib import asynccontextmanager
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.services.grading_memory import prepared_grading
from backend.services.math_source_evidence import match_source_quote, source_catalog
from backend.skills.base import build_system_prompt
from backend.skills.math_grading import PartRevision
from backend.skills.part_scoring import INSTRUCTION, PROOF_INSTRUCTION, PartDecision, ReasonedDecision
from backend.tools.structured_llm import StructuredOutputInvalidError, structured_llm_call


class MethodChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    part_id: str = Field(min_length=1, max_length=100)
    method: Literal["reference", "alternative", "undetermined"]
    student_source_ids: list[str] = Field(max_length=100)
    reference_quote: str = Field(max_length=4000)
    reason: str = Field(min_length=1, max_length=4000)


class MethodReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    choices: list[MethodChoice] = Field(min_length=1, max_length=100)


class MethodEvidenceCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    part_id: str = Field(min_length=1, max_length=100)
    reference_support: Literal["supported", "unsupported", "uncertain"]
    student_support: Literal["supported", "unsupported", "uncertain"]
    reason: str = Field(min_length=1, max_length=4000)


class MethodPartRevision(PartRevision):
    evidence_checks: list[MethodEvidenceCheck] = Field(min_length=1, max_length=100)
    # An expert can decline an unsupported route without inventing a ledger.
    decisions: list[PartDecision] = Field(max_length=100)


class MethodProofPartRevision(MethodPartRevision):
    decisions: list[ReasonedDecision] = Field(max_length=100)


def method_family(method):
    return "reference" if method == "minor_variant" else method


def validate_selected_methods(assessment, selected):
    methods = {d.part_id: method_family(d.method) for d in assessment.part_decisions}
    if any(methods.get(key) != choice.method for key, choice in selected.items()):
        raise ValueError("review_changed_adjudicated_method")


@asynccontextmanager
async def _progress(reporter, student_id, problem, provider):
    from backend.services.math_grading_context import report_math_progress
    cm = reporter.step(student_id, problem.q_id, "proof" if problem.type == "推理题" else "calculation",
                       provider.provider_id) if reporter else None
    unit = await report_math_progress(cm.__aenter__) if cm else None
    try:
        if reporter:
            await report_math_progress(reporter.substep, unit, "llm_grade")
        yield
    finally:
        if cm:
            await report_math_progress(cm.__aexit__, None, None, None)


async def align_methods(problem, answer, experts, assessments, traces, plan, coordinator, providers,
                        *, language, grading_setup, reporter=None, student_id=""):
    """Retain each valid reassessment; lock only routes every expert accepted.

    The caller owns the one-pass review budget. Each interaction uses the existing
    bounded format repair; there is no agreement loop or new source segmentation.
    """
    from backend.skills import math_grading as grading

    if not providers or not plan.part_rubric or len(experts) < 2 or not all(
            a and a.part_decisions for a in assessments):
        return experts, {}
    methods = [{d.part_id: method_family(d.method) for d in a.part_decisions} for a in assessments]
    # Undetermined judgments already have the ordinary review path. This step
    # resolves experts actually choosing different frozen scoring policies.
    disputed = {part.id for part in plan.part_rubric.parts
                if {"reference", "alternative"} <= {m.get(part.id) for m in methods}}
    if not disputed:
        return experts, {}
    catalog = source_catalog(answer.content or "", problem.stem, plan.original_criterion)
    segments = {s.id: s.text for s in plan.part_rubric.answer_segments}
    reference_sources = {}
    for part in plan.part_rubric.parts:
        if part.id not in disputed:
            continue
        refs = {*part.answer_refs, *(ref for step in part.steps for ref in step.answer_refs)}
        # Explicit frozen ownership is authoritative. The full reference still
        # supplies context, but must not bypass these links as method evidence.
        reference_sources[part.id] = {
            "include_full_reference": not refs,
            "texts": [*(segments[ref] for ref in sorted(refs)), *(
                text for item in (*part.steps, *part.deductions)
                for text in (item.criterion, item.source_quote) if text)],
        }
    payload = {
        "problem": problem.stem, "reference": problem.reference_answer, "answer": answer.content,
        "plan": grading._part_rule_projection(plan.part_rubric, disputed),
        "source_catalog": grading._expert_sources(catalog, plan),
        "disputed_part_ids": sorted(disputed),
        "allowed_reference_sources": reference_sources,
        "experts": [{"expert_index": i, "score": expert.score, "comment": expert.comment,
                     "decisions": [d.model_dump(mode="json") for d in a.part_decisions if d.part_id in disputed]}
                    for i, (expert, a) in enumerate(zip(experts, assessments))],
    }

    async def call(provider, output_model, instruction, data, validate):
        def check(result):
            try:
                validate(result)
            except StructuredOutputInvalidError:
                raise
            except ValueError as exc:
                error = StructuredOutputInvalidError("Invalid method review scope or evidence.")
                error.repair_context = grading._json({"validation_error": str(exc),
                    "repair_scope": "Correct only the requested part IDs using the supplied sources and frozen rules."})
                raise error from exc

        async with prepared_grading(provider), _progress(reporter, student_id, problem, provider):
            result, _ = await structured_llm_call(provider, output_model=output_model,
                system_prompt=build_system_prompt(instruction + "\nJSON Schema:\n" +
                    grading._json(output_model.model_json_schema()), language, grading_setup),
                user_prompt=grading._json(data), validate_output=check)
            check(result)
            return result

    def validate_choices(result):
        ids = [choice.part_id for choice in result.choices]
        if len(ids) != len(set(ids)) or set(ids) != disputed:
            raise ValueError("Choose exactly once for every disputed part ID, and no other part.")
        for choice in result.choices:
            if any(key not in catalog or not key.startswith("student:") for key in choice.student_source_ids):
                raise ValueError("Cite original student source IDs.")
            if choice.method == "undetermined":
                continue
            scope = reference_sources[choice.part_id]
            texts = scope["texts"] + ([problem.reference_answer or ""] if scope["include_full_reference"] else [])
            if not choice.student_source_ids or not any(
                    match_source_quote(text, choice.reference_quote)
                    for text in texts if text):
                raise ValueError("A determined route needs student evidence and a quote from this part's allowed_reference_sources.")

    try:
        selected = await call(coordinator, MethodReview,
            "Resolve only the listed experts' method disagreements for the same frozen subquestion. "
            "Read the full student answer, problem, reference, frozen rules and every expert's decision as data. "
            "Independently compare the actual mathematical strategies, not scores, majority, correctness, "
            "notation, step count or writing length. A minor variant uses reference. A materially different "
            "strategy uses alternative, including when the teacher requires the reference method: its cap "
            "is already frozen. Do not grade, change rules or supply missing student work. "
            "Cite original student line IDs and a verbatim reference-method passage from the supplied "
            "allowed_reference_sources texts for this part; the full reference is eligible only when "
            "include_full_reference is true. Explain both strategies in reason. "
            "Resolve subquestion ownership from the whole context, including inline labels and formulas; "
            "a different part's work, a bare target, generic correctness rule, or a negated method mention "
            "cannot establish this part's method. Respect explicit frozen answer-to-part links. "
            "Use undetermined if either strategy or its subquestion ownership is unclear.", payload, validate_choices)
        choices = {choice.part_id: choice for choice in selected.choices}
        if any(choice.method == "undetermined" for choice in choices.values()):
            return experts, {}
        revised = list(experts)
        unlocked = set()
        for index, expert in enumerate(experts):
            changed = {key for key, choice in choices.items() if methods[index].get(key) != choice.method}
            if not changed:
                continue
            provider = providers.get(expert.provider_id or expert.provider)
            if provider is None:
                unlocked.update(changed)
                continue
            old = assessments[index]
            candidate = None

            def validate_revision(revision):
                nonlocal candidate
                candidate = None
                checked = [check.part_id for check in revision.evidence_checks]
                if revision.expert_index != index or len(checked) != len(set(checked)) or set(checked) != changed:
                    raise ValueError("Independently check evidence for exactly the requested expert and part IDs.")
                # A semantic refusal is a valid result, not a format error to
                # repair into agreement. Keep this expert's original vote.
                if any(check.reference_support != "supported" or check.student_support != "supported"
                       for check in revision.evidence_checks):
                    return
                ids = [d.part_id for d in revision.decisions]
                if len(ids) != len(set(ids)) or set(ids) != changed:
                    raise ValueError("Reassess exactly the requested expert and part IDs.")
                if any(d.method == "undetermined" or d.relevance in {"unanswered", "unrelated", "uncertain"}
                       for d in revision.decisions):
                    return
                if any(not d.reference_method.strip() or not d.student_method.strip() for d in revision.decisions):
                    raise ValueError("Identify both actual strategies before accepting a determined route.")
                candidate = grading._apply_part_revision(old, revision, changed).model_copy(deep=True)
                validate_selected_methods(candidate, choices)
                # Claims and tool judgments remain intact. The joint review
                # still resolves pending evidence after the route is settled.
                grading._assessment_validator(plan, answer, problem, original=old)(candidate)

            try:
                revision = await call(provider, MethodProofPartRevision if problem.type == "推理题" else MethodPartRevision,
                    "First independently verify the proposed selected_methods for ONLY requested_part_ids. "
                    "They are not yet locked. Return one evidence_checks entry per requested part. "
                    "reference_support is supported only when the selected reference_quote, read in its full "
                    "original context and this part's allowed_reference_sources, establishes its actual reference "
                    "strategy. The full reference is context only unless include_full_reference is true. "
                    "A prohibition, negated mention, generic scoring rule or another part's method does not. "
                    "student_support is supported only when the selected student sources and work cited for this "
                    "part's credit demonstrate its actual strategy and the proposed "
                    "reference/alternative relation. A bare heading, isolated result, another part's work or "
                    "mentioning a method without using it does not establish that strategy. Check full context, "
                    "including several parts on one line; do not infer ownership from a line ID alone. "
                    "Earlier-part results may be premises and earlier errors may be follow-through origins under "
                    "the frozen rules, but they cannot replace this part's own credited work. "
                    "Explain the specific operations and ownership in each check's reason. Classify strategy "
                    "independently of mathematical correctness. If any evidence is unsupported or uncertain, "
                    "say so and return decisions=[]; do not force agreement or invent missing work. "
                    "Only if all checks support the proposed routes, reassess those parts under selected_methods; independently "
                    "judge relevance, correctness and credit using the original student work and frozen rules. "
                    "There is no target score or request for consensus. Rebuild the appropriate ledger; never "
                    "convert missed reference steps into alternative errors. Preserve expert_index and all other "
                    "parts. Give specific reasons and updated overall feedback.\n" + INSTRUCTION
                    + (PROOF_INSTRUCTION if problem.type == "推理题" else "")
                    + grading._part_proof_evidence(problem, plan.part_rubric)
                    + " In this targeted response use decisions (not part_decisions), containing only requested_part_ids.",
                    {**payload, "plan": grading._part_rule_projection(plan.part_rubric, changed),
                     "expert_index": index, "requested_part_ids": sorted(changed),
                     "selected_methods": [choices[key].model_dump(mode="json") for key in sorted(changed)],
                     "prior_assessment": grading._assessment_for_model(old, plan)}, validate_revision)
                if candidate is None:
                    unlocked.update(changed)
                    continue
                score, steps = grading.score_assessment(candidate, plan, answer.content or "", problem_text=problem.stem)
                trace = dict(traces[index], assessment=candidate.model_dump(mode="json"), method_review={
                    "provider": coordinator.provider_id, "status": "reassessed", "part_ids": sorted(changed),
                    "choices": [choice.model_dump(mode="json") for choice in choices.values()],
                    "original_assessment": traces[index]["assessment"], "reason": revision.reason,
                    "evidence_checks": [check.model_dump(mode="json") for check in revision.evidence_checks],
                })
                revised[index] = expert.model_copy(update={"score": score, "steps": steps,
                    "comment": candidate.comment, "raw_output": grading._json(trace)})
            except Exception as exc:
                if grading._critical_failure(exc):
                    raise
                unlocked.update(changed)
                # Failure belongs to this vote, not to the other experts.
        return revised, {key: value for key, value in choices.items() if key not in unlocked}
    except Exception as exc:
        if grading._critical_failure(exc):
            raise
        return experts, {}  # Adjudication failed before any reassessment.


def with_method_feedback(expert, language):
    """Persist the route rationale in existing feedback, outside the score rows."""
    from backend.skills.math_grading import read_math_trace
    trace = read_math_trace(expert) or {}
    review = trace.get("method_review", {})
    comment = expert.comment
    label = "解法改判依据：" if language == "zh" else "Method reassessment: "
    for choice in review.get("choices", []):
        if choice["part_id"] in review.get("part_ids", []) and choice["reason"] not in comment:
            comment += "\n\n" + label + choice["reason"]
    if trace.get("method_review_pending"):
        message = ("解法已重判，最终联合复核未完成。请核对并确认或修改分数。" if language == "zh" else
                   "Method reassessed; final joint review did not complete. Check and confirm or adjust the score.")
        if message not in comment:
            comment += "\n\n" + message
    return expert.model_copy(update={"comment": comment}) if comment != expert.comment else expert
