"""ConceptSkill: specialization of the shared mathematical evidence pipeline."""
from backend.skills.base import register_skill
from backend.skills.math_grading import MathGradingSkill


@register_skill("概念题", "其他", "其它")
class ConceptSkill(MathGradingSkill):
    name = "ConceptSkill"
    problem_type = "概念题"
    template_name = "concept"
