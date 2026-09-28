"""Independent checks of unsupported durable-lifecycle boundaries; no model I/O."""
import time

import pytest
from sqlalchemy import select

from backend.db import file_repository
from backend.db.knowledge_repository import get_document, request_document_deletion
from backend.db.models import AssignmentRecord, StoredFileRecord
from backend.db.session import session_scope
from backend.domain.errors import DomainError, RecognitionError
from backend.recognition.artifact_codec import build_artifact
from backend.recognition.cache_identity import native_cache_identity
from backend.recognition.models import RecognitionSourceRefV1
from backend.services import recognition_artifacts as service
from backend.services.knowledge_storage import KnowledgeStorageWorker, persist_knowledge_upload
from backend.tests.test_recognition_artifact_store import seeded
from backend.tools.pdf_evidence import PdfIndexRequest


def knowledge_upload(tmp_path):
    storage, _, _, seed = seeded(tmp_path)
    owner = seed.source.owner_id
    upload = persist_knowledge_upload(
        storage=storage, owner_id=owner, original_name="book.pdf", content=b"Local lifecycle PDF fixture",
        content_type="application/pdf",
    )
    document = get_document(upload.document_id, owner)
    original = file_repository.get_file(file_id=document.stored_file_id, owner_id=owner)
    source = RecognitionSourceRefV1(
        owner_id=owner, scope="knowledge_document", business_id=document.id,
        stored_file_id=original.id, content_type=original.content_type, input_sha256=original.sha256,
    )
    binding = service.RecognitionArtifactBindingV1(link="knowledge_document", business_id=document.id)
    envelope = build_artifact(identity=native_cache_identity(source, PdfIndexRequest()), source=source,
                              payload_kind="native_index", payload=seed.payload)
    return storage, binding, original, envelope


def assert_no_artifact_rows(owner_id):
    with session_scope() as db:
        assert not list(db.scalars(select(StoredFileRecord.id).where(
            StoredFileRecord.owner_id == owner_id, StoredFileRecord.kind == service.ARTIFACT_KIND,
        )))


async def assert_no_save(storage, binding, envelope):
    keys_before = sorted(storage.list_keys(""))
    with pytest.raises(RecognitionError) as caught:
        await service.RecognitionArtifactStore(storage).save(
            envelope, binding=binding, authorized_owner_id=envelope.source.owner_id,
        )
    assert caught.value.code == "recognition_artifact_scope_unsupported"
    assert sorted(storage.list_keys("")) == keys_before
    assert_no_artifact_rows(envelope.source.owner_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("deleting", [False, True])
async def test_knowledge_artifacts_fail_closed_until_their_durable_cleanup_is_supported(tmp_path, deleting):
    storage, binding, original, envelope = knowledge_upload(tmp_path)
    owner = envelope.source.owner_id
    if deleting:
        request_document_deletion(binding.business_id, owner)
        # The source file remains "available" while the independent knowledge
        # ledger has already fenced deletion. That flag alone cannot authorize a write.
        assert file_repository.get_file(file_id=original.id, owner_id=owner).availability_status == "available"
    await assert_no_save(storage, binding, envelope)
    if deleting:
        assert await KnowledgeStorageWorker(storage=storage).run_once(max_claims=4) == 1
        assert get_document(binding.business_id, owner) is None
        assert not storage.exists(original.storage_key)
        assert_no_artifact_rows(owner)


@pytest.mark.asyncio
@pytest.mark.parametrize("deleting", [False, True])
async def test_revision_artifacts_cannot_bypass_parent_task_write_intents(tmp_path, deleting):
    storage, binding, original, envelope = seeded(tmp_path, "submission_revision")
    owner = envelope.source.owner_id
    if deleting:
        with session_scope() as db:
            assignment = db.scalar(select(AssignmentRecord).where(AssignmentRecord.teacher_id == owner))
            assignment.deletion_requested_at = time.time()
        assert file_repository.get_file(file_id=original.id, owner_id=owner).availability_status == "available"
    await assert_no_save(storage, binding, envelope)


@pytest.mark.asyncio
async def test_assignment_write_intent_accepts_live_task_and_rejects_late_tombstoned_save(tmp_path):
    storage, binding, _, envelope = seeded(tmp_path)
    store = service.RecognitionArtifactStore(storage)
    owner = envelope.source.owner_id
    row = await store.save(envelope, binding=binding, authorized_owner_id=owner)
    assert storage.exists(row.storage_key)
    assert await store.load(row.id, source=envelope.source, identity=envelope.identity,
                            binding=binding, authorized_owner_id=owner) == envelope
    keys_before = sorted(storage.list_keys(""))
    with session_scope() as db:
        db.get(AssignmentRecord, binding.business_id).deletion_requested_at = time.time()
    with pytest.raises(DomainError) as caught:
        await store.save(envelope, binding=binding, authorized_owner_id=owner)
    assert caught.value.code != "recognition_artifact_scope_unsupported"
    assert sorted(storage.list_keys("")) == keys_before
    with session_scope() as db:
        assert list(db.scalars(select(StoredFileRecord.id).where(
            StoredFileRecord.owner_id == owner, StoredFileRecord.kind == service.ARTIFACT_KIND,
        ))) == [row.id]


@pytest.mark.asyncio
@pytest.mark.parametrize("link", ["knowledge_document", "submission_revision"])
async def test_unsupported_scope_history_load_is_rejected_before_storage_access(tmp_path, monkeypatch, link):
    storage, binding, _, envelope = knowledge_upload(tmp_path) if link == "knowledge_document" else seeded(tmp_path, link)

    def forbidden(*_args, **_kwargs):
        pytest.fail("unsupported scope cannot open storage")

    monkeypatch.setattr(storage, "open", forbidden)
    with pytest.raises(RecognitionError) as caught:
        await service.RecognitionArtifactStore(storage).load(
            "historical-artifact", source=envelope.source, identity=envelope.identity,
            binding=binding, authorized_owner_id=envelope.source.owner_id,
        )
    assert caught.value.code == "recognition_artifact_scope_unsupported"
