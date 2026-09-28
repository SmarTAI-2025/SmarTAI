"""Independent V2 workflow integration with real local tools and synthetic models."""
import asyncio
import hashlib
import json
import time
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from backend.agents.recognition_agent import RecognitionAgent, RecognitionReadRequestV1, RecognitionWorkflowReadV1
from backend.db.models import AssignmentRecord, StoredFileRecord
from backend.db.session import session_scope
from backend.domain.errors import RecognitionError
from backend.recognition.models import RecognitionPolicyV1
from backend.recognition.runtime import RecognitionCapacity
from backend.recognition.workflow_v2 import RecognitionAssemblyV2, RecognitionWorkflowReadV2
from backend.services.recognition_artifacts import RecognitionArtifactStore
from backend.services.recognition_calls import RecognitionCallLookup, RecognitionCallService
from backend.tests.test_recognition_agent import Engine, pdf
from backend.tests.test_recognition_artifact_store import seeded
from backend.tests.test_recognition_executor import FakeEngine
from backend.tests.test_recognition_local_evidence import setup as stored_image


def setup(tmp_path, *, texts=("1. Source alpha.", "2. Source beta."), image=False, engine=None):
    if image:
        reader, data, source, binding, original = stored_image(tmp_path, image=True)
        store = reader.store
    else:
        storage, binding, original, seed = seeded(tmp_path)
        data = pdf(texts)
        digest = hashlib.sha256(data).hexdigest()
        storage.save(original.storage_key, data)
        with session_scope() as db:
            row = db.get(StoredFileRecord, original.id)
            row.sha256, row.size_bytes = digest, len(data)
        source = seed.source.model_copy(update={"input_sha256": digest})
        store = RecognitionArtifactStore(storage)
    service = RecognitionCallService(store=store, binding=binding, authorized_owner_id=source.owner_id)
    return SimpleNamespace(data=data, source=source, binding=binding, original=original,
                           service=service, engine=engine or Engine())


def request(value, **changes):
    return RecognitionReadRequestV1(source=value.source.model_copy(deep=True), purpose="problems",
                                    policy=RecognitionPolicyV1(force_visual=True), **changes)


async def run(value, *, recognize=False, request_value=None, service=True):
    agent = RecognitionAgent(value.engine, capacity=RecognitionCapacity(),
                             call_service=value.service if service else None)
    options = dict(authorized_owner_id=value.source.owner_id)
    if recognize:
        options["prompt_version"] = "synthetic-assembly-v2"
    return await (agent.recognize if recognize else agent.read)(request_value or request(value), value.data, **options)


@pytest.mark.asyncio
@pytest.mark.parametrize("image", [False, True])
@pytest.mark.parametrize("unknown", [False, True])
async def test_first_dispatch_then_all_hits_preserve_history_and_zero_current_spend(tmp_path, image, unknown):
    value = setup(tmp_path, image=image, engine=Engine(known_usage=not unknown))
    first = await run(value)
    first_count = 1 if image else 2
    assert isinstance(first, RecognitionWorkflowReadV2)
    assert first.budget.initial_calls == first_count
    assert all(record.origin == "dispatch" for record in first.read_batch.provenance)
    historical = [unit.model_dump() for unit in first.read_batch.units]
    second = await run(value)
    assert len(value.engine.reads) == first_count
    assert second.budget.total_calls == second.budget.pending_calls == second.budget.settled_calls == 0
    assert second.budget.input_tokens == second.budget.output_tokens == 0
    assert second.budget.reserved_output_tokens == second.budget.charged_output_tokens == 0
    assert second.read_batch.usage.initial_calls == 0
    assert second.read_batch.usage.input_tokens == second.read_batch.usage.output_tokens == 0
    assert second.logical_budget.cache_hits == second.logical_budget.initial_calls == first_count
    assert second.logical_budget.region_count == first_count
    assert [unit.model_dump() for unit in second.read_batch.units] == historical
    assert [record.original.model_dump() for record in second.read_batch.provenance] == historical
    assert all(record.origin == "cache_hit" for record in second.read_batch.provenance)
    assert all(unit.candidate.input_tokens == (None if unknown else 8) for unit in second.read_batch.units)
    assert RecognitionWorkflowReadV2.model_validate_json(second.model_dump_json()) == second


@pytest.mark.asyncio
async def test_mixed_hit_miss_preserves_original_unit_id_in_provenance(tmp_path):
    value = setup(tmp_path)
    first = await run(value, request_value=request(value, scope="pages", pages=[2]))
    assert first.read_batch.units[0].unit_id == "u0000"
    second = await run(value)
    assert len(value.engine.reads) == 2
    assert [record.origin for record in second.read_batch.provenance] == ["dispatch", "cache_hit"]
    assert second.read_batch.units[1].unit_id == "u0001"
    assert second.read_batch.provenance[1].original.unit_id == "u0000"
    assert second.read_batch.units[1].candidate == first.read_batch.units[0].candidate
    assert second.budget.initial_calls == 1 and second.logical_budget.initial_calls == 2
    assert second.budget.input_tokens == 8 and second.logical_budget.cache_hits == 1
    assert RecognitionWorkflowReadV2.model_validate_json(second.model_dump_json()) == second


@pytest.mark.asyncio
async def test_locator_hit_remains_unverified_and_shares_logical_budget_with_reader(tmp_path):
    value = setup(tmp_path, texts=(None,), engine=Engine({"Q7": [1]}, known_usage=False))
    target_request = request(value, scope="targets", targets=["Q7"])
    first = await run(value, request_value=target_request)
    second = await run(value, request_value=target_request)
    assert len(value.engine.locates) == len(value.engine.reads) == 1
    assert first.budget.total_calls == 2 and second.budget.total_calls == 0
    assert second.logical_budget.locator_calls == second.logical_budget.initial_calls == 1
    assert second.logical_budget.locator_cache_hits == second.logical_budget.initial_cache_hits == 1
    assert second.locator_provenance[0].origin == "cache_hit"
    assert second.locator_provenance[0].original.result.candidate.input_tokens is None
    assert "scan_locations_unverified" in second.locator_calls[0].result.reason_codes
    assert not second.recognition_complete
    assert RecognitionWorkflowReadV2.model_validate_json(second.model_dump_json()) == second


@pytest.mark.asyncio
async def test_default_agent_keeps_v1_contract_without_implicit_cache(tmp_path):
    value = setup(tmp_path, texts=("A source paragraph.",))
    first, second = await run(value, service=False), await run(value, service=False)
    assert type(first) is type(second) is RecognitionWorkflowReadV1
    assert first.schema_version == second.schema_version == 1
    assert "provenance" not in second.read_batch.model_dump()
    assert len(value.engine.reads) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["not_success", "corrupt", "unavailable", "source_unavailable"])
async def test_unsuccessful_lookup_is_terminal_and_never_dispatches(tmp_path, monkeypatch, status):
    value = setup(tmp_path)
    async def fail_lookup(*args, **kwargs):
        return RecognitionCallLookup(status)
    monkeypatch.setattr(value.service, "lookup", fail_lookup)
    raw = await run(value)
    assert not value.engine.reads and not value.engine.locates
    assert raw.budget.total_calls == raw.logical_budget.total_calls == 0
    assert raw.read_batch.units == raw.read_batch.provenance == []
    assert raw.read_batch.unprocessed_pages == [1, 2]
    assert "recognition_cache_" + status in raw.stop_codes


@pytest.mark.asyncio
@pytest.mark.parametrize("error", ["recognition_artifact_unavailable", "recognition_timeout"])
async def test_record_failure_retains_dispatched_candidate_and_cost_then_stops(tmp_path, monkeypatch, error):
    value = setup(tmp_path)
    async def failed_record(*args, **kwargs):
        raise RecognitionError(error)
    monkeypatch.setattr(value.service, "record", failed_record)
    raw = await run(value)
    assert len(value.engine.reads) == 1
    assert raw.budget.total_calls == raw.logical_budget.total_calls == 1
    assert raw.budget.input_tokens == 8 and raw.budget.output_tokens == 1
    assert raw.read_batch.units[0].candidate.text == "literal x = -3\n[crossed-out: x = 2]"
    assert raw.read_batch.unprocessed_pages == [2]
    record = raw.read_batch.provenance[0]
    assert record.origin == "dispatch" and record.artifact_id is None and record.storage_error == error
    assert error in raw.stop_codes
    assert RecognitionWorkflowReadV2.model_validate_json(raw.model_dump_json()) == raw


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["owner_id", "business_id"])
async def test_foreign_service_context_is_rejected_before_any_provider(tmp_path, field):
    value = setup(tmp_path)
    altered = request(value)
    setattr(altered.source, field, "other-owner-or-task")
    with pytest.raises(RecognitionError, match="recognition_source_mismatch"):
        await run(value, request_value=altered)
    assert not value.engine.reads and not value.engine.locates


@pytest.mark.asyncio
async def test_same_bytes_do_not_authorize_another_stored_source(tmp_path):
    value = setup(tmp_path)
    await run(value)
    before = len(value.engine.reads)
    changed = request(value)
    changed.source.stored_file_id = "missing-source"
    raw = await run(value, request_value=changed)
    assert len(value.engine.reads) == before
    assert raw.budget.total_calls == raw.logical_budget.total_calls == 0
    assert not raw.read_batch.units
    assert raw.stop_codes


@pytest.mark.asyncio
async def test_same_authorized_source_with_another_scope_does_not_reuse_evidence(tmp_path):
    # An internal caller supplies source-purpose authority; scope is a cache
    # boundary, not a separate database ownership claim for the same original.
    value = setup(tmp_path)
    first = await run(value)
    changed = request(value)
    changed.source.scope = "submission_source"
    second = await run(value, request_value=changed)
    assert len(value.engine.reads) == 4
    assert second.logical_budget.cache_hits == 0 and second.budget.initial_calls == 2
    assert all(record.origin == "dispatch" for record in second.read_batch.provenance)
    assert {record.artifact_id for record in first.read_batch.provenance}.isdisjoint(
        record.artifact_id for record in second.read_batch.provenance)


class RepairEngine(Engine):
    def __init__(self):
        super().__init__()
        self.caps.semantic_repair = self.caps.response_recheck = True
        self.repairs = []

    async def repair(self, request):
        self.repairs.append(request)
        return self.candidate(json.dumps({"decision": "keep_visual", "text": request.repair_context.visual_text,
                                          "source_evidence": "Visible original source marks."}))


@pytest.mark.asyncio
async def test_recognize_preserves_v2_assembly_and_does_not_reuse_unverified_repair(tmp_path):
    value = setup(tmp_path, texts=("1. Source alpha.",), engine=RepairEngine())
    first = await run(value, recognize=True)
    assert isinstance(first, RecognitionAssemblyV2)
    assert len(value.engine.reads) == len(value.engine.repairs) == 1
    assert first.repair_execution.budget.patch_calls == 1
    assert first.repair_execution.provenance[0].origin == "dispatch"
    second = await run(value, recognize=True)
    assert len(value.engine.reads) == len(value.engine.repairs) == 1
    assert second.raw.read_batch.provenance[0].origin == "cache_hit"
    assert second.repair_execution.budget.total_calls == 0
    assert "recognition_cache_not_success" in second.repair_execution.stop_codes
    assert second.repair_execution.provenance == []
    assert RecognitionAssemblyV2.model_validate_json(second.model_dump_json()) == second


@pytest.mark.asyncio
async def test_parent_deleted_after_dispatch_still_returns_paid_evidence(tmp_path):
    value = setup(tmp_path)
    original_read = value.engine.recognize
    async def delete_after_read(unit):
        result = await original_read(unit)
        with session_scope() as db:
            row = db.get(AssignmentRecord, value.binding.business_id)
            row.deletion_requested_at = time.time()
        return result
    value.engine.recognize = delete_after_read
    raw = await run(value)
    assert len(value.engine.reads) == 1
    assert raw.budget.total_calls == 1 and raw.budget.input_tokens == 8
    assert raw.read_batch.units[0].candidate.status == "ok"
    assert raw.read_batch.provenance[0].storage_error is not None
    assert raw.read_batch.unprocessed_pages == [2]


@pytest.mark.asyncio
async def test_cached_initial_grants_one_live_patch_without_recounting_historical_tokens(tmp_path):
    value = setup(tmp_path, texts=("1. Source alpha.",), engine=RepairEngine())
    first = await run(value)
    second = await run(value, recognize=True)
    assert first.budget.initial_calls == 1 and len(value.engine.reads) == 1
    assert len(value.engine.repairs) == 1
    assert second.raw.budget.total_calls == 0
    assert second.repair_execution.budget.initial_calls == 0
    assert second.repair_execution.budget.patch_calls == 1
    assert second.repair_execution.budget.input_tokens == 8
    assert second.repair_execution.logical_budget.initial_cache_hits == 1
    assert second.repair_execution.logical_budget.total_calls == 2
    assert RecognitionAssemblyV2.model_validate_json(second.model_dump_json()) == second


@pytest.mark.asyncio
async def test_document_only_ocr_reuses_exact_export_without_companion_model(tmp_path):
    value = setup(tmp_path, engine=FakeEngine(document=True, known_usage=False))
    first = await run(value)
    # Cross a wall-clock second so a generated PDF trailer ID cannot make this
    # exact-byte cache test accidentally pass within the same timestamp seed.
    await asyncio.sleep(1.1)
    second = await run(value)
    assert len(value.engine.requests) == 1
    assert first.read_batch.units[0].page_numbers == [1, 2]
    assert first.read_batch.units[0].output_mapping == "document_only"
    assert second.logical_budget.initial_calls == second.logical_budget.cache_hits == 1
    assert second.logical_budget.region_count == 2
    assert second.read_batch.units[0].candidate.input_tokens is None
    assert second.budget.total_calls == 0
    assert not second.locator_calls and not second.recognition_complete


@pytest.mark.asyncio
async def test_locator_record_failure_keeps_unknown_spend_and_never_reads(tmp_path, monkeypatch):
    value = setup(tmp_path, texts=(None,), engine=Engine({"Q7": [1]}, known_usage=False))
    async def failed_record(*args, **kwargs):
        raise RecognitionError("recognition_artifact_unavailable")
    monkeypatch.setattr(value.service, "record", failed_record)
    raw = await run(value, request_value=request(value, scope="targets", targets=["Q7"]))
    assert len(value.engine.locates) == 1 and not value.engine.reads
    assert raw.locator_halted and raw.read_batch is None
    assert raw.budget.locator_calls == 1 and raw.budget.input_tokens is None
    assert raw.budget.output_tokens is None and raw.budget.reserved_output_tokens == 4096
    assert raw.locator_provenance[0].storage_error == "recognition_artifact_unavailable"
    assert RecognitionWorkflowReadV2.model_validate_json(raw.model_dump_json()) == raw


@pytest.mark.asyncio
async def test_repair_record_failure_keeps_current_extra_cost_and_raw_result(tmp_path, monkeypatch):
    value = setup(tmp_path, texts=("1. Source alpha.",), engine=RepairEngine())
    original_record = value.service.record
    async def failed_repair(*args, **kwargs):
        if kwargs["kind"] == "patch":
            raise RecognitionError("recognition_artifact_unavailable")
        return await original_record(*args, **kwargs)
    monkeypatch.setattr(value.service, "record", failed_repair)
    result = await run(value, recognize=True)
    assert len(value.engine.reads) == len(value.engine.repairs) == 1
    assert result.repair_execution.budget.total_calls == 2
    assert result.repair_execution.budget.input_tokens == 16
    assert result.repair_execution.calls[0].result.parse_status == "ok"
    assert result.repair_execution.provenance[0].storage_error == "recognition_artifact_unavailable"
    assert "recognition_artifact_unavailable" in result.repair_execution.stop_codes
    assert RecognitionAssemblyV2.model_validate_json(result.model_dump_json()) == result


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["initial", "locator"])
async def test_serialized_persistence_failure_cannot_claim_a_later_paid_dispatch(tmp_path, stage):
    value = setup(tmp_path, texts=(None,) if stage == "locator" else ("First source.", "Second source."),
                  engine=Engine({"Q7": [1]}))
    source_request = request(value, scope="targets", targets=["Q7"]) if stage == "locator" else request(value)
    raw = await run(value, request_value=source_request)
    dumped = raw.model_dump()
    records = dumped["locator_provenance"] if stage == "locator" else dumped["read_batch"]["provenance"]
    records[0]["artifact_id"] = None
    records[0]["storage_error"] = "recognition_artifact_unavailable"
    dumped["stop_codes"].append("recognition_artifact_unavailable")
    if stage == "initial":
        dumped["read_batch"]["stop_codes"].append("recognition_artifact_unavailable")
    with pytest.raises(ValidationError):
        RecognitionWorkflowReadV2.model_validate(dumped)


@pytest.mark.asyncio
async def test_serialized_repair_persistence_failure_cannot_claim_a_later_patch(tmp_path):
    value = setup(tmp_path, engine=RepairEngine())
    result = await run(value, recognize=True)
    assert len(result.repair_execution.calls) == 2
    dumped = result.model_dump()
    record = dumped["repair_execution"]["provenance"][0]
    record["artifact_id"] = None
    record["storage_error"] = "recognition_artifact_unavailable"
    dumped["repair_execution"]["stop_codes"].append("recognition_artifact_unavailable")
    with pytest.raises(ValidationError):
        RecognitionAssemblyV2.model_validate(dumped)
