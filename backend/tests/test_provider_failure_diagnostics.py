"""A known provider failure must survive the duplicate-submission safeguard."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from backend.agents.question_preparation_agent import _SubmissionSafeProvider, provider_submission_is_uncertain
from backend.domain.errors import DomainError, RecognitionError
from backend.models import JobProgress
from backend.progress.tracker import ProgressReporter
from backend.recognition.budget import RecognitionBudget
from backend.recognition.engine import EngineReadInputV1
from backend.recognition.models import NormalizedRegionV1, RecognitionPolicyV1, RecognitionSourceRefV1
from backend.recognition.runtime import RecognitionCapacity, region_budget_key, run_initial_read
from backend.services.background_errors import provider_failure_diagnostic
from backend.skills.recognition_reader import LLMRecognitionEngine
from backend.tools.structured_llm import ainvoke_with_retry


def gemini_unavailable():
    # The same SDK exception type observed in the production incident.
    from langchain_google_genai.chat_models import GoogleAPIError
    return GoogleAPIError(code=503, response_json={
        "error": {"code": 503, "status": "UNAVAILABLE", "message": "PRIVATE_PROVIDER_BODY"},
    })


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["problems", "submissions"])
async def test_gemini_503_is_visible_without_replaying_uncertain_recognition(purpose):
    provider = SimpleNamespace(provider_id="gemini:fixture", supports_vision=True,
                               ainvoke_vision=AsyncMock(side_effect=gemini_unavailable()))
    engine = LLMRecognitionEngine(provider, route_id="selected", fingerprint="frozen")
    source = RecognitionSourceRefV1(owner_id="owner", scope="submission_source", business_id="fixture",
                                   content_type="image/png", input_sha256="a" * 64)
    policy = RecognitionPolicyV1()
    budget = RecognitionBudget(source, policy, engine.capabilities)
    progress = ProgressReporter("fixture")
    kwargs = dict(source=source, policy=policy, capabilities=engine.capabilities,
                  region_keys=(region_budget_key(1, NormalizedRegionV1()),), budget=budget,
                  capacity=RecognitionCapacity(), progress=progress)
    request = EngineReadInputV1(purpose=purpose, input_mode="page_image", page_number=1,
                               content_type="image/png", payload=b"fixture")
    result = await run_initial_read(engine, request, **kwargs)
    assert result.submission_may_exist
    assert result.candidate.safe_error_code == "provider_upstream_unavailable"
    assert budget.snapshot().pending_calls == 1
    with pytest.raises(RecognitionError):
        await run_initial_read(engine, request, **kwargs)
    provider.ainvoke_vision.assert_awaited_once()
    await progress.set_error("provider_submit_uncertain")
    snapshot = (await progress.snapshot()).model_dump(mode="json")
    assert snapshot["provider_failures"] == [{"code": "provider_upstream_unavailable", "http_status": 503}]
    assert "PRIVATE_PROVIDER_BODY" not in str(snapshot)
    assert JobProgress.model_validate(snapshot).model_dump(mode="json") == snapshot


@pytest.mark.asyncio
async def test_question_generation_keeps_503_detail_and_does_not_auto_retry():
    provider = SimpleNamespace(ainvoke=AsyncMock(side_effect=gemini_unavailable()))
    progress = ProgressReporter("fixture")
    guarded = _SubmissionSafeProvider(provider, reporter=progress)
    with pytest.raises(Exception) as error:
        await ainvoke_with_retry(guarded, "fixture")
    assert provider_submission_is_uncertain(error.value)
    provider.ainvoke.assert_awaited_once()
    snapshot = (await progress.snapshot()).model_dump(mode="json")
    assert snapshot["provider_failures"] == [{"code": "provider_upstream_unavailable", "http_status": 503}]
    assert "PRIVATE_PROVIDER_BODY" not in str(snapshot)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["classification", "publication"])
async def test_diagnostic_failure_cannot_replace_the_uncertain_submission_guard(monkeypatch, failure_point):
    from backend.services import background_errors

    progress = ProgressReporter("fixture")
    if failure_point == "classification":
        def broken_classifier(_exc):
            raise RuntimeError("PRIVATE_DIAGNOSTIC_FAILURE")
        monkeypatch.setattr(background_errors, "provider_failure_diagnostic", broken_classifier)
    else:
        monkeypatch.setattr(progress, "_emit", AsyncMock(side_effect=RuntimeError("PRIVATE_SINK_FAILURE")))
    provider = SimpleNamespace(ainvoke=AsyncMock(side_effect=gemini_unavailable()))
    with pytest.raises(Exception) as error:
        await ainvoke_with_retry(_SubmissionSafeProvider(provider, reporter=progress), "fixture")
    assert provider_submission_is_uncertain(error.value)
    provider.ainvoke.assert_awaited_once()


@pytest.mark.parametrize("wrapper", [
    HTTPException(503, "PRIVATE_APPLICATION_FAILURE"),
    DomainError("PRIVATE_APPLICATION_FAILURE", status_code=503),
])
def test_application_status_is_not_evidence_of_a_provider_failure(wrapper):
    assert provider_failure_diagnostic(wrapper) is None
    wrapper.__cause__ = httpx.ConnectError("PRIVATE_CONNECTION_FAILURE")
    assert provider_failure_diagnostic(wrapper).model_dump() == {
        "code": "provider_unreachable", "http_status": None,
    }


@pytest.mark.asyncio
async def test_successful_generation_does_not_record_a_failure():
    result = object()
    provider = SimpleNamespace(ainvoke=AsyncMock(return_value=result))
    progress = ProgressReporter("fixture")
    assert await _SubmissionSafeProvider(provider, reporter=progress).ainvoke("fixture") is result
    assert (await progress.snapshot()).provider_failures == []
    provider.ainvoke.assert_awaited_once()


@pytest.mark.asyncio
async def test_diagnostics_distinguish_timeout_connection_and_unknown_without_raw_messages():
    progress = ProgressReporter("fixture")
    for exc in [httpx.ReadTimeout("PRIVATE_TIMEOUT"), httpx.ConnectError("PRIVATE_HOST"), RuntimeError("PRIVATE_OTHER")]:
        await progress.record_provider_failure(exc)
    snapshot = (await progress.snapshot()).model_dump(mode="json")
    assert snapshot["provider_failures"] == [
        {"code": "provider_timeout", "http_status": None},
        {"code": "provider_unreachable", "http_status": None},
    ]
    assert "PRIVATE" not in str(snapshot)


@pytest.mark.asyncio
async def test_diagnostic_sink_is_bounded_and_uses_upstream_not_application_status():
    progress = ProgressReporter("fixture")
    events = []
    progress.set_event_sink(lambda _event, payload: events.append(payload))
    for status in range(500, 511):
        from backend.llm.providers import ProviderRequestError
        cause = ProviderRequestError("provider_upstream_unavailable", status_code=status)
        error = RecognitionError("provider_upstream_unavailable", submission_may_exist=True)
        error.__cause__ = cause  # RecognitionError itself has application status 422.
        await progress.record_provider_failure(error)
    await progress.record_provider_failure(error)
    snapshot = (await progress.snapshot()).model_dump(mode="json")
    failures = snapshot["provider_failures"]
    assert [failure["http_status"] for failure in failures] == list(range(503, 511))
    assert events[-1]["provider_failures"] == failures


@pytest.mark.asyncio
async def test_submission_failure_diagnostics_survive_reporter_eviction(monkeypatch):
    from backend.db import workflow_repository
    from backend.progress.tracker import remove_reporter
    from backend.services import task_facade
    from backend.services.workflow_worker import LeasedOperation
    from backend.tests.test_task_background_workflows import _Registry, _seed_task

    owner, task = _seed_task(with_question=True)
    async def failed_read(**kwargs):
        await kwargs["reporter"].record_provider_failure(gemini_unavailable())
        raise RecognitionError("provider_submit_uncertain", submission_may_exist=True)

    monkeypatch.setattr(task_facade, "read_question_source", failed_read)
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _owner: _Registry())
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename="synthetic.txt", content=b"synthetic answer", content_type="text/plain", registry=_Registry())
    row = workflow_repository.claim_operation(queued["job_id"], owner_id=owner, worker_id="test", lease_seconds=60)
    await task_facade.run_durable_submission_recognition(LeasedOperation(row, worker_id="test", lease_seconds=60))
    remove_reporter(row.id)
    state = task_facade.task_state(task_id=task, owner_id=owner)
    assert state["error"] == "provider_submit_uncertain"
    assert state["progress"]["provider_failures"] == [{"code": "provider_upstream_unavailable", "http_status": 503}]
    assert "PRIVATE_PROVIDER_BODY" not in str(state)


@pytest.mark.asyncio
async def test_question_failure_diagnostics_survive_reporter_eviction(monkeypatch):
    from backend.api import task_preparation
    from backend.progress.tracker import remove_reporter
    from backend.services import task_facade
    from backend.tests.test_question_preparation_recovery import _RecoveryRegistry, _claim, _queue_question_preparation
    from backend.tests.test_task_background_workflows import _seed_task

    owner, task = _seed_task()
    _, queued, _ = await _queue_question_preparation(owner, task)

    async def failed_prepare(_sources, _provider, **kwargs):
        provider = SimpleNamespace(ainvoke=AsyncMock(side_effect=gemini_unavailable()))
        await _SubmissionSafeProvider(provider, reporter=kwargs["reporter"]).ainvoke("fixture")

    monkeypatch.setattr(task_preparation, "prepare_question_packages", failed_prepare)
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _owner: _RecoveryRegistry())
    await task_preparation.run_durable_question_preparation(_claim(owner, queued["job_id"], "test"))
    remove_reporter(queued["job_id"])
    state = await task_facade.async_task_state(task_id=task, owner_id=owner)
    assert state["error"] == "provider_submit_uncertain"
    assert state["progress"]["provider_failures"] == [{"code": "provider_upstream_unavailable", "http_status": 503}]
    assert "PRIVATE_PROVIDER_BODY" not in str(state)
