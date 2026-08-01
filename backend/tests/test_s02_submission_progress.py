from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from backend.agents.ingest_agent import parse_student_answers
from backend.progress.tracker import ProgressReporter


@pytest.mark.asyncio
async def test_submission_parser_reports_factual_stage_metrics_without_pii(monkeypatch):
    responses = iter([
        {
            "stu_id": "PB001",
            "stu_name": "Kate",
            "stu_ans": [
                {"q_id": "q1", "number": "1", "type": "概念题", "content": "A"},
                {"q_id": "q2", "number": "2", "type": "计算题", "content": "B"},
            ],
        },
        {
            "stu_id": "unknown",
            "stu_name": "[Unknown Student]",
            "stu_ans": [
                {"q_id": "q1", "number": "1", "type": "概念题", "content": "C"},
            ],
        },
    ])

    async def fake_invoke(_provider, _messages):
        return SimpleNamespace(content=json.dumps(next(responses), ensure_ascii=False))

    monkeypatch.setattr("backend.agents.ingest_agent.ainvoke_with_retry", fake_invoke)
    reporter = ProgressReporter("s02-factual-progress")
    student_store = {}

    await parse_student_answers(
        files_data=[
            {"filename": "PB001_Kate.txt", "content": "answers"},
            {"filename": "unresolved.txt", "content": "answers"},
        ],
        problems_data={
            "q1": {"q_id": "q1", "number": "1", "type": "概念题", "stem": "Q1"},
            "q2": {"q_id": "q2", "number": "2", "type": "计算题", "stem": "Q2"},
        },
        student_store=student_store,
        provider=SimpleNamespace(provider_id="mock:s02"),
        reporter=reporter,
        identity_mode="filename",
    )

    progress = await reporter.snapshot()
    assert progress.phase == "parsing"
    assert progress.current_step == "consolidating_submission_results"
    assert progress.total_students == 2
    assert progress.completed_units == 2
    assert progress.stage_metrics == {
        "files_total": 2,
        "files_processed": 2,
        "submissions_recognized": 2,
        "identities_matched": 1,
        "identities_needing_review": 1,
        "answers_split": 3,
        "parse_failures": 0,
    }
    event_text = " ".join(event.message for event in progress.messages)
    assert "Kate" not in event_text
    assert "PB001" not in event_text
    assert "Submission recognized." in event_text


@pytest.mark.asyncio
async def test_stage_metric_updates_are_atomic_and_reject_invalid_values():
    reporter = ProgressReporter("s02-metric-contract")
    await reporter.set_stage_metrics(files_total=3, files_processed=0)
    await reporter.increment_stage_metrics(files_processed=1, answers_split=2)

    progress = await reporter.snapshot()
    assert progress.stage_metrics == {
        "files_total": 3,
        "files_processed": 1,
        "answers_split": 2,
    }

    with pytest.raises(ValueError):
        await reporter.increment_stage_metrics(files_processed=-1)
    with pytest.raises(ValueError):
        await reporter.set_stage_metrics(files_total=True)
