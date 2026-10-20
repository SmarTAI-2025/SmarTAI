"""Scoped OCR response limits with local transports and no model requests."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import HumanMessage

from backend.config import settings
from backend.llm.concurrency import ModelScheduler
from backend.llm.providers import ProviderRequestError, SafeRelayProvider
from backend.llm.request_timeouts import current_response_timeout_seconds, response_timeout_scope
from backend.models import ProviderConfig
from backend.recognition.budget import RecognitionBudget
from backend.recognition.engine import EngineReadInputV1
from backend.recognition.models import NormalizedRegionV1, RecognitionCandidateV1, RecognitionPolicyV1, RecognitionSourceRefV1
from backend.recognition.planner import EngineCapabilitiesV1
from backend.recognition.runtime import RecognitionCapacity, region_budget_key, run_initial_read
from backend.services.execution_control import current_execution, run_controlled


class FakeHost:
    active = 0

    def try_acquire(self, limit):
        if self.active >= limit:
            return None
        self.active += 1
        released = False

        def release():
            nonlocal released
            if not released:
                released = True
                self.active -= 1

        return SimpleNamespace(release=release)


@pytest.fixture
def scheduler(monkeypatch):
    from backend.llm import providers

    scheduler = ModelScheduler(host_capacity=FakeHost(), memory_reader=lambda: None)
    monkeypatch.setattr(providers, "get_scheduler", lambda: scheduler)
    monkeypatch.setattr(providers, "_ENDPOINT_BREAKERS", {})
    return scheduler


def relay(client):
    provider = SafeRelayProvider(ProviderConfig(
        provider_type="deepseek", model="fixture", api_key="fake",
        base_url="https://relay.example/v1",
    ))
    provider._safe_async_client = client
    return provider


def response():
    return httpx.Response(200, json={
        "choices": [{"message": {"content": "literal source"}, "finish_reason": "stop"}],
    })


@pytest.mark.asyncio
@pytest.mark.parametrize("original_read_seconds", [60, 120])
async def test_scoped_ocr_and_other_requests_share_client_without_sharing_timeout(scheduler, original_read_seconds):
    options = {}
    ocr_started, release = asyncio.Event(), asyncio.Event()

    async def handle(request):
        label = json.loads(request.content)["messages"][0]["content"]
        options[label] = request.extensions["timeout"].copy()
        if label == "ocr":
            ocr_started.set()
            await release.wait()
        return response()

    original_timeout = httpx.Timeout(original_read_seconds, connect=15)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), timeout=original_timeout) as client:
        provider = relay(client)

        async def ocr():
            with response_timeout_scope(300):
                return await provider.ainvoke([HumanMessage(content="ocr")])

        async def other():
            await ocr_started.wait()
            try:
                return await provider.ainvoke([HumanMessage(content="other")])
            finally:
                release.set()

        results = await asyncio.wait_for(asyncio.gather(ocr(), other()), timeout=1)
        await provider.ainvoke([HumanMessage(content="after")])
        assert all(result.content == "literal source" for result in results)
        assert options["ocr"] == {"connect": 15, "read": 300, "write": 300, "pool": 300}
        assert options["other"] == options["after"] == original_timeout.as_dict()
        assert client.timeout == original_timeout
    assert current_response_timeout_seconds() is None
    assert scheduler.active == scheduler.memory_active == scheduler.host_capacity.active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("run_seconds", [60, 300, 600])
async def test_scope_respects_existing_run_cap_without_mutating_execution_control(run_seconds):
    options = []

    async def handle(request):
        options.append(request.extensions["timeout"])
        return response()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), timeout=120) as client:
        provider = relay(client)

        async def work():
            control = current_execution()
            assert provider._response_timeout_override() == run_seconds
            with response_timeout_scope(300):
                await provider._relay_call([HumanMessage(content="ocr")])
                assert current_execution() is control
                assert control.response_timeout_seconds == run_seconds
            assert provider._response_timeout_override() == run_seconds

        await run_controlled("response-scope", lambda: False, work(), response_timeout_seconds=run_seconds)
    assert options[0]["read"] == min(300, run_seconds)
    assert options[0]["connect"] == 15
    assert current_execution() is None
    assert current_response_timeout_seconds() is None


@pytest.mark.asyncio
async def test_complete_response_deadline_stops_keepalives_and_restores_scope(scheduler, monkeypatch):
    monkeypatch.setattr(settings, "llm_timeout", 1)
    reads, closed = [], asyncio.Event()

    async def handle(request):
        assert request.extensions["timeout"]["read"] == 0.05
        try:
            # Mock transports do not enforce read timeouts. This active call
            # must still stop at the complete-response deadline.
            while True:
                reads.append("keepalive")
                await asyncio.sleep(0.003)
        finally:
            closed.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), timeout=120) as client:
        provider = relay(client)
        with pytest.raises(ProviderRequestError, match="provider_timeout"):
            with response_timeout_scope(0.05):
                await asyncio.wait_for(provider.ainvoke([HumanMessage(content="ocr")]), timeout=0.5)
    assert len(reads) >= 2 and closed.is_set()
    assert current_response_timeout_seconds() is None
    assert scheduler.active == scheduler.memory_active == scheduler.host_capacity.active == 0


@pytest.mark.asyncio
async def test_external_cancellation_remains_cancellation_and_restores_scope(scheduler):
    entered, closed = asyncio.Event(), asyncio.Event()
    restored = []

    async def handle(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), timeout=120) as client:
        provider = relay(client)

        async def work():
            try:
                with response_timeout_scope(300):
                    await provider.ainvoke([HumanMessage(content="ocr")])
            finally:
                restored.append(current_response_timeout_seconds())

        task = asyncio.create_task(work())
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert restored == [None] and closed.is_set()
    assert current_response_timeout_seconds() is None
    assert scheduler.active == scheduler.memory_active == scheduler.host_capacity.active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(("per_call", "source_scope", "purpose", "kind", "elapsed", "expected"), [
    (300, "submission_source", "submissions", "vision", 0, 300),
    (300, "submission_source", "submissions", "vision", 520, 80),
    (120, "submission_source", "submissions", "vision", 0, None),
    (60, "submission_source", "submissions", "vision", 0, None),
    (300, "knowledge_document", "knowledge", "vision", 0, None),
    (300, "assignment_source", "knowledge", "vision", 0, None),
    (300, "submission_source", "submissions", "ocr", 0, None),
])
async def test_runtime_only_scopes_extended_task_vision_calls(
    per_call, source_scope, purpose, kind, elapsed, expected,
):
    seen = []
    caps = EngineCapabilitiesV1(route_id="selected", fingerprint="frozen",
                               visual_inputs=["page_image"], candidate_kind=kind)

    async def recognize(request):
        seen.append(current_response_timeout_seconds())
        return RecognitionCandidateV1(kind=kind, status="ok", text="literal source", provider_route_id="selected")

    engine = SimpleNamespace(capabilities=caps, recognize=recognize)
    source = RecognitionSourceRefV1(owner_id="owner", scope=source_scope, business_id="source",
                                   content_type="image/png", input_sha256="a" * 64)
    policy = RecognitionPolicyV1(per_call_seconds=per_call, total_seconds=600, read_seconds=600)
    budget = RecognitionBudget(source, policy, caps, clock=lambda: 1000, elapsed_seconds=elapsed)
    capacity = RecognitionCapacity()
    result = await run_initial_read(
        engine, EngineReadInputV1(purpose=purpose, input_mode="page_image", page_number=1,
                                 content_type="image/png", payload=b"inspected-source"),
        source=source, policy=policy, capabilities=caps,
        region_keys=(region_budget_key(1, NormalizedRegionV1()),), budget=budget, capacity=capacity,
    )
    assert result.candidate.status == "ok" and not result.submission_may_exist
    assert seen == [expected]
    assert current_response_timeout_seconds() is None
