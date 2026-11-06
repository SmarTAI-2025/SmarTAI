"""Status persistence must not discard or replay completed model requests."""
import asyncio
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from tenacity import wait_fixed

from backend.config import settings
from backend.llm import providers, request_timeouts
from backend.progress.recognition_activity import observe_recognition
from backend.progress.tracker import get_or_create_reporter, remove_reporter
from backend.services.execution_control import run_controlled
from backend.services.workflow_deadlines import processing_budget, run_owned_work
from backend.tests.test_provider_response_deadlines import MESSAGES, ROUTES, provider_for, scheduler  # noqa: F401
from backend.tests.test_workflow_gemini_cleanup import workflow_call
from backend.tools import structured_llm


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("outcome", ["success", "failure"])
async def test_workflow_status_save_hang_preserves_model_outcome(route, outcome, scheduler, monkeypatch):
    monkeypatch.setattr(request_timeouts, "_LOCAL_STEP_SECONDS", .02)
    monkeypatch.setattr(structured_llm._ainvoke_with_retry.retry, "wait", wait_fixed(0))
    calls = []
    saving, released = asyncio.Event(), asyncio.Event()
    original = providers.ProviderRequestError("provider_authentication_failed", status_code=403)

    async def invoke(options):
        calls.append(options)
        if outcome == "failure":
            raise original
        return SimpleNamespace(content="complete")

    async def save(snapshot):
        if calls:
            saving.set()
            try:
                await asyncio.Event().wait()
            finally:
                released.set()

    job_id = "status-save-regression"
    reporter = get_or_create_reporter(job_id)
    operation = SimpleNamespace(operation_id=job_id, operation_type="submission_recognition", update_progress=save)
    try:
        async with observe_recognition(operation):
            async with provider_for(route, invoke, monkeypatch) as provider:
                call = workflow_call(structured_llm.ainvoke_with_retry(provider, MESSAGES))
                if outcome == "success":
                    assert (await asyncio.wait_for(call, 1)).content == "complete"
                else:
                    with pytest.raises(structured_llm.PermanentLLMError, match="provider_authentication_failed") as error:
                        await asyncio.wait_for(call, 1)
                    assert error.value.__cause__ is original
        assert saving.is_set() and released.is_set()
        assert len(calls) == 1
        assert scheduler.active == scheduler.host_capacity.active == 0
        assert scheduler.proxy_slots._value == 50
        assert not (await reporter.snapshot()).recognition_requests
    finally:
        remove_reporter(job_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("outcome", ["error", "hang"])
@pytest.mark.parametrize("response_only", [False, True])
async def test_rate_wait_status_save_does_not_abort_retry(route, outcome, response_only, scheduler, monkeypatch):
    monkeypatch.setattr(request_timeouts, "_LOCAL_STEP_SECONDS", .02)
    monkeypatch.setattr(settings, "llm_timeout", .01)
    monkeypatch.setattr(structured_llm._ainvoke_with_retry.retry, "wait", wait_fixed(0))
    calls, saves = [], []

    async def invoke(options):
        calls.append(options)
        if len(calls) == 1:
            raise providers.ProviderRequestError("provider_rate_limited", status_code=429)
        return SimpleNamespace(content="complete")

    async def save(snapshot):
        saves.append(snapshot["model_waits"])
        if outcome == "hang":
            await asyncio.Event().wait()
        raise RuntimeError("synthetic status persistence failure")

    job_id = "retry-status-save"
    reporter = get_or_create_reporter(job_id)
    try:
        async with provider_for(route, invoke, monkeypatch) as provider:
            async with processing_budget(job_id, kind="grading"):
                with request_timeouts.response_timeout_scope(.1, response_only=True) if response_only else nullcontext():
                    result = await asyncio.wait_for(run_owned_work(run_controlled(
                        job_id, lambda: False, structured_llm.ainvoke_with_retry(provider, MESSAGES),
                        persist_progress=save,
                    )), 1)
        assert result.content == "complete" and len(calls) == 2
        assert saves[0] and saves[-1] == []
        assert not (await reporter.snapshot()).model_waits
        assert scheduler.active == scheduler.host_capacity.active == 0
        assert scheduler.proxy_slots._value == 50
    finally:
        remove_reporter(job_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("stage", ["queued", "responding"])
async def test_status_save_before_request_has_its_own_deadline(route, stage, scheduler, monkeypatch):
    monkeypatch.setattr(request_timeouts, "_LOCAL_STEP_SECONDS", .02)
    # Advisory persistence has its own allowance, even if the configured
    # model-response budget is shorter. No model call has started yet.
    monkeypatch.setattr(settings, "llm_timeout", .01)
    calls, blocked = [], []

    async def invoke(options):
        calls.append(options)
        return SimpleNamespace(content="complete")

    async def save(snapshot):
        if any(row["state"] == stage for row in snapshot["recognition_requests"]):
            blocked.append(stage)
            await asyncio.Event().wait()

    job_id = "status-before-request"
    get_or_create_reporter(job_id)
    operation = SimpleNamespace(operation_id=job_id, operation_type="question_preparation", update_progress=save)
    try:
        async with observe_recognition(operation):
            async with provider_for(route, invoke, monkeypatch) as provider:
                result = await asyncio.wait_for(workflow_call(provider.ainvoke(MESSAGES)), 1)
        assert result.content == "complete"
        assert blocked == [stage] and len(calls) == 1
        assert scheduler.active == scheduler.host_capacity.active == 0
        assert scheduler.proxy_slots._value == 50
    finally:
        remove_reporter(job_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["openai", "gemini_proxy"])
@pytest.mark.parametrize("stage", ["queued", "processing"])
async def test_stop_during_status_save_remains_cancellation(route, stage, scheduler, monkeypatch):
    calls = []
    saving, released = asyncio.Event(), asyncio.Event()

    async def invoke(options):
        calls.append(options)
        return SimpleNamespace(content="complete")

    async def save(snapshot):
        queued = any(row["state"] == "queued" for row in snapshot["recognition_requests"])
        if (stage == "queued" and queued) or (stage == "processing" and calls):
            saving.set()
            try:
                await asyncio.Event().wait()
            finally:
                released.set()

    job_id = "status-stop-regression"
    get_or_create_reporter(job_id)
    operation = SimpleNamespace(operation_id=job_id, operation_type="submission_recognition", update_progress=save)
    try:
        async with observe_recognition(operation):
            async with provider_for(route, invoke, monkeypatch) as provider:
                task = asyncio.create_task(workflow_call(provider.ainvoke(MESSAGES)))
                try:
                    await asyncio.wait_for(saving.wait(), 1)
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(task, 1)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        assert released.is_set()
        assert len(calls) == (1 if stage == "processing" else 0)
        assert scheduler.active == scheduler.host_capacity.active == 0
        assert scheduler.proxy_slots._value == 50
    finally:
        remove_reporter(job_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["openai", "gemini_proxy"])
async def test_repeated_stop_keeps_request_cancelled_when_final_status_save_hangs(route, scheduler, monkeypatch):
    monkeypatch.setattr(request_timeouts, "_LOCAL_STEP_SECONDS", .05)
    calls = []
    started, saving, released = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def invoke(options):
        calls.append(options)
        started.set()
        await asyncio.Event().wait()

    async def save(snapshot):
        if calls:
            saving.set()
            try:
                await asyncio.Event().wait()
            finally:
                released.set()

    job_id = "status-repeated-stop"
    get_or_create_reporter(job_id)
    operation = SimpleNamespace(operation_id=job_id, operation_type="submission_recognition", update_progress=save)
    try:
        async with observe_recognition(operation):
            async with provider_for(route, invoke, monkeypatch) as provider:
                task = asyncio.create_task(workflow_call(provider.ainvoke(MESSAGES)))
                try:
                    await asyncio.wait_for(started.wait(), 1)
                    task.cancel()
                    await asyncio.wait_for(saving.wait(), 1)
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(task, 1)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        assert released.is_set() and len(calls) == 1
        assert scheduler.active == scheduler.host_capacity.active == 0
        assert scheduler.proxy_slots._value == 50
    finally:
        remove_reporter(job_id)
