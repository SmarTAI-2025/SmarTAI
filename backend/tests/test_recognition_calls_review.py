"""Independent per-call cache boundaries; all model outcomes are synthetic."""
import asyncio
import hashlib
import time
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from backend.db.models import AssignmentRecord, StoredFileRecord
from backend.db.session import session_scope
from backend.domain.errors import NotFound, RecognitionError
from backend.recognition.artifact_codec import build_artifact
from backend.recognition.cache_identity import model_cache_identity
from backend.recognition.engine import EngineLocateInputV1, EngineReadInputV1, EngineRepairInputV1, LocatorImageV1
from backend.recognition.image_executor import read_image_plan
from backend.recognition.models import NormalizedRegionV1, RecognitionPolicyV1
from backend.recognition.planner import EngineCapabilitiesV1
from backend.services import recognition_calls as calls
from backend.services.recognition_artifacts import RecognitionArtifactStore
from backend.tests.test_recognition_artifact_codec import candidate, locator, repair, visual
from backend.tests.test_recognition_artifact_store import seeded
from backend.tests.test_recognition_image_executor import ImageEngine, setup as image_plan
from backend.tests.test_recognition_local_evidence import setup as stored_image


def case(tmp_path, kind="initial", *, unknown=False):
    storage, binding, _, seed = seeded(tmp_path)
    source = seed.source
    pixels = b"synthetic submitted pixels"
    digest = hashlib.sha256(pixels).hexdigest()
    caps = EngineCapabilitiesV1(route_id="chosen", fingerprint="frozen", visual_inputs=["page_image"],
                                target_location=True, semantic_repair=True, response_recheck=True,
                                region_reads=True, bounded_output_tokens=True)
    if kind == "locator":
        payload = locator()
        payload.sheets[0].payload_sha256 = digest
        request = EngineLocateInputV1(purpose="problems", targets=["Q1"],
                                       images=[LocatorImageV1(page_numbers=[1], payload=pixels)])
    elif kind in {"patch", "empty_recovery"}:
        payload = repair()
        payload.image.source_sha256 = source.input_sha256
        payload.image.payload_sha256, payload.image.payload_bytes = digest, len(pixels)
        request = EngineRepairInputV1(purpose="problems", page_number=1, content_type="image/png", payload=pixels,
                                       repair_context=payload.result.context.model_copy(deep=True))
    else:
        payload = visual(payload_sha256=digest, payload_bytes=len(pixels), unit_id="u0042")
        request = EngineReadInputV1(purpose="problems", page_number=1, content_type="image/png", payload=pixels,
                                     input_mode="page_image")
    reply = payload.candidate if kind == "initial" else payload.result.candidate
    if unknown:
        reply.input_tokens = reply.output_tokens = None
    service = calls.RecognitionCallService(store=RecognitionArtifactStore(storage), binding=binding,
                                           authorized_owner_id=source.owner_id)
    options = dict(capabilities=caps, policy=RecognitionPolicyV1(), prompt_version="synthetic-v1",
                   kind=kind, occurrence_id=f"{kind}:0007")
    return SimpleNamespace(service=service, source=source, request=request, payload=payload, options=options,
                           reply=reply, storage=storage, binding=binding)


async def record(value):
    return await value.service.record(value.source, value.request, value.payload, **value.options)


async def lookup(value, **changes):
    return await value.service.lookup(value.source, value.request, **(value.options | changes))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["initial", "locator", "patch", "empty_recovery"])
@pytest.mark.parametrize("unknown", [False, True])
async def test_leaf_tokens_and_historical_ids_are_not_rewritten(tmp_path, kind, unknown):
    value = case(tmp_path, kind, unknown=unknown)
    before = value.payload.model_dump_json()
    fresh = await record(value)
    assert fresh.current_usage.dispatches == 1
    assert fresh.current_usage.input_tokens == (None if unknown else 10)
    assert fresh.current_usage.usage_complete is not unknown
    assert fresh.historical_usage is None
    found = await lookup(value, occurrence_id=f"{kind}:0099")
    assert value.payload.model_dump_json() == before
    if kind in {"patch", "empty_recovery"}:
        assert found.status == "not_success" and found.receipt is None
    else:
        assert found.status == "hit"
        receipt = found.receipt
        assert receipt.occurrence_id == f"{kind}:0099"
        assert receipt.envelope.payload.model_dump_json() == before
        assert receipt.current_usage.dispatches == 0
        assert receipt.current_usage.input_tokens == receipt.current_usage.output_tokens == 0
        assert receipt.historical_usage == fresh.current_usage
        assert calls.RecognitionCallReceiptV2.model_validate_json(receipt.model_dump_json()) == receipt
        if kind == "initial":
            assert receipt.envelope.payload.unit_id == "u0042"


@pytest.mark.asyncio
async def test_real_non_aligned_image_crop_pairs_effective_region_with_original_leaf(tmp_path):
    reader, data, source, binding, _ = stored_image(tmp_path, image=True)
    engine = ImageEngine(result="Literal source text.")
    region = NormalizedRegionV1(x0=.123, y0=.234, x1=.876, y1=.987)
    options = image_plan(data, engine, regions=[region])
    options.update(source=source, authorized_owner_id=source.owner_id)
    batch = await read_image_plan(data, **options)
    unit, request = batch.units[0], engine.requests[0]
    assert unit.region == region and request.region != region
    assert request.region.as_tuple() == unit.image_preparation.effective_region
    service = calls.RecognitionCallService(store=reader.store, binding=binding, authorized_owner_id=source.owner_id)
    arguments = dict(capabilities=engine.capabilities, policy=options["plan"].policy,
                     prompt_version="synthetic-v1", kind="initial", occurrence_id="initial:0007")
    fresh = await service.record(source, request, unit, **arguments)
    found = await service.lookup(source, request, **arguments)
    assert found.status == "hit" and found.receipt.envelope.payload == unit
    assert fresh.envelope.payload.region == region


@pytest.mark.asyncio
@pytest.mark.parametrize("field,change", [
    ("payload_sha256", "f" * 64), ("payload_bytes", 999), ("page_numbers", [2]),
    ("requested_output_tokens", 4000), ("region", NormalizedRegionV1(x1=.8)),
    ("output_mapping", "document_only"),
])
async def test_mismatched_initial_leaf_is_rejected_before_storage(tmp_path, field, change):
    value = case(tmp_path)
    setattr(value.payload, field, change)
    before = sorted(value.storage.list_keys(""))
    with pytest.raises(RecognitionError, match="recognition_artifact_invalid"):
        await record(value)
    assert sorted(value.storage.list_keys("")) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["initial", "locator", "patch"])
async def test_exact_key_does_not_bypass_pairing_when_a_valid_but_wrong_leaf_was_stored(tmp_path, kind):
    value = case(tmp_path, kind)
    value.payload.requested_output_tokens -= 1
    identity = model_cache_identity(value.source, value.request, capabilities=value.options["capabilities"],
                                    policy=value.options["policy"], prompt_version=value.options["prompt_version"])
    payload_kind = {"initial": "visual_read", "locator": "locator", "patch": "repair"}[kind]
    envelope = build_artifact(identity=identity, source=value.source, payload_kind=payload_kind, payload=value.payload)
    await value.service.store.save(envelope, binding=value.binding, authorized_owner_id=value.source.owner_id)
    found = await lookup(value)
    # Repairs never reach success reuse, even before a request-pair check.
    assert found.status == ("not_success" if kind == "patch" else "corrupt")
    assert found.receipt is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["initial", "locator", "patch"])
async def test_wrong_provider_route_is_not_a_fresh_record(tmp_path, kind):
    value = case(tmp_path, kind)
    value.reply.provider_route_id = "other-route"
    with pytest.raises(RecognitionError, match="recognition_artifact_invalid"):
        await record(value)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["ok", "empty"])
async def test_non_error_safe_error_is_not_recorded_as_a_settled_complete_dispatch(tmp_path, status):
    value = case(tmp_path)
    value.payload.candidate = candidate("literal" if status == "ok" else "", status=status,
                                        safe_error_code="provider_auth_failed")
    with pytest.raises(RecognitionError, match="recognition_artifact_invalid"):
        await record(value)


@pytest.mark.asyncio
async def test_unknown_cached_history_cannot_be_edited_to_zero(tmp_path):
    value = case(tmp_path, unknown=True)
    await record(value)
    receipt = (await lookup(value)).receipt
    dumped = receipt.model_dump()
    dumped["historical_usage"]["input_tokens"] = 0
    with pytest.raises(ValidationError):
        calls.RecognitionCallReceiptV2.model_validate(dumped)


@pytest.mark.asyncio
async def test_uncertain_submission_remains_pending_and_never_hits(tmp_path):
    value = case(tmp_path)
    value.payload.candidate = candidate("", status="error", safe_error_code="provider_submit_uncertain")
    value.payload.submission_may_exist = True
    receipt = await record(value)
    assert receipt.current_usage.dispatches == 1 and receipt.current_usage.submission_may_exist
    assert receipt.current_usage.input_tokens is receipt.current_usage.output_tokens is None
    assert not receipt.current_usage.usage_complete
    assert receipt.envelope.payload.candidate.input_tokens == 10
    assert (await lookup(value)).status == "not_success"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["record", "lookup"])
async def test_all_mutable_inputs_are_frozen_before_first_wait(tmp_path, monkeypatch, operation):
    value = case(tmp_path, unknown=True)
    if operation == "lookup":
        await record(value)
    before = value.payload.model_dump_json()
    started, release = asyncio.Event(), asyncio.Event()
    original = calls.run_in_threadpool
    async def waiting(*args, **kwargs):
        if not started.is_set():
            started.set()
            await release.wait()
        return await original(*args, **kwargs)
    monkeypatch.setattr(calls, "run_in_threadpool", waiting)
    pending = asyncio.create_task(record(value) if operation == "record" else lookup(value))
    await started.wait()
    value.source.owner_id = "different-owner"
    value.request.payload = b"different pixels"
    value.request.region.x0 = .1
    value.options["capabilities"].fingerprint = "different-model"
    value.options["policy"].enable_repair = False
    value.payload.candidate.text = "different source text"
    release.set()
    result = await pending
    receipt = result if operation == "record" else result.receipt
    assert receipt.envelope.payload.model_dump_json() == before
    assert receipt.current_usage.input_tokens is None if operation == "record" else receipt.historical_usage.input_tokens is None


@pytest.mark.asyncio
@pytest.mark.parametrize("deleted", ["original", "task"])
async def test_deletion_during_receipt_construction_cannot_return_cached_call(tmp_path, monkeypatch, deleted):
    value = case(tmp_path)
    await record(value)
    original = calls._receipt
    def deleting(*args, **kwargs):
        receipt = original(*args, **kwargs)
        with session_scope() as db:
            if deleted == "original":
                db.get(StoredFileRecord, value.source.stored_file_id).availability_status = "unavailable"
            else:
                db.get(AssignmentRecord, value.binding.business_id).deletion_requested_at = time.time()
        return receipt
    monkeypatch.setattr(calls, "_receipt", deleting)
    found = await lookup(value)
    assert found.status == "source_unavailable" and found.receipt is None


@pytest.mark.asyncio
async def test_tombstoned_task_blocks_record_without_new_object(tmp_path):
    value = case(tmp_path)
    with session_scope() as db:
        db.get(AssignmentRecord, value.binding.business_id).deletion_requested_at = time.time()
    before = sorted(value.storage.list_keys(""))
    with pytest.raises(NotFound, match="assignment"):
        await record(value)
    assert sorted(value.storage.list_keys("")) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["record", "lookup"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_storage_wait_timeout_or_cancel_never_retries(tmp_path, monkeypatch, operation, cancel):
    value = case(tmp_path)
    entered, attempts = asyncio.Event(), []
    async def waiting(*args, **kwargs):
        attempts.append(1)
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(value.service.store, "save" if operation == "record" else "find_success", waiting)
    if not cancel:
        value.options["timeout_seconds"] = 1
    pending = asyncio.create_task(record(value) if operation == "record" else lookup(value))
    await entered.wait()
    if cancel:
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    else:
        with pytest.raises(RecognitionError, match="recognition_timeout"):
            await pending
    assert attempts == [1]
