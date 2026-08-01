from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from sqlalchemy import delete, select

from backend.db.models import (
    AssignmentKnowledgeDocumentRecord,
    AssignmentRecord,
    KnowledgeChunkRecord,
    KnowledgeDocumentRecord,
)
from backend.db.session import session_scope
from backend.domain.errors import InvalidTransition


@dataclass(frozen=True)
class KnowledgeDocument:
    id: str
    owner_id: str
    stored_file_id: str | None
    title: str
    original_name: str
    content_type: str | None
    size_bytes: int
    sha256: str
    status: str
    parser_version: str
    chunk_count: int
    error_code: str | None
    created_at: float
    updated_at: float

    def public(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "original_name": self.original_name,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "status": self.status,
            "parser_version": self.parser_version,
            "chunk_count": self.chunk_count,
            "error_code": self.error_code,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _document(record: KnowledgeDocumentRecord) -> KnowledgeDocument:
    return KnowledgeDocument(**{
        column.name: getattr(record, column.name)
        for column in KnowledgeDocumentRecord.__table__.columns
    })


def create_document(*, document_id: str | None = None, owner_id: str, stored_file_id: str | None,
                    title: str, original_name: str, content_type: str | None, size_bytes: int,
                    sha256: str, status: str = "processing", parser_version: str = "v1") -> KnowledgeDocument:
    now = time.time()
    record = KnowledgeDocumentRecord(
        id=document_id or f"doc_{uuid.uuid4().hex[:16]}", owner_id=owner_id,
        stored_file_id=stored_file_id, title=title, original_name=original_name,
        content_type=content_type, size_bytes=size_bytes, sha256=sha256,
        status=status, parser_version=parser_version, chunk_count=0,
        created_at=now, updated_at=now,
    )
    with session_scope() as session:
        session.add(record)
    return _document(record)


def get_document(document_id: str, owner_id: str) -> KnowledgeDocument | None:
    with session_scope() as session:
        record = session.scalar(select(KnowledgeDocumentRecord).where(
            KnowledgeDocumentRecord.id == document_id,
            KnowledgeDocumentRecord.owner_id == owner_id,
        ))
        return _document(record) if record else None


def get_document_by_hash(owner_id: str, sha256: str) -> KnowledgeDocument | None:
    with session_scope() as session:
        record = session.scalar(select(KnowledgeDocumentRecord).where(
            KnowledgeDocumentRecord.owner_id == owner_id,
            KnowledgeDocumentRecord.sha256 == sha256,
        ))
        return _document(record) if record else None


def list_documents(owner_id: str) -> list[KnowledgeDocument]:
    with session_scope() as session:
        records = list(session.scalars(select(KnowledgeDocumentRecord).where(
            KnowledgeDocumentRecord.owner_id == owner_id
        ).order_by(KnowledgeDocumentRecord.created_at.desc())))
        return [_document(record) for record in records]


def update_document(document_id: str, owner_id: str, **fields) -> KnowledgeDocument | None:
    allowed = {"status", "chunk_count", "error_code", "stored_file_id", "title"}
    values = {key: value for key, value in fields.items() if key in allowed}
    values["updated_at"] = time.time()
    with session_scope() as session:
        record = session.scalar(select(KnowledgeDocumentRecord).where(
            KnowledgeDocumentRecord.id == document_id, KnowledgeDocumentRecord.owner_id == owner_id
        ))
        if record is None:
            return None
        for key, value in values.items():
            setattr(record, key, value)
        session.flush()
        return _document(record)


def replace_document_chunks(document_id: str, chunks: list[str]) -> None:
    with session_scope() as session:
        session.execute(delete(KnowledgeChunkRecord).where(KnowledgeChunkRecord.document_id == document_id))
        session.add_all([
            KnowledgeChunkRecord(id=f"chunk_{uuid.uuid4().hex[:16]}", document_id=document_id,
                                 chunk_index=index, content=content,
                                 chunk_metadata={}, token_count=len(content.split()))
            for index, content in enumerate(chunks)
        ])
        record = session.get(KnowledgeDocumentRecord, document_id)
        if record:
            record.chunk_count = len(chunks)
            record.status = "ready"
            record.error_code = None
            record.updated_at = time.time()


def list_selected_documents(assignment_id: str, owner_id: str) -> list[KnowledgeDocument]:
    """Personal documents selected for an assignment.

    Teacher ownership is resolved through the assignment; the selection itself
    lives in ``assignment_knowledge_documents`` (replacing the legacy
    task-scoped table). Only ready documents are returned so retrieval never
    surfaces a document still being parsed.
    """
    with session_scope() as session:
        records = list(session.scalars(
            select(KnowledgeDocumentRecord)
            .join(AssignmentKnowledgeDocumentRecord,
                  AssignmentKnowledgeDocumentRecord.document_id == KnowledgeDocumentRecord.id)
            .join(AssignmentRecord, AssignmentRecord.id == AssignmentKnowledgeDocumentRecord.assignment_id)
            .where(AssignmentKnowledgeDocumentRecord.assignment_id == assignment_id,
                   AssignmentRecord.teacher_id == owner_id,
                   KnowledgeDocumentRecord.owner_id == owner_id,
                   KnowledgeDocumentRecord.status == "ready")
            .order_by(AssignmentKnowledgeDocumentRecord.selected_at)
        ))
        return [_document(record) for record in records]


def selected_document_metadata(
    assignment_id: str, owner_id: str,
) -> dict[str, dict[str, str | None]]:
    """Return owner-scoped attachment provenance for presentation adapters."""
    with session_scope() as session:
        rows = session.execute(
            select(AssignmentKnowledgeDocumentRecord)
            .join(
                AssignmentRecord,
                AssignmentRecord.id
                == AssignmentKnowledgeDocumentRecord.assignment_id,
            )
            .where(
                AssignmentKnowledgeDocumentRecord.assignment_id == assignment_id,
                AssignmentRecord.teacher_id == owner_id,
            )
        ).scalars().all()
        return {
            row.document_id: {
                "source_kind": row.source_kind,
                "library_material_id": row.library_material_id,
            }
            for row in rows
        }


def set_selected_document_metadata(
    *, assignment_id: str, owner_id: str, document_id: str,
    source_kind: str, library_material_id: str | None,
) -> None:
    if source_kind not in {"upload", "library"}:
        raise ValueError("Unsupported knowledge attachment source")
    from backend.db.models import CourseMaterialRecord

    with session_scope() as session:
        assignment = session.scalar(
            select(AssignmentRecord).where(
                AssignmentRecord.id == assignment_id,
                AssignmentRecord.teacher_id == owner_id,
            )
        )
        row = session.get(
            AssignmentKnowledgeDocumentRecord,
            {"assignment_id": assignment_id, "document_id": document_id},
        )
        if assignment is None or row is None:
            raise ValueError("Knowledge attachment not found")
        if library_material_id is not None:
            material = session.scalar(
                select(CourseMaterialRecord).where(
                    CourseMaterialRecord.id == library_material_id,
                    CourseMaterialRecord.owner_id == owner_id,
                    CourseMaterialRecord.document_id == document_id,
                )
            )
            if material is None:
                raise ValueError("Course material does not match document")
        row.source_kind = source_kind
        row.library_material_id = library_material_id


def list_chunks(document_ids: list[str]) -> list[KnowledgeChunkRecord]:
    if not document_ids:
        return []
    with session_scope() as session:
        return list(session.scalars(select(KnowledgeChunkRecord).where(
            KnowledgeChunkRecord.document_id.in_(document_ids)
        ).order_by(KnowledgeChunkRecord.document_id, KnowledgeChunkRecord.chunk_index)))


def set_task_documents(*, assignment_id: str, owner_id: str, document_ids: list[str]) -> list[KnowledgeDocument]:
    """Select up to three ready personal documents for an assignment.

    Name kept for import compatibility while the retriever/API migrate to the
    assignment scope (Task 6). Ownership is checked through the assignment.
    """
    with session_scope() as session:
        assignment = session.scalar(
            select(AssignmentRecord).where(
                AssignmentRecord.id == assignment_id, AssignmentRecord.teacher_id == owner_id
            )
        )
        if assignment is None:
            raise ValueError("Assignment not found")
        unique_ids = list(dict.fromkeys(document_ids))
        if len(unique_ids) > 3:
            raise ValueError("An assignment can select at most 3 personal knowledge documents")
        if unique_ids:
            valid = set(session.scalars(select(KnowledgeDocumentRecord.id).where(
                KnowledgeDocumentRecord.id.in_(unique_ids), KnowledgeDocumentRecord.owner_id == owner_id,
                KnowledgeDocumentRecord.status == "ready"
            )))
            if valid != set(unique_ids):
                raise ValueError("One or more knowledge documents are unavailable")
        existing_rows = session.scalars(
            select(AssignmentKnowledgeDocumentRecord).where(
                AssignmentKnowledgeDocumentRecord.assignment_id == assignment_id
            )
        ).all()
        existing = {
            row.document_id: (row.source_kind, row.library_material_id)
            for row in existing_rows
        }
        session.execute(delete(AssignmentKnowledgeDocumentRecord).where(
            AssignmentKnowledgeDocumentRecord.assignment_id == assignment_id
        ))
        session.add_all([
            AssignmentKnowledgeDocumentRecord(
                assignment_id=assignment_id,
                document_id=document_id,
                source_kind=existing.get(document_id, ("upload", None))[0],
                library_material_id=existing.get(document_id, ("upload", None))[1],
            )
            for document_id in unique_ids
        ])
    return list_selected_documents(assignment_id, owner_id)


def assert_document_not_frozen_by_active_run(
    session, *, document_id: str, owner_id: str,
) -> None:
    """Prevent every deletion path from invalidating a frozen grading input."""
    from backend.db.models import GradingRunRecord
    from backend.db.workflow_repository import GradingRunSetupRecord

    active_manifests = session.scalars(
        select(GradingRunSetupRecord.input_manifest)
        .join(
            GradingRunRecord,
            GradingRunRecord.id == GradingRunSetupRecord.grading_run_id,
        )
        .where(
            GradingRunSetupRecord.owner_id == owner_id,
            GradingRunRecord.status.in_(("queued", "running")),
        )
    ).all()
    if any(
        document_id in (manifest or {}).get("knowledge_document_ids", [])
        for manifest in active_manifests
    ):
        raise InvalidTransition(
            "Knowledge document is frozen by an active grading run.",
            code="knowledge_document_in_active_grading_run",
        )


def delete_document(document_id: str, owner_id: str) -> KnowledgeDocument | None:
    with session_scope() as session:
        record = session.scalar(select(KnowledgeDocumentRecord).where(
            KnowledgeDocumentRecord.id == document_id, KnowledgeDocumentRecord.owner_id == owner_id
        ))
        if record is None:
            return None
        # A queued/running grading run must keep access to the exact immutable
        # knowledge selection captured in its input manifest.  Unselecting a
        # document from an assignment is safe; deleting its chunks mid-run is
        # not, so defer physical deletion until the run is terminal.
        assert_document_not_frozen_by_active_run(
            session, document_id=document_id, owner_id=owner_id,
        )
        result = _document(record)
        session.execute(delete(AssignmentKnowledgeDocumentRecord).where(
            AssignmentKnowledgeDocumentRecord.document_id == document_id
        ))
        session.execute(delete(KnowledgeChunkRecord).where(KnowledgeChunkRecord.document_id == document_id))
        session.delete(record)
        return result
