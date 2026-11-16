"""Concise presentation of scored evidence; never regrade or rewrite saved votes."""
from collections import defaultdict
import re


def join_expert_feedback(entries):
    """Retain attribution while showing identical explanations only once."""
    grouped = {}
    for provider, description in entries:
        grouped.setdefault(description, []).append(provider)
    return "\n\n".join(f"{', '.join(providers)}: {description}"
                       for description, providers in grouped.items())


def _concise_steps(steps, trace, plan):
    from backend.skills.math_grading import MathAssessment, TRACE_VERSION

    if (not plan or not isinstance(trace, dict) or trace.get("version") != TRACE_VERSION
            or trace.get("plan_id") != plan.plan_id):
        return steps
    try:
        assessment = MathAssessment.model_validate(trace.get("assessment"))
        if plan.part_rubric and assessment.route_id == "parts":
            return _part_steps(steps, assessment, plan.part_rubric)
        route = next(route for route in plan.routes if route.id == assessment.route_id)
        if route.mode == "deductions" and len(steps) == 1 and not assessment.awards:
            return _concise_deductions(steps, assessment, route)
        if route.mode != "points" or len(route.items) != len(steps) or assessment.deductions:
            return steps
        awards = {award.item_id: award for award in assessment.awards}
        claims = {claim.id: claim for claim in assessment.claims}
        if len(awards) != len(assessment.awards) or set(awards) != {item.id for item in route.items}:
            return steps
        rendered = []
        for item, step in zip(route.items, steps):
            award = awards[item.id]
            quotes = " / ".join(claims[key].quote or claims[key].source_quote for key in award.claim_ids)
            legacy = f"{item.description}\n{quotes}\n{award.reason}".strip()
            desc = step["desc"]
            # Recognize only the exact old program template. Preserve appended
            # arithmetic, minimum/binary policies and targeted-review evidence.
            if award.reason.strip() and (desc == legacy or desc.startswith(legacy + "\n")):
                desc = award.reason.strip() + desc[len(legacy):]
            rendered.append({**step, "desc": desc})
        return rendered
    except (ValueError, TypeError, KeyError, StopIteration, AttributeError):
        # Incomplete/older evidence is not permission to guess which text to cut.
        return steps


def _part_steps(steps, assessment, rules):
    """Expose saved route evidence without changing a saved numerical judgment."""
    from fractions import Fraction
    from backend.skills.part_scoring import charged_deductions

    decisions = {d.part_id: d for d in assessment.part_decisions}
    if len(decisions) != len(assessment.part_decisions) or set(decisions) != {part.id for part in rules.parts}:
        return steps
    replacements = {}
    for part in rules.parts:
        decision = decisions[part.id]
        leaves = [(i, s) for i, s in enumerate(steps) if s.get("part_id") == part.id]
        if len(leaves) != 1 or leaves[0][1].get("item_id") is not None:
            continue
        index, step = leaves[0]
        alternative = decision.method == "alternative"
        if not alternative and part.steps:
            if step["score"] == 0 and decision.relevance in {"incorrect", "unanswered", "unrelated", "uncertain"}:
                reasons = {s.id: s.reason for s in decision.steps}
                replacements[index] = [{**step, "item_id": s.id, "desc": reasons.get(s.id, step["desc"])} for s in part.steps]
            continue
        details = []
        if decision.relevance in {"complete", "partial", "incorrect"}:
            cap = part.alternative.maximum.resolve(part.total_pts) if alternative else Fraction(str(part.total_pts))
            details = charged_deductions(part, decision, cap)
            expected = float(cap) - sum(item.points for item in details)
            if abs(expected - step["score"]) > .005 + 1e-9:
                continue  # A differing saved score is not permission to invent charges.
        elif step["score"] != 0:
            continue
        replacements[index] = [{**step, "deductions": [item.model_dump() for item in details],
                                "deduction_base": float(part.alternative.maximum.resolve(part.total_pts) if alternative else part.total_pts),
                                **({"is_alternative": True} if alternative else {})}]
    rows = [item for i, step in enumerate(steps) for item in replacements.get(i, [step])]
    return [{**step, "step_no": i} for i, step in enumerate(rows, 1)]


def _concise_deductions(steps, assessment, route):
    """Keep the stored deduction amounts and policy explanations verbatim."""
    claims = {claim.id: claim for claim in assessment.claims}
    rules = {rule.id: rule for rule in route.items}
    exemptions = {rule.id: rule for rule in route.exemptions}
    description, offset, rendered = steps[0]["desc"], 0, []
    for deduction in assessment.deductions:
        claim = claims[deduction.claim_id]
        exemption = exemptions.get(deduction.rule_id)
        rule = exemption or rules[deduction.rule_id]
        policy = (exemption.description if exemption else
                  exemptions[deduction.exemption_id].description if deduction.exemption_id else
                  route.error_policy_source_quote if deduction.caused_by and route.error_policy == "no_repeat_deduction" else "")
        suffix = f"\n{policy}" if policy else ""
        legacy_suffix = "" if exemption else suffix
        # Only the program's exact old template qualifies. Amounts are read,
        # never recalculated; source and model text are escaped literal data.
        pattern = (re.escape(f"{rule.description}: −") + r"([0-9]+(?:\.[0-9]+)?(?:E[+-]?[0-9]+)?)" +
                   re.escape(f"; {claim.quote or claim.source_quote}\n{deduction.reason}{legacy_suffix}"))
        if rendered:
            pattern = re.escape("\n\n") + pattern
        match = re.match(pattern, description[offset:])
        if match is None:
            return steps
        rendered.append(f"−{match.group(1)}: {deduction.reason}{suffix}")
        offset += match.end()
    if not rendered or (description[offset:] and not description[offset:].startswith("\n")):
        return steps
    return [{**steps[0], "desc": "\n\n".join(rendered) + description[offset:]}]


def _combined_steps(steps, original, rendered, maximum):
    """Replace only known single/weighted projection shapes, keeping every score."""
    if len(original) == len(rendered) == 1 and steps == original[0].get("steps"):
        # A single-expert copy shares its evidence, including route and charges.
        # Never infer weighted deductions from a multi-expert aggregate total.
        return [dict(step) for step in rendered[0].get("steps", [])]
    result = []
    for index, step in enumerate(steps):
        desc = step.get("desc", "")
        candidates = set()
        for old, new in zip(original, rendered):
            for old_step, new_step in zip(old.get("steps", []), new.get("steps", [])):
                if desc == old_step.get("desc"):
                    candidates.add(new_step["desc"])
        if original and all(len(expert.get("steps", [])) > index for expert in original):
            old_text = "\n\n".join(f"{expert['provider']}: {expert['steps'][index]['desc']}" for expert in original)
            if desc == old_text:
                candidates.add(join_expert_feedback(
                    (expert["provider"], expert["steps"][index]["desc"]) for expert in rendered))
        # Mixed routes/floors use one aggregate step, with each full vote labeled.
        if len(steps) == 1 and original:
            def whole(experts):
                return "\n\n".join(f"{expert['provider']} ({expert['score']:g}/{maximum:g}):\n" +
                    "\n".join(item["desc"] for item in expert.get("steps", [])) for expert in experts)
            if desc == whole(original):
                candidates.add(whole(rendered))
        result.append({**step, "desc": next(iter(candidates)) if len(candidates) == 1 else desc})
    return result


def _feedback_context(run_id):
    """Review copies inherit AI evidence only from their recorded source run."""
    from sqlalchemy import select
    from backend.db import workflow_repository
    from backend.db.models import GradingRunEventRecord
    from backend.db.session import session_scope
    from backend.services.grading_checkpoints import MATH_TRACE_EVENT
    from backend.services.math_grading_context import ENGINE_EVENT, PLAN_EVENT, plans_from_snapshots

    seen, identity = set(), None
    while run_id and run_id not in seen:
        seen.add(run_id)
        frozen = workflow_repository.get_run_setup(run_id)
        if frozen is None:
            break
        current_identity = (frozen.owner_id, frozen.assignment_id, frozen.fingerprint)
        if identity is not None and current_identity != identity:
            break
        identity = current_identity
        with session_scope() as session:
            events = [{"message": row.message, "payload": row.payload} for row in session.scalars(
                select(GradingRunEventRecord).where(
                    GradingRunEventRecord.grading_run_id == run_id,
                    GradingRunEventRecord.message.in_([ENGINE_EVENT, PLAN_EVENT, MATH_TRACE_EVENT, "review_revision_created"]),
                ).order_by(GradingRunEventRecord.sequence))]
        events = [event for event in events if isinstance(event["payload"], dict)]
        final = {(event["payload"].get("student_id"), event["payload"].get("q_id")): event["payload"]
                 for event in events if event["message"] == MATH_TRACE_EVENT}
        if final:
            return plans_from_snapshots(frozen.input_manifest or {}, events), final
        sources = [event["payload"].get("source_run_id") for event in events
                   if event["message"] == "review_revision_created"]
        if len(sources) != 1 or not isinstance(sources[0], str):
            break
        run_id = sources[0]
    return {}, {}


def _review_comment(expert, trace, plan):
    """Keep saved review explanations visible when net-score rows are hidden."""
    from backend.skills.math_grading import TRACE_VERSION

    comment = expert.get("comment", "")
    if (not plan or not isinstance(trace, dict) or trace.get("version") != TRACE_VERSION
            or trace.get("plan_id") != plan.plan_id):
        return comment
    review = trace.get("targeted_review")
    reason = review.get("reason") if isinstance(review, dict) else None
    descriptions = "\n".join(step.get("desc", "") for step in expert.get("steps", []))
    labels = ("定向复核依据：", "Targeted review: ")
    if isinstance(reason, str) and reason.strip() and reason not in comment:
        label = next((label for label in labels if label + reason in descriptions), "")
        comment += "\n\n" + label + reason
    if trace.get("review_advisory"):
        advice_labels = ("工具核验与当前判断仍有待核对之处：", "Tool verification remains unresolved: ",
                         "证明依据核对建议：", "Proof evidence to check: ",
                         "解法路线仍需核对：", "Solution method still needs review: ")
        # Recover only the program's existing suffixes, including multiline
        # advice. Do not reinterpret the assessment or manufacture a score row.
        starts = "|".join(re.escape(label) for label in (*labels, *advice_labels))
        for step in expert.get("steps", []):
            for note in re.split(r"(?:^|\n)(?=" + starts + ")", step.get("desc", "")):
                if note.startswith(advice_labels) and note not in comment:
                    comment += "\n\n" + note
    return comment


def concise_result_feedback(results):
    """Project the selected runs' final traces after the caller checks ownership.

    Only copied presentation fields change. Original rows, teacher reviews, scores,
    private traces and already generated report files are never written here.
    """
    by_run = defaultdict(list)
    for result in results:
        if result.ai_expert_results:
            by_run[result.grading_run_id].append(result)
    projected = {}
    for run_id, rows in by_run.items():
        plans, final = _feedback_context(run_id)
        for row in rows:
            saved = final.get((row.student_id, row.q_id), {}).get("experts") or []
            if not isinstance(saved, list):
                continue
            rendered = []
            for expert in row.ai_expert_results:
                matches = [item for item in saved if isinstance(item, dict)
                           and item.get("provider_id") == expert.get("provider_id")
                           and item.get("provider") == expert.get("provider")]
                steps = expert.get("steps", [])
                comment = expert.get("comment", "")
                if len(matches) == 1 and not expert.get("error_kind"):
                    trace, plan = matches[0].get("trace", {}), plans.get(row.q_id)
                    steps = _concise_steps(steps, trace, plan)
                    comment = _review_comment(expert, trace, plan)
                rendered.append({**expert, "steps": steps, "comment": comment})
            if rendered == row.ai_expert_results:
                continue
            projected[row.id] = row.model_copy(update={
                "ai_expert_results": rendered,
                "ai_steps": _combined_steps(row.ai_steps, row.ai_expert_results, rendered, row.ai_max_score),
            })
    return [projected.get(row.id, row) for row in results]
