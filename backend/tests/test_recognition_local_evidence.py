import asyncio
import hashlib
import io
import time

import fitz
from PIL import Image
import pytest

from backend.db.models import AssignmentRecord, StoredFileRecord
from backend.db.session import session_scope
from backend.domain.errors import RecognitionError
from backend.recognition.cache_identity import render_cache_identity
from backend.recognition.local_cache import RecognitionByteCache
from backend.services import recognition_local_evidence as service
from backend.services.recognition_artifacts import RecognitionArtifactStore
from backend.tests.test_recognition_artifact_store import seeded
from backend.tools.pdf_evidence import ImagePrepareRequest, PdfContactSheetRequest, PdfIndexRequest, PdfPagesRequest, PdfRenderRequest, decode_pdf_payload


def setup(tmp_path, image=False):
    storage, binding, original, seed = seeded(tmp_path)
    if image:
        stream = io.BytesIO()
        Image.new("RGB", (120, 90), "white").save(stream, "PNG")
        data, mime = stream.getvalue(), "image/png"
    else:
        with fitz.open() as document:
            page = document.new_page(width=200, height=300)
            page.insert_text((20, 30), "1. Solve x = -3.")
            data, mime = document.tobytes(), "application/pdf"
    digest = hashlib.sha256(data).hexdigest()
    storage.save(original.storage_key, data)
    with session_scope() as db:
        row = db.get(StoredFileRecord, original.id)
        row.sha256, row.content_type, row.size_bytes = digest, mime, len(data)
    source = seed.source.model_copy(update={"input_sha256": digest, "content_type": mime})
    store, cache = RecognitionArtifactStore(storage), RecognitionByteCache()
    reader = service.RecognitionLocalEvidenceReader(store=store, cache=cache, source=source, binding=binding,
                                                     authorized_owner_id=source.owner_id)
    return reader, data, source, binding, original


@pytest.mark.parametrize("field,value", [("owner_id", "other"), ("business_id", "other"),
                                         ("stored_file_id", "other"), ("input_sha256", "f" * 64),
                                         ("scope", "submission_source"), ("content_type", "image/png")])
def test_injected_reader_context_requires_exact_source_binding(tmp_path, field, value):
    reader, _, source, _, _ = setup(tmp_path)
    reader.assert_context(source, authorized_owner_id=source.owner_id)
    with pytest.raises(RecognitionError, match="recognition_source_mismatch"):
        reader.assert_context(source.model_copy(update={field: value}), authorized_owner_id=source.owner_id)
    with pytest.raises(RecognitionError, match="recognition_source_mismatch"):
        reader.assert_context(source, authorized_owner_id="other")


@pytest.mark.asyncio
@pytest.mark.parametrize("command", [PdfIndexRequest(), PdfPagesRequest(pages=[1])])
async def test_native_second_read_in_fresh_reader_reuses_durable_artifact(tmp_path, monkeypatch, command):
    reader, data, source, binding, _ = setup(tmp_path)
    first = await reader.read(data, command)
    assert first.reused == "none" and first.artifact_id
    fresh = service.RecognitionLocalEvidenceReader(store=RecognitionArtifactStore(reader.store.storage), cache=RecognitionByteCache(),
                                                    source=source, binding=binding, authorized_owner_id=source.owner_id)
    async def forbidden(*args, **kwargs):
        pytest.fail("cache hit must not start a PDF worker")
    monkeypatch.setattr(service, "read_pdf_evidence", forbidden)
    second = await fresh.read(data, command)
    assert second.reused == "durable" and second.artifact_id == first.artifact_id
    assert second.evidence == first.evidence
    second.evidence.pages.clear()
    assert (await fresh.read(data, command)).evidence == first.evidence


@pytest.mark.asyncio
@pytest.mark.parametrize("command", [PdfRenderRequest(page_number=1), PdfContactSheetRequest(pages=[1]), ImagePrepareRequest(content_type="image/png")])
async def test_render_reuses_ram_only_and_returns_fresh_validated_copy(tmp_path, monkeypatch, command):
    reader, data, source, binding, _ = setup(tmp_path, isinstance(command, ImagePrepareRequest))
    before = sorted(reader.store.storage.list_keys(""))
    first = await reader.read(data, command)
    assert first.reused == "none" and first.artifact_id is None
    assert decode_pdf_payload(first.evidence).startswith(b"\x89PNG")
    assert sorted(reader.store.storage.list_keys("")) == before
    assert reader.cache.entry_count == 1
    async def forbidden(*args, **kwargs):
        pytest.fail("render hit must not start a worker")
    monkeypatch.setattr(service, "read_pdf_evidence", forbidden)
    monkeypatch.setattr(service, "read_image_evidence", forbidden)
    second = await reader.read(data, command)
    assert second.reused == "memory" and second.evidence == first.evidence
    second.evidence.payload_b64 = "modified"
    assert (await reader.read(data, command)).evidence == first.evidence


@pytest.mark.asyncio
async def test_exported_document_payload_is_never_durable_or_ram_cached(tmp_path, monkeypatch):
    reader, data, _, _, _ = setup(tmp_path)
    calls, original = [], service.read_pdf_evidence
    async def counted(*args, **kwargs):
        calls.append(1)
        return await original(*args, **kwargs)
    monkeypatch.setattr(service, "read_pdf_evidence", counted)
    before = sorted(reader.store.storage.list_keys(""))
    for _ in range(2):
        result = await reader.read(data, PdfPagesRequest(operation="export_pages", pages=[1]))
        assert result.reused == "none" and result.artifact_id is None
        assert decode_pdf_payload(result.evidence).startswith(b"%PDF-")
    assert len(calls) == 2 and reader.cache.entry_count == 0
    assert sorted(reader.store.storage.list_keys("")) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("deletion", ["original", "task"])
async def test_cached_pixels_do_not_bypass_current_source_or_task_availability(tmp_path, deletion):
    reader, data, _, binding, original = setup(tmp_path)
    command = PdfRenderRequest(page_number=1)
    await reader.read(data, command)
    with session_scope() as db:
        if deletion == "original":
            db.get(StoredFileRecord, original.id).availability_status = "unavailable"
        else:
            db.get(AssignmentRecord, binding.business_id).deletion_requested_at = time.time()
    with pytest.raises(RecognitionError, match="recognition_source_unavailable"):
        await reader.read(data, command)
    assert reader.cache.entry_count == 0


@pytest.mark.asyncio
async def test_changed_supplied_bytes_fail_before_hit_or_worker(tmp_path, monkeypatch):
    reader, data, _, _, _ = setup(tmp_path)
    await reader.read(data, PdfIndexRequest())
    async def forbidden(*args, **kwargs):
        pytest.fail("mismatched source cannot dispatch")
    monkeypatch.setattr(service, "read_pdf_evidence", forbidden)
    with pytest.raises(RecognitionError, match="recognition_source_mismatch"):
        await reader.read(data + b"changed", PdfIndexRequest())


@pytest.mark.asyncio
async def test_corrupt_memory_entry_is_evicted_without_same_call_fallback(tmp_path, monkeypatch):
    reader, data, source, _, _ = setup(tmp_path)
    command = PdfRenderRequest(page_number=1)
    reader.cache.put(identity=render_cache_identity(source, command), source=source, authorized_owner_id=source.owner_id, value=b"not JSON")
    async def forbidden(*args, **kwargs):
        pytest.fail("corrupt hit must be explicit, not silently reprocessed")
    monkeypatch.setattr(service, "read_pdf_evidence", forbidden)
    with pytest.raises(RecognitionError, match="recognition_artifact_invalid"):
        await reader.read(data, command)
    assert reader.cache.entry_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt", [False, True])
async def test_bad_or_unavailable_durable_artifact_never_becomes_local_retry(tmp_path, monkeypatch, corrupt):
    reader, data, _, _, _ = setup(tmp_path)
    first = await reader.read(data, PdfIndexRequest())
    from backend.db import file_repository
    row = file_repository.get_file(file_id=first.artifact_id, owner_id=reader.owner)
    if corrupt:
        reader.store.storage.save(row.storage_key, b"corrupt")
    else:
        def unavailable(*args):
            raise OSError("private endpoint")
        monkeypatch.setattr(reader.store.storage, "open", unavailable)
    async def forbidden(*args, **kwargs):
        pytest.fail("bad durable result must not silently rerun")
    monkeypatch.setattr(service, "read_pdf_evidence", forbidden)
    with pytest.raises(RecognitionError, match="recognition_artifact_invalid" if corrupt else "recognition_artifact_unavailable"):
        await reader.read(data, PdfIndexRequest())


@pytest.mark.asyncio
async def test_task_deleted_during_local_processing_has_no_late_cached_result(tmp_path, monkeypatch):
    reader, data, _, binding, _ = setup(tmp_path)
    original = service.read_pdf_evidence
    async def deleting(*args, **kwargs):
        result = await original(*args, **kwargs)
        with session_scope() as db:
            db.get(AssignmentRecord, binding.business_id).deletion_requested_at = time.time()
        return result
    monkeypatch.setattr(service, "read_pdf_evidence", deleting)
    with pytest.raises(RecognitionError, match="recognition_source_unavailable"):
        await reader.read(data, PdfRenderRequest(page_number=1))
    assert reader.cache.entry_count == 0


@pytest.mark.asyncio
async def test_task_deleted_while_decoding_native_hit_is_not_returned(tmp_path, monkeypatch):
    reader, data, _, binding, _ = setup(tmp_path)
    command = PdfIndexRequest()
    await reader.read(data, command)
    decode = service._decode
    def deleting(*args):
        result = decode(*args)
        with session_scope() as db:
            db.get(AssignmentRecord, binding.business_id).deletion_requested_at = time.time()
        return result
    monkeypatch.setattr(service, "_decode", deleting)
    with pytest.raises(RecognitionError, match="recognition_source_unavailable"):
        await reader.read(data, command)


@pytest.mark.asyncio
async def test_task_deleted_while_serializing_pixels_is_not_cached(tmp_path, monkeypatch):
    from backend.tools.pdf_evidence import PdfRenderResult
    reader, data, _, binding, _ = setup(tmp_path)
    serialize = PdfRenderResult.model_dump_json
    def deleting(result, *args, **kwargs):
        content = serialize(result, *args, **kwargs)
        with session_scope() as db:
            db.get(AssignmentRecord, binding.business_id).deletion_requested_at = time.time()
        return content
    monkeypatch.setattr(PdfRenderResult, "model_dump_json", deleting)
    with pytest.raises(RecognitionError, match="recognition_source_unavailable"):
        await reader.read(data, PdfRenderRequest(page_number=1))
    assert reader.cache.entry_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [0, -1, 31, float("nan"), float("inf"), True])
async def test_invalid_deadline_rejected_before_lookup(tmp_path, timeout):
    reader, data, _, _, _ = setup(tmp_path)
    with pytest.raises(RecognitionError, match="recognition_request_invalid"):
        await reader.read(data, PdfIndexRequest(), timeout_seconds=timeout)


@pytest.mark.asyncio
async def test_deadline_covers_cache_authorization_and_cancellation_propagates(tmp_path, monkeypatch):
    reader, data, _, _, _ = setup(tmp_path)
    started = asyncio.Event()
    async def waiting(**kwargs):
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(reader.store, "active_source", waiting)
    with pytest.raises(RecognitionError, match="recognition_timeout"):
        await reader.read(data, PdfIndexRequest(), timeout_seconds=0.01)
    task = asyncio.create_task(reader.read(data, PdfIndexRequest()))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert reader.cache.entry_count == 0
