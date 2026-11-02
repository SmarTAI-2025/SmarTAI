"""Response-only OCR keeps precise dispatch evidence and independent local limits."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from backend.config import settings
from backend.db import workflow_repository
from backend.domain.errors import LeaseLost, RecognitionError
from backend.llm import providers
from backend.llm.providers import ProviderRequestError
from backend.llm.request_timeouts import current_response_scope
from backend.recognition.budget import RecognitionBudget, validate_budget_timing
from backend.recognition.cache_identity import final_cache_identity
from backend.recognition.durable_records import RecognitionRunCheckpointV1, authorize_dispatch
from backend.recognition.engine import EngineReadInputV1
from backend.recognition.models import RecognitionPolicyV1
from backend.recognition.runtime import RecognitionCapacity, run_initial_read
from backend.services import recognition_runs
from backend.services.recognition_session import RecognitionCallSessionV2
from backend.services.recognition_timeouts import (
    LEGACY_SOURCE_TIMEOUT_PROFILE, SOURCE_TIMEOUT_PROFILE, apply_source_timeout_profile,
)
from backend.tests.test_recognition_durable_runs import run, runner
from backend.tests.test_recognition_timeout_policy import Clock, VisionEngine, read_request
from backend.tests.test_recognition_response_timeouts import relay, response as relay_response, scheduler
from backend.skills.recognition_reader import LLMRecognitionEngine


class AdmittedEngine:
    """Fake model implementing the same explicit admission/send hook as providers."""

    def __init__(self, engine, *, local_wait=None, response=None, clock=None):
        self.engine, self.local_wait, self.response, self.clock = engine, local_wait, response, clock
        self.sends = []
        self.scopes = []

    @property
    def capabilities(self):
        return self.engine.capabilities.model_copy(deep=True)

    async def _call(self, method, request):
        scope = current_response_scope()
        assert scope is not None
        self.scopes.append(scope)
        if self.local_wait is not None:
            await scope.wait_local(self.local_wait())
        if self.clock is not None:
            self.clock.now += 590  # A simulated local wait, below its own 600s cap.

        async def submit():
            self.sends.append((method, request.page_number))
            if self.clock is not None:
                self.clock.now += 290  # A response within the individual 300s cap.
            if self.response is not None:
                return await self.response(scope, request)
            return await getattr(self.engine, method)(request)

        return await scope.run_response(submit)

    async def recognize(self, request):
        return await self._call("recognize", request)

    async def repair(self, request):
        return await self._call("repair", request)


def new_policy(request, engine):
    return apply_source_timeout_profile(request, engine=engine, timeout_profile=SOURCE_TIMEOUT_PROFILE)


def new_runner(tmp_path, **kwargs):
    service, request, data, base = runner(tmp_path, **kwargs)
    engine = AdmittedEngine(base)
    return service, new_policy(request, engine), data, engine


def arguments(request, engine, budget, capacity):
    return dict(
        engine=engine,
        request=EngineReadInputV1(purpose=request.purpose, input_mode="page_image", page_number=1,
                                 content_type="image/png", payload=b"synthetic image"),
        source=request.source, policy=request.policy, capabilities=engine.capabilities,
        region_keys=("p1",), budget=budget, capacity=capacity,
    )


def test_new_profile_is_explicit_and_old_profile_identity_is_unchanged():
    request, engine = read_request(), VisionEngine()
    before = request.model_dump_json()
    old = apply_source_timeout_profile(request, engine=engine, timeout_profile=LEGACY_SOURCE_TIMEOUT_PROFILE)
    expected = request.model_copy(update={"policy": RecognitionPolicyV1.model_validate({
        **request.policy.model_dump(), "per_call_seconds": 300, "read_seconds": 6000,
        "locator_seconds": 360, "total_seconds": 6060,
    })})
    assert old.model_dump_json() == expected.model_dump_json()
    assert "timeout_mode" not in old.policy.model_dump()
    identity = lambda value: final_cache_identity(value, capabilities=engine.capabilities,
                                                  prompt_version="faithful-reader-v1", workflow_version=2)
    assert identity(old) == identity(expected)
    selected = new_policy(request, engine)
    assert selected.policy.timeout_mode == "response_only" and selected.policy.per_call_seconds == 300
    assert selected.policy.max_calls == request.policy.max_calls
    assert selected.policy.max_initial_calls == request.policy.max_initial_calls
    assert selected.policy.max_output_tokens == request.policy.max_output_tokens
    assert identity(selected) != identity(old)
    assert request.model_dump_json() == before
    assert apply_source_timeout_profile(request, engine=engine, timeout_profile=None) is request


@pytest.mark.parametrize("kind", ["native", "ocr", "knowledge_purpose", "knowledge_scope"])
def test_new_profile_does_not_change_native_ocr_or_knowledge(kind):
    request, engine = read_request(), VisionEngine()
    if kind == "native":
        engine = None
    elif kind == "ocr":
        engine = VisionEngine(candidate_kind="ocr")
    elif kind == "knowledge_purpose":
        request = request.model_copy(update={"purpose": "knowledge"})
    else:
        request = request.model_copy(update={"source": request.source.model_copy(update={"scope": "knowledge_document"})})
    assert new_policy(request, engine) is request


def test_response_only_budget_has_no_cumulative_deadline_but_retains_call_and_token_limits():
    request, engine, clock = read_request(), VisionEngine(), Clock()
    selected = new_policy(request, engine)
    policy = selected.policy.model_copy(update={"max_initial_calls": 1, "max_calls": 1, "max_output_tokens": 10})
    budget = RecognitionBudget(selected.source, policy, engine.capabilities, clock=clock, elapsed_seconds=50000)
    budget.start_phase("locator")
    clock.now = 100000
    budget.preflight("initial", 10, region_keys=("p1",))
    assert budget.snapshot().total_calls == 0  # Checking admission is not a paid reservation.
    assert budget.remaining("read") is None and budget.local_timeout(10, "read") == 10
    ticket = budget.reserve("initial", 10, region_keys=("p1",))
    with pytest.raises(RecognitionError, match="recognition_budget_exhausted"):
        budget.preflight("initial", 10, region_keys=("p2",))
    budget.settle(ticket, 4, 2, outcome="ok")
    snapshot = budget.snapshot()
    assert snapshot.duration_ms == 150000000
    assert snapshot.global_remaining_seconds is None
    assert snapshot.read_remaining_seconds is None and snapshot.locator_remaining_seconds is None
    assert json.loads(snapshot.model_dump_json())["global_remaining_seconds"] is None
    validate_budget_timing(snapshot, policy)
    with pytest.raises(ValueError):
        validate_budget_timing(snapshot, RecognitionPolicyV1())
    with pytest.raises(ValidationError):
        RecognitionPolicyV1(timeout_mode="unlimited")


def test_durable_authorization_ignores_elapsed_time_only_for_the_explicit_new_mode():
    request, engine = read_request(), VisionEngine()
    checkpoint = RecognitionRunCheckpointV1(operation_id="operation", attempt=1, identity_key="a" * 64, started_at=1)
    args = dict(kind="initial", identity_key="b" * 64, region_keys=("p1",), max_output_tokens=10,
                capabilities=engine.capabilities, now=50000)
    with pytest.raises(RecognitionError, match="recognition_timeout"):
        authorize_dispatch(checkpoint, policy=request.policy, **args)
    pending = authorize_dispatch(checkpoint, policy=new_policy(request, engine).policy, **args)
    assert len(pending.calls) == 1 and pending.calls[0].state == "pending"
    with pytest.raises(RecognitionError, match="provider_submit_uncertain"):
        authorize_dispatch(pending, policy=new_policy(request, engine).policy, **{**args, "identity_key": "c" * 64})


@pytest.mark.asyncio
async def test_response_clock_starts_after_local_admission(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def local_wait():
        entered.set()
        await release.wait()

    engine = AdmittedEngine(VisionEngine(), local_wait=local_wait)
    request = new_policy(read_request(), engine)
    request.policy.per_call_seconds = 0.03
    monkeypatch.setattr(settings, "llm_timeout", 1)
    budget = RecognitionBudget(request.source, request.policy, engine.capabilities)
    task = asyncio.create_task(run_initial_read(**arguments(request, engine, budget, RecognitionCapacity())))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.sleep(0.08)  # Longer than the complete response allowance.
        assert not task.done() and engine.sends == []
        assert budget.snapshot().total_calls == 0
        release.set()
        outcome = await asyncio.wait_for(task, 1)
        assert outcome.candidate.status == "ok" and not outcome.submission_may_exist
        assert engine.sends == [("recognize", 1)]
        assert budget.snapshot().settled_calls == 1
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_recognition_and_model_queues_share_one_local_wait_allowance(monkeypatch):
    entered = asyncio.Event()

    async def local_wait():
        entered.set()
        await asyncio.Event().wait()

    engine = AdmittedEngine(VisionEngine(), local_wait=local_wait)
    request = new_policy(read_request(), engine)
    monkeypatch.setattr(settings, "llm_timeout", 0.2)
    budget = RecognitionBudget(request.source, request.policy, engine.capabilities)
    capacity = RecognitionCapacity(global_limit=1)
    async with capacity.lease(request.source.owner_id):
        task = asyncio.create_task(run_initial_read(**arguments(request, engine, budget, capacity)))
        await asyncio.sleep(0.12)
        assert engine.scopes == []
    await asyncio.wait_for(entered.wait(), 1)
    assert 0 < engine.scopes[0].local_wait_remaining < 0.12
    with pytest.raises(RecognitionError, match="provider_capacity_unavailable") as caught:
        await asyncio.wait_for(task, 1)
    assert not caught.value.submission_may_exist
    assert engine.sends == [] and budget.snapshot().total_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("layer", ["recognition", "provider"])
async def test_queue_timeout_persists_no_pending_and_safe_retry_needs_no_ack(tmp_path, monkeypatch, layer):
    service, request, data, engine = new_runner(tmp_path, initial=["literal 1 + 1 = 3"])
    monkeypatch.setattr(settings, "llm_timeout", 0.02)
    if layer == "provider":
        engine.local_wait = lambda: asyncio.Event().wait()
        first = await run(service, request, data, engine)
    else:
        service.capacity = RecognitionCapacity(global_limit=1)
        async with service.capacity.lease(request.source.owner_id):
            first = await run(service, request, data, engine)
    assert first.status == "needs_review" and first.safe_error_code == "provider_capacity_unavailable"
    assert first.current_usage.total_calls == first.operation_usage.total_calls == 0
    persisted = workflow_repository.get_operation(first.operation_id, owner_id=request.source.owner_id)
    assert persisted.checkpoint["calls"] == [] and engine.sends == []
    assert persisted.lease_token is None
    engine.local_wait = None
    retried = await service.run(request, data, engine=engine, prompt_version="faithful-reader-v1",
                                authorized_owner_id=request.source.owner_id, retry_key="safe-parent-retry")
    assert retried.status == "completed", retried.safe_error_code
    assert engine.sends == [("recognize", 1)]


@pytest.mark.asyncio
async def test_sent_timeout_stops_later_pages_and_pending_never_replays(tmp_path):
    service, request, data, engine = new_runner(tmp_path, pdf=True, pages=2, initial=["literal 1 + 1 = 3"])
    request.policy.per_call_seconds = 0.02

    async def blocked(_scope, _request):
        await asyncio.Event().wait()

    engine.response = blocked
    first = await run(service, request, data, engine)
    assert first.status == "needs_review" and first.safe_error_code == "provider_submit_uncertain"
    assert engine.sends == [("recognize", 1)] and engine.engine.repairs == []
    persisted = workflow_repository.get_operation(first.operation_id, owner_id=request.source.owner_id)
    assert persisted.checkpoint["calls"][0]["state"] == "pending"
    assert first.operation_usage.total_calls == 1 and first.operation_usage.output_tokens is None
    assert (await run(service, request, data, engine)).safe_error_code == "provider_submit_uncertain"
    assert engine.sends == [("recognize", 1)]


@pytest.mark.asyncio
async def test_twelve_admitted_pages_survive_more_than_the_previous_maximum_total(tmp_path, monkeypatch):
    service, request, data, engine = new_runner(tmp_path, pdf=True, pages=12, initial=["literal 1 + 1 = 3"])
    clock = Clock()
    service.clock = engine.clock = clock
    original = recognition_runs.RecognitionAgent

    class TimedAgent(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs, clock=clock)

    monkeypatch.setattr(recognition_runs, "RecognitionAgent", TimedAgent)
    result = await run(service, request, data, engine)
    assert result.status == "completed", result.safe_error_code
    assert engine.sends == [("recognize", number) for number in range(1, 13)] + [
        ("repair", number) for number in range(1, 5)
    ]
    assert result.operation_usage.duration_ms == 14080000
    assert result.assembly.raw.budget.duration_ms == 10560000
    assert result.assembly.repair_execution.budget.duration_ms == 14080000
    assert result.assembly.raw.budget.global_remaining_seconds is None
    assert result.assembly.document.coverage.complete
    assert (await run(service, request, None, engine)).status == "already_done"
    assert len(engine.sends) == 16


@pytest.mark.asyncio
async def test_new_mode_ignores_old_numeric_caps_and_uses_short_renewable_lease(tmp_path, monkeypatch):
    service, request, data, engine = new_runner(tmp_path, initial=["literal 1 + 1 = 3"])
    request.policy.total_seconds = request.policy.read_seconds = request.policy.locator_seconds = 0.001
    claims, beats = [], []
    original_claim = workflow_repository.claim_operation
    original_start = recognition_runs._RecognitionLeaseHeartbeat.start

    def claim(*args, **kwargs):
        claims.append(kwargs["lease_seconds"])
        return original_claim(*args, **kwargs)

    def start(self):
        beats.append(self)
        original_start(self)

    monkeypatch.setattr(workflow_repository, "claim_operation", claim)
    monkeypatch.setattr(recognition_runs._RecognitionLeaseHeartbeat, "start", start)
    result = await run(service, request, data, engine)
    assert result.status == "completed", result.safe_error_code
    assert claims == [300] and len(beats) == 1 and beats[0].task.done()
    assert beats[0].failure is None


@pytest.mark.asyncio
async def test_new_mode_requires_an_explicit_provider_send_boundary():
    engine = VisionEngine()  # Deliberately lacks the scope/run_response contract.
    request = new_policy(read_request(), engine)
    budget = RecognitionBudget(request.source, request.policy, engine.capabilities)
    with pytest.raises(RecognitionError, match="recognition_response_invalid"):
        await run_initial_read(**arguments(request, engine, budget, RecognitionCapacity()))
    assert budget.snapshot().total_calls == 0


def session_for(request, *, guard=None):
    service = SimpleNamespace(
        owner=request.source.owner_id, binding=SimpleNamespace(business_id=request.source.business_id),
        lookup=AsyncMock(return_value=SimpleNamespace(status="miss")),
        before_dispatch=guard or AsyncMock(),
    )
    session = RecognitionCallSessionV2(service, source=request.source, authorized_owner_id=request.source.owner_id)
    return session, service


@pytest.mark.asyncio
async def test_real_reader_provider_queue_does_not_authorize_or_start_a_model_request(scheduler, monkeypatch):
    post_calls = []

    async def handle(request):
        post_calls.append(request)
        return relay_response()

    @asynccontextmanager
    async def unavailable(*args, **kwargs):
        await asyncio.Event().wait()
        yield

    monkeypatch.setattr(settings, "llm_timeout", 0.025)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), timeout=120) as client:
        provider = relay(client)
        engine = LLMRecognitionEngine(provider, route_id="test-route", fingerprint="frozen")
        request = new_policy(read_request(), engine)
        budget = RecognitionBudget(request.source, request.policy, engine.capabilities)
        session, service = session_for(request)
        capacity = RecognitionCapacity(global_limit=1)
        call = arguments(request, engine, budget, capacity)
        monkeypatch.setattr(providers, "get_scheduler", lambda: SimpleNamespace(lease=unavailable))
        with pytest.raises(RecognitionError, match="provider_capacity_unavailable") as caught:
            await run_initial_read(**call, call_session=session)
    assert not caught.value.submission_may_exist
    assert post_calls == [] and service.before_dispatch.await_count == 0
    assert budget.snapshot().pending_calls == budget.snapshot().total_calls == 0
    assert session._pending is None


@pytest.mark.asyncio
async def test_real_reader_uses_byok_admission_without_waiting_for_separate_ocr_gate(scheduler, monkeypatch):
    posts = []

    async def handle(request):
        posts.append(request)
        return relay_response()

    monkeypatch.setattr(settings, "llm_timeout", 1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), timeout=120) as client:
        engine = LLMRecognitionEngine(relay(client), route_id="test-route", fingerprint="frozen")
        request = new_policy(read_request(), engine)
        budget = RecognitionBudget(request.source, request.policy, engine.capabilities)
        session, service = session_for(request)
        capacity = RecognitionCapacity(global_limit=1)
        async with capacity.lease("other-owner"):
            result = await run_initial_read(**arguments(request, engine, budget, capacity), call_session=session)
    assert result.candidate.status == "ok"
    assert len(posts) == service.before_dispatch.await_count == 1
    assert budget.snapshot().settled_calls == 1 and budget.snapshot().pending_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,pending", [
    ("connect_timeout", False), ("read_timeout", True), ("upstream_503", True),
    ("invalid_200", True), ("route_changed", True),
])
async def test_real_reader_retains_unknown_responses_but_settles_proved_no_connection(
    tmp_path, scheduler, failure, pending,
):
    posts = []

    async def handle(request):
        posts.append(request)
        if failure == "connect_timeout":
            raise httpx.ConnectTimeout("synthetic connection timeout")
        if failure == "read_timeout":
            raise httpx.ReadTimeout("synthetic response timeout")
        if failure == "upstream_503":
            return httpx.Response(503, json={"error": {"message": "synthetic unavailability"}})
        if failure == "invalid_200":
            return httpx.Response(200, json={"choices": []})
        provider.config.model = "changed-model"
        return relay_response()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), timeout=120) as client:
        provider = relay(client)
        engine = LLMRecognitionEngine(provider, route_id="test-route", fingerprint="frozen")
        service, request, data, _ = runner(tmp_path)
        request = new_policy(request, engine)
        result = await run(service, request, data, engine)
    assert len(posts) == 1 and result.status == "needs_review"
    persisted = workflow_repository.get_operation(result.operation_id, owner_id=request.source.owner_id)
    checkpoint = persisted.checkpoint["calls"]
    assert len(checkpoint) == 1
    assert checkpoint[0]["state"] == ("pending" if pending else "stored")
    assert checkpoint[0]["submission_may_exist"] is pending
    assert result.assembly.raw.budget.pending_calls == int(pending)
    if pending:
        assert result.safe_error_code == "provider_submit_uncertain"
    else:
        assert result.safe_error_code in {"provider_timeout", "provider_unreachable"}


@pytest.mark.asyncio
async def test_explicit_429_then_local_wait_failure_settles_the_existing_dispatch(
    tmp_path, scheduler, monkeypatch,
):
    from tenacity import wait_fixed
    from backend.tools import structured_llm

    admissions, posts = [], []

    @asynccontextmanager
    async def lease(*args, **kwargs):
        admissions.append(None)
        if len(admissions) > 1:
            await asyncio.Event().wait()
        yield

    async def handle(request):
        posts.append(request)
        return httpx.Response(429, json={"error": {"message": "Rate limit"}})

    monkeypatch.setattr(providers, "get_scheduler", lambda: SimpleNamespace(lease=lease))
    monkeypatch.setattr(settings, "llm_timeout", 0.025)
    monkeypatch.setattr(structured_llm._invoke_with_rate_retry.retry, "wait", wait_fixed(0))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), timeout=120) as client:
        engine = LLMRecognitionEngine(relay(client), route_id="test-route", fingerprint="frozen")
        service, request, data, _ = runner(tmp_path)
        request = new_policy(request, engine)
        result = await run(service, request, data, engine)
    assert len(posts) == 1 and len(admissions) == 2
    assert result.status == "needs_review" and result.safe_error_code == "provider_capacity_unavailable"
    assert result.current_usage.total_calls == result.operation_usage.total_calls == 1
    persisted = workflow_repository.get_operation(result.operation_id, owner_id=request.source.owner_id)
    assert persisted.checkpoint["calls"][0]["state"] == "stored"
    assert result.assembly.raw.budget.pending_calls == 0


@pytest.mark.asyncio
async def test_guard_lease_loss_crosses_real_provider_and_reader_without_becoming_model_error(scheduler):
    posts = []

    async def handle(request):
        posts.append(request)
        return relay_response()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), timeout=120) as client:
        engine = LLMRecognitionEngine(relay(client), route_id="test-route", fingerprint="frozen")
        request = new_policy(read_request(), engine)
        budget = RecognitionBudget(request.source, request.policy, engine.capabilities)
        fence = LeaseLost("Synthetic fence loss")
        guard = AsyncMock(side_effect=fence)
        session, _ = session_for(request, guard=guard)
        with pytest.raises(LeaseLost) as caught:
            await run_initial_read(**arguments(request, engine, budget, RecognitionCapacity()), call_session=session)
    assert caught.value is fence
    assert guard.await_count == 1 and posts == [] and session._pending is None
    assert budget.snapshot().total_calls == 0


@pytest.mark.asyncio
async def test_durable_guard_lease_loss_reaches_worker_without_a_terminal_overwrite(tmp_path, scheduler, monkeypatch):
    posts, claimed = [], []
    original_save = workflow_repository.save_operation_checkpoint
    original_claim = workflow_repository.claim_operation
    fence = LeaseLost("Synthetic fence loss")

    def claim(*args, **kwargs):
        row = original_claim(*args, **kwargs)
        claimed.append(row)
        return row

    def save(*args, **kwargs):
        if kwargs["checkpoint"]["calls"]:
            raise fence
        return original_save(*args, **kwargs)

    async def handle(request):
        posts.append(request)
        return relay_response()

    monkeypatch.setattr(workflow_repository, "claim_operation", claim)
    monkeypatch.setattr(workflow_repository, "save_operation_checkpoint", save)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), timeout=120) as client:
        engine = LLMRecognitionEngine(relay(client), route_id="test-route", fingerprint="frozen")
        service, request, data, _ = runner(tmp_path)
        request = new_policy(request, engine)
        with pytest.raises(LeaseLost) as caught:
            await run(service, request, data, engine)
    assert caught.value is fence and posts == []
    persisted = workflow_repository.get_operation(claimed[0].id, owner_id=request.source.owner_id)
    assert persisted.terminal_summary is None and persisted.checkpoint["calls"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("preflight_delay", [0, 0.05], ids=["normal-db", "slow-db"])
async def test_interrupted_guard_commit_remains_conservative_without_automatic_resubmit(
    tmp_path, monkeypatch, preflight_delay,
):
    service, request, data, engine = new_runner(tmp_path, initial=["literal 1 + 1 = 3"])
    original = recognition_runs._RunContext.write
    original_active = recognition_runs._RunContext.active
    committed = asyncio.Event()

    async def active(self):
        # Slow pre-commit I/O must not consume a synthetic 20ms deadline
        # intended to interrupt the later, already-committed guard write.
        await asyncio.sleep(preflight_delay)
        return await original_active(self)

    async def interrupted(self, checkpoint, **kwargs):
        await original(self, checkpoint, **kwargs)
        if checkpoint.calls and kwargs.get("status") is None and not committed.is_set():
            committed.set()
            # Arm the real cancellation/TimeoutError only after persistence.
            # Do not shorten the product's entire authorization/DB deadline.
            async with asyncio.timeout(0):
                await asyncio.Event().wait()

    monkeypatch.setattr(recognition_runs._RunContext, "active", active)
    monkeypatch.setattr(recognition_runs._RunContext, "write", interrupted)
    result = await run(service, request, data, engine)
    assert committed.is_set() and engine.sends == []
    assert result.status == "needs_review" and result.safe_error_code == "provider_submit_uncertain"
    persisted = workflow_repository.get_operation(result.operation_id, owner_id=request.source.owner_id)
    assert persisted.checkpoint["calls"][0]["state"] == "pending"
    assert (await run(service, request, data, engine)).safe_error_code == "provider_submit_uncertain"
    assert engine.sends == []


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_optional_progress_timeout_keeps_response_but_external_cancel_propagates(monkeypatch, cancel):
    from backend.recognition import local_limits

    entered = asyncio.Event()

    async def stalled(**kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(local_limits, "LOCAL_STEP_SECONDS", 0.025)
    engine = AdmittedEngine(VisionEngine())
    request = new_policy(read_request(), engine)
    budget = RecognitionBudget(request.source, request.policy, engine.capabilities)
    reporter = SimpleNamespace(increment_stage_metrics=stalled)
    task = asyncio.create_task(run_initial_read(
        **arguments(request, engine, budget, RecognitionCapacity()), progress=reporter,
    ))
    await asyncio.wait_for(entered.wait(), 1)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result = await asyncio.wait_for(task, 1)
        assert result.candidate.status == "ok" and not result.submission_may_exist
    assert budget.snapshot().settled_calls == 1 and engine.sends == [("recognize", 1)]
