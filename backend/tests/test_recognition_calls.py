import asyncio
import hashlib
import time

import pytest
from pydantic import ValidationError

from backend.db import file_repository
from backend.db.models import AssignmentRecord, StoredFileRecord
from backend.db.session import session_scope
from backend.domain.errors import RecognitionError
from backend.recognition.artifact_codec import build_artifact, encode_artifact
from backend.recognition.cache_identity import model_cache_identity
from backend.recognition.engine import EngineLocateInputV1, EngineReadInputV1, EngineRepairInputV1, LocatorImageV1
from backend.recognition.models import NormalizedRegionV1, RecognitionPolicyV1
from backend.recognition.planner import EngineCapabilitiesV1
from backend.services import recognition_calls as calls
from backend.services.recognition_artifacts import RecognitionArtifactStore
from backend.tests.test_recognition_artifact_codec import candidate, locator, repair, visual
from backend.tests.test_recognition_artifact_store import seeded


def setup(tmp_path, kind="initial", *, unknown=False, ocr=False):
    storage, binding, original, seed = seeded(tmp_path)
    service = calls.RecognitionCallService(store=RecognitionArtifactStore(storage), binding=binding,
                                           authorized_owner_id=seed.source.owner_id)
    pixels = b"synthetic prepared pixels"
    digest = hashlib.sha256(pixels).hexdigest()
    caps = EngineCapabilitiesV1(route_id="chosen", fingerprint="frozen", visual_inputs=["page_image", "document"],
                                semantic_repair=not ocr, response_recheck=not ocr, target_location=not ocr,
                                candidate_kind="ocr" if ocr else "vision", bounded_output_tokens=not ocr,
                                document_batching=True, max_document_pages=24)
    options = dict(capabilities=caps, policy=RecognitionPolicyV1(), prompt_version="fixture-v1", kind=kind,
                   occurrence_id=f"{kind}:0000")
    if kind == "locator":
        leaf = locator()
        leaf.sheets[0].payload_sha256 = digest
        request = EngineLocateInputV1(purpose="problems", targets=["Q1"],
                                     images=[LocatorImageV1(page_numbers=[1], payload=pixels)])
    elif kind in {"patch", "empty_recovery"}:
        leaf = repair()
        leaf.image.source_sha256 = seed.source.input_sha256
        leaf.image.payload_sha256, leaf.image.payload_bytes = digest, len(pixels)
        request = EngineRepairInputV1(purpose="problems", page_number=1, content_type="image/png", payload=pixels,
                                     repair_context=leaf.result.context.model_copy(deep=True))
    else:
        leaf = visual(payload_sha256=digest, payload_bytes=len(pixels))
        request = EngineReadInputV1(purpose="problems", input_mode="page_image", page_number=1,
                                    content_type="image/png", payload=pixels)
    reply = leaf.candidate if kind == "initial" else leaf.result.candidate
    reply.kind = "ocr" if ocr else "vision"
    if unknown:
        reply.input_tokens = reply.output_tokens = None
    return service, seed.source, request, leaf, options, original


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["initial", "locator"])
@pytest.mark.parametrize("unknown", [False, True])
async def test_hit_has_zero_current_spend_and_preserves_original_leaf(tmp_path, kind, unknown):
    service, source, request, leaf, options, _ = setup(tmp_path, kind, unknown=unknown)
    before = leaf.model_dump_json()
    fresh = await service.record(source, request, leaf, **options)
    assert fresh.origin == "dispatch" and fresh.current_usage.dispatches == 1
    assert fresh.current_usage.input_tokens == (None if unknown else 10)
    assert fresh.current_usage.duration_ms is None
    assert fresh.current_usage.usage_complete is not unknown
    original_bytes = encode_artifact(fresh.envelope)
    options["occurrence_id"] = f"{kind}:0007"
    found = await service.lookup(source, request, **options)
    assert found.status == "hit" and found.artifact_id == fresh.artifact_id
    receipt = found.receipt
    assert receipt.occurrence_id.endswith("0007")
    assert receipt.current_usage.dispatches == receipt.current_usage.input_tokens == receipt.current_usage.output_tokens == 0
    assert receipt.current_usage.duration_ms == 0 and receipt.current_usage.usage_complete
    assert receipt.historical_usage == fresh.current_usage
    assert receipt.envelope.payload.model_dump_json() == before == leaf.model_dump_json()
    assert encode_artifact(receipt.envelope) == original_bytes
    assert calls.RecognitionCallReceiptV2.model_validate_json(receipt.model_dump_json()) == receipt
    if kind == "initial":
        assert receipt.envelope.payload.unit_id == "u0000"
    receipt.envelope.source.original_name = "caller mutation"
    assert (await service.lookup(source, request, **options)).receipt.envelope.source == source


@pytest.mark.asyncio
async def test_ocr_only_unknown_usage_hits_without_semantic_companion(tmp_path):
    service, source, request, leaf, options, _ = setup(tmp_path, unknown=True, ocr=True)
    await service.record(source, request, leaf, **options)
    receipt = (await service.lookup(source, request, **options)).receipt
    assert receipt.current_usage.dispatches == 0
    assert receipt.historical_usage.input_tokens is None and not receipt.historical_usage.usage_complete
    assert receipt.envelope.payload.candidate.kind == "ocr"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["patch", "empty_recovery"])
async def test_repair_proposal_is_saved_but_never_a_successful_hit(tmp_path, kind):
    service, source, request, leaf, options, _ = setup(tmp_path, kind)
    assert (await service.lookup(source, request, **options)).status == "miss"
    fresh = await service.record(source, request, leaf, **options)
    assert fresh.current_usage.dispatches == 1
    found = await service.lookup(source, request, **options)
    assert found.status == "not_success" and found.artifact_id == fresh.artifact_id and found.receipt is None
    value = fresh.model_dump()
    value.update(origin="cache_hit", current_usage=calls._hit_usage().model_dump(), historical_usage=value["current_usage"])
    with pytest.raises(ValidationError, match="unsuccessful evidence"):
        calls.RecognitionCallReceiptV2.model_validate(value)


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["empty", "error", "uncertain", "low", "refused", "truncated"])
async def test_latest_failed_result_does_not_fall_back_to_an_older_good_one(tmp_path, condition):
    service, source, request, leaf, options, _ = setup(tmp_path)
    await service.record(source, request, leaf, **options)
    if condition in {"empty", "error", "uncertain"}:
        leaf.candidate = candidate("", status="empty" if condition == "empty" else "error",
                                   safe_error_code="provider_submit_uncertain" if condition == "uncertain" else None)
        leaf.submission_may_exist = condition == "uncertain"
    elif condition == "low":
        leaf.candidate = candidate("[unclear]")
    else:
        leaf.candidate.finish_reason = "refused" if condition == "refused" else "length"
    recent = await service.record(source, request, leaf, **options)
    if condition == "uncertain":
        assert recent.current_usage.submission_may_exist and recent.current_usage.input_tokens is None
        assert recent.envelope.payload.candidate.input_tokens == 10
    found = await service.lookup(source, request, **options)
    assert found.status == "not_success" and found.artifact_id == recent.artifact_id and found.receipt is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["pixels", "page", "region", "output", "purpose", "prompt", "model", "policy"])
async def test_lookup_identity_keeps_actual_pixels_scope_output_and_context(tmp_path, mutation):
    service, source, request, leaf, options, _ = setup(tmp_path)
    await service.record(source, request, leaf, **options)
    if mutation == "pixels":
        request.payload = b"different"
    elif mutation == "page":
        request.page_number = 2
    elif mutation == "region":
        request.region = NormalizedRegionV1(x1=.5)
    elif mutation == "output":
        request.max_output_tokens = 1024
    elif mutation == "purpose":
        request.purpose = "submissions"
    elif mutation == "prompt":
        options["prompt_version"] = "next"
    elif mutation == "model":
        options["capabilities"].fingerprint = "next"
    else:
        options["policy"].max_patches = 0
    found = await service.lookup(source, request, **options)
    assert found.status == "miss" and found.receipt is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["digest", "length", "page", "region", "output", "route", "kind", "not_run", "uncertain"])
async def test_record_rejects_leaf_not_matching_actual_input_before_storage(tmp_path, monkeypatch, mutation):
    service, source, request, leaf, options, _ = setup(tmp_path)
    if mutation == "digest":
        leaf.payload_sha256 = "e" * 64
    elif mutation == "length":
        leaf.payload_bytes += 1
    elif mutation == "page":
        leaf.page_numbers = [2]
    elif mutation == "region":
        leaf.region = NormalizedRegionV1(x1=.5)
    elif mutation == "output":
        leaf.requested_output_tokens = 1024
    elif mutation == "route":
        leaf.candidate.provider_route_id = "other"
    elif mutation == "kind":
        leaf.candidate.kind = "ocr"
    elif mutation == "not_run":
        leaf.candidate = candidate("", status="not_run")
    else:
        leaf.candidate = candidate("", status="error", safe_error_code="provider_submit_uncertain")
    async def forbidden(*args, **kwargs):
        pytest.fail("mismatched leaf must not write")
    monkeypatch.setattr(service.store, "save", forbidden)
    with pytest.raises(RecognitionError, match="recognition_artifact_invalid"):
        await service.record(source, request, leaf, **options)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,mutation", [("locator", "digest"), ("locator", "target"), ("patch", "digest"),
                                         ("patch", "context"), ("patch", "page")])
async def test_locator_and_patch_actual_input_pairing_is_independent_of_a_valid_cache_key(tmp_path, kind, mutation):
    service, source, request, leaf, options, _ = setup(tmp_path, kind)
    if mutation == "digest":
        (leaf.sheets[0] if kind == "locator" else leaf.image).payload_sha256 = "d" * 64
    elif mutation == "target":
        request.targets = ["Q2"]
    elif mutation == "context":
        request.repair_context.issue_codes = ["different_issue"]
    else:
        request.page_number = 2
        request.repair_context.page_number = 2
        request.repair_context.span_id = "p0002-s0000"
    with pytest.raises(RecognitionError, match="recognition_artifact_invalid"):
        await service.record(source, request, leaf, **options)


@pytest.mark.asyncio
async def test_inconsistent_but_self_hashed_artifact_is_not_a_hit(tmp_path):
    service, source, request, leaf, options, _ = setup(tmp_path)
    identity = model_cache_identity(source, request, capabilities=options["capabilities"], policy=options["policy"],
                                    prompt_version=options["prompt_version"])
    leaf.payload_sha256 = "f" * 64
    envelope = build_artifact(identity=identity, source=source, payload_kind="visual_read", payload=leaf)
    await service.store.save(envelope, binding=service.binding, authorized_owner_id=source.owner_id)
    found = await service.lookup(source, request, **options)
    assert found.status == "corrupt" and found.receipt is None


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["corrupt", "unavailable", "source_cleaned", "deleted"])
async def test_storage_and_lifecycle_states_stay_explicit(tmp_path, monkeypatch, condition):
    service, source, request, leaf, options, original = setup(tmp_path)
    fresh = await service.record(source, request, leaf, **options)
    row = file_repository.get_file(file_id=fresh.artifact_id, owner_id=service.owner)
    if condition == "corrupt":
        service.store.storage.save(row.storage_key, b"invalid")
    elif condition == "unavailable":
        def denied(*args):
            raise OSError("private endpoint")
        monkeypatch.setattr(service.store.storage, "open", denied)
    elif condition == "source_cleaned":
        with session_scope() as db:
            db.get(StoredFileRecord, original.id).availability_status = "unavailable"
    else:
        with session_scope() as db:
            db.get(AssignmentRecord, service.binding.business_id).deletion_requested_at = time.time()
    found = await service.lookup(source, request, **options)
    assert found.status == (condition if condition in {"corrupt", "unavailable"} else "source_unavailable")
    assert found.receipt is None


@pytest.mark.asyncio
async def test_source_cleanup_during_receipt_check_cannot_return_a_hit(tmp_path, monkeypatch):
    service, source, request, leaf, options, original = setup(tmp_path)
    await service.record(source, request, leaf, **options)
    make_receipt = calls._receipt
    def deleting(*args):
        result = make_receipt(*args)
        with session_scope() as db:
            db.get(StoredFileRecord, original.id).availability_status = "unavailable"
        return result
    monkeypatch.setattr(calls, "_receipt", deleting)
    found = await service.lookup(source, request, **options)
    assert found.status == "source_unavailable" and found.receipt is None


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["owner_id", "business_id"])
async def test_wrong_owner_or_business_stops_before_io(tmp_path, monkeypatch, field):
    service, source, request, leaf, options, _ = setup(tmp_path)
    setattr(source, field, "other")
    async def forbidden(*args, **kwargs):
        pytest.fail("unauthorized context cannot touch artifacts")
    monkeypatch.setattr(service.store, "find_success", forbidden)
    monkeypatch.setattr(service.store, "save", forbidden)
    with pytest.raises(RecognitionError, match="recognition_source_mismatch"):
        await service.lookup(source, request, **options)
    with pytest.raises(RecognitionError, match="recognition_source_mismatch"):
        await service.record(source, request, leaf, **options)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["record", "lookup"])
async def test_freeze_all_context_before_first_await(tmp_path, monkeypatch, stage):
    service, source, request, leaf, options, _ = setup(tmp_path)
    before = leaf.model_dump_json()
    original_source = source.model_copy(deep=True)
    if stage == "lookup":
        await service.record(source, request, leaf, **options)
    threadpool = calls.run_in_threadpool
    entered, release = asyncio.Event(), asyncio.Event()
    async def waiting(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            await release.wait()
        return await threadpool(*args, **kwargs)
    monkeypatch.setattr(calls, "run_in_threadpool", waiting)
    task = asyncio.create_task(service.record(source, request, leaf, **options) if stage == "record"
                               else service.lookup(source, request, **options))
    await entered.wait()
    source.owner_id = "other"
    request.payload = b"mutated"
    leaf.candidate.text = "changed"
    options["policy"].force_visual = True
    options["capabilities"].fingerprint = "changed"
    service.binding.business_id = "changed"
    service.owner = "other"
    release.set()
    result = await task
    receipt = result if stage == "record" else result.receipt
    assert receipt.envelope.payload.model_dump_json() == before
    assert receipt.envelope.source == original_source


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["record", "lookup"])
async def test_deadline_cancellation_and_storage_failure_never_retry(tmp_path, monkeypatch, stage):
    service, source, request, leaf, options, _ = setup(tmp_path)
    entered = asyncio.Event()
    count = []
    async def waiting(*args, **kwargs):
        count.append(1)
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(service.store, "save" if stage == "record" else "find_success", waiting)
    async def execute(timeout=10):
        if stage == "record":
            return await service.record(source, request, leaf, **options, timeout_seconds=timeout)
        return await service.lookup(source, request, **options, timeout_seconds=timeout)
    with pytest.raises(RecognitionError, match="recognition_timeout"):
        await execute(.02)
    assert count == [1]
    entered.clear()
    task = asyncio.create_task(execute())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert count == [1, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("kind", "unknown"), ("occurrence_id", "initial:0"),
                                        ("occurrence_id", "locator:0000"), ("timeout_seconds", True),
                                        ("timeout_seconds", 0), ("timeout_seconds", 31),
                                        ("timeout_seconds", float("inf")), ("timeout_seconds", float("nan"))])
async def test_invalid_occurrence_or_deadline_cannot_enter_io(tmp_path, monkeypatch, field, value):
    service, source, request, leaf, options, _ = setup(tmp_path)
    options[field] = value
    async def forbidden(*args, **kwargs):
        pytest.fail("invalid request cannot touch artifacts")
    monkeypatch.setattr(service.store, "find_success", forbidden)
    monkeypatch.setattr(service.store, "save", forbidden)
    with pytest.raises(RecognitionError, match="recognition_request_invalid"):
        await service.lookup(source, request, **options)
    with pytest.raises(RecognitionError, match="recognition_request_invalid"):
        await service.record(source, request, leaf, **options)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["current", "history", "occurrence", "kind", "artifact", "io_nan"])
async def test_receipt_rejects_false_accounting_or_stage_identity(tmp_path, mutation):
    service, source, request, leaf, options, _ = setup(tmp_path)
    await service.record(source, request, leaf, **options)
    value = (await service.lookup(source, request, **options)).receipt.model_dump()
    if mutation == "current":
        value["current_usage"]["input_tokens"] = 10
    elif mutation == "history":
        value["historical_usage"]["output_tokens"] = 0
    elif mutation == "occurrence":
        value["occurrence_id"] = "locator:0000"
    elif mutation == "kind":
        value["kind"] = "locator"
    elif mutation == "artifact":
        value["artifact_id"] = ""
    else:
        value["artifact_io_duration_ms"] = float("nan")
    with pytest.raises(ValidationError):
        calls.RecognitionCallReceiptV2.model_validate(value)
