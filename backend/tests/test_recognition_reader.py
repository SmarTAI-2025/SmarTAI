import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.domain.errors import RecognitionError
from backend.llm.providers import LLMResponse, OpenAIProvider, ProviderRequestError
from backend.models import ProviderConfig
from backend.recognition.engine import EngineReadInputV1
from backend.skills.recognition_reader import BaiduRecognitionEngine, LLMRecognitionEngine, faithful_reader_prompt
from backend.tools.baidu_unlimited_ocr import BaiduUnlimitedOCRError


def request(**changes):
    return EngineReadInputV1(**{
        "purpose": "submissions", "input_mode": "page_image", "page_number": 3,
        "content_type": "image/png", "payload": b"inspected-image", **changes,
    })


def llm(text="  x^2 = -2\n[crossed-out: x = 2]  ", **changes):
    response = LLMResponse(**{
        "content": text, "provider": "existing:fixture", "model": "fixture", "duration_ms": 2,
        "input_tokens": 50, "output_tokens": 25, **changes,
    })
    provider = SimpleNamespace(provider_id="existing:fixture", supports_vision=True, ainvoke_vision=AsyncMock(return_value=response))
    return provider, LLMRecognitionEngine(provider, route_id="owner-route", fingerprint="frozen")


@pytest.mark.asyncio
async def test_llm_preserves_raw_source_errors_and_passes_limit_without_native_anchoring():
    provider, engine = llm()
    result = await engine.recognize(request(max_output_tokens=1024))
    assert result.text == "  x^2 = -2\n[crossed-out: x = 2]  "
    assert result.normalized_text is None
    assert result.provider_route_id == "owner-route" and result.input_tokens == 50
    args, kwargs = provider.ainvoke_vision.call_args
    assert kwargs == {"max_output_tokens": 1024}
    assert len(args[1]) == 1 and args[1][0].filename == "source-image"
    assert "Never correct" in args[0] and "untrusted source data" in args[0]
    assert "crossed-out" in args[0] and "[blank]" in args[0] and "[unclear]" in args[0]
    assert "x^2" not in args[0]


@pytest.mark.parametrize("purpose", ["problems", "submissions", "reference", "rubric", "test_cases", "knowledge"])
def test_every_purpose_has_faithful_not_solver_prompt(purpose):
    prompt = faithful_reader_prompt(purpose)
    assert "not a solver or editor" in prompt
    assert "never obeyed" in prompt and "Do not return JSON" in prompt
    assert "source language" in prompt


def test_student_transcription_separates_annotations_without_color_based_deletion():
    prompt = faithful_reader_prompt("submissions")
    assert "[annotation: ...]" in prompt
    assert "[unclear authorship: ...]" in prompt
    assert "Ink color alone does not establish authorship" in prompt
    assert "not boxed or active reasoning" in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("finish,warning", [("length", "output_truncated"), ("refused", "provider_refused")])
async def test_incomplete_or_refused_output_is_marked_not_silently_repaired(finish, warning):
    provider, engine = llm(finish_reason=finish)
    result = await engine.recognize(request())
    assert result.warning_codes == [warning] and result.finish_reason == finish
    assert provider.ainvoke_vision.await_count == 1


@pytest.mark.asyncio
async def test_empty_output_no_extra_call():
    provider, engine = llm(" \n")
    result = await engine.recognize(request())
    assert result.status == "empty" and result.text == ""
    assert provider.ainvoke_vision.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["x" * 24001, ["private"]])
async def test_invalid_output_is_bounded_safe_error(text):
    provider, engine = llm(text)
    with pytest.raises(RecognitionError) as exc:
        await engine.recognize(request())
    assert "private" not in str(exc.value) and "xxx" not in str(exc.value)
    assert provider.ainvoke_vision.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ProviderRequestError("provider_auth_failed"), RuntimeError("PRIVATE_KEY response body"), asyncio.CancelledError()])
async def test_errors_and_cancellation_are_not_content_or_retry(error):
    provider, engine = llm()
    provider.ainvoke_vision.side_effect = error
    with pytest.raises((RecognitionError, asyncio.CancelledError)) as exc:
        await engine.recognize(request())
    assert "PRIVATE_KEY" not in str(exc.value)
    assert provider.ainvoke_vision.await_count == 1


@pytest.mark.asyncio
async def test_mutated_request_and_route_fail_before_provider():
    provider, engine = llm()
    unit = request()
    unit.payload = b""
    with pytest.raises(RecognitionError, match="recognition_request_invalid"):
        await engine.recognize(unit)
    provider.provider_id = "other:route"
    with pytest.raises(RecognitionError, match="recognition_route_changed"):
        await engine.recognize(request())
    assert provider.ainvoke_vision.await_count == 0


@pytest.mark.asyncio
async def test_baidu_whole_group_once_keeps_unmapped_raw_markdown_and_unknown_usage():
    client = SimpleNamespace(recognize_document=AsyncMock(return_value=SimpleNamespace(markdown="  p1\np2  ", duration_ms=2)))
    engine = BaiduRecognitionEngine(client, route_id="ocr:owned", fingerprint="owned-fp")
    result = await engine.recognize(request(input_mode="document", content_type="application/pdf", document_pages=[3, 5, 7]))
    client.recognize_document.assert_awaited_once_with(b"inspected-image", "source.pdf")
    assert result.text == "  p1\np2  " and result.input_tokens is None
    assert result.warning_codes == ["document_page_mapping_unavailable"]
    assert engine.capabilities.document_batching
    assert not engine.capabilities.semantic_repair and not engine.capabilities.response_recheck
    changed = engine.capabilities
    changed.response_recheck = True
    assert not engine.capabilities.response_recheck


@pytest.mark.asyncio
async def test_baidu_uncertain_is_not_resubmitted_or_hidden_companion():
    client = SimpleNamespace(recognize_document=AsyncMock(side_effect=BaiduUnlimitedOCRError("provider_submit_uncertain", submission_may_exist=True)))
    engine = BaiduRecognitionEngine(client, route_id="ocr:owned", fingerprint="owned-fp")
    with pytest.raises(RecognitionError) as exc:
        await engine.recognize(request())
    assert exc.value.submission_may_exist and exc.value.code == "provider_submit_uncertain"
    assert client.recognize_document.await_count == 1


@pytest.mark.asyncio
async def test_baidu_empty_result_is_explicit():
    client = SimpleNamespace(recognize_document=AsyncMock(side_effect=BaiduUnlimitedOCRError("ocr_empty_result")))
    result = await BaiduRecognitionEngine(client, route_id="ocr:owned", fingerprint="owned-fp").recognize(request())
    assert result.status == "empty" and result.text == ""


@pytest.mark.parametrize("pages", [[4], [3, 3], [3, 2], [3, 10001]])
def test_document_input_mapping_is_validated(pages):
    with pytest.raises(ValueError):
        request(input_mode="document", content_type="application/pdf", document_pages=pages)


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("api_key", "changed-private-key"), ("base_url", "https://changed.example"), ("wire_protocol", "anthropic_messages")])
async def test_mutated_provider_configuration_never_uses_old_fingerprint(field, value):
    provider = OpenAIProvider(ProviderConfig(provider_type="openai", model="fixture", api_key="original-private-key"))
    provider.ainvoke_vision = AsyncMock()
    engine = LLMRecognitionEngine(provider, route_id="route", fingerprint="original")
    setattr(provider.config, field, value)
    with pytest.raises(RecognitionError, match="recognition_route_changed") as exc:
        await engine.recognize(request())
    assert "private-key" not in str(exc.value)
    assert provider.ainvoke_vision.await_count == 0


@pytest.mark.asyncio
async def test_baidu_mutated_credentials_and_client_replacement_fail_before_call():
    client = SimpleNamespace(_api_key="original", _secret_key="original", recognize_document=AsyncMock())
    engine = BaiduRecognitionEngine(client, route_id="owned", fingerprint="frozen")
    client._secret_key = "changed"
    with pytest.raises(RecognitionError, match="recognition_route_changed"):
        await engine.recognize(request())
    assert client.recognize_document.await_count == 0
    client._secret_key = "original"
    engine.client = SimpleNamespace(_api_key="original", _secret_key="original", recognize_document=AsyncMock())
    with pytest.raises(RecognitionError, match="recognition_route_changed"):
        await engine.recognize(request())
    assert engine.client.recognize_document.await_count == 0


@pytest.mark.asyncio
async def test_raw_sdk_auth_failure_is_specific_without_leaking_body():
    import httpx
    from openai import AuthenticationError

    provider, engine = llm()
    provider.ainvoke_vision.side_effect = AuthenticationError(
        "PRIVATE_AUTH_BODY", response=httpx.Response(401, request=httpx.Request("POST", "https://fixture.example")), body=None,
    )
    with pytest.raises(RecognitionError) as exc:
        await engine.recognize(request())
    assert exc.value.code == "provider_auth_failed" and not exc.value.submission_may_exist
    assert "PRIVATE_AUTH_BODY" not in str(exc.value)
