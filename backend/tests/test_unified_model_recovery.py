from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from backend.llm.providers import LLMResponse, ProviderRequestError
from backend.services.background_errors import classify_background_error
from backend.skills.base import classify_skill_error
from backend.tools import structured_llm as llm


class Answer(BaseModel):
    score: int


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gemini", "openai-luna", "zhipu"])
async def test_schema_repair_is_provider_independent_and_bounded(model):
    provider = SimpleNamespace(ainvoke=AsyncMock(side_effect=[
        LLMResponse("invalid", model, model, 10, 5, 4),
        LLMResponse('{"score": 7}', model, model, 20, 8, 6),
    ]))
    result, raw = await llm.structured_llm_call(provider, system_prompt="Grade evidence", user_prompt="Evidence", output_model=Answer)
    assert result.score == 7
    assert provider.ainvoke.await_count == 2
    assert raw.input_tokens == 13 and raw.output_tokens == 10 and raw.duration_ms == 30
    assert '"required"' in provider.ainvoke.call_args.args[0][-1].content
    provider.ainvoke = AsyncMock(return_value=LLMResponse("invalid", model, model, 1))
    with pytest.raises(llm.StructuredOutputInvalidError):
        await llm.structured_llm_call(provider, system_prompt="Grade", user_prompt="Evidence", output_model=Answer)
    assert provider.ainvoke.await_count == 2


@pytest.mark.asyncio
async def test_explicit_daily_limit_never_automatically_retries():
    error = ProviderRequestError("Daily requests quota exceeded", status_code=429)
    provider = SimpleNamespace(ainvoke=AsyncMock(side_effect=error), ainvoke_vision=AsyncMock(side_effect=error))
    with pytest.raises(llm.DailyQuotaError):
        await llm.ainvoke_with_retry(provider, [])
    with pytest.raises(llm.DailyQuotaError):
        await llm.ainvoke_vision_with_rate_retry(provider, "read", [], max_output_tokens=100)
    assert provider.ainvoke.await_count == provider.ainvoke_vision.await_count == 1
    assert classify_background_error(error, "workflow_failed") == "provider_daily_quota_exceeded"
    assert classify_skill_error(error)[0] == "daily_quota_exhausted"


@pytest.mark.asyncio
async def test_vision_rate_retry_does_not_replay_uncertain_timeout():
    provider = SimpleNamespace(ainvoke_vision=AsyncMock(side_effect=ProviderRequestError("provider_timeout")))
    with pytest.raises(ProviderRequestError):
        await llm.ainvoke_vision_with_rate_retry(provider, "read", [], max_output_tokens=100)
    assert provider.ainvoke_vision.await_count == 1


def test_undifferentiated_limit_does_not_invent_a_daily_or_minute_reason():
    error = ProviderRequestError("provider_rate_limited", status_code=429)
    assert isinstance(llm._classify_exception(error), llm.RateLimitError)
    assert classify_background_error(error, "workflow_failed") == "provider_rate_limited"
    assert "无法确定" in classify_skill_error(error)[1]


def test_daily_exhaustion_prevents_whole_item_retry_even_with_transient_failures():
    from backend.agents.grading_agent import _grading_failure_feedback, _is_retryable_full_failure
    from backend.agents.multi_expert import _casts_confidence_vote, dominant_error_kind
    from backend.models import Correction, ExpertResult

    failures = [ExpertResult(provider=kind, score=0, confidence=0, comment="", error_kind=kind)
                for kind in ("transient_llm", "quota_exhausted", "daily_quota_exhausted")]
    assert dominant_error_kind(failures) == "daily_quota_exhausted"
    correction = Correction(q_id="q1", type="proof", score=0, max_score=10, confidence=0,
                            comment="", steps=[], expert_results=failures)
    assert not _is_retryable_full_failure(correction)
    assert not _casts_confidence_vote(failures[-1].model_copy(update={"confidence": 0.9}))
    assert "不会自动跨天等待" in _grading_failure_feedback("daily_quota_exhausted", None)
    assert "未说明限制周期" in _grading_failure_feedback("quota_exhausted", None)
