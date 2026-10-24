"""Fixed responses exercise preparation wiring, not real-model accuracy."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agents import ingest_agent


POLICIES = (
    "每步 1 分",
    "不计过程分",
    "只看最终答案",
    "选项 2 分、解释 2 分",
    "请写出解题过程",  # A request for working alone does not allocate points.
)


def _problem(policy: str, *, criterion: str = "") -> dict:
    return {
        "q_id": "q1", "number": "1", "type": "选择题",
        "stem": f"选择正确选项。教师要求：{policy}",
        "criterion": criterion, "max_score": 4,
    }


def _assert_policy_contract(system: str) -> None:
    # Both preparation entry points must distinguish permission from its negation
    # and preserve allocations which do not contain the old process keywords.
    assert "explicitly allows process" in system
    assert "explicitly forbids it" in system
    assert "unspecified" in system
    assert '"每步1分"' in system and '"选项2分、解释2分"' in system
    assert '"不计过程分"' in system and '"只看最终答案"' in system
    assert "alone is not permission" in system


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", POLICIES)
async def test_material_import_keeps_source_policy_and_candidate_verbatim(monkeypatch, policy):
    source = f"第1题评分标准：{policy}。"
    call = AsyncMock(return_value=SimpleNamespace(content=json.dumps({"candidates": [{
        "q_id": "q1", "target": "criterion", "text_value": policy,
        "source_excerpt": source, "match_status": "exact", "confidence": 1,
    }]}, ensure_ascii=False)))
    monkeypatch.setattr(ingest_agent, "ainvoke_with_retry", call)

    candidates = await ingest_agent.parse_material_import_to_candidates(
        source, {"q1": _problem(policy)}, ["criterion"], "organized", "", object(),
    )

    call.assert_awaited_once()
    messages = call.await_args.args[1]
    _assert_policy_contract(messages[0].content)
    assert f"[Teacher material begins]\n{source}\n[Teacher material ends]" in messages[1].content
    assert candidates[0].text_value == policy
    assert candidates[0].source_excerpt == source


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", POLICIES)
async def test_generated_rubric_receives_exact_teacher_policy_once(monkeypatch, policy):
    problem = _problem(policy)
    # The fixed model keeps explicit allocations/prohibitions. For a request to
    # show working without an allocation, its default rubric remains answer-only.
    generated = "答案唯一：答对满分，答错0分。" if policy == POLICIES[-1] else policy
    call = AsyncMock(return_value=SimpleNamespace(content=json.dumps({"candidates": [{
        "target_id": "q1:criterion", "q_id": "q1", "target": "criterion",
        "text_value": generated,
    }]}, ensure_ascii=False)))
    monkeypatch.setattr(ingest_agent, "ainvoke_with_retry", call)

    candidates = await ingest_agent.generate_missing_question_materials(
        {"q1": problem}, [{"target_id": "q1:criterion", "q_id": "q1", "target": "criterion"}],
        6, object(),
    )

    call.assert_awaited_once()
    messages = call.await_args.args[1]
    _assert_policy_contract(messages[0].content)
    context = json.loads(messages[1].content.split("\n", 1)[1])
    assert context["known_problems"][0]["stem"] == problem["stem"]
    assert context["known_problems"][0]["existing_criterion"] == ""
    assert candidates[0].text_value == generated
    assert problem["criterion"] == ""  # Generation does not overwrite the source.


@pytest.mark.asyncio
async def test_reference_completion_preserves_existing_teacher_criterion(monkeypatch):
    criterion = "不计过程分，只看最终答案。答案错误就是0分。"
    problem = _problem("选择正确选项", criterion=criterion)
    call = AsyncMock(return_value=SimpleNamespace(content=json.dumps({"candidates": [{
        "target_id": "q1:reference_answer", "q_id": "q1", "target": "reference_answer",
        "text_value": "A",
    }]})))
    monkeypatch.setattr(ingest_agent, "ainvoke_with_retry", call)

    candidates = await ingest_agent.generate_missing_question_materials(
        {"q1": problem}, [{"target_id": "q1:reference_answer", "q_id": "q1", "target": "reference_answer"}],
        6, object(),
    )

    call.assert_awaited_once()
    context = json.loads(call.await_args.args[1][1].content.split("\n", 1)[1])
    assert context["known_problems"][0]["existing_criterion"] == criterion
    assert problem["criterion"] == criterion
    assert [(candidate.target, candidate.text_value) for candidate in candidates] == [("reference_answer", "A")]
