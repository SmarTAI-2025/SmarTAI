import hashlib

import pytest
from pydantic import ValidationError

from backend.agents.recognition_agent import RecognitionAgent, RecognitionReadRequestV1
from backend.db import file_repository
from backend.domain.errors import RecognitionError
from backend.recognition.artifact_codec import build_artifact, decode_artifact, encode_artifact
from backend.recognition.cache_identity import final_cache_identity
from backend.recognition.models import RecognitionPolicyV1
from backend.recognition.runtime import RecognitionCapacity
from backend.recognition.workflow_v2 import RecognitionAssemblyV2
from backend.services.recognition_artifacts import RecognitionArtifactStore
from backend.services.recognition_calls import RecognitionCallService
from backend.services.recognition_results import RecognitionResultService
from backend.tests.test_recognition_artifact_store import seeded
from backend.tests.test_recognition_recheck import Engine, source_file


def setup(tmp_path, *, pdf=False, pages=1, initial=None):
    storage, binding, _, seed = seeded(tmp_path)
    data, content_type = source_file(pdf=pdf, pages=pages)
    row = file_repository.save_file(storage=storage, owner_id=seed.source.owner_id, kind="uploaded_test_fixture",
                                   original_name="synthetic-source", content=data, content_type=content_type,
                                   **binding.repository_links)
    source = seed.source.model_copy(update=dict(stored_file_id=row.id, content_type=content_type,
                                                input_sha256=hashlib.sha256(data).hexdigest()))
    store = RecognitionArtifactStore(storage)
    calls = RecognitionCallService(store=store, binding=binding, authorized_owner_id=source.owner_id)
    results = RecognitionResultService(store=store, binding=binding, authorized_owner_id=source.owner_id)
    engine = Engine(initial=initial)
    request = RecognitionReadRequestV1(source=source, purpose="problems", policy=RecognitionPolicyV1(force_visual=True))
    agent = RecognitionAgent(engine, capacity=RecognitionCapacity(), call_service=calls)
    return agent, request, data, results


async def recognize(agent, request, data):
    return await agent.recognize(request, data, authorized_owner_id=request.source.owner_id,
                                 prompt_version="faithful-reader-v1")


@pytest.mark.asyncio
@pytest.mark.parametrize("pdf", [False, True])
@pytest.mark.parametrize("initial", [["literal 1 + 1 = 3"], ["[unclear] + 1 = 3"], [""]])
async def test_v2_current_and_repair_roundtrip_preserves_wrong_answer(tmp_path, pdf, initial):
    agent, request, data, results = setup(tmp_path, pdf=pdf, initial=initial)
    assembly = await recognize(agent, request, data)
    assert assembly.schema_version == 2 and assembly.raw.schema_version == 2
    assert assembly.raw.read_batch.units[0].candidate.text == initial[0]
    assert "1 + 1 = 3" in assembly.document.final_markdown
    assert assembly.document.confidence != "high"
    assert RecognitionAssemblyV2.model_validate_json(assembly.model_dump_json()) == assembly
    receipt = await results.record(assembly)
    assert receipt.schema_version == 2 and receipt.current_usage.total_calls == 1 + len(agent.engine.repairs)
    identity = final_cache_identity(request, capabilities=agent.engine.capabilities,
                                    prompt_version=assembly.prompt_version, workflow_version=2)
    artifact = build_artifact(identity=identity, source=request.source, payload_kind="assembly_v2", payload=assembly)
    assert decode_artifact(encode_artifact(artifact)) == artifact
    assert final_cache_identity(request, capabilities=agent.engine.capabilities,
                                prompt_version=assembly.prompt_version).key != identity.key
    old_lookup = await results.lookup(request, data, capabilities=agent.engine.capabilities, prompt_version=assembly.prompt_version)
    assert old_lookup.status == "miss"
    found = await results.lookup(request, data, capabilities=agent.engine.capabilities,
                                  prompt_version=assembly.prompt_version, workflow_version=2)
    assert found.status == ("hit" if artifact.cacheable_success else "not_success")
    if found.status == "hit":
        assert found.invocation.current_usage.total_calls == found.invocation.current_usage.output_tokens == 0
        assert found.invocation.historical_usage == receipt.current_usage


@pytest.mark.asyncio
async def test_all_hits_have_no_fresh_calls_or_tokens_and_preserve_original_tokens(tmp_path):
    agent, request, data, results = setup(tmp_path, initial=["literal 1 + 1 = 3"])
    first = await recognize(agent, request, data)
    second = await recognize(agent, request, data)
    assert len(agent.engine.reads) == 1 and not agent.engine.repairs
    assert second.raw.budget.total_calls == second.document.usage.output_tokens == 0
    assert second.raw.logical_budget.total_calls == second.raw.logical_budget.cache_hits == 1
    assert second.document.usage.cache_hits == second.raw.read_batch.usage.cache_hits == 1
    evidence = second.raw.read_batch.provenance[0]
    assert evidence.origin == "cache_hit" and evidence.original.candidate.output_tokens == 2
    assert evidence.original == first.raw.read_batch.units[0]
    assert second.repair_execution.logical_budget == second.raw.logical_budget
    receipt = await results.record(second)
    assert receipt.current_usage.total_calls == 0 and receipt.current_usage.cache_hits == 1
    assert receipt.assembly.raw.read_batch.provenance[0].original.candidate.input_tokens == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("path,value", [
    (("raw", "logical_budget", "cache_hits"), 1),
    (("raw", "budget", "initial_calls"), 0),
    (("raw", "read_batch", "provenance"), []),
    (("raw", "read_batch", "provenance", 0, "occurrence_id"), "initial:0007"),
    (("raw", "read_batch", "provenance", 0, "origin"), "cache_hit"),
    (("raw", "read_batch", "provenance", 0, "payload_sha256"), "a" * 64),
    (("raw", "read_batch", "provenance", 0, "source", "stored_file_id"), "other"),
    (("repair_execution", "provenance"), []),
    (("repair_execution", "logical_budget", "patch_calls"), 0),
    (("repair_execution", "budget", "output_tokens"), 0),
    (("document", "final_markdown"), "1 + 1 = 2"),
])
async def test_v2_rejects_tampering_and_false_billing_summaries(tmp_path, path, value):
    agent, request, data, _ = setup(tmp_path)
    result = await recognize(agent, request, data)
    payload = result.model_dump()
    node = payload
    for part in path[:-1]:
        node = node[part]
    node[path[-1]] = value
    with pytest.raises((ValueError, ValidationError, RecognitionError)):
        RecognitionAssemblyV2.model_validate(payload)


@pytest.mark.asyncio
async def test_failed_call_artifact_write_retains_cost_and_stops_repair(tmp_path, monkeypatch):
    agent, request, data, _ = setup(tmp_path)
    async def fail(*args, **kwargs):
        raise RecognitionError("recognition_artifact_unavailable")
    monkeypatch.setattr(agent.call_service, "record", fail)
    assembly = await recognize(agent, request, data)
    assert assembly.raw.budget.total_calls == 1 and assembly.raw.budget.output_tokens == 2
    assert assembly.raw.read_batch.units[0].candidate.text == "[unclear] + 1 = 3"
    assert assembly.raw.read_batch.provenance[0].storage_error == "recognition_artifact_unavailable"
    assert not agent.engine.repairs and "repair_initial_persistence_failed" in assembly.repair_execution.stop_codes
