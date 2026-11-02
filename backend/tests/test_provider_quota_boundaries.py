"""Constructed counterexamples, exercised through the real retry wrappers."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.llm.provider_limits import exhausted_quota_code
from backend.llm.providers import ProviderRequestError
from backend.services.background_errors import classify_background_error
from backend.services.execution_control import run_controlled
from backend.tools import structured_llm as llm


@pytest.mark.asyncio
@pytest.mark.parametrize("vision", [False, True])
@pytest.mark.parametrize("message,code,calls", [
    ("Quota exceeded for requests per day", "provider_daily_quota_exceeded", 1),
    ("日配额已耗尽", "provider_daily_quota_exceeded", 1),
    ("每日额度已用完", "provider_daily_quota_exceeded", 1),
    ("Requests per minute quota exceeded; daily quota has not yet been exhausted", "provider_rate_limited", 3),
    ("Requests per minute quota exceeded; daily quota hasn't yet been exhausted", "provider_rate_limited", 3),
    ("每分钟额度超限，每日额度尚未耗尽", "provider_rate_limited", 3),
    ("Quota exceeded for requests per day; limit: 0", "provider_zero_quota", 1),
])
async def test_manual_round_reports_current_quota_and_automatic_attempt_count(monkeypatch, vision, message, code, calls):
    sleep = AsyncMock()
    monkeypatch.setattr(llm._ainvoke_with_retry.retry, "sleep", sleep)
    monkeypatch.setattr(llm._invoke_with_rate_retry.retry, "sleep", sleep)
    invoke = AsyncMock(side_effect=ProviderRequestError(message, status_code=429))
    provider = SimpleNamespace(ainvoke=invoke, ainvoke_vision=invoke, provider_id="synthetic", model="quota-test")

    async def run():
        with pytest.raises(Exception) as raised:
            if vision:
                await llm.ainvoke_vision_with_rate_retry(provider, "read", [], max_output_tokens=100)
            else:
                await llm.ainvoke_with_retry(provider, [])
        assert classify_background_error(raised.value, "workflow_failed") == code

    await run_controlled("first-user-round", lambda: False, run())
    assert invoke.await_count == calls
    assert sleep.await_count == calls - 1
    await run_controlled("next-user-round", lambda: False, run())
    assert invoke.await_count == calls * 2  # No earlier quota block survives a click.


@pytest.mark.parametrize("message,expected", [
    ("Minute quota exceeded and daily quota has not yet been exhausted", None),
    ("Minute quota exceeded. Daily quota has never been exhausted", None),
    ("Minute quota exceeded; daily quota is not fully exhausted", None),
    ("Minute quota exceeded; daily quota has not been reached", None),
    ("Daily quota remaining: 0", None),
    ("Remaining quota: 0", None),
    ("Invalid response token limit: 0", None),
    ("Quota exceeded for requests per day; remaining: 0", "provider_daily_quota_exceeded"),
    ("Quota exceeded for requests per day; limit: 0.5", "provider_daily_quota_exceeded"),
    ("Quota exceeded for requests per day; limit: 500", "provider_daily_quota_exceeded"),
])
def test_text_evidence_distinguishes_negation_remaining_and_zero_limit(message, expected):
    assert exhausted_quota_code(message) == expected


def quota_body(value, *, daily=True, field="quotaValue"):
    return {"error": {"code": 429, "message": "Quota exceeded", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
            {"quotaId": "GenerateRequestsPerDay" if daily else "GenerateRequestsPerMinute", field: value},
        ]},
    ]}}


@pytest.mark.parametrize("value", [0, "0", 0.0, "0.0"])
@pytest.mark.parametrize("daily", [False, True])
def test_explicit_zero_ceiling_is_not_exhausted_daily_allowance(value, daily):
    from google.genai.errors import ClientError
    assert exhausted_quota_code(ClientError(429, quota_body(value, daily=daily))) == "provider_zero_quota"


@pytest.mark.parametrize("value,field", [(None, "quotaValue"), (False, "quotaValue"), (500, "quotaValue"),
                                         (0, "remaining"), (0, "usage"), (0, "unknown")])
def test_missing_or_unrelated_zero_does_not_invent_zero_quota(value, field):
    assert exhausted_quota_code(quota_body(value, field=field)) == "provider_daily_quota_exceeded"


@pytest.mark.asyncio
async def test_structured_zero_survives_visual_reader_without_uncertain_dispatch():
    from google.genai.errors import ClientError
    from backend.domain.errors import RecognitionError
    from backend.tests.test_recognition_reader import llm as reader, request
    provider, engine = reader()
    provider.ainvoke_vision.side_effect = ClientError(429, quota_body("0"))
    with pytest.raises(RecognitionError) as raised:
        await engine.recognize(request())
    assert raised.value.code == "provider_zero_quota"
    assert raised.value.submission_may_exist is False
    assert provider.ainvoke_vision.await_count == 1


def test_zero_quota_survives_grading_and_never_triggers_whole_item_retry(monkeypatch):
    from google.genai.errors import ClientError
    from backend.agents.grading_agent import _grading_failure_feedback, _is_retryable_full_failure
    from backend.agents.multi_expert import dominant_error_kind
    from backend.models import Correction, ExpertResult, TaskGradingSetup
    from backend.services import task_facade
    from backend.skills.base import classify_skill_error
    kind, message = classify_skill_error(ClientError(429, quota_body(0)))
    assert kind == "zero_quota" and "无可用配额" in message
    failures = [ExpertResult(provider=kind, score=0, confidence=0, comment="", error_kind=kind)
                for kind in ("transient_llm", "quota_exhausted", "daily_quota_exhausted", "zero_quota")]
    assert dominant_error_kind(failures) == "zero_quota"
    correction = Correction(q_id="q1", type="reasoning", score=0, max_score=10, confidence=0,
                            comment="", steps=[], expert_results=failures)
    assert not _is_retryable_full_failure(correction)
    assert "无可用配额" in _grading_failure_feedback(kind, None)
    assert "no available quota" in _grading_failure_feedback(kind, TaskGradingSetup(selected_provider_ids=["synthetic"], primary_provider_id="synthetic", feedback_language="en"))
    row = SimpleNamespace(effective_score=None, ai_expert_results=[{"error_kind": "zero_quota"}])
    monkeypatch.setattr(task_facade.grading_repository, "list_results_for_run", lambda _: [row])
    assert task_facade._grading_failure_code(SimpleNamespace(id="synthetic", status="partial_failed", error_message=None)) == "provider_zero_quota"


@pytest.mark.asyncio
async def test_zero_quota_blocks_only_current_execution(monkeypatch):
    from backend.services.execution_control import current_execution, provider_key
    from backend.tests.test_task_execution_control import LimitedProvider
    sleep = AsyncMock()
    monkeypatch.setattr(llm._ainvoke_with_retry.retry, "sleep", sleep)
    provider = LimitedProvider(ProviderRequestError("provider_zero_quota", status_code=429))

    async def round():
        await current_execution().check(provider)
        with pytest.raises(llm.PermanentLLMError):
            await llm.ainvoke_with_retry(provider, [])
        assert current_execution().blocked[provider_key(provider)] == "provider_zero_quota"
        with pytest.raises(ProviderRequestError, match="provider_zero_quota"):
            await current_execution().check(provider)

    await run_controlled("same-parent", lambda: False, round())
    await run_controlled("same-parent", lambda: False, round())
    assert provider.calls == 2 and sleep.await_count == 0


def test_zero_ceiling_text_can_supply_missing_structured_ceiling_but_not_override_positive_ceiling():
    body = quota_body(None)
    body["error"]["message"] = "Quota exceeded for metric: requests_per_day, limit: 0"
    assert exhausted_quota_code(body) == "provider_zero_quota"
    body["error"]["details"][0]["violations"][0]["quotaValue"] = "500"
    assert exhausted_quota_code(body) == "provider_daily_quota_exceeded"


@pytest.mark.parametrize("message", [
    "Quota exceeded; quota limit: 0",
    "配额上限为0",
    "配额超限，总配额: 0",
    "Quota exceeded for requests per minute; limit: 0",
    "* Quota exceeded for metric: generativelanguage.googleapis.com/requests, limit: 0, model: synthetic",
])
@pytest.mark.parametrize("representation", ["text", "error", "body", "json"])
def test_unstructured_zero_requires_an_explicit_ceiling(message, representation):
    import json
    value = {"error": {"message": message}}
    if representation == "text":
        value = message
    elif representation == "error":
        value = ProviderRequestError(message, status_code=429)
    elif representation == "json":
        value = json.dumps(value)
    assert exhausted_quota_code(value) == "provider_zero_quota"


@pytest.mark.asyncio
@pytest.mark.parametrize("vision", [False, True])
@pytest.mark.parametrize("structured", [False, True, "missing_ceiling"])
@pytest.mark.parametrize("message", [
    "每分钟配额已超出，剩余配额: 0",
    "每分钟配额超限，剩余额度为0",
    "Minute quota exceeded; remaining quota limit: 0",
    "Minute quota exceeded; used quota: 0",
    "Minute quota exceeded; max output token limit: 0",
])
async def test_remaining_or_unrelated_zero_stays_rate_limited_through_final_state(monkeypatch, vision, structured, message):
    from google.genai.errors import ClientError
    from backend.skills.base import classify_skill_error
    from backend.domain.errors import RecognitionError
    from backend.domain.source_outcomes import safe_source_diagnostic
    sleep = AsyncMock()
    monkeypatch.setattr(llm._ainvoke_with_retry.retry, "sleep", sleep)
    monkeypatch.setattr(llm._invoke_with_rate_retry.retry, "sleep", sleep)
    if structured:
        body = quota_body(None if structured == "missing_ceiling" else 500, daily=False)
        body["error"]["message"] = message
        error = ClientError(429, body)
    else:
        error = ProviderRequestError(message, status_code=429)
    invoke = AsyncMock(side_effect=error)
    provider = SimpleNamespace(ainvoke=invoke, ainvoke_vision=invoke)
    with pytest.raises(llm.RateLimitError) as raised:
        if vision:
            await llm.ainvoke_vision_with_rate_retry(provider, "read", [], max_output_tokens=100)
        else:
            await llm.ainvoke_with_retry(provider, [])
    assert invoke.await_count == 3 and sleep.await_count == 2
    final = classify_background_error(raised.value, "workflow_failed")
    assert final == "provider_rate_limited"
    assert classify_skill_error(raised.value)[0] == "quota_exhausted"
    assert RecognitionError(final).code == final
    assert safe_source_diagnostic("parse_failed", final, "recognition") == (final, "recognition")


@pytest.mark.parametrize("message", ["provider_zero_quota", "Quota exceeded; quota limit: 0"])
def test_structured_minute_violation_outranks_a_lossy_outer_message(message):
    from google.genai.errors import ClientError
    body = quota_body(500, daily=False)
    body["error"]["message"] = "每分钟配额超限，剩余配额: 0"
    outer = RuntimeError("Provider failed: " + message)
    outer.__cause__ = ClientError(429, body)
    assert classify_background_error(outer, "workflow_failed") == "provider_rate_limited"


@pytest.mark.parametrize("field", ["remaining quota", "used quota", "remaining quota limit", "剩余配额", "剩余额度", "已用配额", "输出限制"])
def test_zero_must_belong_to_a_ceiling_field_instead_of_a_suffix(field):
    assert exhausted_quota_code(f"Minute quota exceeded; {field}: 0") is None


def test_confirmed_rate_classification_is_not_reparsed_after_details_are_lost():
    from google.genai.errors import ClientError
    body = quota_body(500, daily=False)
    # Deliberately conflicting prose verifies evidence precedence, not just a
    # negated keyword. This must remain true after exception serialization.
    body["error"]["message"] = "Quota exceeded; quota limit: 0"
    classified = llm._classify_exception(ClientError(429, body))
    assert isinstance(classified, llm.RateLimitError)
    assert str(classified) == "provider_rate_limited"
    assert classify_background_error(classified, "workflow_failed") == "provider_rate_limited"
    assert classify_background_error(RuntimeError(str(classified)), "workflow_failed") == "provider_rate_limited"


@pytest.mark.asyncio
@pytest.mark.parametrize("vision", [False, True])
async def test_structured_minute_limit_survives_retry_and_persisted_task_projection(monkeypatch, vision):
    from google.genai.errors import ClientError
    from backend.db import workflow_repository
    from backend.services import task_facade
    from backend.skills.base import classify_skill_error
    from backend.tests.test_task_background_workflows import _Registry, _seed_task

    body = quota_body(500, daily=False)
    body["error"]["message"] = "Quota exceeded; quota limit: 0"
    invoke = AsyncMock(side_effect=ClientError(429, body))
    provider = SimpleNamespace(ainvoke=invoke, ainvoke_vision=invoke)
    monkeypatch.setattr(llm._ainvoke_with_retry.retry, "sleep", AsyncMock())
    monkeypatch.setattr(llm._invoke_with_rate_retry.retry, "sleep", AsyncMock())
    with pytest.raises(llm.RateLimitError) as raised:
        if vision:
            await llm.ainvoke_vision_with_rate_retry(provider, "read", [], max_output_tokens=100)
        else:
            await llm.ainvoke_with_retry(provider, [])
    assert invoke.await_count == 3
    final = classify_background_error(raised.value, "workflow_failed")
    owner, task = _seed_task(with_question=True)
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename="student.txt", content=b"answer", content_type="text/plain", registry=_Registry())
    operation = workflow_repository.get_operation(queued["job_id"], owner_id=owner)
    assert task_facade._fail_operation(task, owner, operation.id, operation.attempt, final)
    assert workflow_repository.get_operation(operation.id, owner_id=owner).error_code == "provider_rate_limited"
    assert task_facade.get_task(task_id=task, owner_id=owner, full=False)["error"] == "provider_rate_limited"

    kind, _ = classify_skill_error(raised.value)
    row = SimpleNamespace(effective_score=None, ai_expert_results=[{"error_kind": kind}])
    monkeypatch.setattr(task_facade.grading_repository, "list_results_for_run", lambda _: [row])
    assert task_facade._grading_failure_code(SimpleNamespace(id="synthetic", status="partial_failed", error_message=None)) == "provider_rate_limited"


@pytest.mark.asyncio
@pytest.mark.parametrize("vision", [False, True])
@pytest.mark.parametrize("old_quota", [0, 500])
@pytest.mark.parametrize("suppress_context", [False, True])
async def test_current_rate_limit_does_not_inherit_old_implicit_quota(monkeypatch, vision, old_quota, suppress_context):
    """Constructed Python context boundary, not evidence of a production trigger."""
    from google.genai.errors import ClientError
    from backend.services.execution_control import current_execution
    sleep = AsyncMock()
    monkeypatch.setattr(llm._ainvoke_with_retry.retry, "sleep", sleep)
    monkeypatch.setattr(llm._invoke_with_rate_retry.retry, "sleep", sleep)

    async def fail(*args, **kwargs):
        try:
            raise ClientError(429, quota_body(old_quota))
        except ClientError:
            current = ProviderRequestError("provider_rate_limited", status_code=429)
            if suppress_context:
                raise current from None
            raise current

    invoke = AsyncMock(side_effect=fail)
    provider = SimpleNamespace(ainvoke=invoke, ainvoke_vision=invoke)

    async def run():
        with pytest.raises(Exception) as raised:
            if vision:
                await llm.ainvoke_vision_with_rate_retry(provider, "read", [], max_output_tokens=100)
            else:
                await llm.ainvoke_with_retry(provider, [])
        assert invoke.await_count == 3 and sleep.await_count == 2
        assert classify_background_error(raised.value, "workflow_failed") == "provider_rate_limited"
        # Exhausting the minute retry budget may stop this round, but cannot
        # inherit a different quota reason from the earlier exception.
        assert set(current_execution().blocked.values()) <= {"provider_rate_limited"}

    await run_controlled("current-click", lambda: False, run())
