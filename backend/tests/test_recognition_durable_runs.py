import asyncio

import pytest

from backend.db import workflow_repository
from backend.domain.errors import RecognitionError
from backend.recognition.durable_records import (
    RecognitionRunCheckpointV1, authorize_dispatch, finish_dispatch, operation_usage,
)
from backend.recognition.models import RecognitionCandidateV1, RecognitionPolicyV1
from backend.services.recognition_runs import RecognitionRunService, OPERATION_TYPE
from backend.tests.test_recognition_v2_artifacts import setup


def runner(tmp_path, **kwargs):
    agent, request, data, results = setup(tmp_path, **kwargs)
    service = RecognitionRunService(store=results.store, capacity=agent.capacity)
    return service, request, data, agent.engine


async def run(service, request, data, engine):
    return await service.run(request, data, engine=engine, prompt_version="faithful-reader-v1",
                             authorized_owner_id=request.source.owner_id)


@pytest.mark.asyncio
async def test_final_reuse_no_new_cost_and_content_free_checkpoint(tmp_path):
    service, request, data, engine = runner(tmp_path, initial=["literal 1 + 1 = 3"])
    first = await run(service, request, data, engine)
    assert first.status == "completed", first.safe_error_code
    assert first.current_usage.initial_calls == first.operation_usage.initial_calls == 1
    assert first.operation_usage.output_tokens == 2
    second = await run(service, request, None, engine)
    assert second.status == "already_done" and second.assembly == first.assembly
    assert second.current_usage.total_calls == second.current_usage.output_tokens == 0
    assert second.operation_usage.output_tokens == 2 and len(engine.reads) == 1
    row = workflow_repository.get_operation(first.operation_id, owner_id=request.source.owner_id)
    checkpoint = RecognitionRunCheckpointV1.model_validate(row.checkpoint)
    assert "1 + 1 = 3" not in checkpoint.model_dump_json() and "payload_b64" not in checkpoint.model_dump_json()
    assert checkpoint.calls[0].state == "stored" and checkpoint.final_artifact_id
    assert row.lease_token is None and row.status == "completed"


@pytest.mark.asyncio
async def test_pending_before_provider_cancel_never_replays(tmp_path):
    service, request, data, engine = runner(tmp_path)
    entered = asyncio.Event()
    calls = 0

    async def blocked(_):
        nonlocal calls
        calls += 1
        entered.set()
        await asyncio.Event().wait()

    engine.recognize = blocked
    task = asyncio.create_task(run(service, request, data, engine))
    await asyncio.wait_for(entered.wait(), timeout=15)
    concurrent = await run(service, request, data, engine)
    assert concurrent.status == "already_running" and concurrent.operation_usage.initial_calls == 1
    assert concurrent.operation_usage.output_tokens is None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    resumed = await run(service, request, data, engine)
    assert resumed.status == "needs_review" and resumed.safe_error_code == "provider_submit_uncertain"
    assert resumed.operation_usage.total_calls == 1 and resumed.operation_usage.input_tokens is None
    assert calls == 1
    again = await run(service, request, None, engine)
    assert again.status == "needs_review" and calls == 1


@pytest.mark.asyncio
async def test_confirmed_first_page_reused_after_local_interruption(tmp_path, monkeypatch):
    service, request, data, engine = runner(tmp_path, pdf=True, pages=2, initial=["literal 1 + 1 = 3"])
    from backend.services.recognition_runs import _GuardedCalls
    original = _GuardedCalls.before_dispatch
    count = 0

    async def stop_before_second(self, *args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise asyncio.CancelledError
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(_GuardedCalls, "before_dispatch", stop_before_second)
    with pytest.raises(asyncio.CancelledError):
        await run(service, request, data, engine)
    assert len(engine.reads) == 1
    monkeypatch.setattr(_GuardedCalls, "before_dispatch", original)
    resumed = await run(service, request, data, engine)
    assert resumed.status == "completed", resumed.safe_error_code
    assert len(engine.reads) == 2
    assert resumed.operation_usage.initial_calls == 2 and resumed.current_usage.initial_calls == 1
    assert resumed.assembly.raw.read_batch.provenance[0].origin == "cache_hit"


@pytest.mark.asyncio
async def test_checkpoint_failure_prevents_provider(tmp_path, monkeypatch):
    service, request, data, engine = runner(tmp_path)
    original = workflow_repository.save_operation_checkpoint

    def fail_pending(*args, **kwargs):
        if kwargs["checkpoint"]["calls"]:
            raise RecognitionError("recognition_artifact_unavailable")
        return original(*args, **kwargs)

    monkeypatch.setattr(workflow_repository, "save_operation_checkpoint", fail_pending)
    result = await run(service, request, data, engine)
    assert result.status == "needs_review" and not engine.reads and not engine.repairs


@pytest.mark.asyncio
async def test_owner_mismatch_and_source_mismatch_never_dispatch(tmp_path):
    service, request, data, engine = runner(tmp_path)
    with pytest.raises(RecognitionError):
        await service.run(request, data, engine=engine, prompt_version="faithful-reader-v1", authorized_owner_id="other")
    with pytest.raises(RecognitionError):
        await run(service, request, b"changed", engine)
    assert not engine.reads


@pytest.mark.asyncio
async def test_failed_operation_is_not_reset(tmp_path):
    service, request, data, engine = runner(tmp_path)
    first = await run(service, request, data, engine)
    row = workflow_repository.get_operation(first.operation_id, owner_id=request.source.owner_id)
    same, created = workflow_repository.create_operation(
        assignment_id=request.source.business_id, owner_id=request.source.owner_id,
        operation_type=OPERATION_TYPE, input_hash=row.input_hash, retry_existing=False,
    )
    assert not created and same.attempt == row.attempt and same.checkpoint == row.checkpoint


def test_durable_budget_pending_unknown_duplicate_and_limits():
    from backend.tests.test_recognition_recheck import Engine
    caps = Engine().capabilities
    checkpoint = RecognitionRunCheckpointV1(operation_id="op", attempt=1, identity_key="a" * 64, started_at=100)
    policy = RecognitionPolicyV1(max_output_tokens=10)
    args = dict(kind="initial", identity_key="b" * 64, region_keys=("p1:r",), max_output_tokens=8,
                policy=policy, capabilities=caps, now=101)
    pending = authorize_dispatch(checkpoint, **args)
    assert checkpoint.calls == [] and operation_usage(pending, now=102).input_tokens is None
    with pytest.raises(RecognitionError) as exc:
        authorize_dispatch(pending, **{**args, "identity_key": "c" * 64})
    assert exc.value.code == "provider_submit_uncertain"
    done = finish_dispatch(pending, identity_key="b" * 64, artifact_id="file",
                           candidate=RecognitionCandidateV1(kind="vision", status="ok", text="raw", input_tokens=2, output_tokens=4),
                           submission_may_exist=False)
    assert operation_usage(done, now=102).output_tokens == 4
    with pytest.raises(RecognitionError) as exc:
        authorize_dispatch(done, **{**args, "identity_key": "c" * 64, "region_keys": ("p2:r",)})
    assert exc.value.code == "recognition_budget_exhausted"
    with pytest.raises(RecognitionError):
        authorize_dispatch(done, **{**args, "max_output_tokens": 2})
    with pytest.raises(RecognitionError):
        authorize_dispatch(checkpoint, **{**args, "now": 800})
    with pytest.raises(RecognitionError):
        operation_usage(done, now=float("nan"))
