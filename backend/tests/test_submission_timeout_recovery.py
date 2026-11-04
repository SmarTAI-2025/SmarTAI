"""Real worker/provider boundaries, scaled clocks and synthetic local responses."""
import asyncio
from contextvars import ContextVar
import io
import json
import threading
import time
from types import SimpleNamespace
import zipfile

import pytest

from backend.db import workflow_repository
from backend.domain.errors import RecognitionError
from backend.llm import request_timeouts
from backend.llm.providers import GeminiProvider
from backend.llm.registry import _GuardedSharedProvider
from backend.models import ProviderConfig
from backend.recognition.budget import RecognitionBudget
from backend.recognition.runtime import RecognitionCapacity, run_initial_read
from backend.services import recognition_runs, submission_work_budget as clocks, task_facade
from backend.services.workflow_worker import WorkflowWorker
from backend.skills.recognition_reader import LLMRecognitionEngine
from backend.tests.test_provider_response_deadlines import MESSAGES, ROUTES, provider_for, scheduler
from backend.tests.test_recognition_budget import context
from backend.tests.test_recognition_reader import request
from backend.tests.test_task_background_workflows import _Registry, _seed_task


@pytest.mark.asyncio
@pytest.mark.parametrize("route", [route for route in ROUTES if route != "gemini_proxy"])
@pytest.mark.parametrize("response_only", [False, True])
async def test_historical_and_current_provider_calls_charge_work_only_after_queue(
    route, response_only, scheduler, monkeypatch,
):
    monkeypatch.setattr(clocks, "_current", ContextVar("submission_budget", default=None))
    budget = clocks.SubmissionWorkBudget("legacy-retry", limit_seconds=0.02)
    clocks._current.set(budget)
    scheduler.host_capacity.capacity = 0
    calls = []

    async def invoke(_options):
        calls.append(True)
        await asyncio.sleep(0.04)
        return SimpleNamespace(content="completed response")

    async with provider_for(route, invoke, monkeypatch) as provider:
        async def work():
            # Historical recognition policies install no ResponseDeadlineScope.
            with request_timeouts.response_timeout_scope(1, response_only=response_only):
                return await provider.ainvoke(MESSAGES)

        pending = asyncio.create_task(work())
        try:
            await asyncio.sleep(0.04)
            assert not pending.done() and budget.elapsed == 0 and calls == []
            scheduler.host_capacity.capacity = 50
            scheduler.wake.set()
            assert (await asyncio.wait_for(pending, 2)).content == "completed response"
            assert budget.exhausted.is_set() and budget.elapsed >= 0.02
            with pytest.raises(RecognitionError, match=clocks.CODE):
                await work()
            assert calls == [True]  # Drained first response; never sent a replacement.
            assert budget.active == scheduler.active == scheduler.host_capacity.active == 0
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            budget.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("shared", [False, True])
async def test_legacy_recognition_dispatch_counts_underlying_model_not_shared_wrapper_queue(
    shared, scheduler, monkeypatch,
):
    monkeypatch.setattr(clocks, "_current", ContextVar("submission_budget", default=None))
    budget = clocks.SubmissionWorkBudget("legacy-recognition", limit_seconds=0.02)
    clocks._current.set(budget)
    scheduler.host_capacity.capacity = 0

    async def invoke(_options):
        assert budget.active == 1
        await asyncio.sleep(0.04)
        return SimpleNamespace(content="faithful transcription")

    async with provider_for("openai", invoke, monkeypatch) as provider:
        if shared:
            provider = _GuardedSharedProvider(provider, "synthetic-owner")
            monkeypatch.setattr(provider, "_admit", lambda *a, **k: None)
        engine = LLMRecognitionEngine(provider, route_id="synthetic-route", fingerprint="synthetic-config")
        source, policy, _ = context()
        caps = engine.capabilities
        pending = asyncio.create_task(run_initial_read(engine, request(), source=source, policy=policy,
            capabilities=caps, region_keys=("page-3",), budget=RecognitionBudget(source, policy, caps),
            capacity=RecognitionCapacity()))
        try:
            await asyncio.sleep(0.04)
            assert not pending.done() and budget.elapsed == 0
            scheduler.host_capacity.capacity = 50
            scheduler.wake.set()
            result = await asyncio.wait_for(pending, 2)
            assert result.candidate.text == "faithful transcription"
            assert budget.exhausted.is_set() and budget.active == 0
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            budget.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_legacy_gemini_proxy_clock_covers_sync_response_and_cancellation_drain(
    cancel, scheduler, monkeypatch,
):
    monkeypatch.setattr(clocks, "_current", ContextVar("submission_budget", default=None))
    budget = clocks.SubmissionWorkBudget("legacy-proxy-retry", limit_seconds=0.02)
    clocks._current.set(budget)
    monkeypatch.setattr(GeminiProvider, "_needs_proxy_mode", property(lambda self: True))
    provider = GeminiProvider(ProviderConfig(provider_type="gemini", model="fixture", api_key="fake"))
    release, started = threading.Event(), asyncio.Event()
    calls = []
    loop = asyncio.get_running_loop()

    def invoke(*args, **kwargs):
        calls.append(True)
        loop.call_soon_threadsafe(started.set)
        assert release.wait(3)
        return SimpleNamespace(content="saved sync response")

    monkeypatch.setattr(provider, "_build_client_sync", lambda: SimpleNamespace(invoke=invoke))
    scheduler.host_capacity.capacity = 0
    pending = asyncio.create_task(provider.ainvoke(MESSAGES))
    try:
        await asyncio.sleep(0.04)
        assert not started.is_set() and budget.elapsed == 0
        scheduler.host_capacity.capacity = 50
        scheduler.wake.set()
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.wait_for(budget.exhausted.wait(), 1)
        if cancel:
            pending.cancel()
            await asyncio.sleep(0.01)
            assert not pending.done() and budget.active == 1 and scheduler.active == 1
        release.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, 2)
        else:
            assert (await asyncio.wait_for(pending, 2)).content == "saved sync response"
        assert budget.active == scheduler.active == scheduler.host_capacity.active == 0
        with pytest.raises(RecognitionError, match=clocks.CODE):
            await provider.ainvoke(MESSAGES)
        assert calls == [True]
    finally:
        release.set()
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        budget.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancel", "exception"])
@pytest.mark.parametrize("route", ["openai", "anthropic", "gemini"])
async def test_legacy_native_failure_always_releases_active_work(route, failure, scheduler, monkeypatch):
    monkeypatch.setattr(clocks, "_current", ContextVar("submission_budget", default=None))
    budget = clocks.SubmissionWorkBudget("legacy-retry", limit_seconds=1)
    clocks._current.set(budget)
    entered = asyncio.Event()

    async def invoke(_options):
        entered.set()
        assert budget.active == 1
        if failure == "exception":
            raise ValueError("synthetic definite failure")
        await asyncio.Event().wait()

    async with provider_for(route, invoke, monkeypatch) as provider:
        pending = asyncio.create_task(provider.ainvoke(MESSAGES))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            if failure == "cancel":
                pending.cancel()
            with pytest.raises(asyncio.CancelledError if failure == "cancel" else ValueError):
                await pending
            assert budget.active == scheduler.active == 0 and budget.timer is None
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            budget.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_race", [False, True])
async def test_real_worker_saves_parallel_structure_results_after_original_lease_window(
    terminal_race, scheduler, monkeypatch,
):
    owner, task_id = _seed_task(with_question=True)
    claims, calls, cancelled = [], [], []
    both_claimed = asyncio.Event()
    loop = asyncio.get_running_loop()
    original_claim = workflow_repository.claim_operation

    def claim(operation_id, **kwargs):
        row = workflow_repository.get_operation(operation_id, owner_id=kwargs["owner_id"])
        if row.operation_type == "submission_structure_v1":
            kwargs["lease_seconds"] = 1.2
        claimed = original_claim(operation_id, **kwargs)
        if row.operation_type == "submission_structure_v1":
            claims.append(claimed)
            if len(claims) == 2:
                loop.call_soon_threadsafe(both_claimed.set)
        return claimed

    monkeypatch.setattr(workflow_repository, "claim_operation", claim)
    monkeypatch.setattr(recognition_runs, "_RENEWABLE_LEASE_SECONDS", 1.2)
    monkeypatch.setattr(recognition_runs, "_RENEWABLE_HEARTBEAT_SECONDS", 0.1)
    heartbeats = []
    original_start = recognition_runs._RecognitionLeaseHeartbeat.start

    def start_heartbeat(self):
        heartbeats.append(self)
        original_start(self)

    monkeypatch.setattr(recognition_runs._RecognitionLeaseHeartbeat, "start", start_heartbeat)
    terminal_commits, releases, raced = set(), {}, []
    original_checkpoint = workflow_repository.save_operation_checkpoint
    original_heartbeat = workflow_repository.heartbeat_operation

    def checkpoint(*args, **kwargs):
        saved = original_checkpoint(*args, **kwargs)
        if terminal_race and kwargs.get("stage") == "parser_saved":
            releases[saved.id] = threading.Event()
            terminal_commits.add(saved.id)
            # Commit succeeded, but its async caller has not received the row.
            assert releases[saved.id].wait(3)
        return saved

    def heartbeat(operation_id, **kwargs):
        try:
            return original_heartbeat(operation_id, **kwargs)
        finally:
            if operation_id in terminal_commits:
                raced.append(operation_id)
                releases[operation_id].set()

    monkeypatch.setattr(workflow_repository, "save_operation_checkpoint", checkpoint)
    monkeypatch.setattr(workflow_repository, "heartbeat_operation", heartbeat)
    original_scope = request_timeouts.response_timeout_scope
    monkeypatch.setattr(request_timeouts, "response_timeout_scope", lambda seconds, **kwargs:
        original_scope(1 if seconds == 900 else seconds, local_wait_seconds=1, **kwargs))
    scheduler.host_capacity.capacity = 0

    async def invoke(_options):
        number = len(calls) + 1
        calls.append(number)
        try:
            await asyncio.sleep(0.65 if number == 1 else 0.8)
        except asyncio.CancelledError:
            cancelled.append(number)
            raise
        return SimpleNamespace(content=json.dumps({"stu_id": f"S{number}", "stu_name": "Synthetic learner",
            "stu_ans": [{"q_id": "q1", "number": "1", "type": "concept",
                         "content": f"saved answer {number}", "flag": []}]}))

    async with provider_for("openai", invoke, monkeypatch) as provider:
        registry = _Registry()
        registry.provider = provider
        monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _owner: registry)
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("S1.txt", "first answer")
            zipped.writestr("S2.txt", "second answer")
        queued = task_facade.queue_task_submission_parsing(task_id=task_id, owner_id=owner,
            filename="answers.zip", content=archive.getvalue(), content_type="application/zip", registry=registry)
        operation = workflow_repository.get_operation(queued["job_id"], owner_id=owner)
        worker = WorkflowWorker(handlers={operation.operation_type: task_facade.run_durable_submission_recognition},
            worker_id="scaled-real-worker", lease_seconds=10, heartbeat_seconds=0.05,
            poll_seconds=60, claim_batch_size=1, max_in_flight=1, shutdown_seconds=1)
        try:
            assert await worker.poll_once() == 1
            await asyncio.wait_for(both_claimed.wait(), 5)
            await asyncio.sleep(0.75)  # Queue < 1s; response < 1s; their sum > original 1.2s lease.
            scheduler.host_capacity.capacity = 50
            scheduler.wake.set()
            async with asyncio.timeout(8):
                while worker.in_flight_count:
                    await asyncio.sleep(0.01)
            assert time.time() > max(row.lease_expires_at for row in claims)
            after = task_facade.get_task(task_id=task_id, owner_id=owner)
            assert after["status"] == "submissions_ready", (after["status"], after.get("error"), cancelled)
            assert len(after["student_data"]) == 2 and calls == [1, 2] and cancelled == []
            assert {s["stu_ans"][0]["content"] for s in after["student_data"].values()} == {"saved answer 1", "saved answer 2"}
            for row in claims:
                saved = workflow_repository.get_operation(row.id, owner_id=owner)
                assert saved.status == "completed" and saved.checkpoint["artifact_id"]
            assert await worker.poll_once() == 0 and calls == [1, 2]
            assert len(heartbeats) == 2 and all(h.task.done() and h.failure is None for h in heartbeats)
            if terminal_race:
                assert set(raced) == {row.id for row in claims}
        finally:
            for release in releases.values():
                release.set()
            await worker.shutdown()
