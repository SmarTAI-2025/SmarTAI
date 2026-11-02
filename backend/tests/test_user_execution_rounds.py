"""User actions, unlike refreshes, never replay an earlier failure. No real models."""
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.db import file_repository, workflow_repository
from backend.domain.errors import RecognitionError
from backend.llm.provider_limits import is_daily_quota_error
from backend.llm.providers import ProviderRequestError
from backend.services import question_sources, task_facade
from backend.services.execution_control import run_controlled
from backend.services.stage_provider_routing import StageProviderRoute
from backend.tests.test_recognition_v2_artifacts import setup
from backend.tests.test_task_background_workflows import _Registry, _seed_task
from backend.tests.test_task_execution_control import LimitedProvider
from backend.tools import structured_llm as llm


@pytest.mark.parametrize("metric,expected", [
    ("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", False),
    ("GenerateContentInputTokensPerMinutePerModel-FreeTier", False),
    ("GenerateRequestsPerDayPerProjectPerModel-FreeTier", True),
])
@pytest.mark.parametrize("representation", ["dict", "json", "sdk", "google_sdk"])
def test_violated_metric_takes_precedence_over_prose(metric, expected, representation):
    body = {"error": {"code": 429, "message": "Quota exceeded; daily quota remaining: 493 of 500",
        "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                     "violations": [{"quotaId": metric, "quotaValue": "0"}]}]}}
    value = body if representation == "dict" else json.dumps(body)
    if representation == "sdk":
        value = ProviderRequestError(body["error"]["message"], status_code=429)
        value.details = body["error"]["details"]
    if representation == "google_sdk":
        from google.genai.errors import ClientError
        value = ClientError(429, body)
    assert is_daily_quota_error(value) is expected


@pytest.mark.parametrize("text,expected", [
    ("requests per day quota exceeded", True),
    ("Daily quota exhausted", True),
    ("provider_daily_quota_exceeded", True),
    ("Minute quota exceeded; daily quota remaining: 493", False),
    ("Minute limit reached. Daily quota has not been exhausted.", False),
    ("Quota exceeded. Check your daily quota and billing.", False),
    ("429 RESOURCE_EXHAUSTED", False),
])
def test_unstructured_daily_limit_requires_explicit_exhaustion(text, expected):
    assert is_daily_quota_error(text) is expected


@pytest.mark.asyncio
async def test_daily_limit_stops_only_current_round_and_next_click_reports_new_error(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(llm._ainvoke_with_retry.retry, "sleep", sleep)
    provider = LimitedProvider(ProviderRequestError("provider_daily_quota_exceeded", status_code=429))

    async def limited_round():
        with pytest.raises(llm.PermanentLLMError, match="provider_daily_quota_exceeded"):
            await llm.ainvoke_with_retry(provider, [])
        with pytest.raises(ProviderRequestError, match="provider_daily_quota_exceeded"):
            await llm.ainvoke_with_retry(provider, [])

    await run_controlled("same-parent", lambda: False, limited_round())
    assert provider.calls == 1 and sleep.await_count == 0
    await run_controlled("same-parent", lambda: False, limited_round())
    assert provider.calls == 2 and sleep.await_count == 0
    provider.error = ProviderRequestError("provider_rate_limited", status_code=429)

    async def minute_round():
        with pytest.raises(llm.RateLimitError):
            await llm.ainvoke_with_retry(provider, [])

    await run_controlled("same-parent", lambda: False, minute_round())
    assert provider.calls == 5 and sleep.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["problems", "submissions"])
async def test_saved_pdf_rounds_retry_failures_but_fresh_start_rereads_every_page(tmp_path, monkeypatch, purpose):
    agent, request, data, results = setup(tmp_path, pdf=True, pages=2, initial=[
        "Q1. x = -2", RecognitionError("provider_daily_quota_exceeded"),
    ])
    agent.engine.decision = "keep_visual"
    storage = results.store.storage
    source = file_repository.save_file(storage=storage, owner_id=request.source.owner_id,
        assignment_id=request.source.business_id, kind="problem_source" if purpose == "problems" else "submission_source",
        original_name="original.pdf", content=data, content_type="application/pdf")
    monkeypatch.setattr(question_sources, "get_storage", lambda: storage)

    @asynccontextmanager
    async def engine(**_kwargs):
        yield agent.engine

    monkeypatch.setattr(question_sources, "recognition_engine", engine)

    async def read(scope, retry=None):
        return await question_sources.read_question_source(owner_id=request.source.owner_id,
            task_id=request.source.business_id, content=data, filename="original.pdf", content_type="application/pdf",
            stored_file_id=source.id, route=StageProviderRoute(route_id="selected", kind="llm", provider=SimpleNamespace()),
            registry=None, purpose=purpose, execution_scope=scope, retry_key=retry)

    for retry, code in [(None, "provider_daily_quota_exceeded"), ("click-2", "provider_response_invalid")]:
        agent.engine.initial[1] = RecognitionError(code)
        with pytest.raises(RecognitionError, match=code):
            await read("first-run", retry)
    assert [call.page_number for call in agent.engine.reads] == [1, 2, 2]
    agent.engine.initial[1] = "Q2. Recovered answer"
    recovered = await read("first-run", "click-3")
    assert "Q1" in recovered.text and "Q2" in recovered.text
    assert [call.page_number for call in agent.engine.reads] == [1, 2, 2, 2]
    await read("first-run", "click-3")  # Same HTTP action/worker replay: no duplicate model work.
    assert len(agent.engine.reads) == 4
    agent.engine.initial[0] = "Q1. Newly recognized answer"
    fresh = await read("new-configuration-page-run")
    assert [call.page_number for call in agent.engine.reads] == [1, 2, 2, 2, 1, 2]
    assert "Newly recognized" in fresh.text


@pytest.mark.parametrize("error", ["provider_daily_quota_exceeded", "provider_response_invalid"])
def test_unchanged_submission_configuration_creates_new_parent_and_scope(error):
    owner, task = _seed_task(with_question=True)
    args = dict(task_id=task, owner_id=owner, filename="student.txt", content=b"answer",
                content_type="text/plain", registry=_Registry())
    first = task_facade.queue_task_submission_parsing(**args)
    original = workflow_repository.get_operation(first["job_id"], owner_id=owner)
    task_facade._fail_operation(task, owner, original.id, original.attempt, error)
    second = task_facade.queue_task_submission_parsing(**args)
    fresh = workflow_repository.get_operation(second["job_id"], owner_id=owner)
    assert fresh.id != original.id and fresh.attempt == 1 and fresh.error_code is None
    assert fresh.payload["recognition_execution_scope"] != original.payload["recognition_execution_scope"]
    assert "retry_from" not in fresh.payload and not fresh.checkpoint.get("parsed_results_artifact")
    assert workflow_repository.get_operation(original.id, owner_id=owner).error_code == error


@pytest.mark.asyncio
async def test_retry_regenerates_unreadable_failed_response_without_discarding_good_page(tmp_path, monkeypatch):
    from backend.tests.test_recognition_durable_runs import runner, run
    service, request, data, engine = runner(tmp_path, pdf=True, pages=2, initial=[
        "Q1. Correctly saved text", RecognitionError("provider_daily_quota_exceeded"),
    ])
    first = await run(service, request, data, engine)
    original = workflow_repository.get_operation(first.operation_id, owner_id=request.source.owner_id)
    broken = original.checkpoint["calls"][-1]["artifact_id"]
    load = service.store.load

    async def unreadable(file_id, **kwargs):
        if file_id == broken:
            raise RecognitionError("recognition_artifact_invalid")
        return await load(file_id, **kwargs)

    monkeypatch.setattr(service.store, "load", unreadable)
    engine.initial[1] = "Q2. New model response"
    retried = await service.run(request, data, engine=engine, prompt_version="faithful-reader-v1",
        authorized_owner_id=request.source.owner_id, retry_key="next-user-click")
    assert retried.status == "completed", retried.safe_error_code
    assert [call.page_number for call in engine.reads] == [1, 2, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["initial", "locator", "patch", "empty_recovery"])
async def test_fresh_round_isolates_all_model_evidence_including_locator_and_repairs(tmp_path, kind):
    from backend.tests.test_recognition_calls import setup as call_setup
    from backend.recognition.models import RecognitionPolicyV1
    service, source, request, leaf, options, _ = call_setup(tmp_path, kind)
    options["policy"] = RecognitionPolicyV1(execution_scope="first-user-round")
    await service.record(source, request, leaf, **options)
    assert (await service.lookup(source, request, **options)).status == (
        "not_success" if kind in {"patch", "empty_recovery"} else "hit"
    )
    options["policy"] = RecognitionPolicyV1(execution_scope="next-configuration-round")
    assert (await service.lookup(source, request, **options)).status == "miss"
