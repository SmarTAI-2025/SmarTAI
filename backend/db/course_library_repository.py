"""Durable, owner-scoped Course Library persistence.

``CourseMaterialRecord`` is deliberately metadata-only.  Every material joins
to the canonical ``KnowledgeDocumentRecord`` and its ``StoredFileRecord`` for
hash, parse state, file metadata, and object-storage identity.  Assignment
reference counts are derived from ``assignment_knowledge_documents`` rather
than copied into mutable counters.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.exc import IntegrityError

from backend.db.models import (
    AssignmentKnowledgeDocumentRecord,
    AssignmentRecord,
    CourseMaterialGroupRecord,
    CourseMaterialRecord,
    CourseRecord,
    KnowledgeChunkRecord,
    KnowledgeDocumentRecord,
    StoredFileRecord,
)
from backend.db.session import session_scope


class DuplicateGroupName(ValueError):
    def __init__(self, group_id: str):
        super().__init__("A material group with that name already exists")
        self.group_id = group_id


class GroupCourseMismatch(ValueError):
    pass


class MaterialDocumentUnavailable(ValueError):
    def __init__(self, status: str):
        super().__init__(f"Knowledge document is not ready: {status}")
        self.status = status


class MaterialReferenced(ValueError):
    def __init__(self, reference_count: int):
        super().__init__("Course material is referenced by assignments")
        self.reference_count = reference_count


@dataclass(frozen=True)
class CourseRef:
    id: str
    name: str
    code: str


@dataclass(frozen=True)
class CourseMaterialGroup:
    group_id: str
    owner_id: str
    name: str
    normalized_name: str
    course_id: str | None
    material_count: int
    created_at: float
    updated_at: float
    course_name: str | None
    course_code: str | None

    def public(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "name": self.name,
            "course_id": self.course_id,
            "material_count": self.material_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "course_name": self.course_name,
            "course_code": self.course_code,
        }


@dataclass(frozen=True)
class CourseMaterial:
    material_id: str
    document_id: str
    stored_file_id: str | None
    storage_backend: str | None
    storage_key: str | None
    owner_id: str
    course_id: str | None
    group_id: str | None
    filename: str
    category: str
    labels: list[str]
    content_type: str
    size_bytes: int
    sha256: str
    created_at: float
    updated_at: float
    last_used_at: float | None
    task_reference_count: int
    parse_status: str
    group_name: str | None
    course_name: str | None
    course_code: str | None

    def public(self) -> dict[str, Any]:
        return {
            "material_id": self.material_id,
            "course_id": self.course_id,
            "group_id": self.group_id,
            "filename": self.filename,
            "category": self.category,
            "labels": list(self.labels),
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_used_at": self.last_used_at,
            "task_reference_count": self.task_reference_count,
            "parse_status": self.parse_status,
            "group_name": self.group_name,
            "course_name": self.course_name,
            "course_code": self.course_code,
        }


@dataclass(frozen=True)
class DeletedMaterial:
    material_id: str
    document_id: str
    storage_key: str | None
    detached_references: int


def get_owned_course(course_id: str, owner_id: str) -> CourseRef | None:
    with session_scope() as session:
        record = session.scalar(
            select(CourseRecord).where(
                CourseRecord.id == course_id,
                CourseRecord.teacher_id == owner_id,
            )
        )
        if record is None:
            return None
        return CourseRef(id=record.id, name=record.name, code=record.code or "")


def _course_ref(session, course_id: str | None, owner_id: str) -> CourseRef | None:
    if course_id is None:
        return None
    record = session.scalar(
        select(CourseRecord).where(
            CourseRecord.id == course_id,
            CourseRecord.teacher_id == owner_id,
        )
    )
    if record is None:
        return None
    return CourseRef(id=record.id, name=record.name, code=record.code or "")


def _group_counts(session, owner_id: str) -> dict[str, int]:
    return {
        group_id: int(count)
        for group_id, count in session.execute(
            select(CourseMaterialRecord.group_id, func.count(CourseMaterialRecord.id))
            .where(
                CourseMaterialRecord.owner_id == owner_id,
                CourseMaterialRecord.group_id.is_not(None),
            )
            .group_by(CourseMaterialRecord.group_id)
        )
        if group_id is not None
    }


def _group_dto(
    session,
    record: CourseMaterialGroupRecord,
    *,
    material_count: int | None = None,
) -> CourseMaterialGroup:
    course = _course_ref(session, record.course_id, record.owner_id)
    if material_count is None:
        material_count = int(session.scalar(
            select(func.count(CourseMaterialRecord.id)).where(
                CourseMaterialRecord.owner_id == record.owner_id,
                CourseMaterialRecord.group_id == record.id,
            )
        ) or 0)
    return CourseMaterialGroup(
        group_id=record.id,
        owner_id=record.owner_id,
        name=record.name,
        normalized_name=record.normalized_name,
        course_id=record.course_id,
        material_count=material_count,
        created_at=record.created_at,
        updated_at=record.updated_at,
        course_name=course.name if course else None,
        course_code=course.code if course else None,
    )


def list_groups(owner_id: str) -> list[CourseMaterialGroup]:
    with session_scope() as session:
        records = list(session.scalars(
            select(CourseMaterialGroupRecord)
            .where(CourseMaterialGroupRecord.owner_id == owner_id)
            .order_by(
                CourseMaterialGroupRecord.normalized_name,
                CourseMaterialGroupRecord.created_at,
            )
        ))
        counts = _group_counts(session, owner_id)
        return [
            _group_dto(session, record, material_count=counts.get(record.id, 0))
            for record in records
        ]


def get_group(group_id: str, owner_id: str) -> CourseMaterialGroup | None:
    with session_scope() as session:
        record = session.scalar(
            select(CourseMaterialGroupRecord).where(
                CourseMaterialGroupRecord.id == group_id,
                CourseMaterialGroupRecord.owner_id == owner_id,
            )
        )
        return _group_dto(session, record) if record is not None else None


def find_group_by_normalized_name(
    owner_id: str, normalized_name: str
) -> CourseMaterialGroup | None:
    with session_scope() as session:
        record = session.scalar(
            select(CourseMaterialGroupRecord).where(
                CourseMaterialGroupRecord.owner_id == owner_id,
                CourseMaterialGroupRecord.normalized_name == normalized_name,
            )
        )
        return _group_dto(session, record) if record is not None else None


def create_group(
    *, owner_id: str, name: str, normalized_name: str, course_id: str | None
) -> tuple[CourseMaterialGroup, bool]:
    existing = find_group_by_normalized_name(owner_id, normalized_name)
    if existing is not None:
        return existing, False
    now = time.time()
    group_id = f"cmg_{uuid.uuid4().hex[:12]}"
    try:
        with session_scope() as session:
            if course_id is not None and _course_ref(session, course_id, owner_id) is None:
                raise LookupError("course")
            session.add(CourseMaterialGroupRecord(
                id=group_id,
                owner_id=owner_id,
                course_id=course_id,
                name=name,
                normalized_name=normalized_name,
                created_at=now,
                updated_at=now,
            ))
    except IntegrityError:
        # Owner/name uniqueness is the concurrency boundary. A racing exact
        # create returns the winner rather than surfacing a transient 500.
        existing = find_group_by_normalized_name(owner_id, normalized_name)
        if existing is not None:
            return existing, False
        raise
    created = get_group(group_id, owner_id)
    assert created is not None
    return created, True


def update_group(
    group_id: str,
    owner_id: str,
    *,
    name: str | None = None,
    normalized_name: str | None = None,
    set_course: bool = False,
    course_id: str | None = None,
) -> CourseMaterialGroup | None:
    try:
        with session_scope() as session:
            record = session.scalar(
                select(CourseMaterialGroupRecord).where(
                    CourseMaterialGroupRecord.id == group_id,
                    CourseMaterialGroupRecord.owner_id == owner_id,
                )
            )
            if record is None:
                return None
            if normalized_name is not None:
                duplicate = session.scalar(
                    select(CourseMaterialGroupRecord).where(
                        CourseMaterialGroupRecord.owner_id == owner_id,
                        CourseMaterialGroupRecord.normalized_name == normalized_name,
                        CourseMaterialGroupRecord.id != group_id,
                    )
                )
                if duplicate is not None:
                    raise DuplicateGroupName(duplicate.id)
                record.name = name or record.name
                record.normalized_name = normalized_name
            if set_course:
                if course_id is not None and _course_ref(session, course_id, owner_id) is None:
                    raise LookupError("course")
                record.course_id = course_id
                # A course-bound group is a strong invariant: moving the group
                # also moves its owner-scoped materials to that course. Generic
                # groups (course_id=None) retain each material's own course.
                if course_id is not None:
                    session.execute(
                        update(CourseMaterialRecord)
                        .where(
                            CourseMaterialRecord.owner_id == owner_id,
                            CourseMaterialRecord.group_id == group_id,
                        )
                        .values(course_id=course_id, updated_at=time.time())
                    )
            record.updated_at = time.time()
            session.flush()
    except IntegrityError:
        duplicate = (
            find_group_by_normalized_name(owner_id, normalized_name)
            if normalized_name is not None else None
        )
        if duplicate is not None and duplicate.group_id != group_id:
            raise DuplicateGroupName(duplicate.group_id) from None
        raise
    return get_group(group_id, owner_id)


def delete_group(group_id: str, owner_id: str) -> int | None:
    with session_scope() as session:
        record = session.scalar(
            select(CourseMaterialGroupRecord).where(
                CourseMaterialGroupRecord.id == group_id,
                CourseMaterialGroupRecord.owner_id == owner_id,
            )
        )
        if record is None:
            return None
        moved = session.execute(
            update(CourseMaterialRecord)
            .where(
                CourseMaterialRecord.owner_id == owner_id,
                CourseMaterialRecord.group_id == group_id,
            )
            .values(group_id=None, updated_at=time.time())
        ).rowcount or 0
        session.delete(record)
        return int(moved)


def _material_statement(owner_id: str):
    return (
        select(
            CourseMaterialRecord,
            KnowledgeDocumentRecord,
            StoredFileRecord,
            CourseMaterialGroupRecord.name,
            CourseRecord.name,
            CourseRecord.code,
        )
        .join(
            KnowledgeDocumentRecord,
            and_(
                KnowledgeDocumentRecord.id == CourseMaterialRecord.document_id,
                KnowledgeDocumentRecord.owner_id == owner_id,
            ),
        )
        .outerjoin(
            StoredFileRecord,
            and_(
                StoredFileRecord.id == KnowledgeDocumentRecord.stored_file_id,
                StoredFileRecord.owner_id == owner_id,
            ),
        )
        .outerjoin(
            CourseMaterialGroupRecord,
            and_(
                CourseMaterialGroupRecord.id == CourseMaterialRecord.group_id,
                CourseMaterialGroupRecord.owner_id == owner_id,
            ),
        )
        .outerjoin(
            CourseRecord,
            and_(
                CourseRecord.id == CourseMaterialRecord.course_id,
                CourseRecord.teacher_id == owner_id,
            ),
        )
        .where(CourseMaterialRecord.owner_id == owner_id)
    )


def _reference_stats(session, owner_id: str, document_ids: list[str]) -> dict[str, tuple[int, float | None]]:
    if not document_ids:
        return {}
    return {
        document_id: (int(count), last_used_at)
        for document_id, count, last_used_at in session.execute(
            select(
                AssignmentKnowledgeDocumentRecord.document_id,
                func.count(AssignmentKnowledgeDocumentRecord.assignment_id),
                func.max(AssignmentKnowledgeDocumentRecord.selected_at),
            )
            .join(
                AssignmentRecord,
                AssignmentRecord.id == AssignmentKnowledgeDocumentRecord.assignment_id,
            )
            .where(
                AssignmentRecord.teacher_id == owner_id,
                AssignmentKnowledgeDocumentRecord.document_id.in_(document_ids),
            )
            .group_by(AssignmentKnowledgeDocumentRecord.document_id)
        )
    }


def _material_dto(row, stats: dict[str, tuple[int, float | None]]) -> CourseMaterial:
    material, document, stored, group_name, course_name, course_code = row
    reference_count, last_used_at = stats.get(document.id, (0, None))
    return CourseMaterial(
        material_id=material.id,
        document_id=document.id,
        stored_file_id=document.stored_file_id,
        storage_backend=stored.storage_backend if stored is not None else None,
        storage_key=stored.storage_key if stored is not None else None,
        owner_id=material.owner_id,
        course_id=material.course_id,
        group_id=material.group_id,
        filename=material.display_name,
        category=material.category,
        labels=list(material.labels or []),
        content_type=document.content_type or (
            stored.content_type if stored is not None else None
        ) or "application/octet-stream",
        size_bytes=document.size_bytes,
        sha256=document.sha256,
        created_at=material.created_at,
        updated_at=max(material.updated_at, document.updated_at),
        last_used_at=last_used_at,
        task_reference_count=reference_count,
        parse_status=document.status,
        group_name=group_name,
        course_name=course_name,
        course_code=course_code or None,
    )


def list_materials(
    owner_id: str,
    *,
    course_id: str | None = None,
    group_id: str | None = None,
    ungrouped: bool = False,
    category: str | None = None,
    query: str | None = None,
) -> list[CourseMaterial]:
    with session_scope() as session:
        stmt = _material_statement(owner_id)
        if course_id is not None:
            stmt = stmt.where(CourseMaterialRecord.course_id == course_id)
        if ungrouped:
            stmt = stmt.where(CourseMaterialRecord.group_id.is_(None))
        elif group_id is not None:
            stmt = stmt.where(CourseMaterialRecord.group_id == group_id)
        if category is not None:
            stmt = stmt.where(CourseMaterialRecord.category == category)
        rows = list(session.execute(
            stmt.order_by(CourseMaterialRecord.created_at.desc(), CourseMaterialRecord.id)
        ))
        stats = _reference_stats(
            session, owner_id, [row[1].id for row in rows]
        )
        materials = [_material_dto(row, stats) for row in rows]
        normalized_query = (query or "").strip().casefold()
        if normalized_query:
            materials = [
                material for material in materials
                if normalized_query in material.filename.casefold()
                or any(normalized_query in label.casefold() for label in material.labels)
                or normalized_query in (material.course_name or "").casefold()
                or normalized_query in (material.group_name or "").casefold()
            ]
        return materials


def get_material(material_id: str, owner_id: str) -> CourseMaterial | None:
    with session_scope() as session:
        row = session.execute(
            _material_statement(owner_id).where(CourseMaterialRecord.id == material_id)
        ).first()
        if row is None:
            return None
        stats = _reference_stats(session, owner_id, [row[1].id])
        return _material_dto(row, stats)


def get_material_by_document(document_id: str, owner_id: str) -> CourseMaterial | None:
    with session_scope() as session:
        row = session.execute(
            _material_statement(owner_id).where(
                CourseMaterialRecord.document_id == document_id
            )
        ).first()
        if row is None:
            return None
        stats = _reference_stats(session, owner_id, [document_id])
        return _material_dto(row, stats)


def create_material(
    *,
    owner_id: str,
    document_id: str,
    filename: str,
    category: str,
    labels: list[str],
    course_id: str | None,
    group_id: str | None,
) -> tuple[CourseMaterial, bool]:
    existing = get_material_by_document(document_id, owner_id)
    if existing is not None:
        return existing, False
    material_id = f"material_{uuid.uuid4().hex[:12]}"
    now = time.time()
    try:
        with session_scope() as session:
            document = session.scalar(
                select(KnowledgeDocumentRecord).where(
                    KnowledgeDocumentRecord.id == document_id,
                    KnowledgeDocumentRecord.owner_id == owner_id,
                )
            )
            if document is None:
                raise LookupError("knowledge_document")
            if document.status != "ready":
                raise MaterialDocumentUnavailable(document.status)
            if course_id is not None and _course_ref(session, course_id, owner_id) is None:
                raise LookupError("course")
            if group_id is not None:
                group = session.scalar(
                    select(CourseMaterialGroupRecord).where(
                        CourseMaterialGroupRecord.id == group_id,
                        CourseMaterialGroupRecord.owner_id == owner_id,
                    )
                )
                if group is None:
                    raise LookupError("group")
                if course_id is None:
                    course_id = group.course_id
                elif group.course_id is not None and group.course_id != course_id:
                    raise GroupCourseMismatch()
            session.add(CourseMaterialRecord(
                id=material_id,
                document_id=document_id,
                owner_id=owner_id,
                course_id=course_id,
                group_id=group_id,
                display_name=filename,
                category=category,
                labels=list(labels),
                created_at=now,
                updated_at=now,
            ))
    except IntegrityError:
        existing = get_material_by_document(document_id, owner_id)
        if existing is not None:
            return existing, False
        raise
    created = get_material(material_id, owner_id)
    assert created is not None
    return created, True


def update_material(
    material_id: str,
    owner_id: str,
    **changes: Any,
) -> CourseMaterial | None:
    allowed = {"filename", "course_id", "group_id", "category", "labels"}
    if unknown := set(changes) - allowed:
        raise ValueError(f"Unsupported course material fields: {sorted(unknown)}")
    with session_scope() as session:
        record = session.scalar(
            select(CourseMaterialRecord).where(
                CourseMaterialRecord.id == material_id,
                CourseMaterialRecord.owner_id == owner_id,
            )
        )
        if record is None:
            return None
        effective_course_id = changes.get("course_id", record.course_id)
        effective_group_id = changes.get("group_id", record.group_id)
        if effective_course_id is not None and _course_ref(
            session, effective_course_id, owner_id
        ) is None:
            raise LookupError("course")
        if effective_group_id is not None:
            group = session.scalar(
                select(CourseMaterialGroupRecord).where(
                    CourseMaterialGroupRecord.id == effective_group_id,
                    CourseMaterialGroupRecord.owner_id == owner_id,
                )
            )
            if group is None:
                raise LookupError("group")
            if effective_course_id is None:
                effective_course_id = group.course_id
            elif group.course_id is not None and group.course_id != effective_course_id:
                raise GroupCourseMismatch()
        if "filename" in changes:
            record.display_name = changes["filename"]
        if "course_id" in changes or (
            "group_id" in changes and changes["group_id"] is not None and record.course_id is None
        ):
            record.course_id = effective_course_id
        if "group_id" in changes:
            record.group_id = changes["group_id"]
        if "category" in changes:
            record.category = changes["category"]
        if "labels" in changes:
            record.labels = list(changes["labels"])
        record.updated_at = time.time()
        session.flush()
    return get_material(material_id, owner_id)


def delete_material(
    material_id: str,
    owner_id: str,
    *,
    confirm_referenced: bool,
) -> DeletedMaterial | None:
    """Atomically detach assignment references and delete DB metadata.

    The returned storage key is deleted by the service/API only after this
    transaction commits, avoiding a live DB row that points to a missing object
    if the SQL mutation fails.
    """
    with session_scope() as session:
        row = session.execute(
            select(CourseMaterialRecord, KnowledgeDocumentRecord, StoredFileRecord)
            .join(
                KnowledgeDocumentRecord,
                and_(
                    KnowledgeDocumentRecord.id == CourseMaterialRecord.document_id,
                    KnowledgeDocumentRecord.owner_id == owner_id,
                ),
            )
            .outerjoin(
                StoredFileRecord,
                and_(
                    StoredFileRecord.id == KnowledgeDocumentRecord.stored_file_id,
                    StoredFileRecord.owner_id == owner_id,
                ),
            )
            .where(
                CourseMaterialRecord.id == material_id,
                CourseMaterialRecord.owner_id == owner_id,
            )
        ).first()
        if row is None:
            return None
        material, document, stored = row
        from backend.db.knowledge_repository import (
            assert_document_not_frozen_by_active_run,
        )

        assert_document_not_frozen_by_active_run(
            session, document_id=document.id, owner_id=owner_id,
        )
        reference_count = int(session.scalar(
            select(func.count(AssignmentKnowledgeDocumentRecord.assignment_id))
            .join(
                AssignmentRecord,
                AssignmentRecord.id == AssignmentKnowledgeDocumentRecord.assignment_id,
            )
            .where(
                AssignmentKnowledgeDocumentRecord.document_id == document.id,
                AssignmentRecord.teacher_id == owner_id,
            )
        ) or 0)
        if reference_count and not confirm_referenced:
            raise MaterialReferenced(reference_count)

        owned_assignments = select(AssignmentRecord.id).where(
            AssignmentRecord.teacher_id == owner_id
        )
        session.execute(
            delete(AssignmentKnowledgeDocumentRecord).where(
                AssignmentKnowledgeDocumentRecord.document_id == document.id,
                AssignmentKnowledgeDocumentRecord.assignment_id.in_(owned_assignments),
            )
        )
        # Break the deliberate KnowledgeDocument <-> StoredFile nullable cycle
        # before deleting both records so SQLite and PostgreSQL behave equally.
        if stored is not None:
            document.stored_file_id = None
            stored.knowledge_document_id = None
            session.flush()
        session.execute(delete(KnowledgeChunkRecord).where(
            KnowledgeChunkRecord.document_id == document.id
        ))
        session.execute(delete(CourseMaterialRecord).where(
            CourseMaterialRecord.id == material_id,
            CourseMaterialRecord.owner_id == owner_id,
        ))
        session.execute(delete(KnowledgeDocumentRecord).where(
            KnowledgeDocumentRecord.id == document.id,
            KnowledgeDocumentRecord.owner_id == owner_id,
        ))
        if stored is not None:
            session.execute(delete(StoredFileRecord).where(
                StoredFileRecord.id == stored.id,
                StoredFileRecord.owner_id == owner_id,
            ))
        return DeletedMaterial(
            material_id=material_id,
            document_id=document.id,
            storage_key=stored.storage_key if stored is not None else None,
            detached_references=reference_count,
        )
