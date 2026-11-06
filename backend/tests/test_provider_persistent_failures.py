"""Real admission/HTTP boundaries, synthetic responses; no provider calls."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from backend.config import settings
from backend.llm.providers import ProviderRequestError
from backend.services.execution_control import current_execution, run_controlled
from backend.tests.test_provider_response_deadlines import MESSAGES, ROUTES, provider_for, scheduler
from backend.tools import structured_llm as llm


def test_google_sdk_integer_status_and_wrapped_cause_are_not_retried():
    from google.genai.errors import ClientError
    error = ClientError(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "private"}})
    wrapper = RuntimeError("SDK wrapper")
    wrapper.__cause__ = error
    assert isinstance(llm._classify_exception(error), llm.PermanentLLMError)
    assert isinstance(llm._classify_exception(wrapper), llm.PermanentLLMError)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,status,vendor_code,code", [
    ("zhipu", 429, "1311", "provider_permission_denied"),
    ("zhipu", 429, "1113", "provider_quota_exceeded"),
    ("zhipu", 400, "1301", "provider_content_blocked"),
    ("zhipu", 400, "1211", "provider_model_not_found"),
    ("qwen", 403, "Arrearage", "provider_quota_exceeded"),
    ("qwen", 403, "DataInspectionFailed", "provider_content_blocked"),
    ("tencent", 429, "429004", "provider_daily_quota_exceeded"),
    ("tencent", 451, "451001", "provider_content_blocked"),
    ("deepseek", 402, "unknown_error", "provider_quota_exceeded"),
])
async def test_explicit_vendor_reason_wins_over_http_without_rate_cooldown(monkeypatch, scheduler, kind, status, vendor_code, code):
    sends = []
    async def invoke(_):
        sends.append(1)
        return httpx.Response(status, json={"error": {"code": vendor_code, "message": "private"}})
    async with provider_for("relay_openai_chat_completions", invoke, monkeypatch) as provider:
        provider.provider_type = kind
        async def job():
            with pytest.raises(llm.PermanentLLMError) as caught:
                await llm.ainvoke_with_retry(provider, MESSAGES)
            from backend.services.background_errors import classify_background_error
            assert classify_background_error(caught.value, "provider_request_failed") == code
            assert all(budget.blocked_until == 0 for budget in scheduler.budgets.values())
        await run_controlled("vendor-errors", lambda: False, job())
    assert sends == [1]


def test_vendor_numeric_codes_and_local_permissions_do_not_leak_into_configuration_stops():
    from backend.domain.errors import Forbidden
    from backend.llm.provider_errors import configuration_failure_code, structured_error_code
    assert structured_error_code({"error": {"code": "1311"}}, "openai") is None
    assert configuration_failure_code(Forbidden("local owner check")) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("status,code", [(401, "provider_auth_failed"),
                                        (403, "provider_permission_denied"),
                                        (404, "provider_model_not_found")])
async def test_three_rejections_stop_only_unsent_calls_without_retry(monkeypatch, scheduler, route, status, code):
    sends = []
    async def invoke(_):
        sends.append(1)
        if route.startswith("relay_"):
            return httpx.Response(status, json={"error": {"message": "private provider detail"}})
        error = RuntimeError("private provider detail")
        error.status_code = status
        raise error

    async with provider_for(route, invoke, monkeypatch) as provider:
        async def job():
            previous_sends = len(sends)
            for _ in range(8):
                with pytest.raises(Exception):
                    await llm.ainvoke_with_retry(provider, MESSAGES)
            assert len(sends) - previous_sends == 3
            assert len(current_execution().blocked) == 1
            assert list(current_execution().blocked.values()) == [
                "provider_model_or_endpoint_not_found" if route.startswith("relay_") and status == 404 else code
            ]
            assert not current_execution().waits
        await run_controlled("config-rejections", lambda: False, job())
        # Manual retry/new run gets a fresh counter; nothing is disabled globally.
        await run_controlled("manual-retry", lambda: False, job())


@pytest.mark.asyncio
async def test_success_or_different_failure_breaks_configuration_streak(monkeypatch, scheduler):
    replies = iter([403, 403, 200, 403, 403, 400, 403, 403, 200])
    sends = []
    async def invoke(_):
        status = next(replies)
        sends.append(status)
        return httpx.Response(status, json={"error": {"message": "private"}}) if status != 200 else SimpleNamespace(content="ok")
    async with provider_for("relay_openai_chat_completions", invoke, monkeypatch) as provider:
        async def job():
            for _ in range(9):
                try:
                    await llm.ainvoke_with_rate_retry(provider, MESSAGES)
                except ProviderRequestError:
                    pass
            assert not current_execution().blocked
        await run_controlled("intermittent", lambda: False, job())
    assert len(sends) == 9


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["openai", "relay_openai_chat_completions"])
@pytest.mark.parametrize("wrapper", ["permission_error", "team_model_access_denied"])
async def test_content_403_is_not_configuration_failure_or_retried(monkeypatch, scheduler, route, wrapper):
    sends = []
    async def invoke(_):
        sends.append(1)
        body = {"error": {"code": "DataInspectionFailed", "type": wrapper, "message": "private"}}
        if route.startswith("relay_"):
            return httpx.Response(403, json=body)
        error = RuntimeError("private")
        error.status_code, error.body = 403, body
        raise error
    async with provider_for(route, invoke, monkeypatch) as provider:
        async def job():
            for _ in range(5):
                with pytest.raises(llm.PermanentLLMError, match="provider_content_blocked"):
                    await llm.ainvoke_with_retry(provider, MESSAGES)
            assert not current_execution().blocked
        await run_controlled("content-refusals", lambda: False, job())
    assert len(sends) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 402, 405, 413, 415, 422, 451])
async def test_deterministic_request_rejection_is_not_automatically_repeated(status):
    sends = []
    async def invoke(_):
        sends.append(1)
        raise ProviderRequestError("provider_request_rejected", status_code=status)
    provider = SimpleNamespace(ainvoke=invoke)
    with pytest.raises(llm.PermanentLLMError):
        await llm.ainvoke_with_retry(provider, [])
    assert len(sends) == 1


@pytest.mark.asyncio
async def test_stopping_rejected_model_keeps_inflight_result_and_other_model(monkeypatch, scheduler):
    monkeypatch.setattr(settings, "max_concurrent_llm_per_endpoint", 2)
    sent, finish = asyncio.Event(), asyncio.Event()
    sends = []
    async def invoke(_):
        sends.append(1)
        if len(sends) == 1:
            sent.set()
            await finish.wait()
            return SimpleNamespace(content="retained")
        return httpx.Response(403, json={"error": {"message": "private"}})
    async with provider_for("relay_openai_chat_completions", invoke, monkeypatch) as provider:
        async def job():
            active = asyncio.create_task(llm.ainvoke_with_rate_retry(provider, MESSAGES))
            await sent.wait()
            try:
                for _ in range(3):
                    with pytest.raises(ProviderRequestError):
                        await llm.ainvoke_with_rate_retry(provider, MESSAGES)
                with pytest.raises(ProviderRequestError) as blocked:
                    await llm.ainvoke_with_rate_retry(provider, MESSAGES)
                assert blocked.value.retryable is False
                assert len(sends) == 4 and not active.done()
                async with provider_for("openai", lambda _: asyncio.sleep(0, result=SimpleNamespace(content="healthy")), monkeypatch) as healthy:
                    assert (await llm.ainvoke_with_rate_retry(healthy, MESSAGES)).content == "healthy"
                finish.set()
                assert (await active).content == "retained"
                assert current_execution().blocked  # A late result cannot restart unsent work.
            finally:
                finish.set()
                await asyncio.gather(active, return_exceptions=True)
        await asyncio.wait_for(run_controlled("parallel-auth", lambda: False, job()), 10)
    assert scheduler.active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("statuses,attempted,failed,unprocessed", [
    ([403, 403, 403, 200], [1, 2, 3], [1, 2, 3], [4]),
    ([403, 200, 403, 200], [1, 2, 3, 4], [1, 3], []),
])
async def test_single_pdf_uses_run_threshold_and_preserves_other_pages(monkeypatch, scheduler, statuses, attempted, failed, unprocessed):
    from backend.recognition.executor import read_pdf_plan
    from backend.skills.recognition_reader import LLMRecognitionEngine
    from backend.tests.test_recognition_executor import pdf, setup
    sends = []
    async def invoke(_):
        status = statuses[len(sends)]
        sends.append(1)
        return httpx.Response(status, json={"error": {"message": "private"}}) if status != 200 else SimpleNamespace(content="literal source")
    async with provider_for("relay_openai_chat_completions", invoke, monkeypatch) as provider:
        engine = LLMRecognitionEngine(provider, route_id="owner-route", fingerprint="frozen")
        data = pdf(4)
        kwargs = await setup(data, engine)
        result = await run_controlled("single-pdf", lambda: False, read_pdf_plan(data, **kwargs))
    assert len(sends) == len(attempted)
    assert [unit.page_numbers[0] for unit in result.units] == attempted
    assert result.failed_pages == failed
    assert result.unprocessed_pages == unprocessed


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["provider_recitation_blocked", "provider_content_blocked"])
@pytest.mark.parametrize("uncertain", [False, True])
async def test_content_refusal_continues_pdf_only_when_submission_is_known(code, uncertain):
    from backend.domain.errors import RecognitionError
    from backend.recognition.executor import read_pdf_plan
    from backend.tests.test_recognition_executor import FakeEngine, pdf, setup
    class Engine(FakeEngine):
        async def recognize(self, request):
            result = await super().recognize(request)
            if len(self.requests) == 1:
                raise RecognitionError(code, submission_may_exist=uncertain)
            return result
    data, engine = pdf(), Engine()
    result = await read_pdf_plan(data, **await setup(data, engine))
    assert [r.page_number for r in engine.requests] == ([1] if uncertain else [1, 2, 3])
    assert result.failed_pages == [1]
    assert result.unprocessed_pages == ([2, 3] if uncertain else [])


@pytest.mark.asyncio
async def test_content_refusal_does_not_skip_other_regions_of_image():
    from backend.domain.errors import RecognitionError
    from backend.recognition.image_executor import read_image_plan
    from backend.recognition.models import NormalizedRegionV1
    from backend.tests.test_recognition_image_executor import ImageEngine, image_data, setup
    class Engine(ImageEngine):
        async def recognize(self, request):
            result = await super().recognize(request)
            if len(self.requests) == 1:
                raise RecognitionError("provider_content_blocked")
            return result
    data, engine = image_data(), Engine()
    regions = [NormalizedRegionV1(x0=0, y0=0, x1=0.5, y1=1),
               NormalizedRegionV1(x0=0.5, y0=0, x1=1, y1=1)]
    result = await read_image_plan(data, **setup(data, engine, regions=regions))
    assert len(engine.requests) == 2 and not result.unprocessed_regions
    assert result.failed_pages == [1] and result.units[1].candidate.status == "ok"


@pytest.mark.asyncio
async def test_refused_page_is_retained_and_manual_retry_reuses_successes(tmp_path):
    from backend.domain.errors import RecognitionError
    from backend.tests.test_recognition_durable_runs import runner, run
    service, request, data, engine = runner(tmp_path, pdf=True, pages=3, initial=[
        RecognitionError("provider_recitation_blocked"), "literal second page", "literal third page",
    ])
    first = await run(service, request, data, engine)
    assert first.status == "needs_review"
    assert [call.page_number for call in engine.reads] == [1, 2, 3]
    assert first.assembly.raw.read_batch.failed_pages == [1]
    assert first.assembly.raw.read_batch.unprocessed_pages == []
    assert (await run(service, request, None, engine)).status == "needs_review"
    assert len(engine.reads) == 3  # Refresh is not permission to replay a refusal.
    engine.initial[0] = "literal first page"
    args = dict(engine=engine, prompt_version="faithful-reader-v1",
                authorized_owner_id=request.source.owner_id, retry_key="content-failed-only:1")
    retried = await service.run(request, data, **args)
    assert retried.status == "completed", retried.safe_error_code
    assert [call.page_number for call in engine.reads] == [1, 2, 3, 1]
    assert [record.origin for record in retried.assembly.raw.read_batch.provenance] == ["dispatch", "cache_hit", "cache_hit"]
