"""Independent review regressions use real artifacts and retry operations."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agents import incremental_question_preparation as inc
from backend.api import task_preparation as api
from backend.db import workflow_repository
from backend.domain.errors import RecognitionError, ValidationError
from backend.progress.tracker import ProgressReporter
from backend.services import task_facade
from backend.tests.test_incremental_question_materials import material_setup, parsed_material
from backend.tests.test_incremental_question_preparation import row, setup, persisted
from backend.tests.test_question_preparation_recovery import _claim, _RecoveryRegistry


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["two_known_targets", "unknown_after_failure"])
@pytest.mark.parametrize("role,target,other", [
    ("reference_answer", "reference_answer", "criterion"),
    ("rubric", "criterion", "reference_answer"),
])
async def test_unlabelled_material_is_not_attached_to_only_observed_question(monkeypatch, mode, role, target, other):
    if mode == "two_known_targets":
        original = api.preflight_problem_source

        async def targeted(**kwargs):
            if kwargs["role"] == "problem":
                kwargs["recognition_options"] = json.dumps({"targets": ["1", "2"]})
            return await original(**kwargs)

        monkeypatch.setattr(api, "preflight_problem_source", targeted)
    literal = "Unlabelled teacher material."
    owner, _, operation, pipeline = await material_setup(monkeypatch, [(role, literal)])
    if mode == "unknown_after_failure":
        async def partial(source, **kwargs):
            draft, text, payload = source
            return draft, text, {**payload, "source_error": "ocr_incomplete"} if draft.role == "problem" else payload

        pipeline.read_source = partial
    monkeypatch.setattr(inc, "_extract_problems_call", AsyncMock(return_value=inc._rows([
        {**row(1), other: "Already supplied other field."}])))
    monkeypatch.setattr(inc, "structured_llm_call", AsyncMock(return_value=parsed_material("", literal)))
    monkeypatch.setattr(inc, "generate_missing_question_materials", AsyncMock(side_effect=AssertionError("No generation expected")))
    with pytest.raises((ValidationError, RecognitionError)):
        await pipeline.run()
    _, rows = persisted(owner, operation)
    assert not next(item for item in rows.values() if item["number"] == "1").get(target)
    assert any(not item["number"] and item.get(target) == literal for item in rows.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("role,target,other", [
    ("reference_answer", "reference_answer", "criterion"),
    ("rubric", "criterion", "reference_answer"),
])
async def test_unnumbered_question_does_not_hide_unassigned_teacher_material(monkeypatch, role, target, other):
    literal = "Literal teacher material without a question number."
    owner, _, operation, pipeline = await material_setup(monkeypatch, [(role, literal)])
    monkeypatch.setattr(inc, "_extract_problems_call", AsyncMock(return_value=inc._rows([
        {**row("", stem="Explain the source proposition."), other: "Already supplied other field."}])))
    monkeypatch.setattr(inc, "structured_llm_call", AsyncMock(return_value=parsed_material("", literal)))
    monkeypatch.setattr(inc, "generate_missing_question_materials", AsyncMock(return_value=[]))
    with pytest.raises(ValidationError):
        await pipeline.run()
    _, rows = persisted(owner, operation)
    assert any(item.get(target) == literal for item in rows.values())
    assert any(item["stem"] == "Explain the source proposition." for item in rows.values())
    assert all(not item["number"] for item in rows.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("incomplete_unit", [False, True])
async def test_changed_model_copy_preserves_source_boundary_evidence(monkeypatch, incomplete_unit):
    owner, task, operation, pipeline = await setup(monkeypatch)
    source = pipeline.sources[0][0].source_token
    key = "extract:" + source + ":0:unit"
    await pipeline.finish(key, inc._rows([row(1, criterion="Teacher rubric.", reference_answer="Teacher answer.")]),
        error=ValidationError("Partial response", code="recognition_response_invalid") if incomplete_unit else None)
    plan = {source: dict(keys=[key], source_complete=True, invalid_numbers=["1"])}
    await pipeline._checkpoint(incremental_source_invalidations={source: ["1"]}, incremental_source_plans=plan)
    await pipeline.finish("invalidation:" + source)
    _, initial = persisted(owner, operation)
    assert any(issue["code"] == "recognition_partial" for issue in initial["q1"]["preparation_issues"])
    task_facade._fail_operation(task, owner, operation.operation_id, operation.attempt,
        "question_targets_incomplete", expected_lease_token=operation.lease_token)
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    changed = _RecoveryRegistry("changed-model")
    retried = await api.retry_question_preparation(task_id=task, job_id=operation.operation_id,
        request=api.RetryQuestionPreparationRequest(expected_workflow_revision=workflow.workflow_revision,
            use_current_configuration=True), background_tasks=None, current=SimpleNamespace(id=owner), registry=changed)
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: changed)
    next_operation = _claim(owner, retried["job_id"], "changed")
    inputs = api._rehydrate_question_preparation_inputs(next_operation)
    next_pipeline = inc.IncrementalQuestionPreparation(operation=next_operation, sources=inputs["sources"],
        provider=inputs["route"].provider, provider_id=inputs["recognition_provider_id"],
        reporter=ProgressReporter(next_operation.operation_id), score_policy=inputs["score_policy"], read_source=AsyncMock())
    await next_pipeline.copy_retry_units()
    copied_operation, copied = persisted(owner, next_operation)
    assert copied_operation.checkpoint["incremental_source_plans"] == plan
    assert (key in copied_operation.checkpoint["incremental_incomplete_units"]) is incomplete_unit
    assert any(issue["code"] == "recognition_partial" for issue in copied["q1"]["preparation_issues"])
    # Reopening the retry after a stop uses the same deterministic saved view.
    from backend.services.question_preparation_manual import recover_manual_question_packages
    recovered, _ = recover_manual_question_packages(copied_operation)
    assert recovered["q1"]["stem"] == "Explain item 1."
    assert any(issue["code"] == "recognition_partial" for issue in recovered["q1"]["preparation_issues"])
