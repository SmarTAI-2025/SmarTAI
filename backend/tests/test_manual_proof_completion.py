"""Exercise manual completion through the durable worker and real persistence."""
import json
from types import SimpleNamespace

import pytest

from backend.api import task_preparation as api
from backend.db import assignment_repository, workflow_repository
from backend.services import task_facade
from backend.services.math_grading_context import problem_from_question
from backend.services.rubric_drafts import saved_draft
from backend.tests.test_auxiliary_operation_recovery import _claim_auxiliary_operation
from backend.tests.test_proof_rubric import STEM, REFERENCE, audit, initial
from backend.tests.test_question_material_format_repair import Provider
from backend.tests.test_task_background_workflows import _BackgroundTasks, _seed_task
from backend.tools.structured_llm import PermanentLLMError


async def complete(monkeypatch, provider, *, stem=STEM):
    owner, task = _seed_task()
    assignment_repository.add_question(task, teacher_id=owner, q_id="q1", order_index=0,
        number="1", type="推理题", stem=stem, criterion="", max_score=10, source={})
    registry = SimpleNamespace(list_configs=lambda: [dict(provider_id=provider.provider_id, enabled=True)],
                               get=lambda _: provider, pick_default=lambda: provider)
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: registry)
    workflow_repository.update_workflow(task, owner_id=owner, question_recognition_provider_id=provider.provider_id)
    response = await api.confirm_ai_completion(task_id=task,
        request=api.ConfirmAICompletionRequest(target_ids=["q1:reference_answer", "q1:criterion"],
            expected_workflow_revision=workflow_repository.get_workflow(task, owner_id=owner).workflow_revision),
        background_tasks=_BackgroundTasks(), current=SimpleNamespace(id=owner), registry=registry)
    assert isinstance(response, dict), response.body
    assert response["status"] == "started", response
    await api.run_durable_ai_completion(_claim_auxiliary_operation(owner, response["job_id"]))
    job = workflow_repository.get_operation(response["job_id"], owner_id=owner)
    data = problem_from_question(assignment_repository.list_questions(task, teacher_id=owner)[0])
    return job, data


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["", "（10分）", " (10 points)"])
async def test_manual_completion_repairs_only_failed_rubric(monkeypatch, marker):
    issue = "square: split the square computation and the independent nonidentity proof"
    provider = Provider([initial(), audit("inconsistent", issue), initial(reference=False), audit()])
    job, data = await complete(monkeypatch, provider, stem=STEM + marker)
    assert job.status == "done" and not job.payload["skipped_target_ids"]
    assert data.reference_answer == REFERENCE and saved_draft(data) is not None
    assert len(provider.calls) == 4
    retry = json.loads(provider.calls[2][-1].content.removeprefix("[Generation request]\n"))
    assert [t["target_id"] for t in retry["requested_targets"]] == ["q1:criterion"]
    assert retry["known_problems"][0]["existing_reference_answer"] == REFERENCE
    assert issue in retry["validation_feedback"]["q1:criterion"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["semantic", "provider", "format"])
async def test_exhausted_manual_repair_preserves_valid_reference(monkeypatch, failure):
    responses = [initial(), audit("inconsistent", "Separate two independent goals.")]
    responses += ([initial(reference=False), audit("inconsistent"), initial(reference=False), audit("inconsistent")]
                  if failure == "semantic" else [PermanentLLMError("provider_quota_exceeded")]
                  if failure == "provider" else ["invalid", "invalid"])
    provider = Provider(responses)
    job, data = await complete(monkeypatch, provider)
    assert job.status == "done"
    assert job.payload["applied_target_ids"] == ["q1:reference_answer"]
    assert job.payload["skipped_target_ids"] == ["q1:criterion"]
    assert data.reference_answer == REFERENCE and not data.criterion
    assert saved_draft(data) is None
    assert len(provider.calls) == {"semantic": 6, "provider": 3, "format": 4}[failure]


@pytest.mark.asyncio
async def test_manual_completion_normal_path_has_no_extra_calls(monkeypatch):
    provider = Provider([initial(), audit()])
    job, data = await complete(monkeypatch, provider)
    assert job.status == "done" and len(provider.calls) == 2
    assert data.reference_answer == REFERENCE and saved_draft(data) is not None


@pytest.mark.asyncio
async def test_accepted_rubric_is_preserved_when_only_reference_is_missing(monkeypatch):
    reference = json.loads(initial())["candidates"][1]
    provider = Provider([initial(reference=False), audit(), json.dumps({"candidates": [reference]})])
    job, data = await complete(monkeypatch, provider)
    assert job.status == "done" and not job.payload["skipped_target_ids"]
    assert data.reference_answer == REFERENCE and saved_draft(data) is not None
    assert len(provider.calls) == 3
    request = json.loads(provider.calls[2][-1].content.removeprefix("[Generation request]\n"))
    assert [t["target_id"] for t in request["requested_targets"]] == ["q1:reference_answer"]
    assert request["known_problems"][0]["existing_criterion"] == data.criterion


@pytest.mark.asyncio
async def test_no_accepted_material_is_still_a_failed_operation(monkeypatch):
    provider = Provider([PermanentLLMError("provider_quota_exceeded")])
    job, data = await complete(monkeypatch, provider)
    assert job.status == "error" and len(provider.calls) == 1
    assert not data.reference_answer and not data.criterion and saved_draft(data) is None
