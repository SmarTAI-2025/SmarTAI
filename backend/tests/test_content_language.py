"""Language defaults are task scoped; generated prose never controls recognition."""
import json
from types import SimpleNamespace

import pytest

from backend.agents.ingest_agent import generate_missing_question_materials
from backend.api import task_preparation
from backend.db import workflow_repository
from backend.services import task_facade
from backend.services.workflow_inputs import question_inputs, question_generation_language
from backend.tests.test_grading_source_readiness import _seed_legacy_structured_case
from backend.tests.test_task_stage_contract import _client
from backend.tests.test_task_background_workflows import _seed_task, _BackgroundTasks
from backend.tests.test_question_material_format_repair import Provider, valid
from backend.tests.test_question_preparation_recovery import _queue_question_preparation, _RecoveryRegistry, _claim, _install_incremental_preparer


@pytest.mark.parametrize("language,opposite", [("en", "zh"), ("zh", "en")])
def test_first_entry_default_is_durable_and_does_not_save_or_overwrite_grading(language, opposite):
    seeded = _seed_legacy_structured_case()
    owner, task_id = seeded["owner_id"], seeded["assignment_id"]
    saved = seeded["workflow"].grading_setup
    workflow = workflow_repository.update_workflow(task_id, owner_id=owner, grading_setup=None, grading_setup_fingerprint=None)
    client = _client(owner)
    path = f"/tasks/{task_id}/content-language/default"
    assert client.put(path, json={"language": language}).json() == {"language": language}
    assert client.put(path, json={"language": opposite}).json() == {"language": language}
    response = client.get(f"/tasks/{task_id}/grading-setup").json()
    assert response["suggested_setup"]["feedback_language"] == language
    assert response["grading_setup"] is None
    assert response["workflow_revision"] == workflow.workflow_revision
    # A later explicit feedback choice remains independent of generation defaults.
    saved["feedback_language"] = opposite
    assert client.put(f"/tasks/{task_id}/grading-setup", json={"expected_workflow_revision": workflow.workflow_revision, "grading_setup": saved}).status_code == 200
    client.put(path, json={"language": language})
    assert client.get(f"/tasks/{task_id}/grading-setup").json()["grading_setup"]["feedback_language"] == opposite
    assert client.put(path, json={"language": "fr"}).status_code == 422
    other_owner, _ = _seed_task()
    assert _client(other_owner).put(path, json={"language": opposite}).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("language,name", [("en", "English"), ("zh", "Simplified Chinese")])
async def test_generated_prose_language_survives_format_repair_without_rewriting_source(language, name):
    provider = Provider(["invalid JSON", valid()])
    original = {"q1": {"stem": "原文 English $x_1$", "max_score": 1}}
    before = json.dumps(original)
    result = await generate_missing_question_materials(original,
        [{"target_id": "q1:reference_answer", "q_id": "q1", "target": "reference_answer"}],
        6, provider, generation_language=language)
    assert result[0].text_value == "42"
    assert len(provider.calls) == 2
    for messages in provider.calls:
        assert f"in {name}, regardless of the source language" in messages[0].content
        assert "Do not add a second-language translation" in messages[0].content
        assert "原文 English $x_1$" in messages[1].content
    assert json.dumps(original) == before


@pytest.mark.asyncio
async def test_language_is_frozen_in_worker_retry_and_restored_input(monkeypatch):
    owner, task_id = _seed_task()
    _, queued, _ = await _queue_question_preparation(owner, task_id, generation_language="en")
    calls = []
    languages = []
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _owner: _RecoveryRegistry())
    _install_incremental_preparer(monkeypatch, calls, question_count=1,
        known_failure_question="q1", generation_languages=languages)
    await task_preparation.run_durable_question_preparation(_claim(owner, queued["job_id"], "language-test"))
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner)
    assert languages == ["en"]
    assert question_inputs(task_id=task_id, owner_id=owner)["input"]["generation_language"] == "en"
    assert question_generation_language(task_id=task_id, owner_id=owner) == "en"
    retry = await task_preparation.retry_question_preparation(task_id=task_id, job_id=queued["job_id"],
        request=task_preparation.RetryQuestionPreparationRequest(expected_workflow_revision=workflow.workflow_revision, use_current_configuration=True),
        background_tasks=_BackgroundTasks(), current=SimpleNamespace(id=owner), registry=_RecoveryRegistry("changed-model"))
    assert workflow_repository.get_operation(retry["job_id"], owner_id=owner).payload["generation_language"] == "en"


def test_language_changes_generation_identity_but_legacy_hash_remains_compatible():
    values = dict(ordered_source_inputs=[], logical_input_revision=0, replace_confirmed=False,
        generation_policy="complete_required_materials", score_policy={}, recognition_provider_id="fake",
        provider_configuration_fingerprint="config")
    hashes = [task_preparation._question_preparation_input_hash(**values, generation_language=language) for language in (None, "en", "zh")]
    assert len(set(hashes)) == 3
    assert hashes[0] == task_preparation._question_preparation_input_hash(**values)


def test_historical_task_without_retained_operation_keeps_legacy_language_behavior():
    owner, task_id = _seed_task()
    workflow_repository.update_workflow(task_id, owner_id=owner, extract_job_id="retired-operation")
    assert question_generation_language(task_id=task_id, owner_id=owner) is None
    workflow_repository.initialize_content_language(task_id, owner_id=owner, language="en")
    assert question_generation_language(task_id=task_id, owner_id=owner) == "en"
