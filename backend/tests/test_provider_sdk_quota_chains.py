"""Real provider + locked SDK/LangChain paths; only HTTP responses are simulated."""
import base64
import inspect
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx
import httpx2
import pytest
from langchain_core.messages import HumanMessage

from backend.llm import providers
from backend.llm.providers import GeminiProvider, SafeRelayProvider, VisionImage
from backend.llm.request_timeouts import response_timeout_scope
from backend.models import ProviderConfig
from backend.services.background_errors import classify_background_error
from backend.services.execution_control import run_controlled
from backend.tests.test_provider_quota_boundaries import quota_body
from backend.tests.test_provider_response_deadlines import scheduler  # noqa: F401
from backend.tools import structured_llm as llm

MESSAGES = [HumanMessage(content="Q1. Example question")]
IMAGE = VisionImage(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="), "image/png")
CASES = [
    ("daily", "provider_daily_quota_exceeded", 1),
    ("zero", "provider_zero_quota", 1),
    ("minute", "provider_rate_limited", 3),
]


def response_body(kind):
    if kind.endswith("_text"):
        message = "Quota exceeded for requests per day" if kind == "daily_text" else "Quota exceeded; quota limit: 0"
        return {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": message}}
    body = quota_body(0 if kind == "zero" else 500, daily=kind == "daily")
    body["error"]["status"] = "RESOURCE_EXHAUSTED"
    body["error"]["message"] = "Quota exceeded; remaining quota: 0"
    return body


@pytest.fixture
def http_response(monkeypatch):
    """Intercept both transports used by the locked SDKs, before any network I/O."""
    from google.genai._api_client import BaseApiClient
    monkeypatch.setattr(BaseApiClient, "_use_aiohttp", lambda self: False)
    state = {"body": {}, "calls": 0}

    def send(self, request, **kwargs):
        state["calls"] += 1
        module = httpx2 if isinstance(request, httpx2.Request) else httpx
        return module.Response(429, request=request, json=state["body"], headers={"retry-after": "1"})

    async def asend(self, request, **kwargs):
        return send(self, request, **kwargs)

    for module in (httpx, httpx2):
        monkeypatch.setattr(module.Client, "send", send)
        monkeypatch.setattr(module.AsyncClient, "send", asend)
    monkeypatch.setattr(llm._ainvoke_with_retry.retry, "sleep", AsyncMock())
    monkeypatch.setattr(llm._invoke_with_rate_retry.retry, "sleep", AsyncMock())
    return state


@asynccontextmanager
async def real_provider(kind, monkeypatch, *, proxy=False, protocol=None):
    config = ProviderConfig(provider_type=kind, model="synthetic-model", api_key="synthetic-key", rpm=0,
        **({"base_url": "https://relay.example/v1", "wire_protocol": protocol} if protocol else {}))
    provider = SafeRelayProvider(config) if protocol else providers.PROVIDER_CLASSES[kind](config)
    clients = []
    if protocol:
        provider._safe_async_client = httpx.AsyncClient()
    else:
        build = provider._build_client_sync

        def tracked_build():
            client = build()
            clients.append(client)
            return client

        monkeypatch.setattr(provider, "_build_client_sync", tracked_build)
    monkeypatch.setattr(GeminiProvider, "_needs_proxy_mode", property(lambda self: proxy))
    try:
        yield provider
    finally:
        if protocol:
            await provider._safe_async_client.aclose()
        for client in clients:
            if isinstance(provider, GeminiProvider):
                await client.aclose()
            else:
                for name in ("root_async_client", "root_client", "_async_client", "_client"):
                    resource = getattr(client, name, None)
                    close = getattr(resource, "close", None)
                    if close:
                        result = close()
                        if inspect.isawaitable(result):
                            await result


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["direct", "proxy", "proxy_response_scope"])
@pytest.mark.parametrize("vision", [False, True])
@pytest.mark.parametrize("kind,code,calls", CASES + [
    ("daily_text", "provider_daily_quota_exceeded", 1), ("zero_text", "provider_zero_quota", 1),
])
async def test_gemini_sdk_wrapper_is_classified_before_retry(monkeypatch, scheduler, http_response, mode, vision, kind, code, calls):
    http_response["body"] = response_body(kind)
    async with real_provider("gemini", monkeypatch, proxy=mode != "direct") as provider:
        async def invoke():
            with pytest.raises(Exception) as raised:
                if vision:
                    await llm.ainvoke_vision_with_rate_retry(provider, "Read", [IMAGE], max_output_tokens=100)
                else:
                    await llm.ainvoke_with_retry(provider, MESSAGES)
            return raised.value

        async def round():
            if mode == "proxy_response_scope":
                with response_timeout_scope(30, response_only=True):
                    return await invoke()
            else:
                return await invoke()

        error = await run_controlled("first-click", lambda: False, round())
        assert http_response["calls"] == calls
        assert classify_background_error(error, "workflow_failed") == code
        error = await run_controlled("next-click", lambda: False, round())
        assert http_response["calls"] == 2 * calls
        assert classify_background_error(error, "workflow_failed") == code


OTHER_PROVIDERS = sorted(set(providers.PROVIDER_CLASSES) - {"gemini"})


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_type", OTHER_PROVIDERS)
@pytest.mark.parametrize("vision", [False, True])
@pytest.mark.parametrize("kind,code,calls", CASES + [("billing", "provider_quota_exceeded", 1)])
async def test_other_registered_vendor_sdk_paths(monkeypatch, scheduler, http_response, provider_type, vision, kind, code, calls):
    # OpenAI/Anthropic wrappers retain body/status; domestic providers use the
    # same real OpenAI SDK, including Qwen's streaming request path.
    if kind == "billing":
        body = {"error": {"type": "insufficient_quota", "message": "Insufficient quota", "code": "insufficient_quota"}}
    else:
        message = {"daily": "Quota exceeded for requests per day", "zero": "Quota exceeded; quota limit: 0",
                   "minute": "Requests per minute quota exceeded; remaining quota: 0"}[kind]
        body = {"error": {"type": "rate_limit_error", "message": message, "code": "rate_limit_exceeded"}}
    http_response["body"] = body
    async with real_provider(provider_type, monkeypatch) as provider:
        with pytest.raises(Exception) as raised:
            if vision:
                await llm.ainvoke_vision_with_rate_retry(provider, "Read", [IMAGE], max_output_tokens=100)
            else:
                await llm.ainvoke_with_retry(provider, MESSAGES)
        assert http_response["calls"] == calls
        assert classify_background_error(raised.value, "workflow_failed") == code


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["openai_chat_completions", "anthropic_messages", "gemini_generate_content"])
@pytest.mark.parametrize("vision", [False, True])
@pytest.mark.parametrize("kind,code,calls", CASES)
async def test_custom_endpoint_protocols(monkeypatch, scheduler, http_response, protocol, vision, kind, code, calls):
    http_response["body"] = response_body(kind)
    async with real_provider("openai", monkeypatch, protocol=protocol) as provider:
        with pytest.raises(Exception) as raised:
            if vision:
                await llm.ainvoke_vision_with_rate_retry(provider, "Read", [IMAGE], max_output_tokens=100)
            else:
                await llm.ainvoke_with_retry(provider, MESSAGES)
        assert http_response["calls"] == calls
        assert classify_background_error(raised.value, "workflow_failed") == code


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["problems", "submissions"])
@pytest.mark.parametrize("kind,code", [("daily", "provider_daily_quota_exceeded"), ("zero", "provider_zero_quota")])
async def test_gemini_sdk_quota_reaches_background_task_and_database(monkeypatch, scheduler, http_response, stage, kind, code):
    from backend.db import workflow_repository, source_outcome_repository
    from backend.services import task_facade
    from backend.tests.test_task_background_workflows import _VisionlessRegistry, _seed_task
    http_response["body"] = response_body(kind)
    async with real_provider("gemini", monkeypatch) as provider:
        class Registry(_VisionlessRegistry):
            def pick_default_id(self):
                return provider.provider_id

            def get(self, provider_id):
                return provider if provider_id == provider.provider_id else None

            def list_configs(self):
                return [{"provider_id": provider.provider_id, "enabled": True}]

        registry = Registry()
        registry.provider = provider
        owner, task = _seed_task(with_question=stage == "submissions")
        args = dict(task_id=task, owner_id=owner, filename="student.txt", content=b"Q1. Example answer", registry=registry)
        queue = task_facade.queue_task_problem_extraction if stage == "problems" else task_facade.queue_task_submission_parsing
        queued = queue(**args, content_type="text/plain")
        args.update(job_id=queued["job_id"], job_attempt=queued["_job_attempt"],
                    claimed_workflow_revision=queued["workflow_revision"], replace_confirmed=False,
                    recognition_provider_id=queued["_recognition_provider_id"])
        if stage == "problems":
            await task_facade.run_task_problem_extraction(**args)
        else:
            await task_facade.run_task_submission_parsing(**args, content_type="text/plain", identity_mode="filename", roster_entries=None)
        operation = workflow_repository.get_operation(queued["job_id"], owner_id=owner)
        assert operation.error_code == code
        assert task_facade.get_task(task_id=task, owner_id=owner, full=False)["error"] == code
        assert http_response["calls"] == 1
        if stage == "submissions":
            outcome = source_outcome_repository.list_source_results(operation_id=operation.id, owner_id=owner, attempt=operation.attempt)[0].outcome
            assert outcome.stable_error_code == code


@pytest.mark.asyncio
@pytest.mark.parametrize("vendor_code,code", [(17, "provider_quota_exceeded"), (19, "provider_quota_exceeded"),
    (282005, "provider_quota_exceeded"), (4, "provider_rate_limited"), (18, "provider_rate_limited")])
async def test_baidu_vendor_codes_keep_dedicated_no_resubmit_contract(vendor_code, code):
    from backend.tools import baidu_unlimited_ocr as baidu
    from backend.tests.test_baidu_unlimited_ocr_tool import _client_for_handler
    submissions = []

    def handle(request):
        if str(request.url).startswith(baidu.TOKEN_ENDPOINT):
            return httpx.Response(200, json={"access_token": "synthetic-token", "expires_in": 3600})
        submissions.append(request.url.path)
        return httpx.Response(200, json={"error_code": vendor_code, "error_msg": "Quota rejected"})

    client, transport = _client_for_handler(handle)
    try:
        with pytest.raises(baidu.BaiduUnlimitedOCRError) as raised:
            await client.recognize_document(b"answer", "student.txt")
    finally:
        await transport.aclose()
    assert len(submissions) == 1 and submissions[0].endswith("/unlimited-ocr-parser/task")
    assert raised.value.code == code
    assert raised.value.retryable is False
    assert classify_background_error(raised.value, "workflow_failed") == code


def test_quota_causes_are_cycle_safe_and_do_not_reuse_implicit_context():
    from google.genai.errors import ClientError
    from backend.llm.provider_limits import quota_error_code
    from backend.llm.providers import ProviderRequestError
    current = ProviderRequestError("provider_rate_limited", status_code=429)
    current.__context__ = ClientError(429, response_body("zero"))
    assert quota_error_code(current) == "provider_rate_limited"
    current.__cause__ = current
    assert quota_error_code(current) == "provider_rate_limited"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,error_kind", [("daily", "daily_quota_exhausted"), ("zero", "zero_quota")])
async def test_real_grading_skill_keeps_google_quota_kind(monkeypatch, scheduler, http_response, kind, error_kind):
    from backend.skills.concept import ConceptSkill
    from backend.skills import math_grading
    from backend.tests.test_math_grading import problem, answer
    http_response["body"] = response_body(kind)
    monkeypatch.setattr(math_grading.kb_tool, "retrieve_for_grading", AsyncMock(return_value=[]))
    async with real_provider("gemini", monkeypatch) as provider:
        result = await ConceptSkill(provider).grade(problem("概念题"), answer())
    assert result.error_kind == error_kind
    assert http_response["calls"] == 1
