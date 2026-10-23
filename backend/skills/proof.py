"""ProofSkill: specialization of the shared mathematical evidence pipeline."""
from backend.skills.base import register_skill
from backend.skills.math_grading import MathGradingSkill


@register_skill("证明题", "推理题")
class ProofSkill(MathGradingSkill):
    name = "ProofSkill"
    problem_type = "证明题"
    template_name = "proof"
