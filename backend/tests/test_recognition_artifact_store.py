from dataclasses import replace
import hashlib
import threading
import uuid

import pytest

from backend.db import file_repository
from backend.db.models import AssignmentRecord, CourseRecord, KnowledgeDocumentRecord, StoredFileRecord, SubmissionRecord, SubmissionRevisionRecord, UserRecord
from backend.db.session import session_scope
from backend.domain.errors import DomainError, RecognitionError
from backend.domain.source_storage import RAW_SOURCE_KINDS, TASK_SOURCE_CLEANUP_KINDS
from backend.progress.tracker import ProgressReporter
from backend.recognition.artifact_codec import build_artifact
from backend.recognition.cache_identity import native_cache_identity
from backend.recognition.cache_identity import model_cache_identity
from backend.recognition.engine import EngineReadInputV1
from backend.recognition.executor import ReadUnitV1
from backend.recognition.models import NormalizedRegionV1, RecognitionCandidateV1, RecognitionPolicyV1, RecognitionSourceRefV1
from backend.recognition.planner import EngineCapabilitiesV1, PageObservationV1
from backend.services import recognition_artifacts as service
from backend.storage.base import StorageUnavailable
from backend.storage.local import LocalStorage
from backend.tools.pdf_evidence import PdfIndexPage, PdfIndexRequest, PdfIndexResult


def seeded(tmp_path, link="assignment"):
    suffix = uuid.uuid4().hex
    owner, course, task, business = [f"{name}_{suffix}" for name in ("owner", "course", "task", "business")]
    data = b"private original PDF bytes"
    digest = hashlib.sha256(data).hexdigest()
    with session_scope() as db:
        db.add(UserRecord(id=owner, username=owner, password_hash="hash", role="teacher", is_active=True))
        db.flush()
        db.add(CourseRecord(id=course, name="Evidence", code=f"ART-{suffix}", teacher_id=owner))
        db.flush()
        db.add(AssignmentRecord(id=task, course_id=course, teacher_id=owner, name="Evidence", status="draft", version=1))
        db.flush()
        if link == "knowledge_document":
            db.add(KnowledgeDocumentRecord(id=business, owner_id=owner, title="Book", original_name="book.pdf",
                                           content_type="application/pdf", size_bytes=len(data), sha256=digest))
        elif link == "submission_revision":
            submission = "submission_" + suffix
            db.add(SubmissionRecord(id=submission, assignment_id=task, student_id=owner))
            db.flush()
            db.add(SubmissionRevisionRecord(id=business, submission_id=submission, revision_number=1, source="teacher_import"))
        else:
            business = task
    storage = LocalStorage(tmp_path)
    binding = service.RecognitionArtifactBindingV1(link=link, business_id=business)
    original = file_repository.save_file(storage=storage, owner_id=owner, kind="uploaded_test_fixture", original_name="source.pdf",
                                        content=data, content_type="application/pdf", **binding.repository_links)
    scope = {"assignment": "assignment_source", "submission_revision": "submission_source", "knowledge_document": "knowledge_document"}[link]
    source = RecognitionSourceRefV1(owner_id=owner, scope=scope, business_id=business, stored_file_id=original.id,
                                    content_type="application/pdf", input_sha256=digest)
    index = PdfIndexResult(contract="smartai.pdf.evidence", schema_version=1, status="ok", total_pages=1, operation="index",
                           window_start=1, window_end=1, complete_window=True,
                           pages=[PdfIndexPage(page_number=1, width_points=200, height_points=300, rotation=0,
                                               observation=PageObservationV1(page_number=1))])
    envelope = build_artifact(identity=native_cache_identity(source, PdfIndexRequest()), source=source, payload_kind="native_index", payload=index)
    return storage, binding, original, envelope


async def save(store, binding, envelope, **kwargs):
    return await store.save(envelope, binding=binding, authorized_owner_id=envelope.source.owner_id, **kwargs)


async def load(store, row, binding, envelope, **changes):
    options = dict(source=envelope.source, identity=envelope.identity, binding=binding, authorized_owner_id=envelope.source.owner_id)
    options.update(changes)
    return await store.load(row.id, **options)


@pytest.mark.asyncio
@pytest.mark.parametrize("link", ["assignment", "submission_revision"])
async def test_real_database_and_storage_roundtrip_in_fresh_store(tmp_path, link):
    storage, binding, original, envelope = seeded(tmp_path, link)
    progress = ProgressReporter("artifact")
    row = await save(service.RecognitionArtifactStore(storage, progress=progress), binding, envelope)
    result = await load(service.RecognitionArtifactStore(LocalStorage(tmp_path), progress=progress), row, binding, envelope)
    assert result == envelope and result.cacheable_success
    assert row.owner_id == original.owner_id and row.kind == service.ARTIFACT_KIND
    with storage.open(row.storage_key) as stream:
        assert row.sha256 == hashlib.sha256(stream.read()).hexdigest()
    assert row.size_bytes < 2048
    assert service.ARTIFACT_KIND not in RAW_SOURCE_KINDS | TASK_SOURCE_CLEANUP_KINDS
    assert [event.message for event in progress._events if event.message] == ["Saving recognition evidence", "Checking stored recognition evidence"]


@pytest.mark.asyncio
async def test_historical_evidence_survives_original_cleanup_but_new_saves_require_available_source(tmp_path):
    storage, binding, original, envelope = seeded(tmp_path)
    store = service.RecognitionArtifactStore(storage)
    row = await save(store, binding, envelope)
    storage.delete(original.storage_key)
    with session_scope() as db:
        db.get(StoredFileRecord, original.id).availability_status = "unavailable"
    assert await load(store, row, binding, envelope) == envelope
    with pytest.raises(RecognitionError, match="recognition_artifact_invalid"):
        await save(store, binding, envelope)


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("owner_id", "another"), ("input_sha256", "f" * 64),
                                          ("stored_file_id", "missing"), ("stored_file_id", None),
                                          ("business_id", "different-task"), ("content_type", "image/png"),
                                          ("scope", "knowledge_document")])
async def test_save_rejects_forged_source_before_storage_write(tmp_path, monkeypatch, field, value):
    storage, binding, _, envelope = seeded(tmp_path)
    envelope = envelope.model_copy(deep=True)
    setattr(envelope.source, field, value)
    def forbidden(*args, **kwargs):
        pytest.fail("invalid context must not write")
    monkeypatch.setattr(storage, "save", forbidden)
    with pytest.raises(RecognitionError, match="recognition_artifact_invalid"):
        await save(service.RecognitionArtifactStore(storage), binding, envelope)


@pytest.mark.asyncio
async def test_another_owner_cannot_save_or_load_even_with_matching_file_id_and_hash(tmp_path, monkeypatch):
    storage, binding, _, envelope = seeded(tmp_path)
    store = service.RecognitionArtifactStore(storage)
    row = await save(store, binding, envelope)
    def forbidden(*args, **kwargs):
        pytest.fail("unauthorized access must not open storage")
    monkeypatch.setattr(storage, "open", forbidden)
    with pytest.raises(RecognitionError, match="recognition_artifact_invalid"):
        await store.save(envelope, binding=binding, authorized_owner_id="another")
    with pytest.raises(RecognitionError, match="recognition_artifact_invalid"):
        await load(store, row, binding, envelope, authorized_owner_id="another")


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [dict(kind="other"), dict(owner_id="other"), dict(content_type="application/json"),
                                     dict(storage_backend="wrong"), dict(assignment_id="wrong"), dict(knowledge_document_id="extra-link"),
                                     dict(size_bytes=0), dict(size_bytes=service.MAX_COMPRESSED_BYTES + 1),
                                     dict(availability_status="unavailable")])
async def test_invalid_row_metadata_rejected_before_open(tmp_path, monkeypatch, changes):
    storage, binding, _, envelope = seeded(tmp_path)
    store = service.RecognitionArtifactStore(storage)
    row = await save(store, binding, envelope)
    monkeypatch.setattr(file_repository, "get_file", lambda **kwargs: replace(row, **changes))
    def forbidden(*args, **kwargs):
        pytest.fail("invalid metadata must not open storage")
    monkeypatch.setattr(storage, "open", forbidden)
    with pytest.raises(RecognitionError, match="recognition_artifact_invalid"):
        await load(store, row, binding, envelope)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["missing", "truncated", "same_size", "row_hash", "row_size", "name", "identity", "source_name"])
async def test_bytes_metadata_and_exact_expected_context_are_verified(tmp_path, monkeypatch, mutation):
    storage, binding, _, envelope = seeded(tmp_path)
    store = service.RecognitionArtifactStore(storage)
    row = await save(store, binding, envelope)
    changes = {}
    if mutation == "missing":
        storage.delete(row.storage_key)
    elif mutation in {"truncated", "same_size"}:
        with storage.open(row.storage_key) as stream:
            content = stream.read()
        storage.save(row.storage_key, content[:-1] if mutation == "truncated" else b"x" * len(content))
    elif mutation in {"row_hash", "row_size", "name"}:
        fields = {"row_hash": dict(sha256="f" * 64), "row_size": dict(size_bytes=row.size_bytes + 1), "name": dict(original_name="wrong.json.gz")}
        monkeypatch.setattr(file_repository, "get_file", lambda **kwargs: replace(row, **fields[mutation]))
    elif mutation == "identity":
        changes["identity"] = envelope.identity.model_copy(update={"tool_version": "next"})
    else:
        changes["source"] = envelope.source.model_copy(update={"original_name": "different"})
    with pytest.raises(RecognitionError, match="recognition_artifact_invalid"):
        await load(store, row, binding, envelope, **changes)


@pytest.mark.asyncio
async def test_read_is_bounded_and_io_is_off_event_loop(tmp_path, monkeypatch):
    storage, binding, _, envelope = seeded(tmp_path)
    store = service.RecognitionArtifactStore(storage)
    row = await save(store, binding, envelope)
    main_thread, reads = threading.get_ident(), []
    original_open = storage.open
    class Stream:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, size):
            reads.append(size)
            assert threading.get_ident() != main_thread
            with original_open(row.storage_key) as stream:
                return stream.read(size)
    monkeypatch.setattr(storage, "open", lambda key: Stream())
    assert await load(store, row, binding, envelope) == envelope
    assert reads == [service.MAX_COMPRESSED_BYTES + 1]


@pytest.mark.asyncio
async def test_transient_storage_failure_is_not_corrupt_cache_or_permission_to_retry_provider(tmp_path, monkeypatch):
    storage, binding, _, envelope = seeded(tmp_path)
    store = service.RecognitionArtifactStore(storage)
    row = await save(store, binding, envelope)
    def unavailable(*args, **kwargs):
        raise StorageUnavailable("private endpoint token")
    monkeypatch.setattr(storage, "open", unavailable)
    with pytest.raises(RecognitionError) as exc:
        await load(store, row, binding, envelope)
    assert exc.value.code == "recognition_artifact_unavailable"
    assert "private" not in str(exc.value) and exc.value.__cause__ is None


@pytest.mark.asyncio
async def test_fence_passes_exactly_to_existing_repository_and_is_assignment_only(tmp_path, monkeypatch):
    storage, binding, _, envelope = seeded(tmp_path)
    captured = {}
    def save_file(**kwargs):
        captured.update(kwargs)
        return "saved"
    monkeypatch.setattr(file_repository, "save_file", save_file)
    fence = service.RecognitionArtifactFenceV1(operation_id="op", attempt=2, lease_token="lease")
    assert await save(service.RecognitionArtifactStore(storage), binding, envelope, fence=fence) == "saved"
    assert (captured["fence_operation_id"], captured["fence_operation_attempt"], captured["fence_lease_token"]) == ("op", 2, "lease")
    assert "lease" not in repr(fence)


@pytest.mark.asyncio
@pytest.mark.parametrize("link", ["submission_revision", "knowledge_document"])
async def test_assignment_fence_cannot_be_attached_to_another_business_link(tmp_path, monkeypatch, link):
    storage, binding, _, envelope = seeded(tmp_path, link)
    def forbidden(*args, **kwargs):
        pytest.fail("invalid fence must not write")
    monkeypatch.setattr(storage, "save", forbidden)
    with pytest.raises(DomainError):
        await save(service.RecognitionArtifactStore(storage), binding, envelope,
                   fence=service.RecognitionArtifactFenceV1(operation_id="op", attempt=1))


@pytest.mark.asyncio
@pytest.mark.parametrize("status,pending", [("error", True), ("error", False), ("empty", False)])
async def test_failed_and_pending_calls_are_persisted_without_becoming_success(tmp_path, status, pending):
    storage, binding, _, native = seeded(tmp_path)
    request = EngineReadInputV1(purpose="problems", input_mode="page_image", page_number=1, content_type="image/png", payload=b"pixels")
    identity = model_cache_identity(native.source, request,
                                    capabilities=EngineCapabilitiesV1(route_id="selected", fingerprint="same-model", visual_inputs=["page_image"]),
                                    policy=RecognitionPolicyV1(), prompt_version="reader-v1")
    candidate = RecognitionCandidateV1(kind="vision", status=status, provider_route_id="selected", input_tokens=31, output_tokens=2,
                                        safe_error_code="provider_submit_uncertain" if pending else "provider_request_failed" if status == "error" else None)
    result = ReadUnitV1(unit_id="u0001", page_numbers=[1], region=NormalizedRegionV1(), input_mode="page_image",
                        payload_sha256=hashlib.sha256(b"pixels").hexdigest(), payload_bytes=6, candidate=candidate,
                        requested_output_tokens=4096, submission_may_exist=pending, output_mapping="single_region")
    envelope = build_artifact(identity=identity, source=native.source, payload_kind="visual_read", payload=result)
    store = service.RecognitionArtifactStore(storage)
    row = await save(store, binding, envelope)
    restored = await load(service.RecognitionArtifactStore(LocalStorage(tmp_path)), row, binding, envelope)
    assert restored == envelope and not restored.cacheable_success
    assert restored.payload.candidate.input_tokens == 31 and restored.payload.submission_may_exist == pending


@pytest.mark.asyncio
async def test_database_failure_and_missing_file_are_distinct_safe_errors(tmp_path, monkeypatch):
    storage, binding, _, envelope = seeded(tmp_path)
    store = service.RecognitionArtifactStore(storage)
    row = await save(store, binding, envelope)
    def database_failed(**kwargs):
        raise RuntimeError("private database credential")
    monkeypatch.setattr(file_repository, "get_file", database_failed)
    with pytest.raises(RecognitionError, match="recognition_artifact_unavailable") as exc:
        await load(store, row, binding, envelope)
    assert "credential" not in str(exc.value)
    monkeypatch.setattr(file_repository, "get_file", lambda **kwargs: None)
    with pytest.raises(RecognitionError, match="recognition_artifact_invalid"):
        await load(store, row, binding, envelope)
