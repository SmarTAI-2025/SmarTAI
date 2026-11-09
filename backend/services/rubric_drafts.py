"""Persist semantic rubric work once; bind numerical preferences without an LLM.

Draft identity deliberately excludes display settings, experts and question IDs.
Question IDs can change when incremental packages are assembled; the frozen plan
still uses the final question identity. Teacher text remains the source of truth.
"""
from __future__ import annotations

import re
from decimal import Decimal
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from backend.services.part_rubric import Key, PartRubric, Points, bind_alternative, bind_parts, render_parts
from backend.services.math_rubric_plan import (
    MathRubricPlan, MathRubricRoute, _Explanation, _build_plan, _content_hash,
    _mapping, _question_input, _validated_explanation, _with_preferences,
    SUBPART_ALLOCATION, subpart_plan,
)


class WrittenRubricDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    part_rubric: PartRubric
    expected_truth_by_part: dict[str, StrictBool] = Field(default_factory=dict, strict=True, exclude_if=lambda v: not v)


class RubricDefinition(WrittenRubricDefinition):
    """Accept historic route definitions when loading prior generations."""

    part_rubric: PartRubric | None = Field(default=None, exclude_if=lambda v: v is None)
    routes: tuple[MathRubricRoute, ...] = Field(default=(), max_length=16)
    follow_through: str = Field(default="Preserve the frozen follow-through rule.", min_length=1, max_length=20000)
    numerical_policy: str = Field(default="Use the stated precision and units.", min_length=1, max_length=20000)
    advisories: tuple[str, ...] = Field(default=(), max_length=20)


class RubricDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["rubric_draft_v1"] = "rubric_draft_v1"
    alternative_policy_version: Literal[2, 3] | None = Field(default=None, exclude_if=lambda v: v is None)
    content_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    teacher_notes: str = ""
    # Unbound means no strictness/partial-credit prose has been appended.
    plan: MathRubricPlan


def draft_fingerprint(problem) -> str:
    context = _question_input(problem)
    # teacher_subpart_points is a generation-only hint; the accepted allocations
    # are already captured in the criterion and question maximum.
    return _content_hash({k: v for k, v in context.items()
                          if k not in {"q_id", "number", "teacher_subpart_points"}})


class RubricAllocationError(ValueError):
    """Model-safe feedback containing only known subpart labels and budgets."""


def _item_subpart(item, labels):
    from backend.services.question_structure import _allocation_marker_pattern

    # An explicit parent ID can bind a step even when the prose contains no
    # numeral. Never assign a subpart by the item's position in the reply.
    from_id = next((label for index, label in enumerate(labels, 1)
                    if item.id.casefold() == label.casefold() or re.fullmatch(
                        rf"(?:sp|subpart[_-]?){index}(?:[.:-][a-z0-9_-]+)?", item.id, re.I)), None)
    mentions = [label for label in labels if re.search(_allocation_marker_pattern(label), item.description, re.I)]
    heading = next((label for label in mentions if re.match(
        _allocation_marker_pattern(label), item.description.lstrip(), re.I)), None)
    if from_id and heading and from_id != heading:
        raise RubricAllocationError(f"Conflicting subpart assignment: item ID names {from_id}, description starts with {heading}.")
    if from_id or heading:
        return from_id or heading
    if len(mentions) == 1:
        return mentions[0]
    raise RubricAllocationError("Each scoring item needs one explicit parent subpart. Use its label in the description or spN in its ID.")


def render_rubric(definition: RubricDefinition, *, language="zh", subpart_labels=()) -> str:
    """One deterministic view of the same scoring data used by the grader."""
    zh = language == "zh"
    lines = []
    for route in definition.routes:
        condition = "" if route.applies_when.strip().lower() == "always" else f" — {route.applies_when}"
        lines.append(f"{route.description}{condition}")
        for item in route.items:
            amount = f"{item.points:g}"
            points = (f"扣{amount}分" if route.mode == "deductions" else f"{amount}分") if zh else (
                f"deduct {amount} points" if route.mode == "deductions" else f"{amount} points")
            limit = ""
            if route.mode == "deductions":
                limit = ("；不限次数" if item.max_occurrences is None else f"；最多{item.max_occurrences}次") if zh else (
                    "; unlimited occurrences" if item.max_occurrences is None else f"; at most {item.max_occurrences} times")
            adjustment = ""
            if item.default_adjustments:
                adjustment = "；未指定政策按批改严格度调整" if zh else "; unspecified policies follow grading strictness"
            if subpart_labels and route.mode == "points" and len(definition.routes) == 1:
                label = _item_subpart(item, subpart_labels)
                description = item.description.replace("\n", "\n  ")
                lines.append(f"- {label} [{points}] {description}{adjustment}")
            else:
                lines.append(f"- {item.description}：{points}{limit}{adjustment}")
        if route.minimum_score:
            condition = ("非空作答" if route.minimum_condition == "nonblank" else "") if zh else (
                "For nonblank answers, " if route.minimum_condition == "nonblank" else "")
            lines.append(f"{condition}最低{route.minimum_score:g}分" if zh else f"{condition}minimum {route.minimum_score:g} points")
        lines.extend(rule.description for rule in route.exemptions)
        if route.error_policy_source_quote:
            lines.append(route.error_policy_source_quote)
    lines.extend((definition.follow_through, definition.numerical_policy))
    return "\n".join(lines)


_PROOF_REQUEST = re.compile(r"\b(?:prove|disprove|show that|establish that)\b|证明|求证|试证", re.I)
_OPEN_PROOF_RULE = {
    "zh": "对题目要求证明的结论，以下分项给出参考证明的评分依据。题目或教师未限定方法时，"
          "其他完整有效的证明同样可得该结论的全部配分。部分证明按实际建立的推论和覆盖的情形给分。"
          "指定的方法、独立子问及其配分须分别满足。",
    "en": "For each conclusion to be proved, the items describe credit for a reference proof. "
          "Unless the question or teacher prescribes a method, another valid complete proof earns "
          "the full allocation for that conclusion. Credit a partial proof for the implications and "
          "cases actually established. Preserve required methods, distinct subquestions and their allocations.",
}


def generated_rubric(problem: dict, definition: RubricDefinition, *, language="zh"):
    """Validate against original teacher rules BEFORE rendering AI additions."""
    original = str(problem.get("criterion") or "")
    prior = saved_draft(problem) if original else None
    if prior is not None:
        # Completing a sibling field does not reinterpret an unchanged rubric.
        # The caller seals this verified plan against the completed response.
        return original, prior
    context = _question_input(problem)
    if definition.part_rubric is not None:
        try:
            rules = bind_parts(definition.part_rubric, {**problem, "criterion_source": context["criterion_source"]})
        except ValueError as exc:
            raise RubricAllocationError(str(exc)) from exc
        from backend.services.true_false_answers import is_decision_only, reference_decisions
        truth_key = definition.expected_truth_by_part
        if truth_key and set(truth_key) != {part.id for part in rules.parts}:
            from backend.services.part_rubric import canonical_label
            aliases = {canonical_label(part.label, rules.label): part.id for part in rules.parts}
            mapped = {aliases.get(canonical_label(key, rules.label), key): value for key, value in truth_key.items()}
            if len(mapped) == len(truth_key):
                truth_key = mapped
        decision_only = is_decision_only(context["stem"], rules.teacher_rubric or "")
        if truth_key and (not decision_only or set(truth_key) != {part.id for part in rules.parts}
                          or not all(len(part.steps) == 1 for part in rules.parts)):
            raise RubricAllocationError(
                f"Return one decision step per part and one boolean per part ID. Required IDs: "
                f"{[p.id for p in rules.parts]}; received {list(truth_key)}."
            )
        if truth_key:
            explicit_key = reference_decisions(problem.get("reference_answer") or "", [p.label for p in rules.parts]) if all(p.label for p in rules.parts) else None
            if explicit_key and any(explicit_key[p.label] != truth_key[p.id] for p in rules.parts):
                raise RubricAllocationError("The truth key contradicts the labelled verdicts in the adopted reference answer.")
            for part in rules.parts:
                explicit = re.search(r"(?:判(?:断)?(?:.*?为)?|答案(?:为|是))\s*(正确|错误|真|假)[。.!！]?\s*$", part.steps[0].criterion)
                if explicit and truth_key[part.id] != (explicit[1] in {"正确", "真"}):
                    raise RubricAllocationError(f"{part.id}: the truth key and the stated expected decision disagree.")
        criterion = original or render_parts(rules, language)
        completed = {**problem, "criterion": criterion}
        if not original:
            completed["ai_completion_provenance"] = {**(problem.get("ai_completion_provenance") or {}),
                                                    "criterion":{"source_kind":"ai_generated"}}
        explanation = _Explanation(routes=(MathRubricRoute(id="parts", description="Apply the frozen per-part rubric.",
            applies_when="always", mode="holistic"),), effective_criterion=criterion,
            follow_through=definition.follow_through, numerical_policy=definition.numerical_policy,
            advisories=(*rules.teacher_gaps, *(("教师评分规则的识别置信度低，请核对原文。" if language == "zh" else "Low confidence interpreting the teacher rubric. Check the original wording.",) if rules.low_confidence else ())))
        base = _build_plan(_question_input(completed), explanation, reason="frozen_part_rubric")
        body = base.model_dump(mode="json", exclude={"plan_id"})
        body["part_rubric"] = rules.model_dump(mode="json")
        if truth_key:
            body["expected_truth_by_part"] = truth_key
        plan = MathRubricPlan(**body, plan_id=_content_hash(body))
        return criterion, RubricDraft(alternative_policy_version=3, content_fingerprint=draft_fingerprint(completed), plan=plan)
    explicit = subpart_plan(context, candidate=definition)
    if explicit:
        return original, RubricDraft(content_fingerprint=draft_fingerprint(problem), plan=explicit)
    labels = [part["label"] for part in (context.get("question_structure") or {}).get("subparts", [])]
    if not original and labels and len(definition.routes) == 1 and definition.routes[0].mode == "points":
        amounts = {}
        for item in definition.routes[0].items:
            label = _item_subpart(item, labels)
            value = Decimal(str(item.points))
            if value != value.quantize(Decimal("0.01")):
                raise RubricAllocationError(f"Use at most two decimal places for the budget of {label}.")
            amounts[label] = amounts.get(label, Decimal(0)) + value
        missing = [label for label in labels if label not in amounts]
        total = sum(amounts.values(), Decimal(0))
        maximum = Decimal(str(context["max_score"]))
        if missing or total != maximum:
            raise RubricAllocationError(
                f"Missing subparts: {', '.join(missing) or 'none'}. "
                f"Item budgets total {total:g}; required {maximum:g}. "
                "Cover every listed subpart and correct the item budgets to the required total.")
    explanation = _validated_explanation(context, _Explanation(
        **definition.model_dump(), effective_criterion=(
            "按以下结构化评分项执行；页面标准与此底稿一致。" if language == "zh" else
            "Apply these structured routes; the displayed rubric uses the same definition."
        ),
    ))
    # Existing teacher material is adopted, never replaced with a generated view.
    canonical_definition = RubricDefinition.model_validate(explanation.model_dump(exclude={"effective_criterion"}))
    proof_points = (problem.get("type") == "推理题" and _PROOF_REQUEST.search(str(problem.get("stem") or ""))
                    and len(explanation.routes) == 1 and explanation.routes[0].mode == "points")
    # Only a fresh generated definition (or its exact saved rendering) can
    # supply generated policies. Existing teacher prose, including exceptions,
    # remains authoritative even if a model proposes a simpler item ledger.
    generated_definition = not original or (
        _question_input(problem)["criterion_source"] == "ai"
        and any((original.removeprefix(rule + "\n") if proof_points else original)
                == render_rubric(canonical_definition, language=lang, subpart_labels=shape)
                for shape in ((), labels) for lang, rule in _OPEN_PROOF_RULE.items())
    )
    criterion = original or render_rubric(canonical_definition, language=language, subpart_labels=labels)
    if not original and proof_points:
        # Freeze method equivalence in the same source that both teachers and graders read.
        criterion = _OPEN_PROOF_RULE["zh" if language == "zh" else "en"] + "\n" + criterion
    completed = {**problem, "criterion": criterion}
    if not original:
        completed["ai_completion_provenance"] = {
            **(problem.get("ai_completion_provenance") or {}), "criterion": {"source_kind": "ai_generated"},
        }
    holistic = all(route.mode == "holistic" for route in explanation.routes)
    plan = _build_plan(_question_input(completed), explanation,
        status="holistic" if holistic else "compiled",
        reason="model_selected_holistic" if holistic else "",
        generated_definition=generated_definition)
    return criterion, RubricDraft(content_fingerprint=draft_fingerprint(completed), plan=plan)


def retarget_draft(draft: RubricDraft, problem) -> RubricDraft:
    """Seal a generated candidate after sibling fields in its response are known."""
    payload = draft.plan.model_dump(mode="json", exclude={"plan_id"})
    context = _question_input(problem)
    payload["input_fingerprint"] = _content_hash(context)
    if draft.plan.part_rubric:
        from backend.services.true_false_answers import adopted_truth_key

        rules = draft.plan.part_rubric
        payload["part_rubric"]["id"] = context["q_id"] or rules.id
        payload["part_rubric"]["label"] = context["number"]
        key = adopted_truth_key(rules, context["stem"], context["reference_answer"])
        payload.pop("expected_truth_by_part", None)
        if key:
            payload["expected_truth_by_part"] = key
    plan = MathRubricPlan(plan_id=_content_hash(payload), **payload)
    return draft.model_copy(update={"content_fingerprint": draft_fingerprint(problem), "plan": plan})


def _replace_draft_parts(draft, parts, context):
    rules = draft.plan.part_rubric
    body = draft.plan.model_dump(mode="json", exclude={"plan_id"})
    body["part_rubric"] = rules.model_copy(update={
        "parts": tuple(parts), "rubric_version": rules.rubric_version + 1,
    }).model_dump(mode="json")
    body["input_fingerprint"] = _content_hash(context)
    plan = MathRubricPlan(plan_id=_content_hash(body), **body)
    return draft.model_copy(update={"alternative_policy_version": 3, "plan": plan})


def _refresh_teacher_alternatives(draft, context):
    """Reinterpret stored teacher wording before adopting a preparation draft."""
    rules = draft.plan.part_rubric
    if not rules or draft.alternative_policy_version == 3:
        return draft
    overrides = {(item["label"], item["stem"]): item["text"] for item in
                 (context.get("alternative_rules") or {}).values()}
    updated = []
    for part in rules.parts:
        policy = bind_alternative(part, label=part.label, stem=part.stem or context["stem"],
            structure=context["question_structure"] or {}, floor=rules.nonblank_floor.resolve(rules.total_pts),
            question_points=rules.total_pts, teacher=rules.teacher_rubric or "",
            override=overrides.get((part.label, part.stem)))
        updated.append(part if policy == part.alternative else part.model_copy(update={"alternative":policy}))
    if tuple(updated) == rules.parts:
        return draft.model_copy(update={"alternative_policy_version": 3})
    return _replace_draft_parts(draft, updated, context)



def saved_draft(problem, *, grading_setup=None, allow_incomplete_alternative=False) -> RubricDraft | None:
    row = _mapping(problem)
    source = row.get("source") or getattr(problem, "_math_rubric_source", {}) or {}
    presentation = source.get("presentation") or {} if isinstance(source, Mapping) else {}
    if not isinstance(presentation, Mapping):
        return None
    value = row.get("rubric_draft") or presentation.get("rubric_draft")
    if not value:
        return None
    try:
        draft = RubricDraft.model_validate(value)
    except (ValueError, TypeError):
        return None
    setup = _mapping(grading_setup) if grading_setup is not None else {}
    context = _question_input(problem)
    if (draft.content_fingerprint != draft_fingerprint(problem)
            or (not draft.plan.part_rubric and draft.teacher_notes != str(setup.get("teacher_notes") or ""))
            or draft.plan.original_criterion != context["criterion"]
            or draft.plan.max_score != context["max_score"]):
        return None
    from backend.services.teacher_point_allocations import teacher_awards_match
    if not teacher_awards_match(draft.plan.part_rubric):
        return None
    try:
        draft = _refresh_teacher_alternatives(draft, context)
    except ValueError:
        return None
    if (draft.plan.part_rubric and not allow_incomplete_alternative
            and draft.plan.part_rubric.missing_alternative_rates()):
        return None
    if draft.plan.part_rubric and draft.plan.expected_truth_by_part:
        from backend.services.true_false_answers import DecisionReferenceConflict, adopted_truth_key
        try:
            key = adopted_truth_key(draft.plan.part_rubric, context["stem"], context["reference_answer"])
        except DecisionReferenceConflict:
            return None
        if key != draft.plan.expected_truth_by_part:
            return retarget_draft(draft, problem)
    # An older cached interpretation may have discarded an unambiguous allocation.
    # Only preparation reads this cache; a running run uses its own frozen plan.
    if draft.plan.part_rubric is None and draft.plan.compilation_reason != SUBPART_ALLOCATION:
        explicit = subpart_plan(context, teacher_notes=draft.teacher_notes, candidate=draft.plan)
        if explicit:
            return draft.model_copy(update={"plan": explicit})
    return draft


def bind_draft(draft: RubricDraft, problem, *, grading_setup=None) -> MathRubricPlan:
    from backend.services.teacher_point_allocations import validate_teacher_point_items
    from backend.services.part_rubric import ordinary_criterion
    if draft.plan.part_rubric and draft.plan.part_rubric.teacher_rubric:
        rules = draft.plan.part_rubric
        validate_teacher_point_items(rules, ordinary_criterion(rules.teacher_rubric))
    if draft.content_fingerprint != draft_fingerprint(problem):
        raise ValueError("rubric_draft_content_changed")
    setup = _mapping(grading_setup) if grading_setup is not None else {}
    if not draft.plan.part_rubric and draft.teacher_notes != str(setup.get("teacher_notes") or ""):
        raise ValueError("rubric_draft_teacher_policy_changed")
    plan = retarget_draft(_refresh_teacher_alternatives(draft, _question_input(problem)), problem).plan
    if plan.part_rubric and (missing := plan.part_rubric.missing_alternative_rates()):
        raise ValueError(f"Complete alternative.deduct_per_error for parts {missing} before grading.")
    return _with_preferences(plan, grading_setup)


def pending_alternatives(overrides, structure):
    """Only persisted teacher edits for parts still present in this question."""
    scopes = {(p["label"], p["stem"]) for p in (structure or {}).get("subparts", [])} or {(None, None)}
    return {key: value for key, value in overrides.items()
            if (value["label"], value["stem"]) in scopes}


def edit_alternatives(problem, presentation, updates, *, previous=None, allow_pending=False):
    """Resolve displayed identities against the complete candidate under its revision lock."""
    from backend.services.part_rubric import UninterpretedAlternative, read_alternative

    context = _question_input(problem)
    structure = context["question_structure"] or {}
    overrides = pending_alternatives(presentation.get("alternative_rules") or {}, structure)
    current = saved_draft(problem, allow_incomplete_alternative=True)
    displayed = saved_draft(previous, allow_incomplete_alternative=True) if previous is not None else current
    identities = {key: (v["label"], v["stem"]) for key, v in overrides.items()}
    if displayed and displayed.plan.part_rubric:
        identities = {p.id: (p.label, p.stem) for p in displayed.plan.part_rubric.parts}
        by_scope = {(v["label"], v["stem"]): v for v in overrides.values()}
        overrides = {key: by_scope[scope] for key, scope in identities.items() if scope in by_scope}

    budgets = {(p["label"], p["stem"]): None for p in structure.get("subparts", [])}
    if not budgets:
        budgets[(None, None)] = problem.max_score
    else:
        points = {i["label"]: float(i["points"]) for i in
                  (presentation.get("rubric_point_summary") or {}).get("items", [])}
        budgets = {scope: points.get(scope[0]) for scope in budgets}
    rules = current.plan.part_rubric if current else None
    if rules:
        budgets = {(p.label, p.stem): p.total_pts for p in rules.parts}
    if updates is not None:
        if not updates or any(identities.get(key) not in budgets for key in updates):
            raise ValueError("Unknown or changed scoring part. Reload the question.")
        for key, text in updates.items():
            label, stem = identities[key]
            # A regenerated plan may assign a new ID to the same source part.
            overrides = {k: v for k, v in overrides.items() if (v["label"], v["stem"]) != (label, stem)}
            overrides[key] = {"text": text.strip(), "label": label, "stem": stem}

    frozen = {(p.label, p.stem): p for p in rules.parts} if rules else {}
    policies = {}
    for item in overrides.values():
        scope = (item["label"], item["stem"])
        budget = budgets[scope]
        prior = frozen.get(scope)
        try:
            if prior:
                policy = bind_alternative(
                    prior, label=prior.label, stem=prior.stem or problem.stem, structure=structure,
                    floor=rules.nonblank_floor.resolve(problem.max_score), question_points=problem.max_score,
                    teacher=rules.teacher_rubric or "", override=item["text"],
                )
            else:
                policy = read_alternative(item["text"], maximum=budget if budget is not None else problem.max_score)
        except UninterpretedAlternative:
            if not allow_pending:
                raise
            continue
        if budget is None and (policy.maximum.points or policy.minimum.points):
            raise ValueError("请先明确该小问的满分，或使用按比例的上限。 Specify the part budget for an absolute score.")
        policies[scope] = policy
    presentation["alternative_rules"] = overrides
    problem._math_rubric_source = {**problem._math_rubric_source, "presentation": presentation}
    if not rules or not updates or len(policies) != len(overrides):
        # The changed ordinary criterion/budgets are compiled through the usual
        # preparation path. Never seal the old steps to a new question fingerprint.
        return
    parts = [p.model_copy(update={"alternative": policies[(p.label, p.stem)]})
             if (p.label, p.stem) in policies else p for p in rules.parts]
    updated = _replace_draft_parts(current, parts, _question_input(problem))
    presentation["rubric_draft"] = updated.model_copy(update={
        "content_fingerprint": draft_fingerprint(problem),
    }).model_dump(mode="json")



def rebind_review_edit(prior, old_problem, problem):
    """Reuse structure only for edits whose scoring destination is unambiguous."""
    from backend.services.part_rubric import _POLICY_TOKEN, alternative_clauses, ordinary_criterion
    from backend.services.question_structure import _allocation_marker_pattern

    if not prior or not prior.plan.part_rubric:
        return None
    old, new = _question_input(old_problem), _question_input(problem)
    if any(old[k] != new[k] for k in ("stem", "number", "type", "reference_answer", "question_structure")):
        return None
    rules = prior.plan.part_rubric
    old_text, new_text = old["criterion"], new["criterion"]
    old_alts, new_alts = alternative_clauses(old_text), alternative_clauses(new_text)
    if len(old_alts) != len(new_alts):
        return None
    for old_alt, new_alt in zip(old_alts, new_alts):
        if _POLICY_TOKEN.sub("#", old_alt) != _POLICY_TOKEN.sub("#", new_alt):
            return None
    old_text, new_text = ordinary_criterion(old_text), ordinary_criterion(new_text)
    parts = list(rules.parts)
    if old_text != new_text or old["max_score"] != new["max_score"]:
        # A single funded step for each part, with only its declared amount
        # changed. Never reinterpret numbers in mathematical criteria or limits.
        if any(len(p.steps) != 1 or p.steps[0].source_quote for p in parts):
            return None
        old_lines, new_lines = old_text.strip().splitlines(), new_text.strip().splitlines()
        if len(old_lines) != len(parts) or len(new_lines) != len(parts):
            return None
        overrides = {(v["label"], v["stem"]): v["text"] for v in new.get("alternative_rules", {}).values()}
        for i, (part, before, after) in enumerate(zip(parts, old_lines, new_lines)):
            target = _allocation_marker_pattern(part.label) if part.label else ""
            statement = target + r"\s*(?:[：:]\s*)?(?:正确结果给|答对(?:给|得)|满分|共)?\s*(\d+(?:\.\d+)?)\s*分[。.]?\s*"
            was = re.fullmatch(statement, before)
            now = re.fullmatch(statement, after)
            if not was or not now or re.sub(r"\d+(?:\.\d+)?(?=\s*分)", "#", before) != re.sub(
                    r"\d+(?:\.\d+)?(?=\s*分)", "#", after):
                return None
            if Decimal(was[1]) != Decimal(str(part.total_pts)):
                return None
            points = float(now[1])
            step = part.steps[0].model_copy(update={"total_pts": points})
            resized = part.model_copy(update={"total_pts": points, "steps": (step,)})
            try:
                policy = bind_alternative(
                    resized, label=part.label, stem=part.stem or new["stem"], structure=new["question_structure"] or {},
                    floor=rules.nonblank_floor.resolve(new["max_score"]), question_points=new["max_score"],
                    teacher=new["criterion"], override=overrides.get((part.label, part.stem)),
                )
            except ValueError:
                return None
            parts[i] = resized.model_copy(update={"alternative": policy})
    try:
        data = rules.model_dump(mode="json")
        data.update(parts=[part.model_dump(mode="json") for part in parts], total_pts=new["max_score"],
                    rubric_version=rules.rubric_version + 1)
        definition = RubricDefinition(part_rubric=PartRubric.model_validate(data),
                                      expected_truth_by_part=prior.plan.expected_truth_by_part)
        _, draft = generated_rubric(new, definition)
        return retarget_draft(draft, problem)
    except ValueError:
        return None


class _AlternativeRates(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deduct_per_error: dict[Key, Points] = Field(min_length=1, max_length=100)


async def compile_parts_draft(problem, provider, *, grading_setup=None, reporter=None, previous=None, alternative_ids=None):
    """Only missing/stale semantic content reaches the bounded compiler."""
    import json
    from backend.domain.errors import ValidationError
    from backend.services.part_rubric import PART_INSTRUCTION
    from backend.tools.structured_llm import structured_llm_call, StructuredOutputInvalidError

    context = _question_input(problem)
    language = (_mapping(grading_setup).get("feedback_language") if grading_setup else None) or "zh"
    prior = saved_draft(problem, allow_incomplete_alternative=True)
    if prior and prior.plan.part_rubric and (missing := prior.plan.part_rubric.missing_alternative_rates()):
        # Older matching drafts may already have teacher-approved ordinary steps.
        # Complete only the missing fields, once per question before expert fan-out.
        def validate_rates(response):
            if set(response.deduct_per_error) != set(missing) or any(v <= 0 for v in response.deduct_per_error.values()):
                error = StructuredOutputInvalidError("Invalid alternative deduction rates.")
                error.repair_context = f"Supply one positive deduct_per_error for each of {missing}, and no other part IDs."
                raise error
        try:
            response, _ = await structured_llm_call(provider,
                system_prompt="Complete only the missing alternative per-independent-error deductions in absolute points. "
                              "Use the original targets, teacher rules and frozen budgets. Return the exact requested IDs.",
                user_prompt=json.dumps({"question":context, "frozen_parts":prior.plan.part_rubric.model_dump(mode="json"),
                                        "missing_part_ids":missing}, ensure_ascii=False),
                output_model=_AlternativeRates, validate_output=validate_rates)
        except StructuredOutputInvalidError as exc:
            raise ValidationError("Complete the missing alternative deduction rates before grading.",
                                  code="invalid_structured_rubric") from exc
        parts = [p.model_copy(update={"alternative":p.alternative.model_copy(update={
                        "deduct_per_error":response.deduct_per_error[p.id]})}) if p.id in missing else p
                 for p in prior.plan.part_rubric.parts]
        return retarget_draft(_replace_draft_parts(prior, parts, context), problem)
    source = {**context}
    if context["criterion_source"] == "ai":
        source["ai_completion_provenance"] = {"criterion": {}}
    draft = None

    previous_rules = previous.plan.part_rubric if previous else None
    request = dict(context)
    if previous_rules:
        request["previous_scoring_rules"] = previous_rules.model_dump(mode="json")
        request["revision_instruction"] = (
            "Apply the new teacher text as authoritative. Preserve unaffected obligations, stable IDs, "
            "budgets and alternative policies. Replace superseded conditions. Previous quotes are historical."
        )

    def validate(candidate):
        nonlocal draft
        try:
            definition = RubricDefinition.model_validate(candidate.model_dump())
            if previous_rules and definition.part_rubric:
                from backend.services.part_rubric import canonical_label
                proposed = definition.part_rubric
                previous_ids = {canonical_label(p.label): p.id for p in previous_rules.parts}
                if any(previous_ids.get(canonical_label(p.label), p.id) != p.id for p in proposed.parts):
                    raise RubricAllocationError(f"Keep existing scoring part IDs: {previous_ids}.")
                if alternative_ids:
                    policies = {p.id: p.alternative for p in proposed.parts}
                    if not alternative_ids.issubset(policies):
                        raise RubricAllocationError(f"Supply the edited alternatives for {sorted(alternative_ids)}.")
                    proposed = previous_rules.model_copy(update={"parts": tuple(
                        p.model_copy(update={"alternative": policies[p.id]}) if p.id in alternative_ids else p
                        for p in previous_rules.parts
                    )})
                    definition = definition.model_copy(update={"expected_truth_by_part": previous.plan.expected_truth_by_part})
                definition = definition.model_copy(update={"part_rubric": proposed.model_copy(
                    update={"rubric_version": previous_rules.rubric_version + 1})})
            _, draft = generated_rubric(source, definition, language=language)
        except RubricAllocationError as exc:
            error = StructuredOutputInvalidError("Invalid frozen scoring rules.")
            error.repair_context = str(exc)
            raise error from exc

    try:
        await structured_llm_call(provider, system_prompt=(
            PART_INSTRUCTION + "\nReturn the written rubric definition in " + language + ".\n" +
            json.dumps(WrittenRubricDefinition.model_json_schema(), ensure_ascii=False)),
            user_prompt=json.dumps(request, ensure_ascii=False), output_model=WrittenRubricDefinition,
            validate_output=validate)
    except StructuredOutputInvalidError as exc:
        raise ValidationError("The scoring rules could not be validated. Review this question's rubric.",
                              code="invalid_structured_rubric") from exc
    return retarget_draft(draft, problem)


def requires_part_rubric(problem):
    row = _mapping(problem)
    source = row.get("source") or getattr(problem, "_math_rubric_source", {}) or {}
    presentation = source.get("presentation") or {}
    draft = row.get("rubric_draft") or presentation.get("rubric_draft") or {}
    return presentation.get("rubric_schema_version") == 2 or bool((draft.get("plan") or {}).get("part_rubric"))


async def compile_draft(problem, provider, *, grading_setup=None, reporter=None):
    """Only missing/stale semantic content reaches the bounded compiler."""
    if requires_part_rubric(problem):
        return await compile_parts_draft(problem, provider, grading_setup=grading_setup, reporter=reporter)
    from backend.services.math_rubric_plan import prepare_math_rubric_plan

    setup = dict(_mapping(grading_setup)) if grading_setup is not None else {}
    # Core obligations and weights must not depend on a numerical slider.
    semantic_setup = {**setup, "strictness": 50, "allow_partial_credit": True}
    plan = await prepare_math_rubric_plan(problem, provider, grading_setup=semantic_setup,
                                         reporter=reporter, bind_preferences=False)
    return RubricDraft(content_fingerprint=draft_fingerprint(problem),
                       teacher_notes=str(setup.get("teacher_notes") or ""), plan=plan)


def remember_draft(problem, draft: RubricDraft, *, question_id=None, expected_version=None, checkpoints=None):
    """Cache metadata without changing teacher content or its version.

    Persist only if the actual question still has the content that was compiled.
    A stale operation cannot overwrite a later teacher edit or another question.
    """
    from copy import deepcopy

    from backend.services.math_rubric_plan import _UNREPRESENTABLE_RULES

    if (draft.plan.compilation_status == "holistic"
            and draft.plan.compilation_reason not in {*_UNREPRESENTABLE_RULES, "model_selected_holistic"}):
        # Freeze a safe fallback for this run, but a transient malformed answer
        # must not become a permanent cache hit on the next explicit AI action.
        return
    value = draft.model_dump(mode="json")
    if question_id is not None and checkpoints is not None:
        import time
        from sqlalchemy import select, update
        from backend.db import grading_repository
        from backend.db.models import AssignmentQuestionRecord, GradingRunRecord
        from backend.db.session import session_scope
        from backend.domain.errors import LeaseLost
        from backend.services.math_grading_context import problem_from_question

        with session_scope() as session:
            run = session.scalar(select(GradingRunRecord).where(
                GradingRunRecord.id == checkpoints.run_id,
            ).with_for_update())
            if (run is None or run.status != "running" or run.lease_owner != checkpoints.worker_id
                    or (run.lease_expiry or 0) <= time.time() or run.released_at is not None
                    or grading_repository._active_assignment_for_run(session, run.id,
                        actor_id=run.teacher_id, lock=True) is None):
                raise LeaseLost("rubric_draft_write_lease_lost")
            record = session.scalar(select(AssignmentQuestionRecord).where(
                AssignmentQuestionRecord.id == question_id,
                AssignmentQuestionRecord.assignment_id == run.assignment_id,
            ).with_for_update())
            if (record is None or record.version != expected_version
                    or draft_fingerprint(problem_from_question(record)) != draft.content_fingerprint):
                return
            source = deepcopy(record.source or {})
            source.setdefault("presentation", {})["rubric_draft"] = value
            # ORM onupdate timestamps represent teacher content edits. Cache
            # metadata must preserve that time as well as the content version.
            session.execute(update(AssignmentQuestionRecord).where(
                AssignmentQuestionRecord.id == record.id,
            ).values(source=source, updated_at=record.updated_at))
    source = deepcopy(problem._math_rubric_source)
    source.setdefault("presentation", {})["rubric_draft"] = value
    problem._math_rubric_source = source
