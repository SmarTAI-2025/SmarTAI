"""Same-content uploads remain distinct authorized source snapshots."""
import pytest

from backend.db import file_repository
from backend.db.models import StoredFileRecord
from backend.db.session import session_scope
from backend.domain.errors import RecognitionError
from backend.recognition.cache_identity import canonical_digest, model_cache_identity
from backend.tests.test_recognition_artifact_codec import candidate
from backend.tests.test_recognition_calls_review import case, lookup, record


def _legacy_name(receipt):
    envelope = receipt.envelope
    return f"ocr-{envelope.identity.layer}-{envelope.identity.key}-{envelope.payload_sha256}.json.gz"


def _make_legacy(receipt):
    with session_scope() as db:
        db.get(StoredFileRecord, receipt.artifact_id).original_name = _legacy_name(receipt)


def _another_source(value):
    original = file_repository.get_file(file_id=value.source.stored_file_id, owner_id=value.source.owner_id)
    with value.storage.open(original.storage_key) as stream:
        content = stream.read()
    repeated = file_repository.save_file(
        storage=value.storage, owner_id=value.source.owner_id, kind=original.kind,
        original_name="uploaded-again.pdf", content=content, content_type=original.content_type,
        assignment_id=value.binding.business_id,
    )
    return value.source.model_copy(update={"stored_file_id": repeated.id})


@pytest.mark.asyncio
@pytest.mark.parametrize("original_available", [True, False])
async def test_same_bytes_new_stored_file_is_a_miss_not_a_corrupt_old_artifact(tmp_path, original_available):
    value = case(tmp_path)
    first = await record(value)
    original = file_repository.get_file(file_id=value.source.stored_file_id, owner_id=value.source.owner_id)
    with value.storage.open(original.storage_key) as stream:
        content = stream.read()
    if not original_available:
        with session_scope() as db:
            db.get(StoredFileRecord, original.id).availability_status = "unavailable"
        value.storage.delete(original.storage_key)
    repeated = file_repository.save_file(
        storage=value.storage, owner_id=value.source.owner_id, kind=original.kind,
        original_name="uploaded-again.pdf", content=content, content_type=original.content_type,
        assignment_id=value.binding.business_id,
    )
    assert repeated.id != original.id and repeated.sha256 == original.sha256
    value.source = value.source.model_copy(update={"stored_file_id": repeated.id})
    identity = model_cache_identity(value.source, value.request, capabilities=value.options["capabilities"],
                                    policy=value.options["policy"], prompt_version=value.options["prompt_version"])
    assert await value.service.store.active_source(source=value.source, identity=identity, binding=value.binding,
                                                   authorized_owner_id=value.source.owner_id)
    found = await lookup(value)
    assert found.status == "miss", f"a distinct valid original must not inherit {found.status} from {first.artifact_id}"
    assert found.receipt is None


@pytest.mark.asyncio
async def test_identical_source_snapshot_still_hits_its_own_evidence(tmp_path):
    value = case(tmp_path)
    fresh = await record(value)
    found = await lookup(value)
    assert found.status == "hit" and found.artifact_id == fresh.artifact_id
    assert found.receipt.envelope.source == value.source


@pytest.mark.asyncio
async def test_new_storage_namespace_uses_complete_source_but_keeps_model_identity(tmp_path):
    value = case(tmp_path)
    first = await record(value)
    first_source = value.source.model_copy(deep=True)
    value.source = _another_source(value)
    second = await record(value)
    assert first.envelope.identity == second.envelope.identity
    assert first.envelope.payload_sha256 == second.envelope.payload_sha256
    for receipt, source in [(first, first_source), (second, value.source)]:
        envelope = receipt.envelope
        row = file_repository.get_file(file_id=receipt.artifact_id, owner_id=source.owner_id)
        expected = (f"ocr-v2-{envelope.identity.layer}-{envelope.identity.key}-"
                    f"{canonical_digest(source.model_dump(mode='json'))}-{envelope.payload_sha256}.json.gz")
        assert row.original_name == expected
    assert (await lookup(value)).artifact_id == second.artifact_id
    value.source = first_source
    assert (await lookup(value)).artifact_id == first.artifact_id


@pytest.mark.asyncio
async def test_legacy_same_source_still_hits_and_explicit_load_is_source_strict(tmp_path):
    value = case(tmp_path)
    saved = await record(value)
    _make_legacy(saved)
    assert (await lookup(value)).artifact_id == saved.artifact_id
    strict = await value.service.store.load(saved.artifact_id, source=value.source, identity=saved.envelope.identity,
                                           binding=value.binding, authorized_owner_id=value.source.owner_id)
    assert strict == saved.envelope
    value.source = _another_source(value)
    with pytest.raises(RecognitionError, match="recognition_artifact_invalid"):
        await value.service.store.load(saved.artifact_id, source=value.source, identity=saved.envelope.identity,
                                       binding=value.binding, authorized_owner_id=value.source.owner_id)
    assert (await lookup(value)).status == "miss"


@pytest.mark.asyncio
async def test_valid_legacy_other_original_is_one_probe_miss_without_searching_old_success(tmp_path, monkeypatch):
    value = case(tmp_path)
    first = await record(value)
    _make_legacy(first)
    first_source = value.source.model_copy(deep=True)
    value.source = _another_source(value)
    newer = await record(value)
    _make_legacy(newer)
    value.source = first_source
    searches, original = [], file_repository.find_latest_assignment_file
    def counted(**kwargs):
        searches.append(kwargs["original_name_prefix"])
        return original(**kwargs)
    monkeypatch.setattr(file_repository, "find_latest_assignment_file", counted)
    found = await lookup(value)
    assert found.status == "miss" and found.receipt is None
    assert len(searches) == 2 and searches[0].startswith("ocr-v2-")
    assert searches[1].startswith("ocr-visual-")


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", [False, True])
async def test_latest_same_source_legacy_failure_does_not_reuse_old_success(tmp_path, pending):
    value = case(tmp_path)
    good = await record(value)
    _make_legacy(good)
    value.payload.candidate = candidate("", status="error", safe_error_code=(
        "provider_submit_uncertain" if pending else "provider_auth_failed"))
    value.payload.submission_may_exist = pending
    failed = await record(value)
    _make_legacy(failed)
    found = await lookup(value)
    assert found.status == "not_success" and found.artifact_id == failed.artifact_id
    assert found.receipt is None


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["corrupt", "failed", "pending"])
async def test_scoped_bad_or_failed_result_never_falls_back_to_legacy(tmp_path, monkeypatch, condition):
    value = case(tmp_path)
    legacy = await record(value)
    _make_legacy(legacy)
    if condition != "corrupt":
        value.payload.candidate = candidate("", status="error", safe_error_code=(
            "provider_submit_uncertain" if condition == "pending" else "provider_auth_failed"))
        value.payload.submission_may_exist = condition == "pending"
    scoped = await record(value)
    if condition == "corrupt":
        row = file_repository.get_file(file_id=scoped.artifact_id, owner_id=value.source.owner_id)
        value.storage.save(row.storage_key, b"corrupted scoped envelope")
    searches, original = [], file_repository.find_latest_assignment_file
    def counted(**kwargs):
        searches.append(kwargs["original_name_prefix"])
        return original(**kwargs)
    monkeypatch.setattr(file_repository, "find_latest_assignment_file", counted)
    found = await lookup(value)
    assert found.status == ("corrupt" if condition == "corrupt" else "not_success")
    assert found.artifact_id == scoped.artifact_id and found.receipt is None
    assert len(searches) == 1 and searches[0].startswith("ocr-v2-")


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["corrupt", "unavailable"])
async def test_bad_other_original_legacy_object_is_not_downgraded_to_miss(tmp_path, monkeypatch, condition):
    value = case(tmp_path)
    saved = await record(value)
    _make_legacy(saved)
    value.source = _another_source(value)
    row = file_repository.get_file(file_id=saved.artifact_id, owner_id=value.source.owner_id)
    if condition == "corrupt":
        value.storage.save(row.storage_key, b"corrupted legacy envelope")
    else:
        def unavailable(*args):
            raise OSError("private storage details")
        monkeypatch.setattr(value.storage, "open", unavailable)
    found = await lookup(value)
    assert found.status == condition and found.receipt is None
