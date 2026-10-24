"""Reasoning (including proof) through the shared evidence pipeline."""
from backend.skills.base import register_skill
from backend.skills.math_grading import MathGradingSkill


@register_skill("推理题")
class ProofSkill(MathGradingSkill):
    name = "ProofSkill"
    problem_type = "推理题"
    template_name = "proof"
