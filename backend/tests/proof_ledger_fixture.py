"""Explicit frozen tiers for settlement/review tests, not a rubric generator.

Deduction ledgers remain supported for teacher-specified rules. Fresh AI proof
generation uses concrete steps; these fixtures exercise the unchanged scorer.
"""
from backend.services.part_rubric import PartRubric


def goal_ledger(rules, goals):
    deductions = [dict(id=f"goal{i}.{level}", criterion=f"{target}: {level}",
                       deduct_pts=loss, limit=1, exclusive_group=f"goal{i}")
                  for i, (target, budget) in enumerate(goals, 1)
                  for level, loss in (("absent", budget), ("partial", budget / 2),
                                      ("local", min(rules.base_unit_pts, budget / 4)))]
    part = {**rules.parts[0].model_dump(exclude={"steps", "deductions"}), "deductions": deductions}
    return PartRubric.model_validate({**rules.model_dump(exclude={"parts"}), "parts": [part]})
