"""Independent knowledge jobs, page manifests and recognition write fences.

Owner lock order matches the original-object cleanup ledger. Derived evidence
and chunks are transactional rows, so deletion cannot race a late object PUT.
"""
from __future__ import annotations

import math
import time
import uuid
from types import SimpleNamespace

from sqlalchemy import func, select

from backend.db.knowledge_storage_repository import lock_knowledge_owner_in_session
from backend.db.models import (KnowledgeChunkRecord, KnowledgeDocumentRecord, KnowledgeEvidenceRecord,
                               KnowledgeIngestionRecord, KnowledgePageRecord, KnowledgeStorageRecord,
                               StoredFileRecord)
from backend.db.session import session_scope
from backend.domain.errors import LeaseLost, NotFound, RecognitionError, InvalidTransition
from backend.rag.chunker import chunk_spans

TERMINAL_PAGES = {"searchable", "searchable_with_warning", "blank_confirmed", "failed"}
GOOD_PAGES = {"searchable", "searchable_with_warning", "blank_confirmed"}
BATCH_PAGES = 24
MAX_DERIVED_BYTES = 128 * 1024 * 1024
MAX_TEXT_CHARS = 32 * 1024 * 1024


def _id(prefix):
    return prefix + uuid.uuid4().hex


def live_document(session, document_id, owner_id, *, lock=False):
    if lock:
        lock_knowledge_owner_in_session(session, owner_id)
    doc = session.scalar(select(KnowledgeDocumentRecord).join(KnowledgeStorageRecord,
        KnowledgeStorageRecord.document_id == KnowledgeDocumentRecord.id).join(StoredFileRecord,
        StoredFileRecord.id == KnowledgeDocumentRecord.stored_file_id).where(
        KnowledgeDocumentRecord.id == document_id, KnowledgeDocumentRecord.owner_id == owner_id,
        KnowledgeStorageRecord.owner_id == owner_id, KnowledgeStorageRecord.state == "available",
        StoredFileRecord.availability_status == "available", StoredFileRecord.owner_id == owner_id,
        StoredFileRecord.knowledge_document_id == document_id, StoredFileRecord.sha256 == KnowledgeDocumentRecord.sha256))
    if doc is None:
        raise NotFound("knowledge_document")
    return doc


def _job(session, job_id, owner_id, token=None):
    row = session.get(KnowledgeIngestionRecord, job_id)
    if row is None or row.owner_id != owner_id:
        raise NotFound("knowledge_ingestion")
    live_document(session, row.document_id, owner_id, lock=True)
    session.refresh(row)
    if token is not None and (row.lease_token != token or (row.lease_expires_at or 0) <= time.time()
                              or row.status != "processing"):
        raise LeaseLost()
    return row


def queue_document(document_id, owner_id, configuration, *, new_version=False, resubmit_uncertain=False):
    with session_scope() as session:
        doc = live_document(session, document_id, owner_id, lock=True)
        existing = session.scalar(select(KnowledgeIngestionRecord).where(
            KnowledgeIngestionRecord.document_id == document_id).order_by(KnowledgeIngestionRecord.created_at.desc()))
        if existing and (not new_version or existing.status in {"queued", "processing"}):
            return existing.id
        return create_ingestion_in_session(session, doc, dict(configuration,
            resubmit_uncertain=bool(new_version and resubmit_uncertain)))


def create_ingestion_in_session(session, doc, configuration):
    """Called under the owner gate during original publication or explicit retry."""
    row = KnowledgeIngestionRecord(id=_id("ki_"), document_id=doc.id, owner_id=doc.owner_id,
        configuration=dict(configuration, source_sha256=doc.sha256), status="queued")
    session.add(row)
    session.flush()
    doc.ingestion_summary = dict(id=row.id, status="queued", total_pages=None, processed_pages=0,
                                 searchable_pages=0, failed_pages=0, coverage_complete=False)
    if not doc.active_version and doc.status != "ready":
        doc.status = "processing"
    return row.id


def claim_next(*, job_id=None):
    now = time.time()
    with session_scope() as session:
        query = (select(KnowledgeIngestionRecord.id).where(
            KnowledgeIngestionRecord.status.in_(["queued", "processing"]),
            (KnowledgeIngestionRecord.lease_expires_at.is_(None)) | (KnowledgeIngestionRecord.lease_expires_at <= now))
            .order_by(KnowledgeIngestionRecord.updated_at).limit(20))
        if job_id is not None:
            query = select(KnowledgeIngestionRecord.id).where(KnowledgeIngestionRecord.id == job_id)
        candidates = list(session.scalars(query))
    for job_id in candidates:
        with session_scope() as session:
            row = session.get(KnowledgeIngestionRecord, job_id)
            if row is None:
                continue
            try:
                row = _job(session, job_id, row.owner_id)
            except NotFound:
                row.status, row.lease_token, row.lease_expires_at = "cancelled", None, None
                continue
            if row.status not in {"queued", "processing"} or (row.lease_expires_at or 0) > now:
                continue
            row.status, row.lease_token, row.lease_expires_at = "processing", uuid.uuid4().hex, now + 360
            row.updated_at = now
            session.flush()
            return row
    return None


def initialize(job, total_pages, *, unit="page", warning_codes=()):
    if not 1 <= total_pages <= 10000:
        raise RecognitionError("recognition_request_invalid")
    with session_scope() as session:
        row = _job(session, job.id, job.owner_id, job.lease_token)
        if row.total_pages:
            if row.total_pages != total_pages:
                raise RecognitionError("recognition_source_mismatch")
            return
        row.total_pages = total_pages
        row.configuration = dict(row.configuration, unit=unit, warning_codes=list(warning_codes))
        session.add_all([KnowledgePageRecord(id=_id("kp_"), ingestion_id=row.id, page_number=n)
                         for n in range(1, total_pages + 1)])
        session.flush()
        _publish(session, row)


def next_pages(job):
    with session_scope() as session:
        row = _job(session, job.id, job.owner_id, job.lease_token)
        return list(session.scalars(select(KnowledgePageRecord).where(
            KnowledgePageRecord.ingestion_id == row.id, KnowledgePageRecord.state.in_(["unprocessed", "processing"]))
            .order_by(KnowledgePageRecord.page_number).limit(BATCH_PAGES)))


def prepare_page(job, page_id, *, batch_extra_remaining):
    with session_scope() as session:
        row = _job(session, job.id, job.owner_id, job.lease_token)
        row.lease_expires_at = time.time() + 360
        page = session.get(KnowledgePageRecord, page_id)
        if page is None or page.ingestion_id != row.id or page.state in TERMINAL_PAGES:
            raise LeaseLost()
        if page.state == "unprocessed":
            batch_start = ((page.page_number - 1) // BATCH_PAGES) * BATCH_PAGES + 1
            batch_reserved = session.scalar(select(func.count()).select_from(KnowledgePageRecord).where(
                KnowledgePageRecord.ingestion_id == row.id, KnowledgePageRecord.extra_reserved.is_(True),
                KnowledgePageRecord.page_number >= batch_start, KnowledgePageRecord.page_number < batch_start + BATCH_PAGES))
            allowed = batch_extra_remaining > 0 and batch_reserved < 2 and row.extra_reserved < math.ceil(row.total_pages * .02)
            page.extra_allowed = page.extra_reserved = allowed
            row.extra_reserved += int(allowed)
            page.state = "processing"
        session.flush()
        return page


def reusable_page(job, page):
    """Reuse known pages; uncertain submits require a new, explicitly authorized job."""
    with session_scope() as session:
        row = _job(session, job.id, job.owner_id, job.lease_token)
        if page.state != "unprocessed":
            return None
        candidates = session.execute(select(KnowledgePageRecord, KnowledgeIngestionRecord)
            .join(KnowledgeIngestionRecord, KnowledgeIngestionRecord.id == KnowledgePageRecord.ingestion_id)
            .where(KnowledgeIngestionRecord.document_id == row.document_id,
                   KnowledgeIngestionRecord.id != row.id, KnowledgePageRecord.page_number == page.page_number,
                   KnowledgePageRecord.state.in_(TERMINAL_PAGES))
            .order_by(KnowledgeIngestionRecord.created_at.desc()).limit(20)).all()
        for previous, version in candidates:
            same = all(version.configuration.get(key) == row.configuration.get(key) for key in
                       ("source_sha256", "policy_version", "prompt_version", "route_id", "route_fingerprint"))
            uncertain = previous.evidence.get("error_code") == "provider_submit_uncertain" or any(
                call.get("state") == "pending" for call in (previous.operation.get("checkpoint") or {}).get("calls", []))
            if uncertain and row.configuration.get("resubmit_uncertain"):
                continue
            if not uncertain and (not same or previous.state not in GOOD_PAGES):
                continue
            start, count = previous.evidence.get("chunk_start", 0), previous.evidence.get("chunk_count", 0)
            chunks = session.scalars(select(KnowledgeChunkRecord).where(KnowledgeChunkRecord.document_id == row.document_id,
                KnowledgeChunkRecord.content_version == version.id, KnowledgeChunkRecord.chunk_index >= start,
                KnowledgeChunkRecord.chunk_index < start + count).order_by(KnowledgeChunkRecord.chunk_index)).all()
            text, end = "", 0
            for chunk in chunks:
                offset = chunk.chunk_metadata["start"]
                if offset > end:
                    raise RecognitionError("recognition_artifact_invalid")
                text += chunk.content[end - offset:]
                end = chunk.chunk_metadata["end"]
            evidence = dict(previous.evidence)
            if uncertain:
                evidence["error_code"] = "provider_submit_uncertain"
            return dict(text=text, state="failed" if uncertain else previous.state, evidence=dict(evidence, reused_page_id=previous.id,
                current_usage=dict(initial_calls=0, input_tokens=0, output_tokens=0, usage_complete=True)))
    return None


def finish_page(job, page_id, *, text="", state, evidence):
    if state not in TERMINAL_PAGES:
        raise ValueError("invalid page state")
    spans = chunk_spans(text)
    with session_scope() as session:
        row = _job(session, job.id, job.owner_id, job.lease_token)
        page = session.get(KnowledgePageRecord, page_id)
        if page is None or page.ingestion_id != row.id:
            raise LeaseLost()
        if page.state in TERMINAL_PAGES:
            return
        total_chars = session.scalar(select(func.coalesce(func.sum(func.length(KnowledgeChunkRecord.content)), 0))
            .where(KnowledgeChunkRecord.document_id == row.document_id))
        if total_chars + sum(len(item["content"]) for item in spans) > MAX_TEXT_CHARS:
            spans, state, evidence = [], "failed", dict(evidence, error_code="knowledge_text_budget")
        for offset, span in enumerate(spans):
            session.add(KnowledgeChunkRecord(id=_id("chunk_"), document_id=row.document_id,
                content_version=row.id, chunk_index=row.next_chunk_index + offset, content=span["content"],
                chunk_metadata=dict(page_number=page.page_number, unit=row.configuration.get("unit", "page"),
                    start=span["start"], end=span["end"], source_sha256=row.configuration["source_sha256"],
                    content_version=row.id, page_id=page.id, artifact_ids=evidence.get("artifact_ids", []),
                    confidence=evidence.get("confidence"), warning_codes=evidence.get("warning_codes", []),
                    page_state=state, error_code=evidence.get("error_code")),
                token_count=0))
        page.evidence = dict(evidence, chunk_start=row.next_chunk_index, chunk_count=len(spans))
        usage = evidence.get("current_usage", evidence.get("usage", {}))
        totals = dict(row.configuration.get("usage", {}))
        for key in ("initial_calls", "patch_calls", "empty_recovery_calls", "duration_ms"):
            totals[key] = totals.get(key, 0) + (usage.get(key) or 0)
        for key in ("input_tokens", "output_tokens"):
            totals["known_" + key] = totals.get("known_" + key, 0) + (usage.get(key) or 0)
        totals["usage_complete"] = totals.get("usage_complete", True) and usage.get("usage_complete", True)
        row.configuration = dict(row.configuration, usage=totals)
        row.next_chunk_index += len(spans)
        page.state = state
        calls = (page.operation.get("checkpoint") or {}).get("calls", [])
        extra_used = any(call.get("kind") in {"patch", "empty_recovery"} for call in calls)
        if page.extra_reserved and not extra_used:
            page.extra_reserved = False
            row.extra_reserved -= 1
        row.updated_at = time.time()
        session.flush()
        _publish(session, row)


def _publish(session, row):
    counts = dict(session.execute(select(KnowledgePageRecord.state, func.count()).where(
        KnowledgePageRecord.ingestion_id == row.id).group_by(KnowledgePageRecord.state)).all())
    processed = sum(counts.get(key, 0) for key in TERMINAL_PAGES)
    available = sum(counts.get(key, 0) for key in GOOD_PAGES)
    partial_readable = sum(bool(evidence.get("chunk_count")) for evidence in session.scalars(
        select(KnowledgePageRecord.evidence).where(KnowledgePageRecord.ingestion_id == row.id,
                                                  KnowledgePageRecord.state == "failed")))
    done = bool(row.total_pages and processed == row.total_pages)
    complete = done and not counts.get("failed")
    if done:
        row.status = ("complete_with_warning" if counts.get("searchable_with_warning") else "complete") if complete else "partial" if available or partial_readable else "failed"
        row.lease_token = row.lease_expires_at = None
    doc = session.get(KnowledgeDocumentRecord, row.document_id)
    summary = dict(id=row.id, status=row.status, total_pages=row.total_pages or None,
        processed_pages=processed, searchable_pages=available - counts.get("blank_confirmed", 0) + partial_readable,
        partially_searchable_pages=partial_readable,
        blank_pages=counts.get("blank_confirmed", 0), warning_pages=counts.get("searchable_with_warning", 0),
        failed_pages=counts.get("failed", 0), coverage_complete=complete,
        unit=row.configuration.get("unit", "page"), error_code=row.error_code,
        policy_version=row.configuration.get("policy_version"), extra_calls_reserved=row.extra_reserved,
        usage=row.configuration.get("usage", {}))
    # The first version may publish incrementally. Reprocessing never replaces
    # an already readable version with failed or incomplete content.
    if complete or doc.active_version == row.id or (doc.active_version is None and doc.status != "ready"):
        if row.next_chunk_index:
            doc.active_version, doc.chunk_count = row.id, row.next_chunk_index
            doc.status = "ready" if complete else "partial"
        elif done:
            doc.status = "ready" if complete else "failed"
        doc.parser_version = "knowledge-economy-v1"
    doc.ingestion_summary, doc.updated_at = summary, time.time()


def release(job, *, status=None, error=None):
    with session_scope() as session:
        row = _job(session, job.id, job.owner_id)
        if row.status in {"complete", "complete_with_warning", "partial", "failed"}:
            return
        if row.lease_token != job.lease_token or (row.lease_expires_at or 0) <= time.time():
            raise LeaseLost()
        if status:
            row.status = status
        elif row.status == "processing":
            row.status = "queued"
        row.error_code = error
        _publish(session, row)
        row.lease_token = row.lease_expires_at = None
        row.updated_at = time.time()


def manifest(document_id, owner_id, *, offset=0, limit=100):
    with session_scope() as session:
        doc = live_document(session, document_id, owner_id)
        job_id = (doc.ingestion_summary or {}).get("id")
        pages = list(session.scalars(select(KnowledgePageRecord).where(KnowledgePageRecord.ingestion_id == job_id)
            .order_by(KnowledgePageRecord.page_number).offset(max(0, offset)).limit(min(100, max(1, limit)))))
        return dict(summary=doc.ingestion_summary or {}, content_version=doc.active_version or "legacy",
            pages=[dict(page_number=p.page_number, state=p.state, **p.evidence) for p in pages],
            offset=offset, next_offset=offset + len(pages) if len(pages) == limit else None)


def resume(document_id, owner_id):
    with session_scope() as session:
        doc = live_document(session, document_id, owner_id, lock=True)
        row = session.get(KnowledgeIngestionRecord, (doc.ingestion_summary or {}).get("id"))
        if row is None or row.status not in {"paused", "cancelled"}:
            raise InvalidTransition("Only a paused or cancelled ingestion can resume.")
        row.status, row.error_code = "queued", None
        row.updated_at = time.time()
        _publish(session, row)


def cancel(document_id, owner_id):
    with session_scope() as session:
        doc = live_document(session, document_id, owner_id, lock=True)
        row = session.get(KnowledgeIngestionRecord, (doc.ingestion_summary or {}).get("id"))
        if row and row.status in {"queued", "processing", "paused"}:
            row.status, row.lease_token, row.lease_expires_at = "cancelled", None, None
            _publish(session, row)


class KnowledgeRunRepository:
    """Adapt one page to the shared durable recognition operation contract."""
    def __init__(self, job, page_id):
        self.job, self.page_id = job, page_id

    def _page(self, session, owner_id):
        if owner_id != self.job.owner_id:
            raise NotFound("knowledge_page")
        row = _job(session, self.job.id, owner_id, self.job.lease_token)
        page = session.get(KnowledgePageRecord, self.page_id)
        if page is None or page.ingestion_id != row.id or page.state != "processing":
            raise LeaseLost()
        return page

    def resolve_binding(self, binding, owner_id):
        if binding.link != "knowledge_document" or binding.business_id != self.job.document_id:
            raise RecognitionError("recognition_source_mismatch")
        with session_scope() as session:
            self._page(session, owner_id)
        return binding.business_id

    def create_operation(self, *, assignment_id, owner_id, input_hash, payload, **_):
        with session_scope() as session:
            page = self._page(session, owner_id)
            if assignment_id != self.job.document_id:
                raise RecognitionError("recognition_source_mismatch")
            created = not page.operation
            if created:
                page.operation = dict(id=page.id, owner_id=owner_id, attempt=1, input_hash=input_hash,
                    payload=payload, checkpoint={}, checkpoint_revision=0, artifact_refs=[], status="pending",
                    lease_token=None, lease_expires_at=None, completed_at=None, terminal_summary=None)
            if page.operation["input_hash"] != input_hash:
                raise RecognitionError("recognition_plan_changed")
            return SimpleNamespace(**page.operation), created

    def get_operation(self, operation_id, *, owner_id):
        with session_scope() as session:
            page = self._page(session, owner_id)
            if page.id != operation_id:
                raise LeaseLost()
            return SimpleNamespace(**page.operation)

    def claim_operation(self, operation_id, *, owner_id, worker_id, lease_seconds):
        with session_scope() as session:
            page = self._page(session, owner_id)
            data = dict(page.operation)
            if page.id != operation_id or (data.get("lease_expires_at") or 0) > time.time():
                raise LeaseLost()
            data.update(status="running", worker_id=worker_id, lease_token=uuid.uuid4().hex,
                        lease_expires_at=time.time() + lease_seconds)
            page.operation = data
            return SimpleNamespace(**data)

    def fence(self, session, owner_id, operation_id, attempt, token):
        page = self._page(session, owner_id)
        data = page.operation
        if (page.id != operation_id or data.get("attempt") != attempt or data.get("lease_token") != token
                or not token or (data.get("lease_expires_at") or 0) <= time.time() or data.get("terminal_summary") is not None):
            raise LeaseLost()
        return page

    def save_operation_checkpoint(self, operation_id, *, owner_id, expected_attempt, expected_checkpoint_revision,
                                  expected_lease_token, checkpoint, artifact_refs, terminal_status, terminal_summary, **_):
        with session_scope() as session:
            page = self.fence(session, owner_id, operation_id, expected_attempt, expected_lease_token)
            data = dict(page.operation)
            if data["checkpoint_revision"] != expected_checkpoint_revision:
                raise LeaseLost()
            refs = set(session.scalars(select(KnowledgeEvidenceRecord.id).where(
                KnowledgeEvidenceRecord.document_id == self.job.document_id,
                KnowledgeEvidenceRecord.id.in_(artifact_refs))))
            if refs != set(artifact_refs):
                raise RecognitionError("recognition_artifact_invalid")
            data.update(checkpoint=checkpoint, checkpoint_revision=expected_checkpoint_revision + 1, artifact_refs=artifact_refs)
            if terminal_status:
                data.update(status=terminal_status, terminal_summary=terminal_summary, completed_at=time.time(),
                            lease_token=None, lease_expires_at=None)
            page.operation = data
            return SimpleNamespace(**data)

    def release_operation(self, operation_id, *, owner_id, worker_id, lease_token):
        with session_scope() as session:
            page = self._page(session, owner_id)
            data = dict(page.operation)
            if page.id == operation_id and data.get("worker_id") == worker_id and data.get("lease_token") == lease_token:
                data.update(lease_token=None, lease_expires_at=None)
                page.operation = data
