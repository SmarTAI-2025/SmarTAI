from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agents import question_preparation_agent as agent
from backend.agents.ingest_agent import AICompletionCandidateOutput
from backend.domain.errors import ValidationError
from backend.models import QuestionScorePolicy
from backend.progress.tracker import ProgressReporter
from backend.services.question_structure import annotate_major_question_structures
from backend.skills.question_score import InterpretedQuestionScore, InterpretedQuestionScorePlan
from backend.tests.test_teacher_score_constraints import draft

NOTE = "第1题20分；第2题30分；第3题50分"


def questions(numbers):
    return annotate_major_question_structures({f"q{i}": {
        "q_id": f"q{i}", "number": number, "type": "计算题",
        "stem": f"Solve problem {number}.", "criterion": "Method: 100%",
        "max_score": {"1": 20, "2": 30, "3": 50}.get(number, 10),
    } for i, number in enumerate(numbers, 1)})


async def run_preparation(monkeypatch, numbers, *, note=NOTE, recovered=False):
    rows = questions(numbers)
    async def extract(text, provider, store, **kwargs):
        store.update(deepcopy(rows))
        return store
    async def score(*args, **kwargs):
        return InterpretedQuestionScorePlan(scores=[
            InterpretedQuestionScore(q_id=q_id, max_score=row["max_score"])
            for q_id, row in rows.items()
        ]), None
    async def generate(**kwargs):
        return [AICompletionCandidateOutput(**target, text_value="Complete answer.")
                for target in kwargs["requested_targets"]]
    generator = AsyncMock(side_effect=generate)
    extracted = AsyncMock()
    failed = AsyncMock()
    monkeypatch.setattr(agent, "extract_problems", extract)
    monkeypatch.setattr("backend.skills.question_score.structured_llm_call", score)
    monkeypatch.setattr(agent, "generate_missing_question_materials", generator)
    call = agent.prepare_question_packages(
        [(draft(), "1. Solve A.\n2. Solve B.\n3. Solve C.")],
        SimpleNamespace(config=SimpleNamespace(max_concurrent=2)),
        provider_id="fake", reporter=ProgressReporter("teacher-coverage"),
        score_policy=QuestionScorePolicy(mode="per_question", per_question_text=note),
        recovered_base_problem_data=rows if recovered else None,
        on_questions_extracted=extracted, on_base_failed=failed,
    )
    return call, generator, extracted, failed


@pytest.mark.asyncio
@pytest.mark.parametrize("numbers", [["1"], ["1", "2", "4"], ["1", "1", "3"]])
async def test_missing_or_wrong_explicit_questions_stop_before_generation(monkeypatch, numbers):
    call, generator, extracted, failed = await run_preparation(monkeypatch, numbers)
    with pytest.raises(ValidationError) as error:
        await call
    assert error.value.code == "question_structure_score_mismatch"
    generator.assert_not_awaited()
    extracted.assert_not_awaited()
    assert failed.await_args.args[0] == "questions_extracted"


@pytest.mark.asyncio
async def test_old_aligned_base_cannot_bypass_coverage_gate(monkeypatch):
    call, generator, _, _ = await run_preparation(monkeypatch, ["1"], recovered=True)
    with pytest.raises(ValidationError):
        await call
    generator.assert_not_awaited()


@pytest.mark.asyncio
async def test_matching_three_majors_preserves_ids_and_total(monkeypatch):
    call, generator, extracted, failed = await run_preparation(monkeypatch, ["1", "2", "3"])
    result = await call
    assert [row["number"] for row in result.values()] == ["1", "2", "3"]
    assert sum(row["max_score"] for row in result.values()) == 100
    assert generator.await_count == 3
    extracted.assert_awaited_once()
    failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_teacher_note_does_not_drop_or_reject_other_questions(monkeypatch):
    call, generator, _, _ = await run_preparation(monkeypatch, ["1", "2", "3"], note="第1题20分")
    result = await call
    assert len(result) == 3 and generator.await_count == 3


@pytest.mark.asyncio
async def test_old_final_artifact_cannot_publish_missing_questions(monkeypatch):
    from backend.api import task_preparation
    writes, failures = [], []
    monkeypatch.setattr(task_preparation.task_facade, "_replace_draft_questions", lambda *a, **k: writes.append(a))
    monkeypatch.setattr(task_preparation.task_facade, "_fail_operation", lambda *a, **k: failures.append(a))
    await task_preparation._run_question_preparation(
        task_id="test-task", owner_id="test-owner", job_id="old-final", job_attempt=1,
        sources=[(draft(), "source", {})], provider=None, claimed_workflow_revision=1,
        replace_confirmed=False, score_policy=QuestionScorePolicy(mode="per_question", per_question_text=NOTE),
        recognition_provider_id="fake", prebuilt_packages=questions(["1"]),
    )
    assert not writes
    assert failures[0][4] == "question_structure_score_mismatch"


@pytest.mark.parametrize("note,numbers", [
    ("第1.1题20分；第1.2题10分", ["1.1", "1.2"]),
    ("第I题10分；第II题20分", ["I", "II"]),
    ("第一大题20分；第二大题30分", ["1", "2"]),
    ("第1题20分；第2题30分", ["第1题：", "Question 2"]),
])
def test_display_number_variants_keep_exact_major_identity(note, numbers):
    from backend.services.teacher_score_constraints import validate_teacher_question_coverage
    validate_teacher_question_coverage(
        {f"q{i}": {"number": number} for i, number in enumerate(numbers)},
        QuestionScorePolicy(mode="per_question", per_question_text=note),
    )


@pytest.mark.parametrize("numbers", [["11"], ["1", "1"]])
def test_prefix_matches_and_duplicates_are_not_valid_coverage(numbers):
    from backend.services.teacher_score_constraints import validate_teacher_question_coverage
    with pytest.raises(ValidationError):
        validate_teacher_question_coverage(
            {f"q{i}": {"number": number} for i, number in enumerate(numbers)},
            QuestionScorePolicy(mode="per_question", per_question_text="第1题20分"),
        )


@pytest.mark.asyncio
async def test_missing_extraction_can_retry_without_reupload_or_bad_cache(monkeypatch):
    from backend.api import task_preparation
    from backend.db import workflow_repository
    from backend.services import task_facade
    from backend.tests.test_question_preparation_recovery import _RecoveryRegistry, _claim
    from backend.tests.test_task_background_workflows import _seed_task, _BackgroundTasks

    owner, task = _seed_task()
    registry = _RecoveryRegistry()
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _owner: registry)
    source = await task_preparation.preflight_problem_source(
        task_id=task, file=None, library_material_id=None, stored_file_id=None,
        inline_text="1. Solve A.\n2. Solve B.\n3. Solve C.", structure_mode="organized",
        role="problem", extraction_hint="", save_to_library=False,
        recognition_provider_id="test-provider", current=SimpleNamespace(id=owner), registry=registry,
    )
    response = await task_preparation._start_question_preparation(
        task_id=task, request=task_preparation.StartQuestionPreparationRequest(
            source_tokens=[source["source_token"]], expected_workflow_revision=0,
            score_policy=QuestionScorePolicy(mode="per_question", per_question_text=NOTE),
            recognition_provider_id="test-provider",
        ), background_tasks=_BackgroundTasks(), current=SimpleNamespace(id=owner),
        registry=registry, allow_prepared_source_reuse=False,
    )
    extraction_calls = []
    async def extract(text, provider, store, **kwargs):
        extraction_calls.append(text)
        store.update(questions(["1"] if len(extraction_calls) == 1 else ["1", "2", "3"]))
        return store
    async def score(*args, **kwargs):
        return InterpretedQuestionScorePlan(scores=[
            InterpretedQuestionScore(q_id=f"q{i}", max_score=maximum)
            for i, maximum in enumerate([20, 30, 50], 1)
        ]), None
    async def generate(**kwargs):
        return [AICompletionCandidateOutput(**target, text_value="Complete answer.")
                for target in kwargs["requested_targets"]]
    generator = AsyncMock(side_effect=generate)
    monkeypatch.setattr(agent, "extract_problems", extract)
    monkeypatch.setattr(agent, "generate_missing_question_materials", generator)
    monkeypatch.setattr("backend.skills.question_score.structured_llm_call", score)
    first = _claim(owner, response["job_id"], "first-worker")
    await task_preparation.run_durable_question_preparation(first)
    failed = workflow_repository.get_operation(first.operation_id, owner_id=owner)
    assert failed.error_code == "question_structure_score_mismatch"
    assert not failed.checkpoint.get("questions_extracted_artifact_id")
    assert not failed.checkpoint.get("base_provider_inflight_stage")
    generator.assert_not_awaited()
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    retried = await task_preparation.retry_question_preparation(
        task_id=task, job_id=first.operation_id,
        request=task_preparation.RetryQuestionPreparationRequest(expected_workflow_revision=workflow.workflow_revision),
        background_tasks=_BackgroundTasks(), current=SimpleNamespace(id=owner), registry=registry,
    )
    assert retried["job_id"] == first.operation_id
    second = _claim(owner, first.operation_id, "second-worker")
    await task_preparation.run_durable_question_preparation(second)
    finished = workflow_repository.get_operation(first.operation_id, owner_id=owner)
    result = task_facade.get_task(task_id=task, owner_id=owner, full=True)
    assert finished.error_code is None
    assert second.attempt == first.attempt + 1
    assert len(extraction_calls) == 2 and generator.await_count == 3
    assert [row["number"] for row in result["problem_data"].values()] == ["1", "2", "3"]
    assert sum(row["max_score"] for row in result["problem_data"].values()) == 100


@pytest.mark.parametrize("note", [
    "第 1–3 题每题 5 分，第 4 题 15 分，第 5 题 20 分。",
    "Questions 1–3 are worth 5 points each; Question 4 is worth 15; Question 5 is worth 20.",
])
@pytest.mark.parametrize("numbers", [["4", "5"], ["1", "2", "3"]])
def test_existing_ui_example_cannot_omit_ranges_or_later_questions(note, numbers):
    from backend.services.teacher_score_constraints import validate_teacher_question_coverage
    with pytest.raises(ValidationError):
        validate_teacher_question_coverage(
            {f"q{i}": {"number": number} for i, number in enumerate(numbers)},
            QuestionScorePolicy(mode="per_question", per_question_text=note),
        )


@pytest.mark.parametrize("note", [
    "第 1–3 题每题 5 分，第 4 题 15 分，第 5 题 20 分。",
    "Questions 1–3 are worth 5 points each; Question 4 is worth 15; Question 5 is worth 20.",
])
def test_existing_ui_example_resolves_all_five_declared_scores(note):
    from backend.services.teacher_score_constraints import explicit_teacher_scores
    outline = explicit_teacher_scores(note)
    assert {number: float(entry.maximum) for number, entry in outline.items()} == {
        "1": 5, "2": 5, "3": 5, "4": 15, "5": 20,
    }



def test_range_aggregate_is_not_copied_as_each_question_maximum():
    from backend.services.teacher_score_constraints import explicit_teacher_scores
    outline = explicit_teacher_scores("第1–3题共15分")
    assert set(outline) == {"1", "2", "3"}
    assert all(entry.maximum is None for entry in outline.values())


def test_explicit_dotted_range_preserves_complete_question_numbers():
    from backend.services.teacher_score_constraints import explicit_teacher_scores
    outline = explicit_teacher_scores("第1.1–1.3题每题5分")
    assert set(outline) == {"1.1", "1.2", "1.3"}
    assert all(entry.maximum == 5 for entry in outline.values())
