"""Saved feedback choices reach grading prompts; scoring stays rubric-bound."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.db import workflow_repository
from backend.models import TaskGradingSetup
from backend.skills import math_grading
from backend.skills.base import build_system_prompt, format_deterministic_feedback
from backend.skills.calculation import CalculationSkill
from backend.skills.concept import ConceptSkill
from backend.skills.proof import ProofSkill
from backend.services.math_grading_context import math_plans_context
from backend.tests.test_grading_source_readiness import _seed_legacy_structured_case
from backend.tests.test_task_stage_contract import _client
from backend.tests.test_math_grading import problem, answer, plan_for, assessment, response

TONES = {"encouraging": "supportive and encouraging", "neutral": "neutral and professional", "strict": "direct and rigorous"}
LENGTHS = {"short": "1-2 sentences", "medium": "3-5 sentences", "long": "detailed"}


def setup_for(tone="neutral", length="medium", language="zh", suggestions=True):
    return TaskGradingSetup(selected_provider_ids=["mock:math"], primary_provider_id="mock:math",
        feedback_tone=tone, feedback_length=length, feedback_language=language, suggest_corrections=suggestions)


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("tone", TONES)
@pytest.mark.parametrize("length", LENGTHS)
def test_feedback_preferences_save_reload_and_freeze(language, tone, length):
    seed = _seed_legacy_structured_case()
    owner, task = seed["owner_id"], seed["assignment_id"]
    client = _client(owner)
    current = client.get(f"/tasks/{task}/grading-setup").json()
    config = {**current["grading_setup"], "feedback_tone": tone, "feedback_length": length,
              "feedback_language": language, "suggest_corrections": False}
    saved = client.put(f"/tasks/{task}/grading-setup", json={
        "expected_workflow_revision": current["workflow_revision"], "grading_setup": config})
    assert saved.status_code == 200, saved.text
    assert saved.json()["grading_setup"] == config
    reread = client.get(f"/tasks/{task}/grading-setup").json()["grading_setup"]
    assert reread == config
    from backend.services.task_facade import start_task_grading
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    started = start_task_grading(task_id=task, owner_id=owner, expected_workflow_revision=workflow.workflow_revision)
    frozen = workflow_repository.get_run_setup(started["job_id"])
    assert frozen.setup == config
    runtime = TaskGradingSetup.model_validate(frozen.setup)
    prompt = build_system_prompt("Grade the answer.", "en" if language == "zh" else "zh", runtime)
    assert TONES[tone] in prompt and LENGTHS[length] in prompt
    assert ("in English" if language == "en" else "in Chinese") in prompt
    assert "Do not add correction suggestions" in prompt
    assert "Teacher notes:" not in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("skill", [CalculationSkill, ConceptSkill, ProofSkill])
@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("tone,length", [("encouraging", "short"), ("neutral", "medium"), ("strict", "long")])
async def test_each_math_skill_uses_feedback_choices_without_changing_score(monkeypatch, skill, language, tone, length):
    call = AsyncMock(return_value=(assessment(), response()))
    monkeypatch.setattr(math_grading, "structured_llm_call", call)
    monkeypatch.setattr(math_grading.kb_tool, "retrieve_for_grading", AsyncMock(return_value=[]))
    setup = setup_for(tone, length, language)
    plan = plan_for()
    with math_plans_context({"q1": plan}):
        result = await skill(SimpleNamespace(provider_id="mock:math"), grading_setup=setup).grade(problem(), answer())
    assert not result.error_kind and result.score == 8
    assert result.comment == assessment().comment
    assert call.await_count == 2  # Initial assessment and its evidence review.
    for request in call.call_args_list:
        prompt = request.kwargs["system_prompt"]
        assert TONES[tone] in prompt and LENGTHS[length] in prompt
        assert ("in English" if language == "en" else "in Chinese") in prompt
    assert plan == plan_for()


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("tone", TONES)
@pytest.mark.parametrize("length", LENGTHS)
def test_deterministic_feedback_uses_same_policy(language, tone, length):
    config = setup_for(tone, length, language)
    text = format_deterministic_feedback(config, zh_message="中文反馈", en_message="English feedback",
        zh_detail="中文详情", en_detail="English details", zh_suggestion="中文建议", en_suggestion="English suggestion")
    assert ("中文反馈" if language == "zh" else "English feedback") in text
    assert ("中文详情" if language == "zh" else "English details") in text if length != "short" else ("详情" not in text and "details" not in text)
    assert ("中文建议" if language == "zh" else "English suggestion") in text
    if tone == "encouraging":
        assert text.startswith("可以补救：" if language == "zh" else "This can be resolved:")
    if tone == "strict":
        assert text.startswith("需要处理：" if language == "zh" else "Action required:")


def test_disabled_model_keeps_saved_feedback_settings_visible(monkeypatch):
    from fastapi import HTTPException
    from backend.api import tasks
    seed = _seed_legacy_structured_case()
    owner, task = seed["owner_id"], seed["assignment_id"]
    client = _client(owner)
    original = client.get(f"/tasks/{task}/grading-setup").json()["grading_setup"]
    def unavailable(*_):
        raise HTTPException(422, detail={"code": "provider_not_enabled"})
    monkeypatch.setattr(tasks, "_validate_grading_setup", unavailable)
    result = client.get(f"/tasks/{task}/grading-setup")
    assert result.status_code == 200
    assert result.json()["grading_setup"] == original
    assert "invalid_grading_setup" in result.json()["readiness"]["blocking_issues"]
