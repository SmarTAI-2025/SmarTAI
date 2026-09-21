from __future__ import annotations

import json
import time
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agents import question_preparation_agent as agent
from backend.agents.ingest_agent import AICompletionCandidateOutput
from backend.domain.errors import ValidationError
from backend.models import ProblemSourceDraft, QuestionScorePolicy
from backend.progress.tracker import ProgressReporter
from backend.services.question_structure import annotate_major_question_structures
from backend.skills.question_score import InterpretedQuestionScore, InterpretedQuestionScorePlan


def questions():
    return annotate_major_question_structures({"q1": {
        "q_id": "q1", "number": "1", "type": "计算题",
        "stem": "(a) Calculate x.\n(b) Calculate y.", "criterion": "method 100%", "max_score": 20,
    }})


def draft():
    return ProblemSourceDraft(
        source_token="test-source", task_id="test-task", owner_id="test-owner",
        role="problem", source_kind="upload", filename="synthetic.txt", structure_mode="organized",
        size_bytes=100, content_sha256="a" * 64, expires_at=time.time() + 3600,
    )


async def prepare(monkeypatch, *, note, rubric, recovered=False):
    rows = questions()
    async def extract(text, provider, store, **kwargs):
        store.update(deepcopy(rows))
        return store
    async def score(*args, **kwargs):
        return InterpretedQuestionScorePlan(scores=[InterpretedQuestionScore(q_id="q1", max_score=20)]), None
    seen = []
    async def generate(**kwargs):
        seen.append(kwargs["problems_data"])
        return [AICompletionCandidateOutput(
            **target, text_value=rubric if target["target"] == "criterion" else "(a) x=1\n(b) y=2",
        ) for target in kwargs["requested_targets"]]
    monkeypatch.setattr(agent, "extract_problems", extract)
    monkeypatch.setattr("backend.skills.question_score.structured_llm_call", score)
    monkeypatch.setattr(agent, "generate_missing_question_materials", generate)
    result = await agent.prepare_question_packages(
        [(draft(), "1. (a) Calculate x. (b) Calculate y.")],
        SimpleNamespace(config=SimpleNamespace(max_concurrent=2)),
        provider_id="fake", reporter=ProgressReporter("teacher-constraints"),
        score_policy=QuestionScorePolicy(mode="per_question", per_question_text=note),
        recovered_base_problem_data=rows if recovered else None,
    )
    return result, seen


@pytest.mark.asyncio
@pytest.mark.parametrize("recovered", [False, True])
async def test_explicit_teacher_8_12_rejects_generated_10_10(monkeypatch, recovered):
    with pytest.raises(ValidationError) as error:
        await prepare(monkeypatch, note="第1大题20分，其中(a)8分，(b)12分", rubric="(a) 10分\n(b) 10分", recovered=recovered)
    assert error.value.code == "question_structure_score_mismatch"


@pytest.mark.asyncio
async def test_teacher_8_12_reaches_generator_but_is_not_a_new_stored_field(monkeypatch):
    result, calls = await prepare(monkeypatch, note="第1大题20分，其中(a)8分，(b)12分", rubric="(a) 8分\n(b) 12分")
    assert calls[0]["q1"]["teacher_subpart_points"] == {"(a)": "8", "(b)": "12"}
    assert list(result) == ["q1"]
    assert "teacher_subpart_points" not in result["q1"]
    assert result["q1"]["max_score"] == 20


@pytest.mark.asyncio
async def test_partial_teacher_allocation_allows_unspecified_remainder(monkeypatch):
    result, _ = await prepare(monkeypatch, note="第1题20分，其中(a)8分", rubric="(a) 8分\n(b) 12分")
    assert result["q1"]["criterion"] == "(a) 8分\n(b) 12分"


@pytest.mark.asyncio
async def test_recovered_final_package_cannot_bypass_teacher_constraints(monkeypatch):
    from backend.api import task_preparation
    packages = questions()
    packages["q1"]["criterion"] = "(a) 10分\n(b) 10分"
    failures, writes = [], []
    monkeypatch.setattr(task_preparation.task_facade, "_fail_operation", lambda *a, **k: failures.append((a, k)))
    monkeypatch.setattr(task_preparation.task_facade, "_replace_draft_questions", lambda *a, **k: writes.append((a, k)))
    await task_preparation._run_question_preparation(
        task_id="test-task", owner_id="test-owner", job_id="test-final", job_attempt=1,
        sources=[(draft(), "source", {})], provider=None, claimed_workflow_revision=1,
        replace_confirmed=False, score_policy=QuestionScorePolicy(mode="per_question", per_question_text="第1大题20分，其中(a)8分，(b)12分"),
        recognition_provider_id="fake", prebuilt_packages=packages,
    )
    assert writes == []
    assert failures[0][0][4] == "question_structure_score_mismatch"


@pytest.mark.parametrize("note", [
    "第1大题20分，其中(a)8分，(b)12分",
    "第一题共20分，其中（a）8，（b）12",
    "Question 1: 20 points; (a) 8 points, (b) 12 points",
    "Q1 20 pts, (a) 8 pts, (b) 12 pts",
    "1: 20分；(a)8分；(b)12分",
])
def test_common_explicit_teacher_formats(note):
    from backend.services.teacher_score_constraints import explicit_teacher_scores
    from decimal import Decimal
    outline = explicit_teacher_scores(note)
    assert outline["1"].maximum == Decimal("20")
    assert outline["1"].subparts == {"(a)": Decimal("8"), "(b)": Decimal("12")}


@pytest.mark.asyncio
async def test_explicit_major_total_wins_over_incorrect_model_mapping(monkeypatch):
    from backend.skills.question_score import resolve_question_score_policy
    call = AsyncMock(return_value=(InterpretedQuestionScorePlan(scores=[
        InterpretedQuestionScore(q_id="q1", max_score=10),
    ]), None))
    monkeypatch.setattr("backend.skills.question_score.structured_llm_call", call)
    result = await resolve_question_score_policy(questions(), QuestionScorePolicy(
        mode="per_question", per_question_text="第1大题20分，其中(a)8分，(b)12分",
    ), object())
    assert result["q1"].max_score == 20
    assert call.await_count == 1


@pytest.mark.asyncio
async def test_actual_generation_payload_keeps_exact_points(monkeypatch):
    from backend.agents import ingest_agent
    problem = questions()["q1"]
    problem["teacher_subpart_points"] = {"(a)": "8", "(b)": "12"}
    call = AsyncMock(return_value=SimpleNamespace(content=json.dumps({"candidates": [{
        "target_id": "q1:criterion", "q_id": "q1", "target": "criterion", "text_value": "(a) 8分\n(b) 12分",
    }]})))
    monkeypatch.setattr(ingest_agent, "ainvoke_with_retry", call)
    await ingest_agent.generate_missing_question_materials(
        {"q1": problem}, [{"target_id": "q1:criterion", "q_id": "q1", "target": "criterion"}], 6, object(),
    )
    messages = call.call_args.args[1]
    payload = json.loads(messages[1].content.split("\n", 1)[1])
    assert payload["known_problems"][0]["teacher_subpart_points"] == {"(a)": "8", "(b)": "12"}
    assert len(payload["known_problems"]) == 1
    assert call.await_count == 1


@pytest.mark.asyncio
async def test_recovered_question_candidates_are_checked_before_reuse():
    problem = questions()["q1"]
    problem["teacher_subpart_points"] = {"(a)": "8", "(b)": "12"}
    target = {"target_id": "q1:criterion", "q_id": "q1", "target": "criterion"}
    with pytest.raises(ValidationError) as error:
        await agent.generate_major_question_materials(
            problems_data={"q1": problem}, requested_targets=[target], test_case_count=6,
            provider=SimpleNamespace(config=SimpleNamespace(max_concurrent=2)),
            reporter=ProgressReporter("recovered-teacher-points"),
            recovered_candidates_by_question={"q1": [AICompletionCandidateOutput(**target, text_value="(a) 10分\n(b) 10分")]},
            completed_question_ids=["q1"],
        )
    assert error.value.code == "question_structure_score_mismatch"
