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
import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select

from backend.db.models import StoredFileRecord
from backend.db.session import session_scope
from backend.storage.base import StorageBackend


logger = logging.getLogger(__name__)
MAX_ORIGINAL_NAME_LENGTH = 512


def _bounded_original_name(value: str) -> str:
    name = str(value or "upload.bin")
    if len(name) <= MAX_ORIGINAL_NAME_LENGTH:
        return name
    digest = hashlib.sha256(name.encode("utf-8", errors="replace")).hexdigest()[:16]
    suffix = Path(name).suffix[:20]
    prefix_length = MAX_ORIGINAL_NAME_LENGTH - len(suffix) - len(digest) - 1
    return f"{name[:prefix_length]}~{digest}{suffix}"


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
    bounded_original_name = _bounded_original_name(original_name)
    safe_name = Path(bounded_original_name).name or "upload.bin"
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
        id=file_id, owner_id=owner_id, kind=kind, original_name=bounded_original_name,
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
        persisted: StoredFile | None = None
        verification_completed = False
        try:
            with session_scope() as session:
                existing = session.scalar(
                    select(StoredFileRecord).where(
                        StoredFileRecord.id == file_id,
                        StoredFileRecord.owner_id == owner_id,
                    )
                )
                persisted = _record_to_dto(existing) if existing is not None else None
                verification_completed = True
        except Exception:
            logger.warning(
                "Stored file commit state could not be verified; file_id=%s",
                file_id,
            )
        if persisted is not None and (
            persisted.storage_key == record.storage_key
            and persisted.sha256 == record.sha256
            and persisted.size_bytes == record.size_bytes
        ):
            return persisted
        if verification_completed and persisted is None:
            try:
                storage.delete(key)
            except Exception:
                logger.warning(
                    "Uncommitted storage object cleanup failed; file_id=%s",
                    file_id,
                )
        raise
    return record


def archive_member_reference_content(
    *,
    container_sha256: str,
    member_name: str,
) -> bytes:
    """Return a small deterministic pointer to a preserved archive member."""
    return json.dumps(
        {
            "container_sha256": container_sha256,
            "member_name": _bounded_original_name(member_name),
            "schema": "smartai.archive-member-reference.v1",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def create_archive_member_reference(
    *,
    storage: StorageBackend,
    source_file_id: str,
    owner_id: str,
    assignment_id: str,
    member_name: str,
) -> StoredFile:
    """Persist a bounded pointer when archive member bytes cannot be saved.

    The full archive is already durable and checkpoint-referenced.  A small
    independent object avoids duplicating a potentially 100 MB archive for
    every failed member while giving each logical source its own StoredFile row.
    """
    with session_scope() as session:
        source = session.scalar(
            select(StoredFileRecord).where(
                StoredFileRecord.id == source_file_id,
                StoredFileRecord.owner_id == owner_id,
                StoredFileRecord.assignment_id == assignment_id,
            )
        )
        if source is None:
            raise ValueError("source_file_not_found")
        container_name = source.original_name
        container_sha256 = source.sha256
    content = archive_member_reference_content(
        container_sha256=container_sha256,
        member_name=member_name,
    )
    return save_file(
        storage=storage,
        owner_id=owner_id,
        kind="submission_source_reference",
        original_name=f"{container_name} :: {member_name}",
        content=content,
        content_type="application/vnd.smartai.archive-member-reference+json",
        storage_prefix=f"assignments/{assignment_id}/submission-source-references",
        assignment_id=assignment_id,
    )


def delete_unlinked_file(
    *,
    storage: StorageBackend,
    file_id: str,
    owner_id: str,
    assignment_id: str,
    delete_object: bool = True,
) -> bool:
    """Delete metadata first; FK RESTRICT protects any committed source link."""
    stored: StoredFile | None = None
    try:
        with session_scope() as session:
            record = session.scalar(
                select(StoredFileRecord).where(
                    StoredFileRecord.id == file_id,
                    StoredFileRecord.owner_id == owner_id,
                    StoredFileRecord.assignment_id == assignment_id,
                ).with_for_update()
            )
            if record is None:
                return False
            from backend.db.workflow_repository import WorkflowOperationRecord

            operation_artifact_refs = session.scalars(
                select(WorkflowOperationRecord.artifact_refs).where(
                    WorkflowOperationRecord.owner_id == owner_id,
                    WorkflowOperationRecord.assignment_id == assignment_id,
                )
            ).all()
            if any(
                file_id in refs
                for refs in operation_artifact_refs
                if isinstance(refs, list)
            ):
                return False
            stored = _record_to_dto(record)
            session.delete(record)
            session.flush()
    except Exception:
        # A committed workflow source may now reference the row. In that case
        # preserving the object is safer than guessing whether the commit won.
        return False
    if delete_object and stored is not None:
        try:
            storage.delete(stored.storage_key)
        except Exception:
            logger.warning(
                "Unlinked storage object cleanup failed; file_id=%s",
                file_id,
            )
    return True


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
