"""Contracts reproduced against the official providers on 2026-10-04."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage

from backend.llm.image_capability import explicitly_rejects_images
from backend.llm.providers import LLMResponse, SafeRelayProvider, _response_metadata
from backend.models import ProviderConfig
from backend.services.background_errors import classify_background_error
from backend.services.question_sources import question_recognition_options
from backend.tests.test_recognition_reader import llm, request
from backend.tools.structured_llm import PermanentLLMError, ainvoke_with_retry
from backend.tools.structured_llm import extract_and_parse_json
from pydantic import BaseModel


def test_gemini_recitation_is_not_an_http_request_or_image_capability_rejection():
    response = AIMessage(content="", response_metadata={"finish_reason": "RECITATION"},
                         usage_metadata={"input_tokens": 1326, "output_tokens": 0, "total_tokens": 1326})
    metadata = _response_metadata(response)
    assert metadata == {"input_tokens": 1326, "output_tokens": 0, "finish_reason": "refused",
                        "refusal_code": "provider_recitation_blocked"}



@pytest.mark.asyncio
async def test_visual_reader_keeps_specific_refusal_and_usage():
    _, engine = llm(text="", finish_reason="refused", refusal_code="provider_recitation_blocked")
    candidate = await engine.recognize(request())
    assert candidate.safe_error_code == "provider_recitation_blocked"
    assert candidate.finish_reason == "refused" and candidate.input_tokens == 50



@pytest.mark.asyncio
async def test_generation_does_not_parse_or_retry_a_blocked_response():
    provider = SimpleNamespace(ainvoke=AsyncMock(return_value=LLMResponse(
        content="", provider="fixture", model="fixture", duration_ms=1,
        finish_reason="refused", refusal_code="provider_recitation_blocked")))
    with pytest.raises(PermanentLLMError) as caught:
        await ainvoke_with_retry(provider, [])
    assert classify_background_error(caught.value, "problem_extraction_failed") == "provider_recitation_blocked"
    provider.ainvoke.assert_awaited_once()



def test_relay_gemini_recitation_without_content_is_a_valid_blocked_response():
    provider = SafeRelayProvider(ProviderConfig(provider_type="gemini", model="fixture", api_key="fixture",
                                                wire_protocol="gemini_generate_content", base_url="https://relay.example"))
    response = provider._parse_response({"candidates": [{"finishReason": "RECITATION", "content": {}}]}, 1)
    assert response.content == "" and response.refusal_code == "provider_recitation_blocked"



def test_connection_establishment_failure_does_not_claim_possible_paid_submission():
    import httpx
    from backend.agents.question_preparation_agent import _provider_submission_may_exist
    assert not _provider_submission_may_exist(httpx.ConnectError("TLS EOF"))
    assert not _provider_submission_may_exist(httpx.ConnectTimeout("connect"))
    assert _provider_submission_may_exist(httpx.ReadTimeout("response"))
    assert _provider_submission_may_exist(httpx.WriteError("request"))



@pytest.mark.asyncio
async def test_ocr_connection_failure_is_retryable_but_response_timeout_is_uncertain():
    import httpx
    from backend.domain.errors import RecognitionError
    provider, engine = llm()
    for error, uncertain in [(httpx.ConnectError("TLS EOF"), False), (httpx.ReadTimeout("response"), True)]:
        provider.ainvoke_vision.side_effect = error
        with pytest.raises(RecognitionError) as caught:
            await engine.recognize(request())
        assert caught.value.submission_may_exist is uncertain

