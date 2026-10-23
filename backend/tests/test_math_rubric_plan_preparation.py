"""Narrow preparation regression: generated standards, never a teacher-edit gate."""
import pytest
from unittest.mock import AsyncMock

from backend.agents.ingest_agent import AICompletionCandidateOutput
from backend.agents.question_preparation_agent import (
    _validate_major_question_candidates, generate_major_question_materials, requested_major_question_materials,
)
from backend.domain.errors import ValidationError
from backend.services.question_structure import (
    build_major_question_structure, validate_generated_math_rubric_percentages,
)
from backend.tests.test_math_rubric_plan import ReplayProvider


def problem(criterion="", type="计算题"):
    p = {
        "q_id": "q1", "number": "1", "stem": "计算 $2+3$ 并写出过程。",
        "criterion": criterion, "reference_answer": "$2+3=5$", "max_score": 10, "type": type,
        "material_provenance": {"criterion": {"source_kind": "uploaded_rubric"}},
    }
    p["question_structure"] = build_major_question_structure(p, major_order=0).model_dump(mode="json")
    return p


TARGETS = [{"target_id": "q1:criterion", "q_id": "q1", "target": "criterion"}]


def candidates(text):
    return [AICompletionCandidateOutput(**TARGETS[0], text_value=text)]


@pytest.mark.parametrize("text", ["方法60%，结果60%", "推理30%，结论60%", "method 100.1%", "方法-100%"])
def test_generated_math_percentages_rejected_by_existing_provider_validation(text):
    with pytest.raises(ValidationError) as raised:
        _validate_major_question_candidates("q1", TARGETS, candidates(text), problem())
    assert raised.value.code == "provider_response_invalid"


@pytest.mark.parametrize("text", [
    "方法40%，结果60%", "方法33.3%，推导33.3%，结论33.4%",
    "每错扣10%，非空最低60%", "方法一占100%，或者采用等价方法占100%",
    "结果50%，方法50%，精度要求误差不超过1%",
    "方法40%，结果60%，总计100%",
    "方法60%(其中建模30%、推导30%)，结果40%",
])
def test_valid_or_conditional_rubrics_are_not_flattened(text):
    assert _validate_major_question_candidates("q1", TARGETS, candidates(text), problem())[0].text_value == text


@pytest.mark.parametrize("type", ["编程题", "选择题", "多选题", "填空题"])
def test_other_question_algorithms_unchanged(type):
    validate_generated_math_rubric_percentages("旧规则60%", question_type=type, has_subparts=False)


@pytest.mark.parametrize("criterion", ["酌情给分", "方法60%，结果60%"])
def test_existing_teacher_text_is_not_overwritten_or_newly_blocked(criterion):
    p = problem(criterion)
    assert requested_major_question_materials({"q1": p}) == []
    assert p["criterion"] == criterion
    # It is interpreted later by prepare_math_rubric_plan, for the entire class.


@pytest.mark.asyncio
async def test_generated_invalid_percentages_get_existing_bounded_format_repair():
    first = {"candidates": [c.model_dump() for c in candidates("方法60%，结果60%")]}
    corrected = {"candidates": [c.model_dump() for c in candidates("方法40%，结果60%")]}
    provider = ReplayProvider(first, corrected)
    p = problem()
    result = await generate_major_question_materials(
        problems_data={"q1": p}, requested_targets=TARGETS, test_case_count=1,
        provider=provider, reporter=AsyncMock(), generation_language="en",
    )
    assert result[0].text_value == "方法40%，结果60%"
    assert len(provider.messages) == 2
    prompt = provider.messages[0][0].content
    assert "valid alternative methods" in prompt
    assert "first error" in prompt
    assert "in English" in prompt
