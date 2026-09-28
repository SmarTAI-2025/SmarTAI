"""Knowledge adapter for the shared recognition artifact codec and cache."""
from __future__ import annotations

import hashlib
from types import SimpleNamespace
import uuid

from sqlalchemy import func, select
from starlette.concurrency import run_in_threadpool

from backend.db.knowledge_ingestion_repository import MAX_DERIVED_BYTES, live_document
from backend.db.models import KnowledgeEvidenceRecord
from backend.db.session import session_scope
from backend.domain.errors import NotFound, RecognitionError
from backend.recognition.artifact_codec import decode_artifact, encode_artifact
from backend.services.recognition_artifacts import RecognitionCacheLookup


class KnowledgeEvidenceStore:
    def __init__(self, repository, *, progress=None):
        self.repository, self.progress = repository, progress

    def _context(self, session, source, identity, binding, owner):
        if (binding.link != "knowledge_document" or binding.business_id != self.repository.job.document_id
                or source.scope != "knowledge_document" or source.business_id != binding.business_id
                or source.owner_id != owner or identity.owner_id != owner
                or source.input_sha256 != identity.source_sha256 or source.content_type != identity.source_content_type):
            raise RecognitionError("recognition_source_mismatch")
        doc = live_document(session, binding.business_id, owner)
        if (doc.sha256 != source.input_sha256 or doc.stored_file_id != source.stored_file_id
                or doc.content_type != source.content_type):
            raise RecognitionError("recognition_source_mismatch")
        return doc

    def _active(self, source, identity, binding, owner):
        with session_scope() as session:
            try:
                self._context(session, source, identity, binding, owner)
                return True
            except NotFound:
                return False

    async def active_source(self, *, source, identity, binding, authorized_owner_id):
        return await run_in_threadpool(self._active, source, identity, binding, authorized_owner_id)

    def _load(self, file_id, source, identity, binding, owner):
        with session_scope() as session:
            self._context(session, source, identity, binding, owner)
            row = session.get(KnowledgeEvidenceRecord, file_id)
            if row is None or row.document_id != binding.business_id or row.identity_key != identity.key:
                raise RecognitionError("recognition_artifact_invalid")
            content = row.content
            if len(content) != row.size_bytes or hashlib.sha256(content).hexdigest() != row.sha256:
                raise RecognitionError("recognition_artifact_invalid")
        envelope = decode_artifact(content)
        if envelope.source != source or envelope.identity != identity:
            raise RecognitionError("recognition_artifact_invalid")
        return envelope

    async def load(self, file_id, *, source, identity, binding, authorized_owner_id):
        return await run_in_threadpool(self._load, file_id, source, identity, binding, authorized_owner_id)

    def _save(self, envelope, binding, owner, fence):
        if fence is None:
            raise RecognitionError("recognition_artifact_invalid")
        content = encode_artifact(envelope)
        with session_scope() as session:
            page = self.repository.fence(session, owner, fence.operation_id, fence.attempt, fence.lease_token)
            self._context(session, envelope.source, envelope.identity, binding, owner)
            size = session.scalar(select(func.coalesce(func.sum(KnowledgeEvidenceRecord.size_bytes), 0))
                .where(KnowledgeEvidenceRecord.document_id == binding.business_id))
            if size + len(content) > MAX_DERIVED_BYTES:
                raise RecognitionError("recognition_artifact_limit")
            row = KnowledgeEvidenceRecord(id="ke_" + uuid.uuid4().hex, document_id=binding.business_id,
                page_id=page.id, identity_key=envelope.identity.key, content=content,
                sha256=hashlib.sha256(content).hexdigest(), size_bytes=len(content))
            session.add(row)
            return SimpleNamespace(id=row.id, size_bytes=row.size_bytes)

    async def save(self, envelope, *, binding, authorized_owner_id, fence=None):
        result = await run_in_threadpool(self._save, envelope, binding, authorized_owner_id, fence)
        if self.progress:
            await self.progress.increment_stage_metrics(recognition_artifacts_saved=1, recognition_artifact_bytes=result.size_bytes)
        return result

    async def find_success(self, *, source, identity, binding, authorized_owner_id, payload_kind):
        def find():
            with session_scope() as session:
                try:
                    self._context(session, source, identity, binding, authorized_owner_id)
                except NotFound:
                    return RecognitionCacheLookup("source_unavailable")
                ids = list(session.scalars(select(KnowledgeEvidenceRecord.id).where(
                    KnowledgeEvidenceRecord.document_id == binding.business_id,
                    KnowledgeEvidenceRecord.identity_key == identity.key).limit(64)))
            for file_id in ids:
                try:
                    envelope = self._load(file_id, source, identity, binding, authorized_owner_id)
                except RecognitionError:
                    return RecognitionCacheLookup("corrupt", file_id)
                if envelope.payload_kind == payload_kind and envelope.cacheable_success:
                    return RecognitionCacheLookup("hit", file_id, envelope)
            return RecognitionCacheLookup("miss")
        return await run_in_threadpool(find)
