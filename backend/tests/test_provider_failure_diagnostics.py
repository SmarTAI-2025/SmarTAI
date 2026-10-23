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
@pytest.mark.parametrize("text", ["", "PRIVATE_BLOCKED_PARTIAL_TEXT"])
@pytest.mark.parametrize("reason", ["provider_recitation_blocked", "provider_content_blocked"])
async def test_refusal_survives_runtime_validation_with_page_and_no_replay(text, reason):
    from backend.tests.test_recognition_reader import llm, request
    provider, engine = llm(text, finish_reason="refused", refusal_code=reason)
    source = RecognitionSourceRefV1(owner_id="owner", scope="assignment_source", business_id="fixture",
                                   content_type="application/pdf", input_sha256="a" * 64)
    policy = RecognitionPolicyV1()
    budget = RecognitionBudget(source, policy, engine.capabilities)
    progress = ProgressReporter("fixture")
    kwargs = dict(source=source, policy=policy, capabilities=engine.capabilities,
                  region_keys=(region_budget_key(8, NormalizedRegionV1()),), budget=budget,
                  capacity=RecognitionCapacity(), progress=progress)
    result = await run_initial_read(engine, request(page_number=8), **kwargs)
    assert result.candidate.safe_error_code == reason
    assert result.candidate.status == "error" and result.candidate.text == ""
    assert result.candidate.finish_reason == "refused"
    assert result.candidate.input_tokens == 50
    assert not result.submission_may_exist
    assert budget.snapshot().pending_calls == 0
    with pytest.raises(RecognitionError):
        await run_initial_read(engine, request(page_number=8), **kwargs)
    provider.ainvoke_vision.assert_awaited_once()
    snapshot = (await progress.snapshot()).model_dump(mode="json")
    assert snapshot["provider_failures"] == [{"code": reason, "http_status": None, "pages": [8]}]
    assert "PRIVATE" not in str(snapshot)


def test_invalid_recognition_diagnostic_is_not_discarded_or_mislabelled_as_provider_http():
    failure = provider_failure_diagnostic(RecognitionError("recognition_response_invalid", submission_may_exist=True))
    assert failure is not None
    assert failure.code == "recognition_response_invalid" and failure.http_status is None


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


def test_public_recognition_diagnostics_validate_legacy_snapshots():
    from backend.progress.recognition_activity import public_progress

    snapshot = {"phase": "error", "provider_failures": [
        {"code": "provider_upstream_unavailable", "http_status": 503, "detail": "PRIVATE_BODY"},
        {"code": "provider_timeout", "http_status": None},
        {"code": "PRIVATE_EXCEPTION", "http_status": 503},
        {"code": "provider_timeout", "http_status": True},
        {"code": "provider_timeout", "http_status": "503"},
        {"code": "provider_timeout", "http_status": 200},
        {"code": "provider_recitation_blocked", "pages": [8], "body": "PRIVATE_BODY"},
        {"code": "provider_content_blocked", "pages": [True]},
        "PRIVATE_ROW",
    ]}
    result = public_progress(snapshot, terminal=True)
    # Only the newest eight source rows are considered, including invalid rows.
    assert result["provider_failures"] == [
        {"code": "provider_timeout", "http_status": None},
        {"code": "provider_recitation_blocked", "http_status": None, "pages": [8]},
    ]
    assert "PRIVATE" not in str(result)
    assert public_progress(result, terminal=True) == result
    snapshot["provider_failures"] = [{"code": "provider_timeout"}] * 100
    assert len(public_progress(snapshot)["provider_failures"]) == 8


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [None, "recognition_response_invalid", "provider_recitation_blocked", "provider_content_blocked"])
async def test_submission_failure_diagnostics_survive_reporter_eviction(monkeypatch, reason):
    from backend.db import workflow_repository
    from backend.progress.tracker import remove_reporter
    from backend.services import task_facade
    from backend.services.workflow_worker import LeasedOperation
    from backend.tests.test_task_background_workflows import _Registry, _seed_task

    owner, task = _seed_task(with_question=True)
    async def failed_read(**kwargs):
        await kwargs["reporter"].record_provider_failure(
            RecognitionError(reason) if reason else gemini_unavailable(), pages=[8] if reason else None,
        )
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
    assert state["progress"]["provider_failures"] == [
        {"code": reason, "http_status": None, "pages": [8]} if reason
        else {"code": "provider_upstream_unavailable", "http_status": 503}]
    assert "PRIVATE_PROVIDER_BODY" not in str(state)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [None, "recognition_response_invalid", "provider_recitation_blocked", "provider_content_blocked"])
async def test_question_failure_diagnostics_survive_reporter_eviction(monkeypatch, reason):
    from backend.agents import incremental_question_preparation
    from backend.api import task_preparation
    from backend.progress.tracker import remove_reporter
    from backend.services import task_facade
    from backend.tests.test_question_preparation_recovery import _RecoveryRegistry, _claim, _queue_question_preparation
    from backend.tests.test_task_background_workflows import _seed_task

    owner, task = _seed_task()
    _, queued, _ = await _queue_question_preparation(owner, task)

    async def failed_extract(_text, _provider, **kwargs):
        if reason:
            await kwargs["reporter"].record_provider_failure(RecognitionError(reason), pages=[8])
            raise RecognitionError("provider_submit_uncertain", submission_may_exist=True)
        provider = SimpleNamespace(ainvoke=AsyncMock(side_effect=gemini_unavailable()))
        await _SubmissionSafeProvider(provider, reporter=kwargs["reporter"]).ainvoke("fixture")

    monkeypatch.setattr(incremental_question_preparation, "_extract_problems_call", failed_extract)
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _owner: _RecoveryRegistry())
    await task_preparation.run_durable_question_preparation(_claim(owner, queued["job_id"], "test"))
    remove_reporter(queued["job_id"])
    state = await task_facade.async_task_state(task_id=task, owner_id=owner)
    assert state["error"] == "provider_submit_uncertain"
    assert state["progress"]["provider_failures"] == [
        {"code": reason, "http_status": None, "pages": [8]} if reason
        else {"code": "provider_upstream_unavailable", "http_status": 503}]
    assert "PRIVATE_PROVIDER_BODY" not in str(state)


@pytest.mark.asyncio
async def test_refused_page_is_persisted_and_only_explicit_retry_reads_it_again(tmp_path):
    from backend.llm.providers import LLMResponse
    from backend.tests.test_recognition_durable_runs import runner, run
    from backend.tests.test_recognition_reader import llm

    service, request, data, _ = runner(tmp_path, pdf=True, pages=2)
    request = request.model_copy(update={"policy": request.policy.model_copy(update={"enable_repair": False})})
    provider, engine = llm("literal 1 + 1 = 3", finish_reason="stop")
    success = provider.ainvoke_vision.return_value
    provider.ainvoke_vision.side_effect = [
        success, LLMResponse(content="", provider=success.provider, model=success.model, duration_ms=1,
                             input_tokens=50, output_tokens=0, finish_reason="refused",
                             refusal_code="provider_recitation_blocked"), success,
    ]
    service.progress = ProgressReporter("fixture")
    first = await run(service, request, data, engine)
    assert first.safe_error_code == "provider_recitation_blocked"
    assert first.assembly.document.coverage.failed_pages == [2]
    assert provider.ainvoke_vision.await_count == 2
    restored = await run(service, request, None, engine)
    assert restored.safe_error_code == first.safe_error_code
    assert provider.ainvoke_vision.await_count == 2
    last = restored.assembly.raw.read_batch.units[-1].candidate
    assert last.safe_error_code == "provider_recitation_blocked" and last.finish_reason == "refused"
    retry = await service.run(request, data, engine=engine, prompt_version="faithful-reader-v1",
                              authorized_owner_id=request.source.owner_id, retry_key="explicit-retry:1",
                              acknowledge_pending_retry=True)
    assert retry.status == "completed", (retry.safe_error_code, provider.ainvoke_vision.await_count,
                                        retry.assembly.document.coverage if retry.assembly else None)
    assert provider.ainvoke_vision.await_count == 3
    assert retry.assembly.raw.read_batch.provenance[0].origin == "cache_hit"
    assert (await service.progress.snapshot()).provider_failures[-1].pages == [2]
