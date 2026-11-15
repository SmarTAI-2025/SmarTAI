"""Question preparation, persisted retry and scoring on the same bounded rubric."""
import json
from unittest.mock import AsyncMock

import pytest

from backend.agents import incremental_question_preparation as inc
from backend.domain.errors import ValidationError
from backend.services.question_structure import summarize_rubric_points
from backend.services.rubric_drafts import RubricDefinition, generated_rubric
from backend.skills import math_grading
from backend.tests.test_incremental_question_preparation import persisted, row, setup
from backend.tests.test_question_material_format_repair import Provider


def problem(count=5):
    return inc._rows([row("1.12", stem="Judge each statement.\n" +
                          "\n".join(f"({i}) Statement {i}." for i in range(1, count + 1)),
                          reference_answer=" ".join(f"({i}) True." for i in range(1, count + 1)))])["q1"]


def item(i, points, *, label=None, suffix=""):
    return {"id": f"subpart_{i}{suffix}", "description":
            f"{label or f'({i})'} 判断正确得 {points} 分，部分正确得 1 分，否则 0 分。{suffix}",
            "points": points, "source": "ai"}


def definition(items):
    return {"routes": [{"id": "main", "description": "每项独立评分，总分 10 分。",
                        "applies_when": "always", "mode": "points", "items": items}],
            "follow_through": "后续错误不重复扣分。", "numerical_policy": "按各项实际得分汇总。"}


def reply(items):
    parts = [{"id": entry["id"], "label": f"({i})", "stem": f"Statement {i}.", "total_pts": entry["points"],
              "alternative": {"deduct_per_error":1}, "steps": [{"id": "step", "criterion": entry["description"], "total_pts": entry["points"]}]}
             for i, entry in enumerate(items, 1)]
    rubric = {"id": "q1", "label": "1.12", "stem": "Judge each statement.", "total_pts": 10, "parts": parts}
    return json.dumps({"candidates": [{"q_id": "q1", "target_id": "q1:criterion", "target": "criterion",
                                       "rubric_definition": {"part_rubric": rubric}}]}, ensure_ascii=False)


@pytest.mark.parametrize("language", ["zh", "en"])
def test_same_generated_budget_survives_display_and_actual_grading(language):
    p = problem()
    available = [item(i, 2) for i in range(1, 6)]
    criterion, draft = generated_rubric(p, RubricDefinition.model_validate(definition(available)), language=language)
    summary = summarize_rubric_points(criterion, 10, p["question_structure"])
    assert summary.is_valid and [v.points for v in summary.items] == ["2"] * 5
    # The model's proposed total and the numbers in its explanation are not awards.
    scores = [2, 1, 0, 2, 1]
    ids = [i["id"] for i in available]
    assessment = math_grading.MathAssessment(
        route_id="main", route_reason="Only route.", claims=[math_grading.Claim(
            id="c", quote="work", item_ids=ids, verdict="supported", reason="Identified evidence.")],
        awards=[math_grading.Award(item_id=key, claim_ids=["c"], score=v,
                                   reason="Full 2, partial 1, otherwise 0.") for key, v in zip(ids, scores)],
        score=10, comment="Proposed total 10.", confidence=1)
    total, steps = math_grading.score_assessment(assessment, draft.plan, "work")
    assert total == 6 and [s.score for s in steps] == scores
    for awards in [assessment.awards[:-1], [*assessment.awards[:-1], assessment.awards[0]],
                   [assessment.awards[0].model_copy(update={"score": 3}), *assessment.awards[1:]]]:
        with pytest.raises(ValueError):
            math_grading.score_assessment(assessment.model_copy(update={"awards": awards}), draft.plan, "work")


def test_nonuniform_parts_and_multiple_real_steps():
    p = problem(3)
    items = [item(3, 5), item(1, 1.75, label="第1小问", suffix=".a"),
             item(2, 2.5, label="第二小题"), item(1, .75, label="Part 1", suffix=".b")]
    text, _ = generated_rubric(p, RubricDefinition.model_validate(definition(items)))
    result = summarize_rubric_points(text, 10, p["question_structure"])
    assert result.is_valid and [v.points for v in result.items] == ["2.5", "2.5", "5"]


@pytest.mark.asyncio
async def test_retry_uses_measured_defect_then_persists_only_corrected_criterion(monkeypatch):
    owner, _, op, pipeline = await setup(monkeypatch)
    p = problem()
    monkeypatch.setattr(inc, "_extract_problems_call", AsyncMock(return_value={"q1": p}))
    correct = [item(i, 2) for i in range(1, 6)]
    pipeline.provider = Provider([reply(correct[:-1]), reply([*correct[:-1], item(5, 3)]), reply(correct)])
    await pipeline.run()
    requests = [json.loads(call[1].content.split("\n", 1)[1]) for call in pipeline.provider.calls]
    assert len(requests) == 3
    assert "validation_feedback" not in requests[0]
    assert "Missing subparts: (5)" in requests[1]["validation_feedback"]["q1:criterion"]
    assert "total 11" in requests[2]["validation_feedback"]["q1:criterion"]
    assert all(r["requested_targets"] == [{"target_id": "q1:criterion", "q_id": "q1",
                                          "target": "criterion"}] for r in requests)
    saved, rows = persisted(owner, op)
    assert rows["q1"]["reference_answer"] == p["reference_answer"]
    assert saved.checkpoint["preparation_counts"]["rubrics"] == 1
    assert saved.checkpoint["material_repair_feedback"] == {}


@pytest.mark.asyncio
async def test_manual_retry_starts_with_last_subpart_diagnostic(monkeypatch):
    from backend.services import task_facade
    from backend.db import workflow_repository

    owner, _, op, pipeline = await setup(monkeypatch)
    p = problem()
    monkeypatch.setattr(inc, "_extract_problems_call", AsyncMock(return_value={"q1": p}))
    invalid = reply([item(i, 2) for i in range(1, 5)])
    pipeline.provider = Provider([invalid] * 3)
    with pytest.raises(ValidationError):
        await pipeline.run()
    saved, rows = persisted(owner, op)
    assert rows["q1"]["criterion"] == ""
    reasons, = saved.checkpoint["material_repair_feedback"].values()
    assert "Missing subparts: (5)" in reasons["q1:criterion"]
    assert len(pipeline.provider.calls) == 3
