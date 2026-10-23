"""Calculation grading through the shared evidence and rubric pipeline."""
from backend.skills.base import register_skill
from backend.skills.math_grading import MathGradingSkill


@register_skill("计算题")
class CalculationSkill(MathGradingSkill):
    name = "CalculationSkill"
    problem_type = "计算题"
    template_name = "calc"
