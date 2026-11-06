"""Advisory retry status cannot abort valid retries or mask required checks."""
import asyncio
from types import SimpleNamespace
from contextlib import nullcontext
import pytest
from tenacity import wait_fixed
from backend.llm import providers, request_timeouts
from backend.progress.tracker import get_or_create_reporter, remove_reporter
from backend.services.execution_control import run_controlled
from backend.tests.test_provider_response_deadlines import MESSAGES, ROUTES, provider_for, scheduler  # noqa: F401
from backend.tools import structured_llm

@pytest.mark.asyncio
@pytest.mark.parametrize('route,response_only', [(route, True) for route in ROUTES] +
                         [(route, False) for route in ROUTES if route != 'gemini_proxy'])
@pytest.mark.parametrize('retry_kind', ['rate', 'ordinary'])
@pytest.mark.parametrize('outcome', ['error', 'hang'])
async def test_rate_wait_persistence(route, response_only, retry_kind, outcome, scheduler, monkeypatch):
    monkeypatch.setattr(request_timeouts, '_LOCAL_STEP_SECONDS', .02)
    retry = structured_llm._invoke_with_rate_retry if retry_kind == 'rate' else structured_llm._ainvoke_with_retry
    invoke_with_retry = structured_llm.ainvoke_with_rate_retry if retry_kind == 'rate' else structured_llm.ainvoke_with_retry
    monkeypatch.setattr(retry.retry, 'wait', wait_fixed(0))
    calls, saves = [], []
    async def invoke(options):
        calls.append(options)
        if len(calls) == 1:
            raise providers.ProviderRequestError('provider_rate_limited', status_code=429)
        return SimpleNamespace(content='complete')
    async def save(snapshot):
        saves.append(snapshot['model_waits'])
        if outcome == 'hang':
            await asyncio.Event().wait()
        raise RuntimeError('synthetic status persistence failure')
    job_id = 'four-pr-retry-status'
    reporter = get_or_create_reporter(job_id)
    try:
        async with provider_for(route, invoke, monkeypatch) as provider:
            with request_timeouts.response_timeout_scope(.1, response_only=True) if response_only else nullcontext():
                result = await asyncio.wait_for(run_controlled(
                    job_id, lambda: False,
                    invoke_with_retry(provider, MESSAGES), persist_progress=save,
                ), 1)
        assert result.content == 'complete' and len(calls) == 2
        assert saves[0] and saves[-1] == []
        assert not (await reporter.snapshot()).model_waits
        assert scheduler.active == scheduler.host_capacity.active == 0
        assert scheduler.proxy_slots._value == 50
    finally:
        remove_reporter(job_id)
