"""Independent boundaries for terminal receipts, not model-cache orchestration."""
import asyncio
import time

import pytest
from pydantic import ValidationError

from backend.db import file_repository
from backend.db.models import AssignmentRecord, StoredFileRecord
from backend.db.session import session_scope
from backend.domain.errors import NotFound, RecognitionError
from backend.recognition.invocation import RecognitionInvocationV1
from backend.recognition.models import RecognitionCandidateV1
from backend.services import recognition_results as results
from backend.tests.test_recognition_fusion import assemble, workflow
from backend.tests.test_recognition_results import lookup, setup


@pytest.mark.asyncio
async def test_unbounded_ocr_unknown_history_is_not_a_free_original_call(tmp_path):
    service, original_assembly, _, data = setup(tmp_path)
    candidate = RecognitionCandidateV1(kind="ocr", status="ok", text="A literal answer.", provider_route_id="chosen")
    raw = workflow([""], [candidate], purpose="submissions", document_group=True)
    source = original_assembly.raw.request.source.model_copy(deep=True)
    raw.request.source = source
    raw.read_batch.source = source.model_copy(deep=True)
    raw.read_batch.units[0].output_mapping = "single_region"
    assembly = assemble(raw)
    current = await service.record(assembly)
    found = await lookup(service, assembly, data)
    assert found.status == "hit"
    receipt = found.invocation
    assert receipt.current_usage.total_calls == 0 and receipt.current_usage.output_tokens == 0
    assert receipt.historical_usage == current.current_usage
    assert receipt.historical_usage.total_calls == 1
    assert receipt.historical_usage.input_tokens is receipt.historical_usage.output_tokens is None
    assert not receipt.historical_usage.usage_complete
    assert receipt.assembly.raw.budget.charged_output_tokens is None
    assert receipt.assembly.raw.budget.reserved_output_tokens == 0
    assert not receipt.assembly.raw.budget.bounded_output_tokens
    assert receipt.assembly.raw.read_batch.units[0].candidate == candidate
    assert RecognitionInvocationV1.model_validate_json(receipt.model_dump_json()) == receipt


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["input_tokens", "output_tokens", "usage_complete"])
async def test_receipt_rejects_turning_unknown_history_into_known_usage(tmp_path, field):
    service, assembly, _, data = setup(tmp_path, unknown=True)
    await service.record(assembly)
    receipt = (await lookup(service, assembly, data)).invocation
    value = receipt.model_dump()
    value["historical_usage"][field] = True if field == "usage_complete" else 0
    with pytest.raises(ValidationError):
        RecognitionInvocationV1.model_validate(value)


@pytest.mark.asyncio
async def test_record_snapshots_nested_caller_evidence_before_await(tmp_path, monkeypatch):
    service, assembly, _, data = setup(tmp_path, unknown=True)
    before = assembly.model_dump_json()
    entered, release = asyncio.Event(), asyncio.Event()
    original_pool = results.run_in_threadpool
    async def waiting(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            await release.wait()
        return await original_pool(*args, **kwargs)
    monkeypatch.setattr(results, "run_in_threadpool", waiting)
    pending = asyncio.create_task(service.record(assembly))
    await entered.wait()
    assembly.raw.request.source.owner_id = "other-owner"
    assembly.raw.read_batch.units[0].candidate.text = "changed after dispatch"
    release.set()
    receipt = await pending
    assert receipt.assembly.model_dump_json() == before
    assert receipt.current_usage.input_tokens is None
    restored = await lookup(service, receipt.assembly, data)
    assert restored.status == "hit" and restored.invocation.assembly.model_dump_json() == before


@pytest.mark.asyncio
async def test_original_cleanup_blocks_terminal_reuse_but_not_historical_evidence(tmp_path):
    service, assembly, original, data = setup(tmp_path)
    receipt = await service.record(assembly)
    service.store.storage.delete(original.storage_key)
    with session_scope() as db:
        db.get(StoredFileRecord, original.id).availability_status = "unavailable"
    found = await lookup(service, assembly, data)
    assert found.status == "source_unavailable" and found.invocation is None
    historical = await service.store.load(
        receipt.artifact_id, source=assembly.raw.request.source, identity=receipt.identity,
        binding=service.binding, authorized_owner_id=service.owner,
    )
    assert historical.payload == assembly


@pytest.mark.asyncio
async def test_tombstoned_task_cannot_record_even_already_executed_evidence(tmp_path):
    service, assembly, _, _ = setup(tmp_path)
    with session_scope() as db:
        db.get(AssignmentRecord, service.binding.business_id).deletion_requested_at = time.time()
    before = sorted(service.store.storage.list_keys(""))
    with pytest.raises(NotFound, match="assignment"):
        await service.record(assembly)
    assert sorted(service.store.storage.list_keys("")) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_record_wait_timeout_or_cancel_does_not_retry_or_relabel_history(tmp_path, monkeypatch, cancel):
    service, assembly, _, _ = setup(tmp_path, unknown=True, uncertain=True)
    before = assembly.model_dump_json()
    entered, calls = asyncio.Event(), []
    async def waiting(*args, **kwargs):
        calls.append(1)
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(service.store, "save", waiting)
    if cancel:
        task = asyncio.create_task(service.record(assembly))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(RecognitionError, match="recognition_timeout"):
            await service.record(assembly, timeout_seconds=1)
    assert calls == [1] and assembly.model_dump_json() == before


@pytest.mark.asyncio
async def test_saved_failed_evidence_stays_not_success_and_is_not_skipped_for_old_success(tmp_path):
    service, assembly, _, data = setup(tmp_path)
    success = await service.record(assembly)
    raw = workflow([""], [RecognitionCandidateV1(kind="vision", status="error", provider_route_id="chosen",
                                                 safe_error_code="provider_auth_failed")], purpose="submissions")
    raw.request.source = assembly.raw.request.source.model_copy(deep=True)
    raw.read_batch.source = raw.request.source.model_copy(deep=True)
    failed = assemble(raw)
    recorded_failure = await service.record(failed)
    assert recorded_failure.current_usage.total_calls == 1
    assert recorded_failure.current_usage.input_tokens is None
    assert recorded_failure.identity == success.identity
    found = await lookup(service, assembly, data)
    assert found.status == "not_success" and found.artifact_id == recorded_failure.artifact_id
    assert found.invocation is None
    row = file_repository.get_file(file_id=success.artifact_id, owner_id=service.owner)
    assert row is not None
