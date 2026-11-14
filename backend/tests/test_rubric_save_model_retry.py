"""Explicit model recovery changes one edit operation, never the task binding."""
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


def test_failed_bound_model_can_be_replaced_for_one_save(monkeypatch):
    args, original, bad, good, text = scenario(monkeypatch)
    client = _client(args['owner_id'])
    url = f"/tasks/{args['task_id']}/problems/q1"
    failed = client.put(url, json={'criterion':text})
    assert failed.status_code == 422
    assert bad.ainvoke.await_count == 1 and not good.calls
    unchanged, = assignment_repository.list_questions(args['task_id'], teacher_id=args['owner_id'])
    assert (unchanged.version, unchanged.criterion, unchanged.source) == (original.version, original.criterion, original.source)
    saved = client.put(url, json={'criterion':text, 'rubric_provider_id':'model-b'})
    assert saved.status_code == 200, saved.text
    assert len(good.calls) == 1 and bad.ainvoke.await_count == 1
    stored, = assignment_repository.list_questions(args['task_id'], teacher_id=args['owner_id'])
    assert stored.criterion == text
    assert saved_draft(problem_from_question(stored)).plan.part_rubric.parts[0].deductions[0].deduct_pts == 2
    workflow = workflow_repository.get_workflow(args['task_id'], owner_id=args['owner_id'])
    assert workflow.question_recognition_provider_id == 'model-a'
    assert 'rubric_provider_id' not in stored.source.get('presentation', {})


@pytest.mark.parametrize('provider_id', ['another-owner-model', 'disabled'])
def test_unavailable_or_foreign_retry_model_keeps_original_rules(monkeypatch, provider_id):
    args, original, bad, good, text = scenario(monkeypatch)
    response = _client(args['owner_id']).put(f"/tasks/{args['task_id']}/problems/q1",
        json={'criterion':text, 'rubric_provider_id':provider_id})
    assert response.status_code == 422
    assert response.json()['error']['code'] == 'recognition_provider_not_enabled'
    assert not good.calls and not bad.ainvoke.called
    stored, = assignment_repository.list_questions(args['task_id'], teacher_id=args['owner_id'])
    assert (stored.version, stored.criterion, stored.source) == (original.version, original.criterion, original.source)
