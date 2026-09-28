"""Explicit durable lookup never schedules a provider or hides unusable evidence."""
import asyncio
from dataclasses import replace
import json
import time

import pytest
from sqlalchemy import event

from backend.db import assignment_repository, file_repository
from backend.db.models import AssignmentRecord, StoredFileRecord
from backend.db.session import get_engine, session_scope
from backend.domain.errors import RecognitionError
from backend.recognition import runtime
from backend.recognition.artifact_codec import build_artifact
from backend.recognition.repair_response import parse_repair_response
from backend.services import recognition_artifacts as service
from backend.skills.recognition_reader import BaiduRecognitionEngine, LLMRecognitionEngine
from backend.storage.base import StorageUnavailable
from backend.tests.test_recognition_artifact_codec import candidate, identity, locator, repair, visual
from backend.tests.test_recognition_artifact_store import seeded


@pytest.fixture(autouse=True)
def no_provider_dispatch(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("cache lookup must never schedule a provider")

    for name in ("run_initial_read", "run_locator_call", "run_repair_call"):
        monkeypatch.setattr(runtime, name, forbidden)
    for name in ("recognize", "locate", "repair"):
        monkeypatch.setattr(LLMRecognitionEngine, name, forbidden)
    monkeypatch.setattr(BaiduRecognitionEngine, "recognize", forbidden)


def options(binding, envelope):
    return dict(source=envelope.source, identity=envelope.identity, binding=binding,
                authorized_owner_id=envelope.source.owner_id)


def visual_artifact(native, *, empty=False, reply=None):
    payload = visual(reply or (candidate("", status="empty") if empty else candidate("Visible source")))
    return build_artifact(identity=identity("visual_read", native.source, payload), source=native.source,
                          payload_kind="visual_read", payload=payload)


async def save(store, binding, envelope):
    return await store.save(envelope, binding=binding, authorized_owner_id=envelope.source.owner_id)


async def find(store, binding, envelope, **changes):
    values = {**options(binding, envelope), "payload_kind": envelope.payload_kind}
    values.update(changes)
    return await store.find_success(**values)


def set_row(file_id, **changes):
    with session_scope() as db:
        row = db.get(StoredFileRecord, file_id)
        for key, value in changes.items():
            setattr(row, key, value)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["hit", "miss", "not_success", "corrupt", "unavailable", "source_unavailable"])
async def test_six_states_are_explicit_and_do_not_dispatch(tmp_path, monkeypatch, status):
    storage, binding, original, envelope = seeded(tmp_path)
    store = service.RecognitionArtifactStore(storage)
    if status == "not_success":
        envelope = visual_artifact(envelope, empty=True)
    row = None if status == "miss" else await save(store, binding, envelope)
    if status == "corrupt":
        storage.save(row.storage_key, b"broken gzip")
    elif status == "unavailable":
        def unavailable(*_args, **_kwargs):
            raise StorageUnavailable("PRIVATE_BODY endpoint credential")
        monkeypatch.setattr(storage, "open", unavailable)
    elif status == "source_unavailable":
        set_row(original.id, availability_status="unavailable")
    result = await find(store, binding, envelope)
    assert result.status == status
    assert (result.envelope is not None) == (status == "hit")
    if result.envelope is not None:
        assert result.envelope == envelope and result.envelope.cacheable_success
    if status not in {"miss", "source_unavailable"}:
        assert result.artifact_id == row.id
    assert "PRIVATE_BODY" not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [candidate("[unclear]"), candidate(warning_codes=["source_form_uncertain"])])
async def test_schema_ok_but_uncertain_visual_is_not_a_successful_cache_hit(tmp_path, reply):
    storage, binding, _, native = seeded(tmp_path)
    envelope = visual_artifact(native, reply=reply)
    store = service.RecognitionArtifactStore(storage)
    row = await save(store, binding, envelope)
    result = await find(store, binding, envelope)
    assert result.status == "not_success" and result.artifact_id == row.id and result.envelope is None
    restored = await store.load(row.id, **options(binding, envelope))
    assert restored.payload.candidate == reply


@pytest.mark.asyncio
async def test_source_mathematical_wrongness_is_not_a_cache_quality_repair_rule(tmp_path):
    storage, binding, _, native = seeded(tmp_path)
    reply = candidate("2 + 2 = 5\nx = (-1")
    envelope = visual_artifact(native, reply=reply)
    store = service.RecognitionArtifactStore(storage)
    await save(store, binding, envelope)
    result = await find(store, binding, envelope)
    assert result.status == "hit" and result.envelope.payload.candidate.text == reply.text


@pytest.mark.asyncio
async def test_structurally_valid_locator_with_explicit_uncertainty_is_not_success(tmp_path):
    storage, binding, _, native = seeded(tmp_path)
    payload = locator()
    payload = locator(reply=payload.result.candidate.model_copy(update={"warning_codes": ["source_form_uncertain"]}))
    assert payload.result.parse_status == "ok" and payload.result.locations[0].status == "candidate"
    envelope = build_artifact(identity=identity("locator", native.source, payload), source=native.source,
                              payload_kind="locator", payload=payload)
    store = service.RecognitionArtifactStore(storage)
    row = await save(store, binding, envelope)
    result = await find(store, binding, envelope)
    assert result.status == "not_success" and result.artifact_id == row.id and result.envelope is None
    restored = await store.load(row.id, **options(binding, envelope))
    assert restored.payload.result.candidate == payload.result.candidate


@pytest.mark.asyncio
async def test_keep_visual_json_does_not_promote_unclear_original_text_to_success(tmp_path):
    storage, binding, _, native = seeded(tmp_path)
    payload = repair()
    context = payload.result.context.model_copy(update={
        "visual_text": "[unclear]", "before_text": "[unclear]", "issue_codes": ["source_form_uncertain"],
    })
    payload.image.source_sha256 = native.source.input_sha256
    payload.result = parse_repair_response(candidate(json.dumps({
        "decision": "keep_visual", "text": "[unclear]", "source_evidence": "Unclear strokes",
    })), context)
    assert payload.result.parse_status == "ok" and payload.result.decision == "keep_visual"
    envelope = build_artifact(identity=identity("repair", native.source, payload), source=native.source,
                              payload_kind="repair", payload=payload)
    store = service.RecognitionArtifactStore(storage)
    row = await save(store, binding, envelope)
    result = await find(store, binding, envelope)
    assert result.status == "not_success" and result.artifact_id == row.id and result.envelope is None
    restored = await store.load(row.id, **options(binding, envelope))
    assert restored.payload.result.final_text == "[unclear]"
    assert restored.payload.result.candidate == payload.result.candidate


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["empty", "corrupt", "missing", "unavailable", "wrong_kind"])
async def test_latest_bad_candidate_does_not_fall_back_to_older_success(tmp_path, monkeypatch, failure):
    storage, binding, _, native = seeded(tmp_path)
    envelope = visual_artifact(native)
    store = service.RecognitionArtifactStore(storage)
    older = await save(store, binding, envelope)
    latest = await save(store, binding, visual_artifact(native, empty=failure == "empty"))
    set_row(older.id, created_at=100)
    set_row(latest.id, created_at=200)
    expected = "corrupt"
    if failure == "empty":
        expected = "not_success"
    elif failure == "corrupt":
        storage.save(latest.storage_key, b"invalid")
    elif failure == "missing":
        storage.delete(latest.storage_key)
    elif failure == "unavailable":
        real_open = storage.open
        def sometimes_unavailable(key):
            if key == latest.storage_key:
                raise StorageUnavailable("PRIVATE_BODY")
            return real_open(key)
        monkeypatch.setattr(storage, "open", sometimes_unavailable)
        expected = "unavailable"
    else:
        set_row(latest.id, content_type="application/json")
    result = await find(store, binding, envelope)
    assert result.status == expected and result.artifact_id == latest.id
    assert result.envelope is None


@pytest.mark.asyncio
async def test_query_is_owner_assignment_key_scoped_with_sql_limit_one(tmp_path, monkeypatch):
    storage, binding, _, envelope = seeded(tmp_path)
    store = service.RecognitionArtifactStore(storage)
    first = await save(store, binding, envelope)
    latest = await save(store, binding, envelope)
    set_row(first.id, created_at=100)
    set_row(latest.id, created_at=200)
    statements = []

    def before_execute(_conn, _cursor, statement, parameters, _context, _many):
        if "ORDER BY stored_files.created_at DESC" in statement:
            statements.append((statement, parameters))

    def forbidden(*_args, **_kwargs):
        pytest.fail("lookup cannot materialize the owner's entire file list")

    monkeypatch.setattr(file_repository, "list_files", forbidden)
    engine = get_engine()
    event.listen(engine, "before_cursor_execute", before_execute)
    try:
        result = await find(store, binding, envelope)
    finally:
        event.remove(engine, "before_cursor_execute", before_execute)
    assert result.status == "hit" and result.artifact_id == latest.id
    assert len(statements) == 1
    statement, parameters = statements[0]
    assert "stored_files.owner_id =" in statement and "stored_files.assignment_id =" in statement
    assert "LIMIT" in statement and parameters[-2:] == (1, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["assignment", "source", "query"])
async def test_unknown_database_failure_is_unavailable_never_miss(tmp_path, monkeypatch, where):
    storage, binding, _, envelope = seeded(tmp_path)
    def broken(*_args, **_kwargs):
        raise RuntimeError("PRIVATE_BODY database credential")
    owner, name = {
        "assignment": (assignment_repository, "get_assignment"),
        "source": (file_repository, "get_file"),
        "query": (file_repository, "find_latest_assignment_file"),
    }[where]
    monkeypatch.setattr(owner, name, broken)
    result = await find(service.RecognitionArtifactStore(storage), binding, envelope)
    assert result.status == "unavailable" and result.envelope is None
    assert "PRIVATE_BODY" not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("owner_id", "other-owner"), ("input_sha256", "f" * 64), ("business_id", "other-task"),
    ("content_type", "image/png"), ("stored_file_id", None), ("scope", "knowledge_document"),
])
async def test_invalid_context_fails_before_repository_or_storage_access(tmp_path, monkeypatch, field, value):
    storage, binding, _, envelope = seeded(tmp_path)
    values = options(binding, envelope)
    values["source"] = values["source"].model_copy(update={field: value})
    def forbidden(*_args, **_kwargs):
        pytest.fail("forged context must fail before I/O")
    monkeypatch.setattr(assignment_repository, "get_assignment", forbidden)
    monkeypatch.setattr(storage, "open", forbidden)
    with pytest.raises(RecognitionError, match="recognition_artifact_invalid"):
        await service.RecognitionArtifactStore(storage).find_success(**values, payload_kind="native_index")
    with pytest.raises(RecognitionError, match="recognition_artifact_invalid"):
        await service.RecognitionArtifactStore(storage).active_source(**values)


@pytest.mark.asyncio
async def test_explicit_authorized_owner_cannot_be_replaced_by_source_owner(tmp_path):
    storage, binding, _, envelope = seeded(tmp_path)
    values = options(binding, envelope)
    values["authorized_owner_id"] = "another"
    store = service.RecognitionArtifactStore(storage)
    with pytest.raises(RecognitionError, match="recognition_artifact_invalid"):
        await store.active_source(**values)
    with pytest.raises(RecognitionError, match="recognition_artifact_invalid"):
        await store.find_success(**values, payload_kind="native_index")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["owner", "business", "mime", "hash"])
async def test_stored_original_must_match_frozen_context_not_just_exist(tmp_path, monkeypatch, change):
    storage, binding, original, envelope = seeded(tmp_path)
    fields = {"owner": dict(owner_id="other"), "business": dict(assignment_id="other-task"),
              "mime": dict(content_type="image/png"), "hash": dict(sha256="f" * 64)}[change]
    monkeypatch.setattr(file_repository, "get_file", lambda **_kwargs: replace(original, **fields))
    store = service.RecognitionArtifactStore(storage)
    for call in (store.active_source(**options(binding, envelope)), find(store, binding, envelope)):
        with pytest.raises(RecognitionError, match="recognition_source_mismatch"):
            await call


@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable", ["missing_row", "cleanup_pending", "unavailable", "tombstone"])
async def test_source_cleanup_or_task_tombstone_blocks_operational_hit_but_not_history(tmp_path, unavailable):
    storage, binding, original, envelope = seeded(tmp_path)
    store = service.RecognitionArtifactStore(storage)
    row = await save(store, binding, envelope)
    if unavailable == "missing_row":
        with session_scope() as db:
            db.delete(db.get(StoredFileRecord, original.id))
    elif unavailable == "tombstone":
        with session_scope() as db:
            db.get(AssignmentRecord, binding.business_id).deletion_requested_at = time.time()
    else:
        set_row(original.id, availability_status=unavailable)
    assert not await store.active_source(**options(binding, envelope))
    assert (await find(store, binding, envelope)).status == "source_unavailable"
    assert await store.load(row.id, **options(binding, envelope)) == envelope


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["source_cleanup", "task_tombstone", "source_hash"])
async def test_hit_rechecks_source_and_parent_after_artifact_io(tmp_path, monkeypatch, change):
    storage, binding, original, envelope = seeded(tmp_path)
    store = service.RecognitionArtifactStore(storage)
    row = await save(store, binding, envelope)
    real_open = storage.open
    opened = []

    def changed_during_io(key):
        opened.append(key)
        if key == row.storage_key:
            if change == "source_cleanup":
                set_row(original.id, availability_status="unavailable")
            elif change == "task_tombstone":
                with session_scope() as db:
                    db.get(AssignmentRecord, binding.business_id).deletion_requested_at = time.time()
            else:
                set_row(original.id, sha256="f" * 64)
        return real_open(key)

    monkeypatch.setattr(storage, "open", changed_during_io)
    if change == "source_hash":
        with pytest.raises(RecognitionError, match="recognition_source_mismatch"):
            await find(store, binding, envelope)
    else:
        assert (await find(store, binding, envelope)).status == "source_unavailable"
    assert opened == [row.storage_key]


@pytest.mark.asyncio
async def test_source_unavailable_short_circuits_before_lookup_and_storage(tmp_path, monkeypatch):
    storage, binding, original, envelope = seeded(tmp_path)
    set_row(original.id, availability_status="unavailable")
    def forbidden(*_args, **_kwargs):
        pytest.fail("unavailable source must not read cached content")
    monkeypatch.setattr(file_repository, "find_latest_assignment_file", forbidden)
    monkeypatch.setattr(storage, "open", forbidden)
    assert (await find(service.RecognitionArtifactStore(storage), binding, envelope)).status == "source_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("layer_kind", ["visual_read", "unknown", None])
async def test_payload_kind_and_identity_layer_must_agree(tmp_path, layer_kind):
    storage, binding, _, envelope = seeded(tmp_path)
    with pytest.raises(RecognitionError, match="recognition_artifact_invalid"):
        await find(service.RecognitionArtifactStore(storage), binding, envelope, payload_kind=layer_kind)


@pytest.mark.asyncio
async def test_lookup_freezes_caller_context_before_first_await(tmp_path):
    storage, binding, _, envelope = seeded(tmp_path)
    await save(service.RecognitionArtifactStore(storage), binding, envelope)
    entered, release = asyncio.Event(), asyncio.Event()

    class Progress:
        async def set_current_step(self, *_args, **_kwargs):
            entered.set()
            await release.wait()

        async def increment_stage_metrics(self, **_kwargs):
            pass

    store = service.RecognitionArtifactStore(storage, progress=Progress())
    pending = asyncio.create_task(find(store, binding, envelope))
    await entered.wait()
    envelope.identity.tool_version = "changed-while-queued"
    release.set()
    result = await pending
    assert result.status == "hit"
