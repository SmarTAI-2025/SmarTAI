"""Owner-scoped normalized assignment tag API."""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from backend.auth import require_teacher
from backend.db import tag_repository
from backend.models import User
from backend.tools.catalog_matching import (
    CatalogMatch,
    match_catalog_items,
    normalize_catalog_text,
)


router = APIRouter(prefix="/tags", tags=["tags"])

TagColor = Literal["slate", "blue", "teal", "green", "amber", "rose", "violet"]


class CreateTagRequest(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    color: TagColor = "slate"
    force_create: bool = False


class UpdateTagRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=40)
    color: Optional[TagColor] = None


def normalize_tag_name(name: str) -> tuple[str, str]:
    """Return a Unicode-aware display name and owner-local de-duplication key."""
    display, normalized_name = normalize_catalog_text(name)
    if not display:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Tag name cannot be blank",
        )
    return display, normalized_name


def _owned_tag_or_404(tag_id: str, owner_id: str) -> tag_repository.Tag:
    tag = tag_repository.get_tag(tag_id, owner_id)
    # Owner is part of the SQL predicate: another owner's ID is indistinguishable
    # from an unknown ID and does not disclose that the row exists.
    if tag is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Tag not found")
    return tag


def _serialize_tag(tag: tag_repository.Tag) -> dict:
    return {**tag.public(), "usage_count": tag.usage_count}


def _serialize_match(match: CatalogMatch[tag_repository.Tag]) -> dict:
    return {
        "item": _serialize_tag(match.item),
        "match_kind": match.match_kind,
        "score": match.score,
        "reason": match.reason,
    }


@router.get("/")
def list_tags(current: User = Depends(require_teacher)):
    return [_serialize_tag(tag) for tag in tag_repository.list_tags(current.id)]


@router.get("/search")
def search_tags(
    q: str = Query(min_length=1, max_length=40),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current: User = Depends(require_teacher),
):
    matches = match_catalog_items(
        q,
        tag_repository.list_tags(current.id),
        fields_for_item=lambda tag: {"name": tag.name},
    )
    start = (page - 1) * page_size
    return {
        "items": [_serialize_match(match) for match in matches[start:start + page_size]],
        "total": len(matches),
        "page": page,
        "page_size": page_size,
    }


@router.post("/")
def create_tag(
    req: CreateTagRequest,
    current: User = Depends(require_teacher),
):
    name, normalized_name = normalize_tag_name(req.name)
    exact = tag_repository.find_by_normalized_name(current.id, normalized_name)
    if exact is not None:
        return {**_serialize_tag(exact), "created": False}

    matches = match_catalog_items(
        name,
        tag_repository.list_tags(current.id),
        fields_for_item=lambda tag: {"name": tag.name},
    )
    related = [match for match in matches if match.match_kind == "related"][:5]
    if related and not req.force_create:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "similar_items",
                "resource": "tag",
                "candidates": [_serialize_match(match) for match in related],
            },
        )
    tag, created = tag_repository.create_or_get_tag(
        owner_id=current.id,
        name=name,
        normalized_name=normalized_name,
        color=req.color,
    )
    return {**_serialize_tag(tag), "created": created}


@router.put("/{tag_id}")
def update_tag(
    tag_id: str,
    req: UpdateTagRequest,
    current: User = Depends(require_teacher),
):
    _owned_tag_or_404(tag_id, current.id)
    name = None
    normalized_name = None
    if "name" in req.model_fields_set and req.name is not None:
        name, normalized_name = normalize_tag_name(req.name)
    color = req.color if "color" in req.model_fields_set else None
    try:
        tag = tag_repository.update_tag(
            tag_id,
            current.id,
            name=name,
            normalized_name=normalized_name,
            color=color,
        )
    except tag_repository.DuplicateTagName as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "tag_name_exists", "tag_id": exc.tag_id},
        ) from exc
    if tag is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Tag not found")
    return _serialize_tag(tag)


@router.delete("/{tag_id}")
def delete_tag(
    tag_id: str,
    current: User = Depends(require_teacher),
):
    affected_tasks = tag_repository.delete_tag(tag_id, current.id)
    if affected_tasks is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Tag not found")
    return {
        "status": "success",
        "tag_id": tag_id,
        "affected_tasks": affected_tasks,
    }
