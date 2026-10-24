"""Recognition, edits and grading must agree on the selected question type."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from backend.agents import ingest_agent
from backend.db.models import AssignmentQuestionRecord
from backend.db.session import session_scope
from backend.domain.errors import DomainError
from backend.models import ProblemInfo
from backend.services import task_facade
from backend.services.math_grading_context import uses_math_grading
from backend.skills.base import get_skill_for_type
from backend.skills.calculation import CalculationSkill
from backend.skills.concept import ConceptSkill
from backend.skills.programming import ProgrammingSkill
from backend.skills.proof import ProofSkill
from backend.tests.test_task_background_workflows import _seed_task


def test_preparation_uses_actual_selected_skill_without_another_type_rule(monkeypatch):
    from backend.skills import base
    monkeypatch.setitem(base._SKILL_REGISTRY, "计算题", ProgrammingSkill)
    assert get_skill_for_type("calculation") is ProgrammingSkill
    assert uses_math_grading("calculation") is False


@pytest.mark.parametrize("english,chinese,skill", [
    ("calculation", "计算题", CalculationSkill), ("programming", "编程题", ProgrammingSkill),
    ("proof", "推理题", ProofSkill), ("reasoning", "推理题", ProofSkill),
    ("concept", "概念题", ConceptSkill), ("other", "其他", ConceptSkill),
])
def test_teacher_type_edit_reaches_same_skill_as_recognition(english, chinese, skill):
    owner, task = _seed_task(with_question=True)
    task_facade.update_problem(task_id=task, owner_id=owner, q_id="q1", patch={"type": english})
    row = task_facade.get_task(task_id=task, owner_id=owner)["problem_data"]["q1"]
    assert row["type"] == chinese
    problem = ProblemInfo.model_validate(row)
    assert get_skill_for_type(problem.type) is get_skill_for_type(english) is skill
    assert uses_math_grading(english) == uses_math_grading(problem.type) == (skill not in {ProgrammingSkill})


@pytest.mark.parametrize("value", ["", "  ", "unrecognized", "choice", "multiple_choice", "fill_blank", "short_answer"])
def test_missing_type_preserves_stem_and_blocks_confirm_and_grading(value):
    owner, task = _seed_task(with_question=True)
    with session_scope() as session:
        row = session.scalar(select(AssignmentQuestionRecord).where(AssignmentQuestionRecord.assignment_id == task))
        row.type = value
        row.number = "1"
        row.criterion = "Award 10 points for a correct answer."
        stem = row.stem
    assert "question_review_blocked" in task_facade.grading_readiness(task_id=task, owner_id=owner)["blocking_issues"]
    with pytest.raises(DomainError, match="before confirming"):
        task_facade.update_problem(task_id=task, owner_id=owner, q_id="q1", patch={"review_status": "confirmed"})
    with pytest.raises(ValueError, match="question type"):
        get_skill_for_type(value)
    task_facade.update_problem(task_id=task, owner_id=owner, q_id="q1", patch={"type": "other"})
    task_facade.update_problem(task_id=task, owner_id=owner, q_id="q1", patch={"review_status": "confirmed"})
    fixed = task_facade.get_task(task_id=task, owner_id=owner)["problem_data"]["q1"]
    assert fixed["type"] == "其他" and fixed["stem"] == stem


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["计算题", None, "unrecognized"])
async def test_one_response_saves_stem_and_type_without_classifier(monkeypatch, kind):
    payload = dict(q_id="q1", number="1", stem="Calculate 2+2.", criterion="")
    if kind is not None:
        payload["type"] = kind
    call = AsyncMock(return_value=SimpleNamespace(content=json.dumps({"problems": [payload]})))
    monkeypatch.setattr(ingest_agent, "ainvoke_with_retry", call)
    result = await ingest_agent._extract_problems_call("1. Calculate 2+2.", SimpleNamespace(provider_id="test"), partial_source=True)
    call.assert_awaited_once()
    assert result["q1"]["stem"] == payload["stem"]
    assert result["q1"]["type"] == ("计算题" if kind == "计算题" else "")


@pytest.mark.asyncio
async def test_ocr_only_stem_does_not_invent_other():
    rows = await ingest_agent.extract_problems_from_ocr_markdown("# 1.\nCalculate 2+2.", {})
    assert rows["q1"]["stem"]
    assert rows["q1"]["type"] == ""
