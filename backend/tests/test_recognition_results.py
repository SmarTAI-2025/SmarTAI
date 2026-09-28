import asyncio
import time

import pytest
from pydantic import ValidationError

from backend.db import file_repository
from backend.db.models import AssignmentRecord
from backend.db.session import session_scope
from backend.domain.errors import RecognitionError
from backend.recognition.artifact_codec import encode_artifact, decode_artifact
from backend.recognition.invocation import RecognitionInvocationV1
from backend.recognition.models import RecognitionCandidateV1
from backend.services import recognition_results as results
from backend.services.recognition_artifacts import RecognitionArtifactStore
from backend.tests.test_recognition_artifact_store import seeded
from backend.tests.test_recognition_fusion import assemble, workflow


def setup(tmp_path, *, unknown=False, uncertain=False):
    storage, binding, original, seed = seeded(tmp_path)
    text = "[unclear]" if uncertain else "A literal answer."
    reply = RecognitionCandidateV1(kind="vision", status="ok", text=text, provider_route_id="chosen",
                                    input_tokens=None if unknown else 10, output_tokens=None if unknown else 2)
    raw = workflow([""], [reply], purpose="submissions")
    raw.request.source = seed.source.model_copy(deep=True)
    raw.read_batch.source = seed.source.model_copy(deep=True)
    assembly = assemble(raw)
    service = results.RecognitionResultService(store=RecognitionArtifactStore(storage), binding=binding,
                                               authorized_owner_id=seed.source.owner_id)
    return service, assembly, original, b"private original PDF bytes"


async def lookup(service, assembly, data, **kwargs):
    return await service.lookup(assembly.raw.request, data, capabilities=assembly.raw.engine_capabilities,
                                prompt_version=assembly.prompt_version, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [False, True])
async def test_terminal_hit_has_new_zero_dispatch_usage_without_rewriting_old_candidate(tmp_path, unknown):
    service, assembly, _, data = setup(tmp_path, unknown=unknown)
    before = assembly.model_dump_json()
    fresh = await service.record(assembly)
    assert not fresh.reused and fresh.historical_usage is None
    assert fresh.current_usage.total_calls == 1
    assert fresh.current_usage.input_tokens == (None if unknown else 10)
    assert fresh.current_usage.usage_complete is not unknown
    restored = await lookup(service, assembly, data)
    assert restored.status == "hit" and restored.invocation.reused
    receipt = restored.invocation
    assert receipt.current_usage.total_calls == 0
    assert receipt.current_usage.input_tokens == receipt.current_usage.output_tokens == 0
    assert receipt.current_usage.usage_complete and receipt.current_usage.cache_hits == 1
    assert receipt.historical_usage == fresh.current_usage
    assert receipt.assembly.model_dump_json() == before == assembly.model_dump_json()
    assert RecognitionInvocationV1.model_validate_json(receipt.model_dump_json()) == receipt
    row = file_repository.get_file(file_id=fresh.artifact_id, owner_id=service.owner)
    with service.store.storage.open(row.storage_key) as stream:
        blob = stream.read()
    envelope = decode_artifact(blob)
    assert encode_artifact(envelope) == blob
    receipt.assembly.raw.read_batch.units[0].candidate.text = "caller change"
    assert (await lookup(service, assembly, data)).invocation.assembly.model_dump_json() == before


@pytest.mark.asyncio
async def test_miss_and_unsuccessful_final_never_become_automatic_dispatch(tmp_path):
    service, assembly, _, data = setup(tmp_path, uncertain=True)
    assert (await lookup(service, assembly, data)).status == "miss"
    fresh = await service.record(assembly)
    assert fresh.current_usage.total_calls == 1
    found = await lookup(service, assembly, data)
    assert found.status == "not_success" and found.artifact_id == fresh.artifact_id
    assert found.invocation is None
    value = fresh.model_dump()
    value.update(reused=True, current_usage=dict(input_tokens=0, output_tokens=0, cache_hits=1), historical_usage=fresh.current_usage.model_dump())
    with pytest.raises(ValidationError, match="unsuccessful evidence"):
        RecognitionInvocationV1.model_validate(value)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["current", "historical", "identity", "missing_artifact"])
async def test_receipt_rejects_misleading_usage_or_source_identity(tmp_path, mutation):
    service, assembly, _, data = setup(tmp_path)
    await service.record(assembly)
    receipt = (await lookup(service, assembly, data)).invocation
    value = receipt.model_dump()
    if mutation == "current":
        value["current_usage"]["input_tokens"] = 10
    elif mutation == "historical":
        value["historical_usage"]["output_tokens"] = 0
    elif mutation == "identity":
        value["identity"]["source_sha256"] = "f" * 64
    else:
        value["artifact_id"] = ""
    with pytest.raises(ValidationError):
        RecognitionInvocationV1.model_validate(value)


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["corrupt", "unavailable", "deleted"])
async def test_bad_storage_or_deleted_task_is_explicit_not_a_hit(tmp_path, monkeypatch, condition):
    service, assembly, _, data = setup(tmp_path)
    fresh = await service.record(assembly)
    row = file_repository.get_file(file_id=fresh.artifact_id, owner_id=service.owner)
    if condition == "corrupt":
        service.store.storage.save(row.storage_key, b"bad")
    elif condition == "unavailable":
        def denied(*args):
            raise OSError("secret endpoint")
        monkeypatch.setattr(service.store.storage, "open", denied)
    else:
        with session_scope() as db:
            db.get(AssignmentRecord, service.binding.business_id).deletion_requested_at = time.time()
    found = await lookup(service, assembly, data)
    assert found.status == {"deleted": "source_unavailable"}.get(condition, condition)
    assert found.invocation is None


@pytest.mark.asyncio
async def test_changed_bytes_fail_even_when_metadata_and_final_artifact_match(tmp_path):
    service, assembly, _, data = setup(tmp_path)
    await service.record(assembly)
    with pytest.raises(RecognitionError, match="recognition_source_mismatch"):
        await lookup(service, assembly, data + b"other")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["purpose", "prompt", "model", "policy"])
async def test_final_reuse_requires_exact_purpose_prompt_model_and_policy(tmp_path, change):
    service, assembly, _, data = setup(tmp_path)
    await service.record(assembly)
    request = assembly.raw.request.model_copy(deep=True)
    caps = assembly.raw.engine_capabilities.model_copy(deep=True)
    prompt = assembly.prompt_version
    if change == "purpose":
        request.purpose = "problems"
    elif change == "prompt":
        prompt += "-next"
    elif change == "model":
        caps.fingerprint += "-next"
    else:
        request.policy.max_patches = 0
    found = await service.lookup(request, data, capabilities=caps, prompt_version=prompt)
    assert found.status == "miss" and found.invocation is None


@pytest.mark.asyncio
async def test_task_deleted_during_receipt_validation_cannot_return_final_hit(tmp_path, monkeypatch):
    service, assembly, _, data = setup(tmp_path)
    await service.record(assembly)
    original = results.invocation_receipt
    def deleting(**kwargs):
        receipt = original(**kwargs)
        with session_scope() as db:
            db.get(AssignmentRecord, service.binding.business_id).deletion_requested_at = time.time()
        return receipt
    monkeypatch.setattr(results, "invocation_receipt", deleting)
    assert (await lookup(service, assembly, data)).status == "source_unavailable"


@pytest.mark.asyncio
async def test_lookup_deadline_and_cancellation_cover_storage_wait(tmp_path, monkeypatch):
    service, assembly, _, data = setup(tmp_path)
    started = asyncio.Event()
    async def waiting(**kwargs):
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(service.store, "find_success", waiting)
    with pytest.raises(RecognitionError, match="recognition_timeout"):
        await lookup(service, assembly, data, timeout_seconds=.01)
    started.clear()
    task = asyncio.create_task(lookup(service, assembly, data))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [0, -1, 31, True, float("nan"), float("inf")])
async def test_invalid_deadlines_fail_before_any_io(tmp_path, monkeypatch, timeout):
    service, assembly, _, data = setup(tmp_path)
    async def forbidden(*args, **kwargs):
        pytest.fail("invalid timeout cannot enter storage")
    monkeypatch.setattr(service.store, "save", forbidden)
    monkeypatch.setattr(service.store, "find_success", forbidden)
    with pytest.raises(RecognitionError, match="recognition_request_invalid"):
        await lookup(service, assembly, data, timeout_seconds=timeout)
    with pytest.raises(RecognitionError, match="recognition_request_invalid"):
        await service.record(assembly, timeout_seconds=timeout)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["owner", "business"])
async def test_unauthorized_source_fails_before_io(tmp_path, monkeypatch, changed):
    service, assembly, _, data = setup(tmp_path)
    request = assembly.raw.request.model_copy(deep=True)
    setattr(request.source, "owner_id" if changed == "owner" else "business_id", "other")
    async def forbidden(*args, **kwargs):
        pytest.fail("unauthorized source cannot enter storage")
    monkeypatch.setattr(service.store, "find_success", forbidden)
    with pytest.raises(RecognitionError, match="recognition_source_mismatch"):
        await service.lookup(request, data, capabilities=assembly.raw.engine_capabilities, prompt_version=assembly.prompt_version)


@pytest.mark.asyncio
async def test_lookup_freezes_request_and_caps_before_first_wait(tmp_path, monkeypatch):
    service, assembly, _, data = setup(tmp_path)
    await service.record(assembly)
    request, caps = assembly.raw.request.model_copy(deep=True), assembly.raw.engine_capabilities.model_copy(deep=True)
    entered, release = asyncio.Event(), asyncio.Event()
    threadpool = results.run_in_threadpool
    async def waiting(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            await release.wait()
        return await threadpool(*args, **kwargs)
    monkeypatch.setattr(results, "run_in_threadpool", waiting)
    task = asyncio.create_task(service.lookup(request, data, capabilities=caps, prompt_version=assembly.prompt_version))
    await entered.wait()
    request.source.owner_id = "different"
    caps.fingerprint = "changed"
    release.set()
    assert (await task).status == "hit"


@pytest.mark.asyncio
async def test_record_preserves_unknown_or_failed_evidence_and_does_not_retry_storage(tmp_path, monkeypatch):
    service, assembly, _, _ = setup(tmp_path, unknown=True, uncertain=True)
    before = assembly.model_dump_json()
    calls = []
    async def failure(*args, **kwargs):
        calls.append(1)
        raise RecognitionError("recognition_artifact_unavailable")
    monkeypatch.setattr(service.store, "save", failure)
    with pytest.raises(RecognitionError, match="recognition_artifact_unavailable"):
        await service.record(assembly)
    assert calls == [1] and assembly.model_dump_json() == before
