"""Backend contracts for factual recognition progress and problem review state."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from backend.agents import ingest_agent
from backend.models import JobProgress, ProblemInfo
from backend.progress.tracker import ProgressReporter


def test_new_progress_fields_and_problem_review_status_are_backward_compatible():
    progress = JobProgress()
    assert progress.started_at is None
    assert progress.contract_version == 1
    assert progress.job_id is None
    assert progress.workflow is None
    assert progress.stage_sequence == []
    assert progress.current_step is None
    assert progress.total_steps is None
    assert progress.completed_steps is None

    problem = ProblemInfo(
        q_id="q1",
        number="1",
        type="概念题",
        stem="Explain the concept.",
        criterion="Correct explanation.",
    )
    assert problem.review_status == "needs_review"


@pytest.mark.asyncio
async def test_stage_progress_updates_all_fields_together():
    reporter = ProgressReporter("stage-contract")
    before = time.time()
    await reporter.set_stage_progress(
        "source_prepared",
        total_steps=4,
        completed_steps=1,
    )
    snapshot = await reporter.snapshot()

    assert snapshot.started_at is not None
    assert before <= snapshot.started_at <= time.time()
    assert snapshot.current_step == "source_prepared"
    assert snapshot.total_steps == 4
    assert snapshot.completed_steps == 1

    with pytest.raises(ValueError):
        await reporter.set_stage_progress(
            "invalid",
            total_steps=4,
            completed_steps=5,
        )
    unchanged = await reporter.snapshot()
    assert unchanged.current_step == "source_prepared"
    assert unchanged.completed_steps == 1


@pytest.mark.asyncio
async def test_configured_workflow_rejects_stage_regression_or_replacement():
    reporter = ProgressReporter("workflow-contract")
    await reporter.configure_workflow("question_preparation", ["validate", "extract", "commit"])
    await reporter.set_stage_progress(
        "extract",
        total_steps=3,
        completed_steps=1,
    )

    with pytest.raises(ValueError):
        await reporter.set_stage_progress("legacy", total_steps=4, completed_steps=1)
    with pytest.raises(ValueError):
        await reporter.set_stage_progress("extract", total_steps=3, completed_steps=0)
    with pytest.raises(ValueError):
        await reporter.configure_workflow("question_preparation", ["one", "two"])

    snapshot = await reporter.snapshot()
    assert snapshot.job_id == "workflow-contract"
    assert snapshot.workflow == "question_preparation"
    assert snapshot.stage_sequence == ["validate", "extract", "commit"]
    assert snapshot.current_step == "extract"
    assert snapshot.completed_steps == 1


@pytest.mark.asyncio
async def test_problem_extraction_reports_only_real_milestones(monkeypatch):
    async def fake_ainvoke(_provider, _messages):
        return SimpleNamespace(content=(
            '{"problems":[{"q_id":"q1","number":"1",'
            '"type":"概念题","stem":"Question",'
            '"criterion":"Criterion"}]}'
        ))

    monkeypatch.setattr(ingest_agent, "ainvoke_with_retry", fake_ainvoke)
    class RecordingReporter(ProgressReporter):
        def __init__(self):
            super().__init__("extract-stage-contract")
            self.stage_updates = []

        async def set_stage_progress(
            self,
            current_step,
            *,
            total_steps,
            completed_steps,
            message=None,
        ):
            self.stage_updates.append(
                (current_step, total_steps, completed_steps)
            )
            await super().set_stage_progress(
                current_step,
                total_steps=total_steps,
                completed_steps=completed_steps,
                message=message,
            )

    reporter = RecordingReporter()
    problem_store = {}

    await ingest_agent.extract_problems(
        "1. Question",
        SimpleNamespace(provider_id="mock:model"),
        problem_store,
        reporter=reporter,
    )

    snapshot = await reporter.snapshot()
    assert snapshot.phase == "done"
    assert snapshot.current_step == "completed"
    assert snapshot.total_steps == 4
    assert snapshot.completed_steps == 4
    assert snapshot.started_at is not None
    assert problem_store["q1"]["review_status"] == "needs_review"
    assert reporter.stage_updates == [
        ("source_prepared", 4, 1),
        # The active recognition/organization steps are not counted complete
        # until the corresponding real work returns.
        ("calling_recognition", 4, 1),
        ("organizing_structure", 4, 2),
        ("completed", 4, 4),
    ]
    messages = [event.message for event in snapshot.messages]
    assert [
        "Problem source prepared.",
        "Problem recognition started.",
        "Organizing recognized problem structure.",
        "Problem recognition completed.",
    ] == [message for message in messages if message in {
        "Problem source prepared.",
        "Problem recognition started.",
        "Organizing recognized problem structure.",
        "Problem recognition completed.",
    }]


@pytest.mark.asyncio
async def test_nested_problem_extraction_preserves_outer_progress_contract(monkeypatch):
    async def fake_ainvoke(_provider, _messages):
        return SimpleNamespace(content=(
            '{"problems":[{"q_id":"q1","number":"1",'
            '"type":"概念题","stem":"Question",'
            '"criterion":"Criterion"}]}'
        ))

    monkeypatch.setattr(ingest_agent, "ainvoke_with_retry", fake_ainvoke)
    reporter = ProgressReporter("nested-extract-contract")
    stages = [
        "validating_sources",
        "extracting_questions",
        "aligning_uploaded_materials",
        "generating_solutions",
        "aligning_rubrics",
        "preparing_programming_tests",
        "detecting_conflicts",
        "committing_question_packages",
    ]
    await reporter.configure_workflow("question_preparation", stages)
    await reporter.set_phase("parsing")
    await reporter.set_stage_progress(
        "extracting_questions",
        total_steps=8,
        completed_steps=1,
    )

    problem_store = {}
    await ingest_agent.extract_problems(
        "1. Question",
        SimpleNamespace(provider_id="mock:model"),
        problem_store,
        reporter=reporter,
        manage_progress_lifecycle=False,
    )

    snapshot = await reporter.snapshot()
    assert snapshot.phase == "parsing"
    assert snapshot.workflow == "question_preparation"
    assert snapshot.stage_sequence == stages
    assert snapshot.current_step == "extracting_questions"
    assert snapshot.total_steps == 8
    assert snapshot.completed_steps == 1
    assert snapshot.total_questions == 1
