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
from backend.tests.test_recognition_lease_heartbeat import heartbeat_control
from backend.tests.test_task_background_workflows import _Registry, _seed_task


@pytest.mark.asyncio
@pytest.mark.parametrize("route", [route for route in ROUTES if route != "gemini_proxy"])
@pytest.mark.parametrize("response_only", [False, True])
async def test_historical_and_current_provider_calls_charge_work_only_after_queue(
    route, response_only, scheduler, monkeypatch,
):
    monkeypatch.setattr(clocks, "_current", ContextVar("submission_budget", default=None))
    budget = clocks.SubmissionWorkBudget("legacy-retry")
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
            assert not pending.done() and budget.active == 0 and calls == []
            scheduler.host_capacity.capacity = 50
            scheduler.wake.set()
            assert (await asyncio.wait_for(pending, 2)).content == "completed response"
            assert not budget.exhausted.is_set() and budget.active == 0
            budget.expire()
            with pytest.raises(RecognitionError, match=clocks.CODE):
                await work()
            assert calls == [True]  # Drained first response; never sent a replacement.
            assert budget.active == scheduler.active == scheduler.host_capacity.active == 0
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            budget.expire()


@pytest.mark.asyncio
@pytest.mark.parametrize("shared", [False, True])
async def test_legacy_recognition_dispatch_counts_underlying_model_not_shared_wrapper_queue(
    shared, scheduler, monkeypatch,
):
    monkeypatch.setattr(clocks, "_current", ContextVar("submission_budget", default=None))
    budget = clocks.SubmissionWorkBudget("legacy-recognition")
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
            assert not pending.done() and budget.active == 0
            scheduler.host_capacity.capacity = 50
            scheduler.wake.set()
            result = await asyncio.wait_for(pending, 2)
            assert result.candidate.text == "faithful transcription"
            assert not budget.exhausted.is_set() and budget.active == 0
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            budget.expire()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_legacy_gemini_proxy_clock_covers_sync_response_and_cancellation_drain(
    cancel, scheduler, monkeypatch,
):
    monkeypatch.setattr(clocks, "_current", ContextVar("submission_budget", default=None))
    budget = clocks.SubmissionWorkBudget("legacy-proxy-retry")
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
        assert not started.is_set() and budget.active == 0
        scheduler.host_capacity.capacity = 50
        scheduler.wake.set()
        await asyncio.wait_for(started.wait(), 2)
        budget.expire()
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
        budget.expire()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancel", "exception"])
@pytest.mark.parametrize("route", ["openai", "anthropic", "gemini"])
async def test_legacy_native_failure_always_releases_active_work(route, failure, scheduler, monkeypatch):
    monkeypatch.setattr(clocks, "_current", ContextVar("submission_budget", default=None))
    budget = clocks.SubmissionWorkBudget("legacy-retry")
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
            assert budget.active == scheduler.active == 0
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            budget.expire()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_race", [False, True])
async def test_real_worker_saves_parallel_structure_results_after_original_lease_window(
    terminal_race, scheduler, monkeypatch, heartbeat_control,
):
    # Advance lease wall time explicitly; provider/DB deadlines keep their real
    # production budgets instead of inheriting a 100 ms heartbeat deadline.
    lease_time = [time.time()]
    lease_clock = SimpleNamespace(time=lambda: lease_time[0])
    monkeypatch.setattr(workflow_repository, "time", lease_clock)
    monkeypatch.setattr(recognition_runs, "time", lease_clock)
    owner, task_id = _seed_task(with_question=True)
    claims, calls, cancelled = [], [], []
    both_claimed = asyncio.Event()
    loop = asyncio.get_running_loop()
    original_claim = workflow_repository.claim_operation

    def claim(operation_id, **kwargs):
        row = workflow_repository.get_operation(operation_id, owner_id=kwargs["owner_id"])
        if row.operation_type == "submission_structure_v1":
            kwargs["lease_seconds"] = 300
        claimed = original_claim(operation_id, **kwargs)
        if row.operation_type == "submission_structure_v1":
            claims.append(claimed)
            if len(claims) == 2:
                loop.call_soon_threadsafe(both_claimed.set)
        return claimed

    monkeypatch.setattr(workflow_repository, "claim_operation", claim)
    heartbeats = []
    original_start = recognition_runs._RecognitionLeaseHeartbeat.start

    def start_heartbeat(self):
        heartbeats.append(self)
        original_start(self)

    monkeypatch.setattr(recognition_runs._RecognitionLeaseHeartbeat, "start", start_heartbeat)
    terminal_commits, releases, raced, terminal_waits = set(), {}, [], []
    original_checkpoint = workflow_repository.save_operation_checkpoint
    original_heartbeat = workflow_repository.heartbeat_operation

    def checkpoint(*args, **kwargs):
        saved = original_checkpoint(*args, **kwargs)
        if terminal_race and kwargs.get("stage") == "parser_saved":
            releases[saved.id] = threading.Event()
            terminal_commits.add(saved.id)
            # Commit succeeded, but its async caller has not received the row.
            # Do not wake the other child before its commit callback returns.
            target = next(item.task for item in heartbeats if item.row.id == saved.id)
            loop.call_soon_threadsafe(heartbeat_control.wake, target)
            assert releases[saved.id].wait(10)
        return saved

    def heartbeat(operation_id, **kwargs):
        try:
            return original_heartbeat(operation_id, **kwargs)
        finally:
            if operation_id in terminal_commits:
                raced.append(operation_id)

    async def wait_for_terminal(awaitables, **kwargs):
        for item in heartbeats:
            terminal = item.context.terminal_write
            if terminal is not None and terminal in awaitables:
                terminal_waits.append(item.row.id)
                releases[item.row.id].set()
        return await asyncio.wait(awaitables, **kwargs)

    monkeypatch.setattr(recognition_runs.asyncio, "wait", wait_for_terminal)
    monkeypatch.setattr(workflow_repository, "save_operation_checkpoint", checkpoint)
    monkeypatch.setattr(workflow_repository, "heartbeat_operation", heartbeat)
    scheduler.host_capacity.capacity = 0
    both_started, finish_responses = asyncio.Event(), asyncio.Event()

    async def invoke(_options):
        number = len(calls) + 1
        calls.append(number)
        if len(calls) == 2:
            both_started.set()
        try:
            await finish_responses.wait()
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
            worker_id="controlled-real-worker", lease_seconds=1200, heartbeat_seconds=60,
            poll_seconds=60, claim_batch_size=1, max_in_flight=1, shutdown_seconds=1)
        try:
            assert await worker.poll_once() == 1
            await asyncio.wait_for(both_claimed.wait(), 10)
            for _ in claims:
                await asyncio.wait_for(heartbeat_control.sleeping.get(), 10)
            async with asyncio.timeout(10):
                while len(scheduler.pending) != 2:
                    await asyncio.sleep(0.01)
            assert calls == [] and scheduler.active == 0 and len(heartbeats) == 2
            # Both requests are queued. Renew before the initial 300s lease.
            lease_time[0] += 200
            heartbeat_control.wake()
            for _ in claims:
                await asyncio.wait_for(heartbeat_control.sleeping.get(), 10)
            scheduler.host_capacity.capacity = 50
            scheduler.wake.set()
            await asyncio.wait_for(both_started.wait(), 10)
            # Queue + response crosses the original lease, with both responses
            # still in flight. Each phase remains below its respective queue/response budget.
            lease_time[0] += 200
            heartbeat_control.wake()
            for _ in claims:
                await asyncio.wait_for(heartbeat_control.sleeping.get(), 10)
            for row in claims:
                renewed = workflow_repository.get_operation(row.id, owner_id=owner)
                assert renewed.lease_expires_at == lease_time[0] + 300
                assert lease_time[0] > row.lease_expires_at
            finish_responses.set()
            async with asyncio.timeout(15):
                while worker.in_flight_count:
                    await asyncio.sleep(0.01)
            assert lease_time[0] > max(row.lease_expires_at for row in claims)
            after = task_facade.get_task(task_id=task_id, owner_id=owner)
            assert after["status"] == "submissions_ready", (
                after["status"], after.get("error"), calls, cancelled, [h.failure for h in heartbeats])
            assert len(after["student_data"]) == 2 and calls == [1, 2] and cancelled == []
            assert {s["stu_ans"][0]["content"] for s in after["student_data"].values()} == {"saved answer 1", "saved answer 2"}
            for row in claims:
                saved = workflow_repository.get_operation(row.id, owner_id=owner)
                assert saved.status == "completed" and saved.checkpoint["artifact_id"]
            assert await worker.poll_once() == 0 and calls == [1, 2]
            assert len(heartbeats) == 2 and all(h.task.done() and h.failure is None for h in heartbeats)
            if terminal_race:
                assert set(raced) == set(terminal_waits) == {row.id for row in claims}
        finally:
            finish_responses.set()
            for release in releases.values():
                release.set()
            await worker.shutdown()
