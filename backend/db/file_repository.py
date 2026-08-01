"""Durable file metadata linked to assignments, submission revisions, or
knowledge documents.

The legacy ``task_id`` link is gone: a stored file now carries explicit
nullable FK columns (``assignment_id`` / ``submission_revision_id`` /
``knowledge_document_id``). Exactly one business link is expected per file,
enforced in application code; the columns stay nullable so a knowledge-only
upload that predates its document row can still be recorded.

Owner-scoped reads use the owner predicate in SQL so a non-owner reads nothing.
"""
from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select

from backend.db.models import StoredFileRecord
from backend.db.session import session_scope
from backend.storage.base import StorageBackend


@dataclass(frozen=True)
class StoredFile:
    id: str
    owner_id: str
    kind: str
    original_name: str
    storage_backend: str
    storage_key: str
    content_type: str | None
    size_bytes: int
    sha256: str
    created_at: float
    assignment_id: str | None = None
    submission_revision_id: str | None = None
    knowledge_document_id: str | None = None


def _record_to_dto(record: StoredFileRecord) -> StoredFile:
    return StoredFile(
        id=record.id,
        owner_id=record.owner_id,
        kind=record.kind,
        original_name=record.original_name,
        storage_backend=record.storage_backend,
        storage_key=record.storage_key,
        content_type=record.content_type,
        size_bytes=record.size_bytes,
        sha256=record.sha256,
        created_at=record.created_at,
        assignment_id=record.assignment_id,
        submission_revision_id=record.submission_revision_id,
        knowledge_document_id=record.knowledge_document_id,
    )


def save_file(*, storage: StorageBackend, owner_id: str, kind: str,
              original_name: str, content: bytes, content_type: str | None = None,
              storage_prefix: str | None = None, assignment_id: str | None = None,
              submission_revision_id: str | None = None,
              knowledge_document_id: str | None = None) -> StoredFile:
    file_id = uuid.uuid4().hex
    safe_name = Path(original_name).name or "upload.bin"
    # Prefix reflects the business link so object-storage listings stay organized.
    if storage_prefix:
        prefix = storage_prefix
    elif submission_revision_id:
        prefix = f"revisions/{submission_revision_id}"
    elif assignment_id:
        prefix = f"assignments/{assignment_id}"
    else:
        prefix = f"users/{owner_id}/files"
    key = f"{prefix}/{file_id}/{safe_name}"
    digest = hashlib.sha256(content).hexdigest()
    storage.save(key, content)
    record = StoredFile(
        id=file_id, owner_id=owner_id, kind=kind, original_name=original_name,
        storage_backend=getattr(storage, "name", "unknown"), storage_key=key,
        content_type=content_type, size_bytes=len(content), sha256=digest,
        created_at=time.time(), assignment_id=assignment_id,
        submission_revision_id=submission_revision_id,
        knowledge_document_id=knowledge_document_id,
    )
    try:
        with session_scope() as session:
            session.add(StoredFileRecord(**record.__dict__))
    except Exception:
        storage.delete(key)
        raise
    return record


def list_files(*, owner_id: str, assignment_id: str | None = None,
               submission_revision_id: str | None = None) -> list[StoredFile]:
    with session_scope() as session:
        stmt = select(StoredFileRecord).where(StoredFileRecord.owner_id == owner_id)
        if assignment_id is not None:
            stmt = stmt.where(StoredFileRecord.assignment_id == assignment_id)
        if submission_revision_id is not None:
            stmt = stmt.where(StoredFileRecord.submission_revision_id == submission_revision_id)
        records = list(session.scalars(stmt))
    return [_record_to_dto(r) for r in records]


def get_file(*, file_id: str, owner_id: str) -> StoredFile | None:
    with session_scope() as session:
        record = session.get(StoredFileRecord, file_id)
        if record is None or record.owner_id != owner_id:
            return None
        return _record_to_dto(record)


def delete_files_for_revision(*, storage: StorageBackend, submission_revision_id: str,
                              owner_id: str) -> int:
    files = list_files(owner_id=owner_id, submission_revision_id=submission_revision_id)
    with session_scope() as session:
        session.execute(delete(StoredFileRecord).where(
            StoredFileRecord.submission_revision_id == submission_revision_id,
            StoredFileRecord.owner_id == owner_id,
        ))
    for file in files:
        storage.delete(file.storage_key)
    return len(files)


def delete_files_for_assignment(*, storage: StorageBackend, assignment_id: str,
                                owner_id: str) -> int:
    files = list_files(owner_id=owner_id, assignment_id=assignment_id)
    with session_scope() as session:
        session.execute(delete(StoredFileRecord).where(
            StoredFileRecord.assignment_id == assignment_id,
            StoredFileRecord.owner_id == owner_id,
        ))
    for file in files:
        storage.delete(file.storage_key)
    return len(files)


def delete_file_record(*, file_id: str, owner_id: str) -> bool:
    with session_scope() as session:
        result = session.execute(delete(StoredFileRecord).where(
            StoredFileRecord.id == file_id, StoredFileRecord.owner_id == owner_id
        ))
        return bool(result.rowcount)
