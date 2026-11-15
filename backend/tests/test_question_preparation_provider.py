"""Question-model selection persists for this task and leaves later stages independent."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from backend.db import assignment_repository, workflow_repository
from backend.services import task_facade, stage_provider_routing
from backend.services.math_grading_context import problem_from_question
from backend.services.rubric_drafts import saved_draft
from backend.tests.test_frozen_part_rubric import _persist_method_question
from backend.tests.test_question_material_format_repair import Provider
from backend.tests.test_task_stage_contract import _client
from backend.tools.structured_llm import PermanentLLMError


def scenario(monkeypatch):
    args = _persist_method_question()
    original, = assignment_repository.list_questions(args['task_id'], teacher_id=args['owner_id'])
    prior = saved_draft(problem_from_question(original))
    raw = prior.plan.part_rubric.model_dump(mode='json')
    part = raw['parts'][0]
    del part['steps']
    part['deductions'] = [dict(id='calc', criterion='独立计算错误', deduct_pts=2, source_quote='每错扣2分')]
    part['alternative'] = dict(deduct_per_error=1)
    good = Provider([json.dumps({'part_rubric': raw})])
    bad = SimpleNamespace(provider_id='model-a', ainvoke=AsyncMock(side_effect=PermanentLLMError('provider_quota_exceeded')))
    configs = [dict(provider_id='model-a', enabled=True), dict(provider_id='model-b', enabled=True),
               dict(provider_id='disabled', enabled=False)]
    registry = SimpleNamespace(list_configs=lambda: configs,
        get=lambda key: {'model-a':bad, 'model-b':good, 'disabled':good}.get(key))
    def scoped(owner):
        assert owner == args['owner_id']
        return registry
    monkeypatch.setattr(task_facade, '_registry_for_owner', scoped)
    monkeypatch.setattr(stage_provider_routing, 'resolve_owner_default_provider_id', lambda *_: 'model-b')
    workflow_repository.update_workflow(args['task_id'], owner_id=args['owner_id'], question_recognition_provider_id='model-a')
    text = '按独立计算错误扣分，每错扣2分。其他正确解法给满分，每错扣1分。'
    return args, original, bad, good, text


def test_failed_bound_model_can_be_replaced_for_later_question_edits(monkeypatch):
    args, original, bad, good, text = scenario(monkeypatch)
    client = _client(args['owner_id'])
    url = f"/tasks/{args['task_id']}/problems/q1"
    failed = client.put(url, json={'criterion':text})
    assert failed.status_code == 422
    assert bad.ainvoke.await_count == 1 and not good.calls
    unchanged, = assignment_repository.list_questions(args['task_id'], teacher_id=args['owner_id'])
    assert (unchanged.version, unchanged.criterion, unchanged.source) == (original.version, original.criterion, original.source)
    flow = workflow_repository.get_workflow(args['task_id'], owner_id=args['owner_id'])
    selected = client.put(f"/tasks/{args['task_id']}/question-preparation/provider", json={
        'provider_id': 'model-b', 'expected_workflow_revision': flow.workflow_revision})
    assert selected.status_code == 200, selected.text
    assert not good.calls
    saved = client.put(url, json={'criterion':text, 'expected_workflow_revision': selected.json()['workflow_revision']})
    assert saved.status_code == 200, saved.text
    assert len(good.calls) == 1 and bad.ainvoke.await_count == 1
    stored, = assignment_repository.list_questions(args['task_id'], teacher_id=args['owner_id'])
    assert stored.criterion == text
    assert saved_draft(problem_from_question(stored)).plan.part_rubric.parts[0].deductions[0].deduct_pts == 2
    workflow = workflow_repository.get_workflow(args['task_id'], owner_id=args['owner_id'])
    assert workflow.question_recognition_provider_id == 'model-b'
    assert workflow.submission_recognition_provider_id == flow.submission_recognition_provider_id
    assert workflow.grading_setup == flow.grading_setup
    assert 'rubric_provider_id' not in stored.source.get('presentation', {})


@pytest.mark.parametrize('provider_id', ['another-owner-model', 'disabled'])
def test_unavailable_or_foreign_retry_model_keeps_original_rules(monkeypatch, provider_id):
    args, original, bad, good, text = scenario(monkeypatch)
    flow = workflow_repository.get_workflow(args["task_id"], owner_id=args["owner_id"])
    response = _client(args["owner_id"]).put(f"/tasks/{args['task_id']}/question-preparation/provider",
        json={"provider_id":provider_id, "expected_workflow_revision": flow.workflow_revision})
    assert response.status_code == 422
    assert response.json()['error']['code'] == 'recognition_provider_not_enabled'
    assert not good.calls and not bad.ainvoke.called
    stored, = assignment_repository.list_questions(args['task_id'], teacher_id=args['owner_id'])
    assert (stored.version, stored.criterion, stored.source) == (original.version, original.criterion, original.source)


@pytest.mark.parametrize('state', ['stale', 'busy', 'later_stage'])
def test_model_selection_rechecks_task_state(monkeypatch, state):
    args, original, bad, good, text = scenario(monkeypatch)
    flow = workflow_repository.get_workflow(args['task_id'], owner_id=args['owner_id'])
    patch = {'workflow_revision': flow.workflow_revision + 1} if state == 'stale' else (
        {'active_job_id': 'another-job'} if state == 'busy' else {'presentation_status': 'submissions_ready'})
    workflow_repository.update_workflow(args['task_id'], owner_id=args['owner_id'], **patch)
    response = _client(args['owner_id']).put(f"/tasks/{args['task_id']}/question-preparation/provider",
        json={'provider_id':'model-b', 'expected_workflow_revision': flow.workflow_revision})
    assert response.status_code == 409, response.text
    assert workflow_repository.get_workflow(args['task_id'], owner_id=args['owner_id']).question_recognition_provider_id == 'model-a'
    assert not good.calls and not bad.ainvoke.called


def test_retired_per_save_override_is_rejected(monkeypatch):
    args, original, bad, good, text = scenario(monkeypatch)
    response = _client(args['owner_id']).put(f"/tasks/{args['task_id']}/problems/q1",
        json={'criterion':text, 'rubric_provider_id':'model-b'})
    assert response.status_code == 422
    assert not good.calls and not bad.ainvoke.called


@pytest.mark.asyncio
@pytest.mark.parametrize("ocr_only", [False, True])
async def test_ai_completion_freezes_selected_question_model_not_global_default(monkeypatch, ocr_only):
    from backend.api import task_preparation
    from backend.tests.test_task_background_workflows import _seed_task, _BackgroundTasks
    owner, task = _seed_task(with_question=True, criterion='')
    providers = {key: SimpleNamespace(provider_id=key) for key in ['model-a', 'model-b']}
    registry = SimpleNamespace(list_configs=lambda: [dict(provider_id=key, enabled=True) for key in providers],
        get=providers.get)
    monkeypatch.setattr(task_facade, '_registry_for_owner', lambda _: registry)
    monkeypatch.setattr(stage_provider_routing, 'resolve_owner_default_provider_id', lambda *_: 'model-a')
    if ocr_only:
        workflow_repository.update_workflow(task, owner_id=owner, question_recognition_provider_id='ocr:baidu_unlimited_ocr:fixture')
    response = _client(owner).put(f'/tasks/{task}/question-preparation/provider',
        json={'provider_id':'model-b', 'expected_workflow_revision':workflow_repository.get_workflow(task, owner_id=owner).workflow_revision})
    assert response.status_code == 200, response.text
    if ocr_only:
        workflow_repository.update_workflow(task, owner_id=owner, question_recognition_provider_id='ocr:baidu_unlimited_ocr:fixture')
    result = await task_preparation.confirm_ai_completion(task_id=task,
        request=task_preparation.ConfirmAICompletionRequest(target_ids=['q1:criterion'],
            expected_workflow_revision=workflow_repository.get_workflow(task, owner_id=owner).workflow_revision),
        background_tasks=_BackgroundTasks(), current=SimpleNamespace(id=owner), registry=registry)
    assert result['status'] == 'started', result
    operation = workflow_repository.get_operation(result['job_id'], owner_id=owner)
    assert operation.payload['provider_id'] == ('model-a' if ocr_only else 'model-b')


def test_question_model_is_owner_scoped(monkeypatch):
    from backend.tests.test_task_background_workflows import _seed_task
    args, original, bad, good, text = scenario(monkeypatch)
    other, _ = _seed_task()
    response = _client(other).put(f"/tasks/{args['task_id']}/question-preparation/provider",
        json={'provider_id':'model-b', 'expected_workflow_revision':0})
    assert response.status_code == 404
    assert not good.calls and not bad.ainvoke.called
