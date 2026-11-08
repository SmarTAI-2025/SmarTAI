"""Canonical provider replies for workflow tests that isolate model decisions."""
from decimal import Decimal
from backend.services.rubric_drafts import RubricDefinition, generated_rubric, retarget_draft


def definition_for(problem, *, budgets=None):
    maximum = Decimal(str(problem.get("max_score") or 10))
    structure = problem.get("question_structure") or {}
    subparts = structure.get("subparts") or [{"label": None, "stem": None}]
    remaining = maximum
    result = []
    known = problem.get("teacher_subpart_points") or {}
    for index, subpart in enumerate(subparts, 1):
        points = (Decimal(str(budgets[index - 1])) if budgets else Decimal(str(known[subpart["label"]]))
                  if subpart["label"] in known else (remaining / (len(subparts) - index + 1)).quantize(Decimal(".01")))
        remaining -= points
        result.append(dict(id=f"p{index}", label=subpart["label"], stem=subpart.get("stem"), total_pts=float(points),
                           steps=[dict(id="s1", criterion="Award credit for the stated goal.", total_pts=float(points))]))
    return dict(part_rubric=dict(id=problem.get("q_id") or "q1", label=str(problem.get("number") or ""),
                                 stem=structure.get("shared_stem") or problem.get("stem") or "",
                                 total_pts=float(maximum), parts=result))


def freeze_reply(problem, candidates):
    criterion = next((c for c in candidates if c.target == "criterion"), None)
    if criterion is None:
        return candidates
    row = {**problem, **{c.target: c.text_value for c in candidates}, "ai_completion_provenance": {
        **(problem.get("ai_completion_provenance") or {}), **{c.target: {"source_kind":"ai_generated"} for c in candidates}}}
    _, draft = generated_rubric(row, RubricDefinition.model_validate(definition_for(row)))
    criterion.rubric_draft = retarget_draft(draft,row).model_dump(mode="json")
    return candidates
