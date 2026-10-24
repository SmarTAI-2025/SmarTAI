"""Result-credit parity across the standard and policy-resolution paths."""
from unittest.mock import AsyncMock

import pytest

from backend.models import StudentAnswerInfo
from backend.services.process_credit_policy import process_credit_policy
from backend.skills import objective
from backend.skills.objective import ObjectiveSkill
from backend.tests.test_process_credit_policy_acceptance import RawReplayProvider, question
from backend.tests.test_process_credit_policy_scope import resolved_response


@pytest.fixture(autouse=True)
def no_external_knowledge(monkeypatch):
    monkeypatch.setattr(objective.kb_tool, "retrieve_for_grading", AsyncMock(return_value=[]))


@pytest.mark.asyncio
async def test_multi_choice_subset_result_marks_survive_a_process_prohibition():
    criterion = "全部选对4分，部分选对2分，错选0分；解题过程不影响评分。"
    p = question(criterion, "多选题")
    p.stem = "选择所有正确选项。A. 1+1=2；B. 2+2=4；C. 1+1=3。"
    p.reference_answer = "AB"
    payload = resolved_response(criterion, process_credit="forbidden", result_score=2, process_score=0)
    payload.update(student_answer="A", reference_answer="AB", partial_credit_applicable=True)
    provider = RawReplayProvider(payload)
    result = await ObjectiveSkill(provider).grade(
        p, StudentAnswerInfo(q_id="q1", number="1", type="多选题", content="A"))
    assert not result.error_kind
    assert result.score == 2
    assert result.steps == []
    assert p.criterion == criterion and criterion in provider.messages[0][1].content
    assert len(provider.messages) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction", ["不计过程分。", "解题过程不影响评分。"])
@pytest.mark.parametrize("selected,correct,partial,result_score,expected", [
    ("AB", True, False, 4, 4),
    ("A", False, True, 2, 2),
    ("AC", False, False, 0, 0),
    ("AC", False, False, 2, 0),  # Fixed contradictory score probes the gate.
    ("", False, False, 2, 0),
    ("A", False, None, 2, 0),  # Missing permission must retain the existing default.
])
async def test_standard_and_resolved_paths_share_multi_choice_result_eligibility(
    restriction, selected, correct, partial, result_score, expected,
):
    criterion = "全部选对4分，部分选对2分，错选0分；" + restriction
    assert process_credit_policy(criterion).unresolved == (restriction == "解题过程不影响评分。")
    payload = resolved_response(criterion, correct=correct, process_credit="forbidden",
                                result_score=result_score, process_score=0)
    payload.update(student_answer=selected, reference_answer="AB")
    if partial is None:
        payload.pop("partial_credit_applicable")
    else:
        payload["partial_credit_applicable"] = partial
    provider = RawReplayProvider(payload)
    p = question(criterion, "多选题")
    p.reference_answer = "AB"
    result = await ObjectiveSkill(provider).grade(
        p, StudentAnswerInfo(q_id="q1", number="1", type="多选题", content=selected))
    assert not result.error_kind
    assert result.score == expected and result.steps == []
    assert len(provider.messages) == (1 if selected else 0)
    if selected:
        assert "partial_credit_applicable" in provider.messages[0][0].content


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,expected", [("选择题", 0), ("填空题", 0), ("多选题", 2)])
async def test_subset_flag_is_scoped_to_multi_choice_and_cannot_authorize_working(kind, expected):
    criterion = "全部选对4分，部分选对2分，错选0分；解题过程不影响评分。"
    payload = resolved_response(criterion, process_credit="forbidden", result_score=2, process_score=1)
    payload.update(student_answer="A", reference_answer="AB", partial_credit_applicable=True)
    provider = RawReplayProvider(payload)
    result = await ObjectiveSkill(provider).grade(
        question(criterion, kind), StudentAnswerInfo(q_id="q1", number="1", type=kind, content="A"))
    assert not result.error_kind
    assert result.score == expected
    assert len(provider.messages) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction", ["不计过程分。", "解题过程不影响评分。"])
async def test_explicit_whole_answer_zero_still_blocks_a_model_subset_flag(restriction):
    criterion = "答案错误就是0分；" + restriction
    payload = resolved_response(criterion, process_credit="forbidden", result_score=2, process_score=0)
    payload.update(student_answer="A", reference_answer="AB", partial_credit_applicable=True)
    provider = RawReplayProvider(payload)
    result = await ObjectiveSkill(provider).grade(
        question(criterion, "多选题"), StudentAnswerInfo(q_id="q1", number="1", type="多选题", content="A"))
    assert not result.error_kind
    assert result.score == 0 and len(provider.messages) == 1


@pytest.mark.asyncio
async def test_unmet_working_condition_does_not_block_subset_result_marks():
    criterion = "全部选对4分，部分选对2分，错选0分；只有答案正确才给过程2分；解题过程酌情计分。"
    payload = resolved_response(criterion, process_credit="allowed", correct_answer_required=True,
                                other_conditions_met=False, result_score=2, process_score=1)
    payload.update(student_answer="A", reference_answer="AB", partial_credit_applicable=True)
    provider = RawReplayProvider(payload)
    result = await ObjectiveSkill(provider).grade(
        question(criterion, "多选题"), StudentAnswerInfo(q_id="q1", number="1", type="多选题", content="A"))
    assert not result.error_kind
    assert result.score == 2 and len(provider.messages) == 1
