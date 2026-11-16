"""Weekly quota reasons survive real grading and HTTP presentation boundaries."""
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, UploadFile
from PIL import Image
from starlette.datastructures import Headers

from backend.llm.registry import SharedPoolLimitError, _GuardedSharedProvider
from backend.services import task_facade
from backend.services.math_grading_context import math_plans_context
from backend.tests.test_shared_weekly_budget import client, metered

CODES = (
    'shared_pool_weekly_limit_reached', 'shared_pool_budget_pending',
    'shared_pool_usage_unavailable', 'shared_pool_billing_unavailable', 'shared_pool_request_too_large',
)


@pytest.mark.asyncio
@pytest.mark.parametrize('code', CODES)
async def test_weekly_failure_survives_skill_question_and_task_without_retry(monkeypatch, code):
    from backend.agents.grading_agent import _grade_single_answer
    from backend.skills import math_grading
    from backend.tests.test_grading_review_triggers import frozen_case
    from backend.tests.test_math_evidence_reliability import raw_provider
    from backend.tools.structured_llm import _classify_exception
    monkeypatch.setattr(math_grading.kb_tool, 'retrieve_for_grading', AsyncMock(return_value=[]))
    problem, answer, plan, _ = frozen_case()
    provider, calls = raw_provider(SharedPoolLimitError(code))
    registry = SimpleNamespace(list_available=lambda: [provider], pick_default=lambda: provider)
    with math_plans_context({problem.q_id: plan}):
        correction = await _grade_single_answer(problem=problem, answer=answer, student_id='synthetic',
            registry=registry, multi_sample_n=1)
    assert len(calls) == 1
    assert correction.synthesis_method == 'all_failed'
    assert correction.expert_results[0].error_kind == code
    assert '未知错误' not in correction.comment and 'BYOK' in correction.comment
    if code == 'shared_pool_weekly_limit_reached':
        assert '每周一' in correction.comment
    monkeypatch.setattr(task_facade.grading_repository, 'list_results_for_run', lambda _: [
        SimpleNamespace(effective_score=None, ai_expert_results=[e.model_dump() for e in correction.expert_results]),
    ])
    assert task_facade._grading_failure_code(SimpleNamespace(id='run', status='partial_failed', error_message=None)) == code
    assert str(_classify_exception(SharedPoolLimitError(code))) == code


@pytest.mark.asyncio
async def test_source_preflight_reports_weekly_quota_as_429_without_generation(metered):
    from backend.api import task_preparation
    from backend.tests.test_task_background_workflows import _seed_task
    provider, calls, _ = metered
    owner, task = _seed_task()
    provider = _GuardedSharedProvider(provider._provider, owner)
    from backend.services.shared_weekly_budget import reserve, finish
    for _ in range(5):
        charge, _ = reserve(owner, 'gpt-6.1-sol', 0, max_output_tokens=100000)
        finish(owner, charge, usage={'input_tokens': 0, 'output_tokens': 100000, 'total_tokens': 100000,
            'input_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0}})
    registry = SimpleNamespace(get=lambda key: provider if key == provider.provider_id else None,
        pick_vision=lambda preferred: preferred,
        list_configs=lambda: [dict(provider_id=provider.provider_id, enabled=True)])
    png = io.BytesIO()
    Image.new('RGB', (32, 32), 'white').save(png, format='PNG')
    with pytest.raises(HTTPException) as error:
        await task_preparation.preflight_problem_source(task_id=task,
            file=UploadFile(io.BytesIO(png.getvalue()), filename='source.png', headers=Headers({'content-type': 'image/png'})),
            library_material_id=None, stored_file_id=None, inline_text=None, structure_mode='organized', role='problem',
            extraction_hint='', save_to_library=False, recognition_provider_id=provider.provider_id,
            current=SimpleNamespace(id=owner), registry=registry)
    assert error.value.status_code == 429
    assert error.value.detail['code'] == 'shared_pool_weekly_limit_reached'
    assert not any(name == 'create' for name, _ in calls)
