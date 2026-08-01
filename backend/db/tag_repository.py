"""Normalized, owner-scoped tags and assignment/tag associations."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from backend.db.models import AssignmentRecord, AssignmentTagRecord, TagRecord
from backend.db.session import session_scope
from backend.domain.errors import NotFound, ValidationError


class DuplicateTagName(ValueError):
    def __init__(self, tag_id: str):
        super().__init__("A tag with that name already exists")
        self.tag_id = tag_id


@dataclass(frozen=True)
class Tag:
    id: str
    name: str
    normalized_name: str
    color: str
    owner_id: str
    created_at: float
    updated_at: float
    usage_count: int = 0

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "normalized_name": self.normalized_name,
            "color": self.color,
            "owner_id": self.owner_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _usage_counts(session, owner_id: str, tag_ids: list[str]) -> dict[str, int]:
    if not tag_ids:
        return {}
    return {
        tag_id: int(count)
        for tag_id, count in session.execute(
            select(AssignmentTagRecord.tag_id, func.count(AssignmentTagRecord.assignment_id))
            .join(
                AssignmentRecord,
                AssignmentRecord.id == AssignmentTagRecord.assignment_id,
            )
            .where(
                AssignmentRecord.teacher_id == owner_id,
                AssignmentTagRecord.tag_id.in_(tag_ids),
            )
            .group_by(AssignmentTagRecord.tag_id)
        )
    }


def _tag(record: TagRecord, usage_count: int = 0) -> Tag:
    return Tag(
        id=record.id,
        name=record.name,
        normalized_name=record.normalized_name,
        color=record.color,
        owner_id=record.owner_id,
        created_at=record.created_at,
        updated_at=record.updated_at,
        usage_count=usage_count,
    )


def list_tags(owner_id: str) -> list[Tag]:
    with session_scope() as session:
        records = list(session.scalars(
            select(TagRecord)
            .where(TagRecord.owner_id == owner_id)
            .order_by(TagRecord.normalized_name, TagRecord.created_at)
        ))
        counts = _usage_counts(session, owner_id, [record.id for record in records])
        return [_tag(record, counts.get(record.id, 0)) for record in records]


def get_tag(tag_id: str, owner_id: str) -> Tag | None:
    with session_scope() as session:
        record = session.scalar(
            select(TagRecord).where(
                TagRecord.id == tag_id,
                TagRecord.owner_id == owner_id,
            )
        )
        if record is None:
            return None
        count = _usage_counts(session, owner_id, [record.id]).get(record.id, 0)
        return _tag(record, count)


def find_by_normalized_name(owner_id: str, normalized_name: str) -> Tag | None:
    with session_scope() as session:
        record = session.scalar(
            select(TagRecord).where(
                TagRecord.owner_id == owner_id,
                TagRecord.normalized_name == normalized_name,
            )
        )
        if record is None:
            return None
        count = _usage_counts(session, owner_id, [record.id]).get(record.id, 0)
        return _tag(record, count)


def create_or_get_tag(
    *, owner_id: str, name: str, normalized_name: str, color: str
) -> tuple[Tag, bool]:
    existing = find_by_normalized_name(owner_id, normalized_name)
    if existing is not None:
        return existing, False
    tag_id = f"tag_{uuid.uuid4().hex[:12]}"
    now = time.time()
    try:
        with session_scope() as session:
            session.add(TagRecord(
                id=tag_id,
                owner_id=owner_id,
                name=name,
                normalized_name=normalized_name,
                color=color,
                created_at=now,
                updated_at=now,
            ))
    except IntegrityError:
        existing = find_by_normalized_name(owner_id, normalized_name)
        if existing is not None:
            return existing, False
        raise
    created = get_tag(tag_id, owner_id)
    assert created is not None
    return created, True


def update_tag(
    tag_id: str,
    owner_id: str,
    *,
    name: str | None = None,
    normalized_name: str | None = None,
    color: str | None = None,
) -> Tag | None:
    try:
        with session_scope() as session:
            record = session.scalar(
                select(TagRecord).where(
                    TagRecord.id == tag_id,
                    TagRecord.owner_id == owner_id,
                )
            )
            if record is None:
                return None
            if normalized_name is not None:
                duplicate = session.scalar(
                    select(TagRecord).where(
                        TagRecord.owner_id == owner_id,
                        TagRecord.normalized_name == normalized_name,
                        TagRecord.id != tag_id,
                    )
                )
                if duplicate is not None:
                    raise DuplicateTagName(duplicate.id)
                record.name = name or record.name
                record.normalized_name = normalized_name
            if color is not None:
                record.color = color
            record.updated_at = time.time()
            session.flush()
    except IntegrityError:
        duplicate = (
            find_by_normalized_name(owner_id, normalized_name)
            if normalized_name is not None else None
        )
        if duplicate is not None and duplicate.id != tag_id:
            raise DuplicateTagName(duplicate.id) from None
        raise
    return get_tag(tag_id, owner_id)


def set_assignment_tags(
    *, assignment_id: str, owner_id: str, tag_ids: list[str]
) -> list[Tag]:
    """Replace one owner's assignment tag set in a single transaction."""
    unique_ids = list(dict.fromkeys(tag_ids))
    with session_scope() as session:
        assignment = session.scalar(
            select(AssignmentRecord).where(
                AssignmentRecord.id == assignment_id,
                AssignmentRecord.teacher_id == owner_id,
            )
        )
        if assignment is None:
            raise NotFound("assignment")
        if unique_ids:
            valid_ids = set(session.scalars(
                select(TagRecord.id).where(
                    TagRecord.owner_id == owner_id,
                    TagRecord.id.in_(unique_ids),
                )
            ))
            if valid_ids != set(unique_ids):
                raise ValidationError("tags_unavailable")
        session.execute(
            delete(AssignmentTagRecord).where(
                AssignmentTagRecord.assignment_id.in_(
                    select(AssignmentRecord.id).where(
                        AssignmentRecord.id == assignment_id,
                        AssignmentRecord.teacher_id == owner_id,
                    )
                )
            )
        )
        now = time.time()
        session.add_all([
            AssignmentTagRecord(
                assignment_id=assignment_id,
                tag_id=tag_id,
                assigned_at=now,
            )
            for tag_id in unique_ids
        ])
    by_id = {tag.id: tag for tag in list_tags(owner_id)}
    return [by_id[tag_id] for tag_id in unique_ids]


def list_assignment_tags(*, assignment_id: str, owner_id: str) -> list[Tag]:
    with session_scope() as session:
        assignment = session.scalar(
            select(AssignmentRecord.id).where(
                AssignmentRecord.id == assignment_id,
                AssignmentRecord.teacher_id == owner_id,
            )
        )
        if assignment is None:
            raise NotFound("assignment")
        records = list(session.scalars(
            select(TagRecord)
            .join(AssignmentTagRecord, AssignmentTagRecord.tag_id == TagRecord.id)
            .join(
                AssignmentRecord,
                AssignmentRecord.id == AssignmentTagRecord.assignment_id,
            )
            .where(
                AssignmentTagRecord.assignment_id == assignment_id,
                AssignmentRecord.teacher_id == owner_id,
                TagRecord.owner_id == owner_id,
            )
            .order_by(AssignmentTagRecord.assigned_at, TagRecord.id)
        ))
        counts = _usage_counts(session, owner_id, [record.id for record in records])
        return [_tag(record, counts.get(record.id, 0)) for record in records]


def list_assignment_tag_ids(*, assignment_id: str, owner_id: str) -> list[str]:
    """Return ordered tag IDs for the normalized task/assignment façade."""
    with session_scope() as session:
        assignment = session.scalar(
            select(AssignmentRecord.id).where(
                AssignmentRecord.id == assignment_id,
                AssignmentRecord.teacher_id == owner_id,
            )
        )
        if assignment is None:
            raise NotFound("assignment")
        return list(session.scalars(
            select(AssignmentTagRecord.tag_id)
            .join(TagRecord, TagRecord.id == AssignmentTagRecord.tag_id)
            .join(
                AssignmentRecord,
                AssignmentRecord.id == AssignmentTagRecord.assignment_id,
            )
            .where(
                AssignmentTagRecord.assignment_id == assignment_id,
                AssignmentRecord.teacher_id == owner_id,
                TagRecord.owner_id == owner_id,
            )
            .order_by(AssignmentTagRecord.assigned_at, AssignmentTagRecord.tag_id)
        ))


def usage_count(tag_id: str, owner_id: str | None = None) -> int:
    """Count owner-consistent assignment uses without trusting caller memory."""
    with session_scope() as session:
        if owner_id is None:
            resolved_owner = session.scalar(
                select(TagRecord.owner_id).where(TagRecord.id == tag_id)
            )
        else:
            resolved_owner = session.scalar(
                select(TagRecord.owner_id).where(
                    TagRecord.id == tag_id,
                    TagRecord.owner_id == owner_id,
                )
            )
        if resolved_owner is None:
            return 0
        owned_assignments = select(AssignmentRecord.id).where(
            AssignmentRecord.teacher_id == resolved_owner
        )
        return int(session.scalar(
            select(func.count(AssignmentTagRecord.assignment_id)).where(
                AssignmentTagRecord.tag_id == tag_id,
                AssignmentTagRecord.assignment_id.in_(owned_assignments),
            )
        ) or 0)


def serialize_tag(tag: Tag, *, usage_count: int | None = None) -> dict[str, Any]:
    """Compatibility serializer used by task-history facet adapters."""
    return {
        **tag.public(),
        "usage_count": tag.usage_count if usage_count is None else usage_count,
    }


def delete_tag(tag_id: str, owner_id: str) -> int | None:
    """Delete a tag and atomically detach it from the owner's assignments."""
    with session_scope() as session:
        record = session.scalar(
            select(TagRecord).where(
                TagRecord.id == tag_id,
                TagRecord.owner_id == owner_id,
            )
        )
        if record is None:
            return None
        owned_assignment_ids = select(AssignmentRecord.id).where(
            AssignmentRecord.teacher_id == owner_id
        )
        affected = int(session.scalar(
            select(func.count(AssignmentTagRecord.assignment_id)).where(
                AssignmentTagRecord.tag_id == tag_id,
                AssignmentTagRecord.assignment_id.in_(owned_assignment_ids),
            )
        ) or 0)
        session.execute(
            delete(AssignmentTagRecord).where(
                AssignmentTagRecord.tag_id == tag_id,
                AssignmentTagRecord.assignment_id.in_(owned_assignment_ids),
            )
        )
        session.delete(record)
        return affected
