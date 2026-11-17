"""Final math feedback projection; scores and private history remain immutable."""
from collections import defaultdict
import re


def final_math_feedback(comment, steps, trace, language):
    """Show the adopted vote, keeping review history in the private trace."""
    from backend.skills.math_grading import TRACE_VERSION
    if not trace or trace.get("version") != TRACE_VERSION or not isinstance(trace.get("assessment"), dict):
        return comment, steps
    assessment = trace["assessment"]
    if trace.get("method_review") or trace.get("targeted_review"):
        comment = assessment.get("comment", comment)
    reason = trace.get("targeted_review", {}).get("reason")
    if reason:
        annotations = ["\n定向复核依据：" + reason, "\nTargeted review: " + reason]
        steps = [{**step, "desc": re.sub("(?:" + "|".join(re.escape(text) for text in annotations)
                                         + r")(?=\n|$)", "", step["desc"])} for step in steps]
    reminders = []
    if trace.get("review_advisory"):
        # Net-score rows may be hidden after their deductions are restored.
        # Retain the existing mathematical reminders outside those rows.
        labels = ("工具核验与当前判断仍有待核对之处：", "Tool verification remains unresolved: ",
                  "证明依据核对建议：", "Proof evidence to check: ")
        starts = "|".join(re.escape(label) for label in labels)
        for step in steps:
            for note in re.split(r"(?:^|\n)(?=" + starts + ")", step.get("desc", "")):
                if note.startswith(labels) and note not in reminders:
                    reminders.append(note)
    decisions = assessment.get("part_decisions") or []
    alternative = [part for part in decisions if part.get("method") == "alternative"
                   and part.get("relevance") in {"partial", "complete", "incorrect"}]
    notes = {
        "zh": ("解题思路与标答不同，按另解标准评分。", "部分小问解题思路与标答不同，按另解标准评分。"),
        "en": ("Different approach from the reference; scored using the alternative rubric.",
               "Some parts use a different approach and are scored using the alternative rubric."),
    }
    # Replace only our own current-method note, never mathematical feedback.
    for messages in notes.values():
        for message in messages:
            comment = comment.removesuffix("\n\n" + message)
    for note in reminders:
        if note not in comment:
            comment += "\n\n" + note
    if alternative:
        note = notes["zh" if language == "zh" else "en"][len(alternative) != len(decisions)]
        comment = (comment + "\n\n" + note).strip()
    return comment, steps


def final_result_feedback(results, language="en"):
    """Clean final feedback after the existing read-time detail restoration."""
    from backend.services.grading_feedback import concise_result_feedback, _feedback_context, _combined_steps

    originals = {row.id: row for row in results}
    results = concise_result_feedback(results)
    by_run = defaultdict(list)
    for row in results:
        if row.ai_expert_results:
            by_run[row.grading_run_id].append(row)
    projected = {}
    for run_id, rows in by_run.items():
        plans, final = _feedback_context(run_id)
        for row in rows:
            saved = final.get((row.student_id, row.q_id), {}).get("experts") or []
            plan = plans.get(row.q_id)
            if not isinstance(saved, list) or not plan:
                continue
            rendered = []
            for expert in row.ai_expert_results:
                matches = [item for item in saved if isinstance(item, dict)
                           and item.get("provider_id") == expert.get("provider_id")
                           and item.get("provider") == expert.get("provider")]
                comment, steps = expert.get("comment", ""), expert.get("steps", [])
                if len(matches) == 1 and not expert.get("error_kind"):
                    trace = matches[0].get("trace", {})
                    if isinstance(trace, dict) and trace.get("plan_id") == plan.plan_id:
                        comment, steps = final_math_feedback(comment, steps, trace, language)
                rendered.append({**expert, "comment": comment, "steps": steps})
            if rendered != row.ai_expert_results:
                projected[row.id] = row.model_copy(update={
                    "ai_expert_results": rendered,
                    "ai_comment": _combined_comment(row.ai_comment, originals[row.id].ai_expert_results, rendered),
                    "ai_steps": _combined_steps(row.ai_steps, row.ai_expert_results, rendered, row.ai_max_score),
                })
    return [projected.get(row.id, row) for row in results]


def _combined_comment(comment, original, rendered):
    """Keep existing summaries, replacing only their exact embedded vote text."""
    if len(original) == 1 and comment == original[0].get("comment"):
        return rendered[0]["comment"]
    def combined(experts):
        try:
            return "\n\n———\n\n".join(
                f"【{expert['provider']}（{expert['score']:.1f}/{expert['max_score']:.0f}, conf="
                + (f"{expert['confidence']:.2f}" if expert.get("confidence") is not None else "unknown")
                + f"）】\n{expert['comment']}" for expert in experts)
        except (KeyError, TypeError, ValueError):
            return None
    if original and comment == combined(original):
        return combined(rendered) or comment
    return comment
