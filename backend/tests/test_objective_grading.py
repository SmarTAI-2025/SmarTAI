"""Response forms use the semantic grader; the retired objective route is closed."""
import pytest

from backend.skills.base import get_skill_for_type
from backend.skills.calculation import CalculationSkill
from backend.skills.concept import ConceptSkill
from backend.skills.proof import ProofSkill
from backend.skills.math_grading import MathAssessment, score_assessment
from backend.services.rubric_drafts import RubricDefinition, generated_rubric


@pytest.mark.parametrize("form", ["选择题", "多选题", "填空题", "choice", "multiple_choice", "fill_blank", "short"])
def test_response_form_cannot_dispatch_a_separate_grader(form):
    with pytest.raises(ValueError, match="question type"):
        get_skill_for_type(form)


@pytest.mark.parametrize("kind,skill", [("calculation", CalculationSkill), ("concept", ConceptSkill),
                                        ("reasoning", ProofSkill), ("other", ConceptSkill)])
def test_response_forms_share_the_selected_semantic_strategy(kind, skill):
    assert get_skill_for_type(kind) is skill


@pytest.mark.parametrize("kind", ["计算题", "概念题"])
def test_two_blanks_keep_the_correct_blank_credit(kind):
    row = dict(q_id="q1", number="1", type=kind, stem="分别填写两个空。", criterion="", max_score=4)
    definition = RubricDefinition(routes=[dict(id="blanks", description="逐空给分", applies_when="全部作答",
        mode="points", items=[dict(id=f"blank{i}", description=f"第{i}空", points=2, source="ai") for i in (1, 2)])],
        follow_through="两个空独立给分。", numerical_policy="按题干要求判断。")
    _, draft = generated_rubric(row, definition)
    answer = "第一空正确，第二空错误"
    assessment = MathAssessment(route_id="blanks", route_reason="两个独立空位", confidence=.9, comment="第一空正确",
        claims=[dict(id=f"s{i}", quote=quote, item_ids=[f"blank{i}"], verdict=verdict, reason=reason)
            for i, quote, verdict, reason in [(1,"第一空正确","supported","第一空符合标答"),
                                              (2,"第二空错误","refuted","第二空不符合标答")]],
        awards=[dict(item_id=f"blank{i}", score=score, claim_ids=[f"s{i}"], reason="按各空给分")
                for i, score in [(1,2),(2,0)]])
    score, steps = score_assessment(assessment, draft.plan, answer)
    assert score == 2 and sum(step.score for step in steps) == 2
