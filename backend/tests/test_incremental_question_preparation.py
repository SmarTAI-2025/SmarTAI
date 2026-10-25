"""Durable boundary regressions for the current preparation workflow."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agents import incremental_question_preparation as inc
from backend.agents.ingest_agent import AICompletionCandidateOutput
from backend.api import task_preparation as api
from backend.db import workflow_repository
from backend.domain.errors import ValidationError
from backend.progress.tracker import ProgressReporter
from backend.services import task_facade
from backend.services.question_preparation_artifacts import read_base_preparation_artifact, QUESTIONS_EXTRACTED_STAGE
from backend.tests.test_question_preparation_recovery import _queue_question_preparation, _claim, _RecoveryRegistry
from backend.tests.test_task_background_workflows import _seed_task, _BackgroundTasks


def row(number, *, stem=None, criterion="", reference_answer=""):
    return dict(number=str(number), type="概念题", stem=stem or f"Explain item {number}.",
                criterion=criterion, reference_answer=reference_answer, max_score=10)


def generated(target, value):
    return AICompletionCandidateOutput(q_id="q1", target=target,
        target_id="q1:" + target, text_value=value)


async def setup(monkeypatch, text="one"):
    owner, task = _seed_task()
    _, queued, _ = await _queue_question_preparation(owner, task, source_text=text)
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: _RecoveryRegistry())
    operation = _claim(owner, queued["job_id"], "incremental")
    inputs = api._rehydrate_question_preparation_inputs(operation)
    async def read(source, **kwargs):
        return source
    pipeline = inc.IncrementalQuestionPreparation(operation=operation, sources=inputs["sources"],
        provider=inputs["route"].provider, provider_id=inputs["recognition_provider_id"],
        reporter=ProgressReporter(operation.operation_id), score_policy=inputs["score_policy"],
        read_source=read)
    return owner, task, operation, pipeline


def persisted(owner, operation):
    saved = workflow_repository.get_operation(operation.operation_id, owner_id=owner)
    file_id = saved.checkpoint["questions_extracted_artifact_id"]
    envelope = read_base_preparation_artifact(file_id, owner_id=owner,
        task_id=saved.assignment_id, operation_id=saved.id, attempt=saved.attempt,
        input_hash=saved.input_hash, provider_record_id="test-provider", stage=QUESTIONS_EXTRACTED_STAGE)
    return saved, envelope.payload.problem_data


@pytest.mark.asyncio
async def test_one_chunk_is_durable_while_another_waits_and_manual_never_calls_model(monkeypatch):
    owner, task, operation, pipeline = await setup(monkeypatch)
    blocked = asyncio.Event()
    saved_first = asyncio.Event()
    monkeypatch.setattr(inc, "_chunk_problem_text", lambda *_: ["one", "two"])
    async def extract(text, *args, **kwargs):
        if text == "two":
            await blocked.wait()
        return inc._rows([row(1 if text == "one" else 2)])
    monkeypatch.setattr(inc, "_extract_problems_call", extract)
    async def generate(*args, **kwargs):
        saved_first.set()
        await blocked.wait()
        return []
    monkeypatch.setattr(inc, "generate_missing_question_materials", generate)
    runner = asyncio.create_task(pipeline.run())
    await asyncio.wait_for(saved_first.wait(), 3)
    _, rows = persisted(owner, operation)
    assert len(rows) == 1 and rows["q1"]["stem"] == "Explain item 1."
    runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runner
    from backend.services.task_execution import stop_task_run
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    stop_task_run(task_id=task, owner_id=owner, job_id=operation.operation_id,
                  expected_revision=workflow.workflow_revision)
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    provider = AsyncMock(side_effect=AssertionError("manual handoff must never invoke AI"))
    monkeypatch.setattr(inc, "generate_missing_question_materials", provider)
    result = await api.manually_complete_question_preparation(task, operation.operation_id,
        api.ManualQuestionPreparationRequest(expected_workflow_revision=workflow.workflow_revision),
        current=SimpleNamespace(id=owner))
    assert result["status"] == "ready_for_review", getattr(result, "body", result)
    provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_field_keeps_valid_answer_and_retries_only_rubric(monkeypatch):
    owner, _, operation, pipeline = await setup(monkeypatch)
    monkeypatch.setattr(inc, "_extract_problems_call", AsyncMock(return_value=inc._rows([row(1)])))
    calls = []
    async def generate(data, targets, *args, **kwargs):
        calls.append([target["target"] for target in targets])
        return [generated("reference_answer", "A valid explanation.")] if len(calls) == 1 else []
    monkeypatch.setattr(inc, "generate_missing_question_materials", generate)
    with pytest.raises(ValidationError):
        await pipeline.run()
    saved, rows = persisted(owner, operation)
    assert rows["q1"]["reference_answer"] == "A valid explanation."
    assert not rows["q1"]["criterion"]
    assert calls == [["reference_answer", "criterion"], ["criterion"], ["criterion"]]
    assert saved.checkpoint["preparation_counts"] == dict(questions=1, stems=1, reference_answers=1, rubrics=0)
    assert not saved.checkpoint["incremental_inflight_units"]


@pytest.mark.asyncio
async def test_manual_handoff_does_not_publish_malformed_generated_unicode(monkeypatch):
    from backend.tests.test_question_material_format_repair import Provider
    from backend.tools.structured_llm import StructuredOutputInvalidError
    from backend.services.task_execution import stop_task_run

    owner, task, operation, pipeline = await setup(monkeypatch)
    monkeypatch.setattr(inc, "_extract_problems_call", AsyncMock(return_value=inc._rows([
        row(1, criterion="Teacher rubric.", reference_answer="Teacher answer."),
        row(2, criterion="Supplied rubric."),
    ])))
    broken = (r'{"candidates":[{"target_id":"q1:reference_answer","q_id":"q1",'
              r'"target":"reference_answer","text_value":"\u8bbe $\\mathbb{Z}$ \uffa"}]}')
    pipeline.provider = Provider([broken, broken])
    with pytest.raises(StructuredOutputInvalidError):
        await pipeline.run()
    saved, rows = persisted(owner, operation)
    assert len(pipeline.provider.calls) == 2
    assert rows["q1"]["reference_answer"] == "Teacher answer."
    assert rows["q2"]["reference_answer"] == ""
    assert rows["q2"]["criterion"] == "Supplied rubric."
    assert saved.checkpoint["preparation_counts"] == dict(questions=2, stems=2, reference_answers=1, rubrics=2)
    assert not any(key.startswith("generated:") for key in saved.checkpoint["incremental_unit_artifacts"])

    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    stop_task_run(task_id=task, owner_id=owner, job_id=operation.operation_id,
                  expected_revision=workflow.workflow_revision)
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    forbidden = AsyncMock(side_effect=AssertionError("manual handoff must never invoke AI"))
    monkeypatch.setattr(inc, "generate_missing_question_materials", forbidden)
    result = await api.manually_complete_question_preparation(task, operation.operation_id,
        api.ManualQuestionPreparationRequest(expected_workflow_revision=workflow.workflow_revision),
        current=SimpleNamespace(id=owner))
    assert result["status"] == "ready_for_review", getattr(result, "body", result)
    from backend.db import assignment_repository
    questions = assignment_repository.list_questions(task, teacher_id=owner)
    assert [question.reference_answer for question in questions] == ["Teacher answer.", ""]
    assert [question.criterion for question in questions] == ["Teacher rubric.", "Supplied rubric."]
    forbidden.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [9, 10, 28])
async def test_real_worker_roundtrip_multiple_snapshot_sizes(monkeypatch, count):
    owner, task, operation, _ = await setup(monkeypatch)
    monkeypatch.setattr(inc, "_chunk_problem_text", lambda *_: [str(i) for i in range(1, count + 1)])
    monkeypatch.setattr(inc, "_extract_problems_call", AsyncMock(
        side_effect=lambda text, *a, **kw: inc._rows([row(int(text), criterion="Full credit for a correct explanation.",
                                                       reference_answer="Supplied answer.")])))
    await api.run_durable_question_preparation(operation)
    saved, rows = persisted(owner, operation)
    assert saved.status == "done", (saved.error_code, saved.checkpoint)
    assert list(rows) == [f"q{i}" for i in range(1, count + 1)]
    assert [rows[f"q{i}"]["number"] for i in range(1, count + 1)] == [str(i) for i in range(1, count + 1)]
    assert task_facade.get_task(task_id=task, owner_id=owner)["problem_count"] == count
    assert len(saved.checkpoint["incremental_unit_artifacts"]) == count


@pytest.mark.asyncio
async def test_one_chunk_failure_does_not_cancel_other_questions(monkeypatch):
    owner, _, operation, pipeline = await setup(monkeypatch)
    monkeypatch.setattr(inc, "_chunk_problem_text", lambda *_: ["8", "9", "10"])
    async def extract(text, *a, **kw):
        if text == "8":
            raise ValidationError("Invalid response", code="provider_response_invalid")
        return inc._rows([row(int(text), criterion="Teacher rubric", reference_answer="Teacher answer")])
    monkeypatch.setattr(inc, "_extract_problems_call", extract)
    with pytest.raises(ValidationError):
        await pipeline.run()
    _, rows = persisted(owner, operation)
    assert [item["number"] for item in rows.values()] == ["9", "10"]


@pytest.mark.asyncio
async def test_retired_worker_rejects_before_resolving_any_provider(monkeypatch):
    owner, _, operation, _ = await setup(monkeypatch)
    operation.payload["manual_saved_sources"] = {"artifacts": {}}
    registry = AsyncMock(side_effect=AssertionError("retired path must not resolve provider"))
    monkeypatch.setattr(task_facade, "_registry_for_owner", registry)
    await api.run_durable_question_preparation(operation)
    saved = workflow_repository.get_operation(operation.operation_id, owner_id=owner)
    assert saved.error_code == "saved_text_recovery_retired"
    registry.assert_not_called()


@pytest.mark.asyncio
async def test_two_hundred_questions_save_independent_fields_with_bounded_current_manifest(monkeypatch):
    owner, task, operation, _ = await setup(monkeypatch)
    monkeypatch.setattr(inc, "_extract_problems_call", AsyncMock(return_value=inc._rows([row(i) for i in range(1, 201)])))
    calls = []
    async def generate(data, targets, *args, **kwargs):
        calls.append(data["q1"]["number"])
        return [generated(target["target"], "A complete explanation." if target["target"] == "reference_answer"
                          else "Full credit for a correct explanation.") for target in targets]
    monkeypatch.setattr(inc, "generate_missing_question_materials", generate)
    await api.run_durable_question_preparation(operation)
    saved, rows = persisted(owner, operation)
    assert saved.status == "done", (saved.error_code, len(saved.artifact_refs))
    assert len(calls) == 200 and len(set(calls)) == 200
    assert len(rows) == 200 and all(value["criterion"] and value["reference_answer"] for value in rows.values())
    assert 400 <= len(saved.artifact_refs) < 450
    assert len(saved.checkpoint["artifact_attempts"]) < 450
    assert task_facade.get_task(task_id=task, owner_id=owner)["problem_count"] == 200


@pytest.mark.asyncio
async def test_composite_subparts_from_different_chunks_remain_one_scored_question(monkeypatch):
    owner, _, operation, pipeline = await setup(monkeypatch)
    await pipeline.finish("extract:" + pipeline.sources[0][0].source_token + ":0:a",
                          inc._rows([row("1(a)", stem="Compute A.")]))
    await pipeline.finish("extract:" + pipeline.sources[0][0].source_token + ":1:b",
                          inc._rows([row("1(b)", stem="Compute B.")]))
    _, rows = persisted(owner, operation)
    assert len(rows) == 1
    assert rows["q1"]["number"] == "1"
    assert "Compute A." in rows["q1"]["stem"] and "Compute B." in rows["q1"]["stem"]
    assert len(rows["q1"]["question_structure"]["subparts"]) == 2


@pytest.mark.asyncio
async def test_page_closed_question_is_saved_while_next_page_is_still_reading(monkeypatch):
    owner, _, operation, pipeline = await setup(monkeypatch)
    saved = asyncio.Event()
    waiting = asyncio.Event()
    async def read(source, *, on_pages):
        await on_pages([dict(page_number=1, text="Question 1\nExplain A."),
                        dict(page_number=2, text="Question 2\nExplain B.")])
        await waiting.wait()
        return source
    pipeline.read_source = read
    monkeypatch.setattr(inc, "_extract_problems_call", AsyncMock(
        return_value=inc._rows([row(1, criterion="Teacher rubric.", reference_answer="Teacher answer.")])))
    original_finish = pipeline.finish
    async def finish(key, *args, **kwargs):
        await original_finish(key, *args, **kwargs)
        if key.startswith("extract:"):
            saved.set()
    pipeline.finish = finish
    runner = asyncio.create_task(pipeline.run())
    try:
        await asyncio.wait_for(saved.wait(), 3)
        _, rows = persisted(owner, operation)
        assert rows["q1"]["stem"] and rows["q1"]["reference_answer"]
    finally:
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner


@pytest.mark.asyncio
async def test_independent_generation_parser_retains_valid_field_from_mixed_response(monkeypatch):
    from backend.agents import ingest_agent
    from backend.llm.providers import LLMResponse
    provider = SimpleNamespace(provider_id="test")
    response = LLMResponse(content=json.dumps({"candidates": [
        {"target_id": "q1:reference_answer", "q_id": "q1", "target": "reference_answer", "text_value": "Good answer."},
        {"target_id": "q1:criterion", "q_id": "q1", "target": "criterion", "text_value": {"invalid": "object"}},
    ]}), provider="test", model="test", duration_ms=1)
    invoke = AsyncMock(return_value=response)
    monkeypatch.setattr(ingest_agent, "ainvoke_with_retry", invoke)
    result = await ingest_agent.generate_missing_question_materials(
        inc._rows([row(1)]),
        [dict(q_id="q1", target_id="q1:" + target, target=target) for target in ("reference_answer", "criterion")],
        6, provider, allow_partial=True)
    assert [candidate.target for candidate in result] == ["reference_answer"]
    invoke.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalid_question_item_cannot_erase_valid_siblings_or_claim_success(monkeypatch):
    from backend.agents import ingest_agent
    from backend.llm.providers import LLMResponse
    owner, _, operation, pipeline = await setup(monkeypatch, text="Teacher rubric. Teacher answer.")
    response = LLMResponse(content=json.dumps({"problems": [
        row(1, criterion="Teacher rubric.", reference_answer="Teacher answer."), "invalid question item",
    ]}), provider="test", model="test", duration_ms=1)
    invoke = AsyncMock(return_value=response)
    monkeypatch.setattr(ingest_agent, "ainvoke_with_retry", invoke)
    with pytest.raises(ingest_agent.PartialQuestionExtractionError):
        await pipeline.run()
    saved, rows = persisted(owner, operation)
    assert len(rows) == 1 and rows["q1"]["stem"] == "Explain item 1."
    assert rows["q1"]["reference_answer"] == "Teacher answer."
    assert invoke.await_count == 3
    assert any(key.startswith("extract:") for key in saved.checkpoint["incremental_incomplete_units"])
    assert not saved.checkpoint["incremental_inflight_units"]


@pytest.mark.asyncio
async def test_large_projection_recovers_from_durable_units_without_ai(monkeypatch):
    from backend.services import question_preparation_artifacts as artifacts
    from backend.services.task_execution import stop_task_run
    owner, task, operation, pipeline = await setup(monkeypatch)
    monkeypatch.setattr(artifacts, "MAX_QUESTION_PREPARATION_ARTIFACT_BYTES", 16_000)
    monkeypatch.setattr(inc, "_chunk_problem_text", lambda *_: ["1", "2"])
    extract = AsyncMock(side_effect=lambda text, *a, **kw: inc._rows([
        row(text, stem="A" * 4_500, criterion="Teacher rubric.", reference_answer="Teacher answer.")]))
    monkeypatch.setattr(inc, "_extract_problems_call", extract)
    result = await pipeline.run()
    saved = workflow_repository.get_operation(operation.operation_id, owner_id=owner)
    assert len(result) == 2 and len(saved.checkpoint["incremental_unit_artifacts"]) == 2
    assert saved.checkpoint["incremental_view_from_units"] is True
    assert saved.checkpoint["questions_extracted_artifact_id"] is None
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    stop_task_run(task_id=task, owner_id=owner, job_id=saved.id, expected_revision=workflow.workflow_revision)
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    extract.side_effect = AssertionError("Manual recovery must never call AI")
    response = await api.manually_complete_question_preparation(task, saved.id,
        api.ManualQuestionPreparationRequest(expected_workflow_revision=workflow.workflow_revision),
        current=SimpleNamespace(id=owner))
    assert response["status"] == "ready_for_review"
    rows = task_facade.get_task(task_id=task, owner_id=owner, full=True)["problem_data"]
    assert [value["number"] for value in rows.values()] == ["1", "2"]
    assert all(value["stem"] == "A" * 4_500 for value in rows.values())
    assert extract.await_count == 2


@pytest.mark.asyncio
async def test_source_gap_without_proven_boundaries_saves_text_but_cannot_generate(monkeypatch):
    from backend.domain.errors import RecognitionError
    owner, _, operation, pipeline = await setup(monkeypatch)
    async def read(source, **kwargs):
        draft, text, payload = source
        return draft, text, {**payload, "source_error": "recognition_response_invalid",
            "recognition": {"coverage": {"processed_pages": [7, 9], "failed_pages": [8]}}}
    pipeline.read_source = read
    monkeypatch.setattr(inc, "_extract_problems_call", AsyncMock(return_value=inc._rows([row(1)])))
    generate = AsyncMock(side_effect=AssertionError("An unbounded partial source cannot unlock generation"))
    monkeypatch.setattr(inc, "generate_missing_question_materials", generate)
    with pytest.raises(RecognitionError):
        await pipeline.run()
    _, rows = persisted(owner, operation)
    assert rows["q1"]["stem"] == "Explain item 1."
    assert any(issue["code"] == "recognition_partial" for issue in rows["q1"]["preparation_issues"])
    assert not rows["q1"].get("reference_answer") and not rows["q1"].get("criterion")
    generate.assert_not_awaited()
