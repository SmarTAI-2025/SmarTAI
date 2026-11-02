"""Selected questions define completeness; each main material role has one source."""
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers

from backend.agents import incremental_question_preparation as inc
from backend.api import task_preparation as api
from backend.db import workflow_repository
from backend.domain.errors import ValidationError
from backend.tests.test_incremental_question_preparation import setup, row, persisted
from backend.services import task_facade
from backend.tests.test_question_preparation_recovery import _RecoveryRegistry, _claim
from backend.tests.test_incremental_question_materials import material_setup
from backend.tests.test_task_background_workflows import _seed_task


@pytest.mark.asyncio
@pytest.mark.parametrize("targets,missing", [(["2"], False), (["2"], True), ([], False)])
@pytest.mark.parametrize("paged", [False, True])
async def test_completeness_checks_selected_questions_only(monkeypatch, targets, missing, paged):
    preflight = api.preflight_problem_source
    async def scoped(**kwargs):
        kwargs.update(structure_mode="extract_from_source", recognition_options=json.dumps({"targets": targets}),
                      extraction_hint="题号: " + ", ".join(targets) if targets else "")
        return await preflight(**kwargs)
    monkeypatch.setattr(api, "preflight_problem_source", scoped)
    text = "Question 1\nExplain the first item.\nQuestion 2\nExplain the second item."
    owner, _, operation, pipeline = await setup(monkeypatch, text=text)
    async def read(source, *, on_pages):
        if paged:
            await on_pages([dict(page_number=1, text=text)])
        return source
    pipeline.read_source = read
    async def extract(text, *args, **kwargs):
        return inc._rows([row(2, criterion="Teacher rubric.", reference_answer="Teacher answer.")]) if (
            not missing and "Question 2" in text) else {}
    extraction = AsyncMock(side_effect=extract)
    monkeypatch.setattr(inc, "_extract_problems_call", extraction)
    generation = AsyncMock(side_effect=AssertionError("Missing/unselected questions must not generate materials"))
    monkeypatch.setattr(inc, "generate_missing_question_materials", generation)
    if missing or not targets and paged:
        with pytest.raises(ValidationError) as error:
            await pipeline.run()
        assert error.value.code == "question_targets_incomplete"
        assert extraction.await_count == 3
    else:
        result = await pipeline.run()
        assert [item["number"] for item in result.values()] == ["2"]
        assert extraction.await_count == 1
    _, saved = persisted(owner, operation)
    if targets:
        assert [item["number"] for item in saved.values()] == ["2"]
        assert bool(saved["q1"]["stem"]) is not missing
    generation.assert_not_awaited()


async def source(owner, task, registry, role, text, kind="upload"):
    upload = UploadFile(io.BytesIO(text.encode()), filename=role + ".txt",
                        headers=Headers({"content-type": "text/plain"}))
    result = await api.preflight_problem_source(task_id=task, file=None if kind == "inline_text" else upload,
        inline_text=text if kind == "inline_text" else None, library_material_id=None, stored_file_id=None,
        structure_mode="organized", role=role, extraction_hint="", save_to_library=kind == "library",
        recognition_provider_id="test-provider", current=SimpleNamespace(id=owner), registry=registry)
    assert isinstance(result, dict), getattr(result, "body", result)
    if kind == "library":
        result = await api.preflight_problem_source(task_id=task, file=None, inline_text=None,
            library_material_id=result["source"]["library_material_id"], stored_file_id=None,
            structure_mode="organized", role=role, extraction_hint="", save_to_library=False,
            recognition_provider_id="test-provider", current=SimpleNamespace(id=owner), registry=registry)
        assert isinstance(result, dict), getattr(result, "body", result)
    return result["source_token"]


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["problem", "reference_answer", "rubric", "programming_tests"])
@pytest.mark.parametrize("kind", ["upload", "inline_text", "library"])
async def test_duplicate_material_roles_are_rejected_before_queueing(role, kind):
    owner, task = _seed_task()
    registry = _RecoveryRegistry()
    tokens = [await source(owner, task, registry, role, "First source."),
              await source(owner, task, registry, role, "Second source.", kind)]
    if role != "problem":
        tokens.insert(0, await source(owner, task, registry, "problem", "Question text."))
    for reuse in (False, True):
        result = await api._start_question_preparation(task_id=task,
            request=api.StartQuestionPreparationRequest(source_tokens=tokens, expected_workflow_revision=0,
                recognition_provider_id="test-provider"), current=SimpleNamespace(id=owner), registry=registry,
            background_tasks=None, allow_prepared_source_reuse=reuse)
        assert getattr(result, "status_code", None) == 422, result
        assert json.loads(result.body)["error"]["code"] == "question_preparation_source_limit"
        workflow = workflow_repository.get_workflow(task, owner_id=owner)
        assert workflow.active_job_id is None and workflow.workflow_revision == 0


@pytest.mark.asyncio
async def test_one_source_of_each_preparation_role_is_allowed():
    owner, task = _seed_task()
    registry = _RecoveryRegistry()
    tokens = [await source(owner, task, registry, role, f"Material {index}.") for index, role in enumerate(
        ["problem", "reference_answer", "rubric", "programming_tests"])]
    result = await api._start_question_preparation(task_id=task,
        request=api.StartQuestionPreparationRequest(source_tokens=tokens, expected_workflow_revision=0,
            recognition_provider_id="test-provider"), current=SimpleNamespace(id=owner), registry=registry,
        background_tasks=None, allow_prepared_source_reuse=False)
    assert result["status"] == "started", getattr(result, "body", result)


@pytest.mark.asyncio
async def test_frozen_duplicate_question_sources_cannot_run_retry_or_enter_manual(monkeypatch):
    owner, task = _seed_task()
    registry = _RecoveryRegistry()
    tokens = [await source(owner, task, registry, "problem", text) for text in ("First.", "Second.")]
    # An operation published before the limit existed must not bypass it.
    with monkeypatch.context() as before_limit:
        before_limit.setattr(api, "validate_preparation_source_roles", lambda _: None)
        queued = await api._start_question_preparation(task_id=task,
            request=api.StartQuestionPreparationRequest(source_tokens=tokens, expected_workflow_revision=0,
                recognition_provider_id="test-provider"), current=SimpleNamespace(id=owner), registry=registry,
            background_tasks=None, allow_prepared_source_reuse=False)
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: registry)
    extraction = AsyncMock(side_effect=AssertionError("Invalid sources must not call AI"))
    monkeypatch.setattr(inc, "_extract_problems_call", extraction)
    await api.run_durable_question_preparation(_claim(owner, queued["job_id"], "duplicate-sources"))
    failed = workflow_repository.get_operation(queued["job_id"], owner_id=owner)
    assert failed.status == "error"
    revision = workflow_repository.get_workflow(task, owner_id=owner).workflow_revision
    retry = await api.retry_question_preparation(task, failed.id,
        api.RetryQuestionPreparationRequest(expected_workflow_revision=revision), background_tasks=None,
        current=SimpleNamespace(id=owner), registry=registry)
    assert retry.status_code == 422, getattr(retry, "body", retry)
    manual = await api.manually_complete_question_preparation(task, failed.id,
        api.ManualQuestionPreparationRequest(expected_workflow_revision=revision), current=SimpleNamespace(id=owner))
    assert manual.status_code == 422, getattr(manual, "body", manual)
    extraction.assert_not_awaited()


@pytest.mark.asyncio
async def test_frozen_duplicate_sources_with_saved_snapshot_cannot_bypass_manual_limit(monkeypatch):
    from backend.services.task_execution import stop_task_run
    with monkeypatch.context() as before_limit:
        before_limit.setattr(api, "validate_preparation_source_roles", lambda _: None)
        before_limit.setattr(inc, "validate_preparation_source_roles", lambda _: None)
        owner, task, operation, pipeline = await material_setup(monkeypatch, [("problem", "Second source")])
    draft = pipeline.sources[0][0]
    await pipeline.finish(pipeline._chunk_key(draft, "Question", 0, ""), inc._rows([row(1)]))
    saved = workflow_repository.get_operation(operation.operation_id, owner_id=owner)
    assert saved.checkpoint["questions_extracted_artifact_id"]
    assert not saved.checkpoint.get("incremental_view_from_units")
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    stop_task_run(task_id=task, owner_id=owner, job_id=operation.operation_id, expected_revision=workflow.workflow_revision)
    revision = workflow_repository.get_workflow(task, owner_id=owner).workflow_revision
    result = await api.manually_complete_question_preparation(task, operation.operation_id,
        api.ManualQuestionPreparationRequest(expected_workflow_revision=revision), current=SimpleNamespace(id=owner))
    assert result.status_code == 422
    assert json.loads(result.body)["error"]["code"] == "question_preparation_source_limit"
    assert workflow_repository.get_workflow(task, owner_id=owner).workflow_revision == revision
