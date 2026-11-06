"""Offline transport tests for bounded timeout replay and unknown usage."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest
from pydantic import BaseModel

from backend.agents.question_preparation_agent import _SubmissionSafeProvider, provider_submission_is_uncertain
from backend.llm.providers import LLMResponse, ProviderRequestError, VisionImage
from backend.llm.request_timeouts import is_response_timeout, response_timeout_scope
from backend.services.execution_control import current_execution, run_controlled
from backend.tests.test_provider_response_deadlines import ROUTES, MESSAGES, provider_for, scheduler  # noqa: F401
from backend.tools import structured_llm as llm


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("vision", [False, True])
async def test_timeout_retries_reenter_admission_and_preserve_unknown_usage(route, vision, scheduler, monkeypatch):
    sends, admissions, sleeps, authorization = [], [], [], []
    acquire = scheduler.acquire
    async def counted_acquire(**kwargs):
        admissions.append(1)
        return await acquire(**kwargs)
    monkeypatch.setattr(scheduler, "acquire", counted_acquire)
    async def sleep(seconds):
        sleeps.append(seconds)
    retry = llm._invoke_with_rate_retry if vision else llm._ainvoke_with_retry
    monkeypatch.setattr(retry.retry, "sleep", sleep)
    async def invoke(options):
        sends.append(options)
        if len(sends) < 3:
            await asyncio.sleep(1)  # The real response deadline cancels it.
        return SimpleNamespace(content="complete", usage_metadata={"input_tokens": 10, "output_tokens": 5})
    async def before_send():
        authorization.append("authorize")

    async with provider_for(route, invoke, monkeypatch) as provider:
        with response_timeout_scope(.02, response_only=True, before_send=before_send,
                                    on_send=lambda: authorization.append("reserve")) as state:
            if vision:
                result = await llm.ainvoke_vision_with_rate_retry(
                    provider, "read", [VisionImage(b"source", "image/png")], max_output_tokens=20)
            else:
                result = await llm.ainvoke_with_retry(provider, MESSAGES)
        assert not provider._endpoint_breaker().is_open
        assert not provider._endpoint_breaker()._failures
    assert result.content == "complete" and result.input_tokens is result.output_tokens is None
    assert len(sends) == len(admissions) == state.attempt_count == 3
    assert authorization == ["authorize", "reserve"] and sleeps == [0, 0]
    assert state.had_uncertain_timeout and not state.submission_may_exist
    assert scheduler.active == scheduler.host_capacity.active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("sequence", [("timeout", "429", "timeout"), ("429", "timeout", "429")])
async def test_mixed_failures_share_three_attempts_without_erasing_uncertainty(sequence, scheduler, monkeypatch):
    calls, sleeps = [], []
    from backend.tests.test_automatic_model_scheduling import FakeClock
    clock = FakeClock()
    scheduler.clock = clock
    async def sleep(seconds):
        sleeps.append(seconds)
        clock.advance(seconds, scheduler)
    monkeypatch.setattr(llm._invoke_with_rate_retry.retry, "sleep", sleep)
    async def invoke(options):
        failure = sequence[len(calls)]
        calls.append(failure)
        if failure == "timeout":
            raise httpx.ReadTimeout("unknown response")
        raise ProviderRequestError("provider_rate_limited", status_code=429)
    async with provider_for("openai", invoke, monkeypatch) as provider:
        async def work():
            with response_timeout_scope(180, response_only=True) as state:
                with pytest.raises(Exception) as caught:
                    await llm.ainvoke_with_rate_retry(provider, MESSAGES)
                assert state.submission_may_exist and state.had_uncertain_timeout
                assert getattr(caught.value, "submission_may_exist", False)
                assert not current_execution().blocked
        await run_controlled("mixed-timeouts", lambda: False, work())
    assert calls == list(sequence)
    assert len(sleeps) == 2
    assert [seconds == 0 for seconds in sleeps] == [failure == "timeout" for failure in sequence[:2]]
    assert all(seconds == 0 or seconds >= 65 for seconds in sleeps)


@pytest.mark.asyncio
async def test_cancel_queued_timeout_retry_keeps_unknown_marker_without_another_send(scheduler, monkeypatch):
    calls = []
    async def invoke(options):
        calls.append(1)
        scheduler.host_capacity.capacity = 0
        raise httpx.ReadTimeout("unknown response")
    async with provider_for("openai", invoke, monkeypatch) as provider:
        with response_timeout_scope(180, response_only=True) as state:
            task = asyncio.create_task(llm.ainvoke_with_rate_retry(provider, MESSAGES))
            try:
                async with asyncio.timeout(2):
                    while not scheduler.pending:
                        await asyncio.sleep(.001)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    assert calls == [1] and state.submission_may_exist
    assert not state.last_attempt_rejected
    assert not scheduler.pending and scheduler.active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
async def test_model_stop_interrupts_queued_timeout_retry_without_erasing_uncertainty(scheduler, monkeypatch, route):
    calls = []
    async def invoke(options):
        calls.append(1)
        scheduler.host_capacity.capacity = 0
        raise httpx.ReadTimeout("unknown response")

    async with provider_for(route, invoke, monkeypatch) as provider:
        async def work():
            with response_timeout_scope(180, response_only=True) as state:
                task = asyncio.create_task(llm.ainvoke_with_rate_retry(provider, MESSAGES))
                try:
                    async with asyncio.timeout(2):
                        while not calls or not scheduler.pending:
                            await asyncio.sleep(.001)
                    current_execution().block(provider, "provider_rate_limited")
                    with pytest.raises(ProviderRequestError, match="provider_rate_limited") as caught:
                        await asyncio.wait_for(asyncio.shield(task), 1)
                    assert caught.value.retryable is False
                    assert caught.value.submission_may_exist and state.submission_may_exist
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        await run_controlled("stop-timeout-retry", lambda: False, work())
    assert calls == [1] and not scheduler.pending and scheduler.active == 0


@pytest.mark.asyncio
async def test_guarded_material_generation_timeout_retries_without_losing_billing_warning():
    class Provider:
        calls = 0
        async def ainvoke(self, messages):
            self.calls += 1
            if self.calls < 3:
                raise httpx.ReadTimeout("unknown response")
            return LLMResponse("complete", "fake", "fake", 1, 10, 5)
    provider = Provider()
    response = await llm.ainvoke_with_retry(_SubmissionSafeProvider(provider), MESSAGES)
    assert provider.calls == 3 and response.content == "complete"
    assert response.input_tokens is response.output_tokens is None
    provider.calls = -10
    with pytest.raises(llm.ResponseTimeoutError) as caught:
        await llm.ainvoke_with_retry(_SubmissionSafeProvider(provider), MESSAGES)
    assert provider.calls == -7 and provider_submission_is_uncertain(caught.value)


@pytest.mark.asyncio
async def test_timeout_and_format_repair_do_not_multiply_attempts():
    class Output(BaseModel):
        value: int
    class Provider:
        calls = 0
        async def ainvoke(self, messages):
            self.calls += 1
            if self.calls != 2:
                raise TimeoutError()
            return LLMResponse("not JSON", "fake", "fake", 1)
    provider = Provider()
    with pytest.raises(llm.ResponseTimeoutError):
        await llm.structured_llm_call(provider, system_prompt="system", user_prompt="source", output_model=Output)
    assert provider.calls == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancel", "non_retryable", "auth"])
async def test_explicit_stops_never_gain_timeout_retry_authority(failure):
    class Provider:
        calls = 0
        async def ainvoke(self, messages):
            self.calls += 1
            if failure == "cancel":
                raise asyncio.CancelledError()
            error = ProviderRequestError("provider_timeout" if failure == "non_retryable" else "provider_auth_failed",
                                         status_code=None if failure == "non_retryable" else 401)
            if failure == "non_retryable":
                error.retryable = False
            error.__context__ = TimeoutError()
            raise error
    provider = Provider()
    with pytest.raises(asyncio.CancelledError if failure == "cancel" else Exception):
        await llm.ainvoke_with_rate_retry(provider, MESSAGES)
    assert provider.calls == 1


def test_timeout_classification_uses_only_current_typed_evidence():
    wrapper = RuntimeError("SDK wrapper")
    wrapper.__cause__ = httpx.ReadTimeout("unknown")
    assert is_response_timeout(wrapper)
    wrapper.__cause__ = None
    wrapper.__context__ = httpx.ReadTimeout("previous request")
    assert not is_response_timeout(wrapper)
    for code in ("provider_capacity_unavailable", "grading_wall_time_limit_reached", "provider_submit_uncertain"):
        error = ProviderRequestError(code)
        error.__cause__ = TimeoutError()
        assert not is_response_timeout(error)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code", [(408, "provider_request_rejected"), (504, "provider_upstream_unavailable")])
async def test_gateway_timeout_statuses_receive_the_same_bounded_retry(status, code):
    from backend.llm.providers import _is_overload_failure
    error = ProviderRequestError(code, status_code=status)
    assert is_response_timeout(error) and not _is_overload_failure(error)
    class Provider:
        calls = 0
        async def ainvoke(self, messages):
            self.calls += 1
            raise ProviderRequestError(code, status_code=status)
    provider = Provider()
    with pytest.raises(llm.ResponseTimeoutError) as caught:
        await llm.ainvoke_with_rate_retry(provider, MESSAGES)
    assert provider.calls == 3 and caught.value.submission_may_exist


@pytest.mark.asyncio
async def test_timeout_waves_do_not_cool_down_or_block_other_requests(scheduler, monkeypatch):
    async def invoke(options):
        raise httpx.ReadTimeout("unknown")
    async with provider_for("openai", invoke, monkeypatch) as provider:
        async def work():
            async def fail():
                with response_timeout_scope(180, response_only=True):
                    with pytest.raises(llm.ResponseTimeoutError):
                        await llm.ainvoke_with_retry(provider, MESSAGES)
            await asyncio.gather(*(fail() for _ in range(6)))
            assert not current_execution().blocked
            assert not provider._endpoint_breaker().is_open
            assert not provider._endpoint_breaker()._failures
        await asyncio.wait_for(run_controlled("timeout-wave", lambda: False, work()), 3)


@pytest.mark.asyncio
async def test_timeout_retries_observe_normal_send_spacing(scheduler, monkeypatch):
    sent = []
    async def invoke(options):
        sent.append(asyncio.get_running_loop().time())
        raise httpx.ReadTimeout("unknown")
    async with provider_for("openai", invoke, monkeypatch) as provider:
        provider.config.rpm = 600  # One send every 0.1 seconds.
        with pytest.raises(llm.ResponseTimeoutError):
            await llm.ainvoke_with_retry(provider, MESSAGES)
    assert len(sent) == 3
    assert all(later - earlier >= .08 for earlier, later in zip(sent, sent[1:]))


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["problems", "submissions", "reference", "rubric", "test_cases", "knowledge"])
async def test_recognition_accepts_recovered_content_but_not_invented_usage(purpose, scheduler, monkeypatch):
    from backend.recognition.budget import RecognitionBudget, validate_budget_accounting
    from backend.recognition.engine import EngineReadInputV1
    from backend.recognition.models import RecognitionPolicyV1, RecognitionSourceRefV1
    from backend.recognition.runtime import RecognitionCapacity, run_initial_read
    from backend.skills.recognition_reader import LLMRecognitionEngine
    calls = []
    async def invoke(options):
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ReadTimeout("unknown")
        return SimpleNamespace(content="literal source", usage_metadata={"input_tokens": 10, "output_tokens": 5})
    async with provider_for("openai", invoke, monkeypatch) as provider:
        engine = LLMRecognitionEngine(provider, route_id="selected", fingerprint="frozen")
        source = RecognitionSourceRefV1(owner_id="fixture", scope="assignment_source", business_id="source",
                                       content_type="image/png", input_sha256="a" * 64)
        policy = RecognitionPolicyV1(per_call_seconds=180, timeout_mode="response_only")
        budget = RecognitionBudget(source, policy, engine.capabilities)
        request = EngineReadInputV1(purpose=purpose, input_mode="page_image", page_number=1,
                                   content_type="image/png", payload=b"fixture")
        result = await run_initial_read(engine, request, source=source, policy=policy,
                                        capabilities=engine.capabilities, region_keys=("p1",),
                                        budget=budget, capacity=RecognitionCapacity())
    assert calls == [1, 1, 1] and result.candidate.text == "literal source"
    assert result.candidate.status == "ok" and not result.submission_may_exist
    assert result.candidate.input_tokens is result.candidate.output_tokens is None
    snapshot = budget.snapshot()
    assert snapshot.total_calls == snapshot.settled_calls == 1
    assert not snapshot.usage_complete and snapshot.unknown_input_calls == snapshot.unknown_output_calls == 1
    validate_budget_accounting(snapshot, engine.capabilities,
                              [(result.candidate, result.submission_may_exist, request.max_output_tokens)])
