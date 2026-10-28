"""Independent U3 synthetic recovery probes. No product edits or live model calls.

These are fault-injected backend integration checks, not real OCR/GUI acceptance.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agents import incremental_question_preparation as inc
from backend.api import task_preparation as api
from backend.db import workflow_repository
from backend.domain.errors import RecognitionError, ValidationError
from backend.services import task_facade
from backend.services.task_execution import stop_task_run
from backend.tests.test_incremental_question_materials import material_setup
from backend.tests.test_incremental_question_preparation import row, persisted, generated
from backend.tests.test_question_preparation_recovery import (
    _RecoveryRegistry, _claim, _queue_question_preparation,
)
from backend.tests.test_task_background_workflows import _seed_task, _BackgroundTasks


@pytest.mark.asyncio
@pytest.mark.parametrize("broken_role", ["rubric", "reference_answer"])
async def test_three_materials_reordered_numbers_gap_pause_and_manual_keep_every_issue(monkeypatch, broken_role):
    """Q3,Q1,Q2; teacher material has another order; page2 failure survives pause."""
    rubrics = {"1": "Rule A: one exact condition.", "2": "Rule B: two exact conditions.", "3": "Rule C: three exact conditions."}
    answers = {"1": "Answer A: alpha.", "2": "Answer B: beta.", "3": "Answer C: gamma."}
    rubric_text = "\n".join(f"Question {n}\n{rubrics[n]}" for n in ("2", "1", "3"))
    answer_text = "\n".join(f"Question {n}\n{answers[n]}" for n in ("1", "3", "2"))
    owner, task, operation, pipeline = await material_setup(monkeypatch, [("rubric", rubric_text), ("reference_answer", answer_text)])
    original_tokens = {source[0].role: source[0].source_token for source in pipeline.sources}

    async def read(source, **kwargs):
        draft, text, payload = source
        if draft.role == broken_role:
            return draft, text, {**payload, "source_error": "recognition_response_invalid",
                "recognition": {"coverage": {"processed_pages": [1, 3], "failed_pages": [2]}}}
        return source

    model_calls = []
    async def parse_material(*args, **kwargs):
        prompt = kwargs["user_prompt"]
        values = rubrics if "Rule A:" in prompt else answers
        model_calls.append("rubric" if values is rubrics else "reference_answer")
        return inc._MaterialItems(items=[inc._MaterialItem(number=n, text_value=value)
            for n, value in values.items()]), None

    pipeline.read_source = read
    extract = AsyncMock(return_value=inc._rows([row(n, stem=f"Synthetic stem {n}.") for n in ("3", "1", "2")]))
    generate = AsyncMock(side_effect=AssertionError("Never replace supplied incomplete teacher material with generated text"))
    monkeypatch.setattr(inc, "_extract_problems_call", extract)
    monkeypatch.setattr(inc, "structured_llm_call", parse_material)
    monkeypatch.setattr(inc, "generate_missing_question_materials", generate)
    with pytest.raises(RecognitionError) as failed:
        await pipeline.run()
    assert failed.value.code == "recognition_response_invalid"
    saved, before = persisted(owner, operation)
    assert len(before) == 3
    assert [item["number"] for item in before.values()] == ["3", "1", "2"]
    for item in before.values():
        assert item["stem"] == f"Synthetic stem {item['number']}."
        assert item["criterion"] == rubrics[item["number"]]
        assert item["reference_answer"] == answers[item["number"]]
        for role, field in (("rubric", "criterion"), ("reference_answer", "reference_answer")):
            assert item["material_provenance"][field]["source_token"] == original_tokens[role]
        assert any(issue["code"] == "recognition_partial" and issue["severity"] == "blocking"
            for issue in item["preparation_issues"])
    before_issues = {item["number"]: item["preparation_issues"] for item in before.values()}
    before_by_number = {item["number"]: item for item in before.values()}
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    stop_task_run(task_id=task, owner_id=owner, job_id=operation.operation_id, expected_revision=workflow.workflow_revision)
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    before_calls = list(model_calls)
    forbidden = AsyncMock(side_effect=AssertionError("Manual handoff cannot invoke a model"))
    monkeypatch.setattr(inc, "_extract_problems_call", forbidden)
    monkeypatch.setattr(inc, "structured_llm_call", forbidden)
    response = await api.manually_complete_question_preparation(task, operation.operation_id,
        api.ManualQuestionPreparationRequest(expected_workflow_revision=workflow.workflow_revision), current=SimpleNamespace(id=owner))
    assert response["status"] == "ready_for_review"
    for _ in range(2):
        published = task_facade.get_task(task_id=task, owner_id=owner)["problem_data"]
        assert len(published) == 3
        for item in published.values():
            original = before_by_number[item["number"]]
            for field in ("q_id", "number", "type", "stem", "max_score", "material_provenance"):
                assert item[field] == original[field], field
            assert item["criterion"] == rubrics[item["number"]]
            assert item["reference_answer"] == answers[item["number"]]
            for prior in before_issues[item["number"]]:
                matching = [issue for issue in item["preparation_issues"] if issue["issue_id"] == prior["issue_id"]]
                assert len(matching) == 1
                assert {key: value for key, value in matching[0].items() if key != "severity"} == {
                    key: value for key, value in prior.items() if key != "severity"}
        blockers = task_facade.grading_readiness(task_id=task, owner_id=owner)["blocking_issues"]
        # Current product contract makes optional reference-answer issues advisory.
        # Its original issue must survive, but only an incomplete rubric blocks.
        assert ("question_review_blocked" in blockers) is (broken_role == "rubric")
        expected_severity = "blocking" if broken_role == "rubric" else "warning"
        for item in published.values():
            partial = [issue for issue in item["preparation_issues"] if issue["code"] == "recognition_partial"]
            assert partial and all(issue["severity"] == expected_severity for issue in partial)
    assert model_calls == before_calls
    assert set(model_calls) == {"rubric", "reference_answer"}
    generate.assert_not_awaited()
    forbidden.assert_not_awaited()
    print("U3_CUSTOM " + json.dumps({"case": "three_material_gap_pause_manual", "broken_role": broken_role,
        "question_numbers": [v["number"] for v in published.values()], "retained_fields": 9,
        "unresolved_issues_retained": True, "blocks_question_review": broken_role == "rubric", "manual_new_calls": 0}))


@pytest.mark.asyncio
async def test_duplicate_numbers_two_retries_preserve_success_and_manual_unresolved(monkeypatch):
    """Three stems, including chapter-local number 1; two failed retries then stop."""
    owner, task = _seed_task()
    _, queued, _ = await _queue_question_preparation(owner, task,
        source_text="Chapter B: 1. beta.\nChapter A: 7. gamma.\nChapter A: 1. alpha.")
    registry = _RecoveryRegistry()
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: registry)
    stems = ["Chapter B: beta.", "Chapter A: gamma.", "Chapter A: alpha."]
    numbers = ["1", "7", "1"]
    extracts = AsyncMock(return_value=inc._rows([row(number, stem=stem) for number, stem in zip(numbers, stems)]))
    calls = []
    async def generate(data, targets, *args, **kwargs):
        stem = data["q1"]["stem"]
        calls.append(stem)
        if stem == stems[2]:
            raise ValidationError("Synthetic unresolved material", code="provider_response_invalid")
        return [generated(t["target"], f"Saved {t['target']} from {stem}") for t in targets]
    monkeypatch.setattr(inc, "_extract_problems_call", extracts)
    monkeypatch.setattr(inc, "generate_missing_question_materials", generate)
    job = queued["job_id"]
    for attempt in range(3):
        await api.run_durable_question_preparation(_claim(owner, job, f"u3-retry-{attempt}"))
        saved = workflow_repository.get_operation(job, owner_id=owner)
        assert saved.status == "error"
        assert saved.checkpoint["preparation_counts"] == dict(questions=3, stems=3, reference_answers=2, rubrics=2)
        if attempt < 2:
            workflow = workflow_repository.get_workflow(task, owner_id=owner)
            response = await api.retry_question_preparation(task, job,
                api.RetryQuestionPreparationRequest(expected_workflow_revision=workflow.workflow_revision),
                background_tasks=_BackgroundTasks(), current=SimpleNamespace(id=owner), registry=registry)
            assert isinstance(response, dict), getattr(response, "body", response)
            job = response["job_id"]
    extracts.assert_awaited_once()
    assert calls.count(stems[0]) == calls.count(stems[1]) == 1
    assert calls.count(stems[2]) == 3
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    before_calls = list(calls)
    response = await api.manually_complete_question_preparation(task, job,
        api.ManualQuestionPreparationRequest(expected_workflow_revision=workflow.workflow_revision), current=SimpleNamespace(id=owner))
    assert response["status"] == "ready_for_review"
    rows = task_facade.get_task(task_id=task, owner_id=owner)["problem_data"]
    assert [v["number"] for v in rows.values()] == numbers
    assert [v["stem"] for v in rows.values()] == stems
    for q_id, stem in zip(("q1", "q2"), stems[:2]):
        assert rows[q_id]["q_id"] == q_id
        assert rows[q_id]["criterion"] == f"Saved criterion from {stem}"
        assert rows[q_id]["reference_answer"] == f"Saved reference_answer from {stem}"
    assert not rows["q3"]["criterion"] and not rows["q3"]["reference_answer"]
    assert "question_review_blocked" in task_facade.grading_readiness(task_id=task, owner_id=owner)["blocking_issues"]
    assert calls == before_calls
    print("U3_CUSTOM " + json.dumps({"case": "repeat_number_two_retries_manual", "question_numbers": numbers,
        "extract_calls": extracts.await_count, "generation_calls_by_stem": {s:calls.count(s) for s in stems},
        "manual_new_calls": 0, "unresolved_blockers_retained": True}))
