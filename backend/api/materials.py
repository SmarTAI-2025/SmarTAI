"""Durable teacher-owned Course Library API.

Library rows classify canonical knowledge documents; upload bytes and parse
state remain in the existing StoredFile/object-storage and KnowledgeDocument
pipeline. Every repository read/write is owner-scoped in SQL.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field

from backend.auth import require_teacher
from backend.api.errors import domain_error_response
from backend.config import settings
from backend.db import course_library_repository as library_repo
from backend.knowledge.service import ingest_document
from backend.models import User
from backend.domain.errors import DomainError
from backend.storage import get_storage
from backend.tools.catalog_matching import match_catalog_items, normalize_catalog_text


router = APIRouter(prefix="/course-materials", tags=["course-materials"])

MaterialCategory = Literal["textbook", "answer", "lecture", "rubric", "other"]


class CreateMaterialGroupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    course_id: Optional[str] = None
    force_create: bool = False


class UpdateMaterialGroupRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    course_id: Optional[str] = None


class UpdateCourseMaterialRequest(BaseModel):
    filename: Optional[str] = Field(default=None, min_length=1, max_length=240)
    course_id: Optional[str] = None
    group_id: Optional[str] = None
    category: Optional[MaterialCategory] = None
    labels: Optional[list[str]] = Field(default=None, max_length=20)


def _owned_course_or_404(course_id: str, owner_id: str) -> library_repo.CourseRef:
    course = library_repo.get_owned_course(course_id, owner_id)
    if course is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Course not found")
    return course


def _owned_group_or_404(
    group_id: str, owner_id: str
) -> library_repo.CourseMaterialGroup:
    group = library_repo.get_group(group_id, owner_id)
    if group is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Material group not found")
    return group


def _owned_material_or_404(
    material_id: str, owner_id: str
) -> library_repo.CourseMaterial:
    material = library_repo.get_material(material_id, owner_id)
    if material is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Course material not found")
    return material


def _normalize_labels(values: list[str]) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()
    for value in values:
        display, key = normalize_catalog_text(str(value))
        if not display:
            continue
        if len(display) > 60:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "material_label_too_long", "max_length": 60},
            )
        if key in seen:
            continue
        labels.append(display)
        seen.add(key)
    if len(labels) > 20:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "too_many_material_labels", "max_items": 20},
        )
    return labels


def _parse_labels(raw: str) -> list[str]:
    value = raw.strip()
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = value.split(",")
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_material_labels"},
        )
    return _normalize_labels(parsed)


def _material_fields(material: library_repo.CourseMaterial) -> dict[str, str]:
    return {
        "filename": material.filename,
        "category": material.category,
        "labels": " ".join(material.labels),
        "group": material.group_name or "",
        "course": " ".join(filter(None, [material.course_name, material.course_code])),
    }


def _serialize_material(
    material: library_repo.CourseMaterial,
    *,
    match_kind: Optional[str] = None,
    match_score: Optional[float] = None,
    match_reason: Optional[str] = None,
) -> dict:
    return {
        **material.public(),
        "match_kind": match_kind,
        "match_score": match_score,
        "match_reason": match_reason,
    }


def _serialize_group(group: library_repo.CourseMaterialGroup) -> dict:
    return group.public()


@router.get("/")
def list_course_materials(
    q: str = Query(default="", max_length=200),
    course_id: Optional[str] = Query(default=None),
    group_id: Optional[str] = Query(default=None),
    category: Optional[MaterialCategory] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=100),
    current: User = Depends(require_teacher),
):
    if course_id is not None:
        _owned_course_or_404(course_id, current.id)
    if group_id not in (None, "ungrouped"):
        _owned_group_or_404(group_id, current.id)

    rows = library_repo.list_materials(
        current.id,
        course_id=course_id,
        group_id=group_id if group_id not in (None, "ungrouped") else None,
        ungrouped=group_id == "ungrouped",
        category=category,
    )
    query = q.strip()
    if query:
        matches = match_catalog_items(
            query,
            rows,
            fields_for_item=_material_fields,
        )
        result_rows = [
            _serialize_material(
                match.item,
                match_kind=match.match_kind,
                match_score=match.score,
                match_reason=match.reason,
            )
            for match in matches
        ]
    else:
        result_rows = [_serialize_material(item) for item in rows]

    total = len(result_rows)
    start = (page - 1) * page_size
    owner_all = library_repo.list_materials(current.id)
    groups = library_repo.list_groups(current.id)
    return {
        "items": result_rows[start:start + page_size],
        "total": total,
        "page": page,
        "page_size": page_size,
        "summary": {
            "materials": len(owner_all),
            "groups": len(groups),
            "referenced": sum(
                1 for item in owner_all if item.task_reference_count > 0
            ),
            "parsed": sum(1 for item in owner_all if item.parse_status == "ready"),
        },
        "storage": settings.storage_backend,
        "capabilities": {
            "durable": True,
            "ocr": False,
            "accepted_types": ["pdf", "docx", "pptx", "md", "txt", "rst"],
        },
    }


@router.post("/")
async def upload_course_material(
    file: UploadFile = File(...),
    course_id: Optional[str] = Form(default=None),
    group_id: Optional[str] = Form(default=None),
    category: MaterialCategory = Form(default="other"),
    labels: str = Form(default="[]"),
    current: User = Depends(require_teacher),
):
    group = None
    if course_id is not None:
        _owned_course_or_404(course_id, current.id)
    if group_id is not None:
        group = _owned_group_or_404(group_id, current.id)
        if course_id is None:
            course_id = group.course_id
        elif group.course_id is not None and group.course_id != course_id:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "material_group_course_mismatch"},
            )

    filename = Path(file.filename or "upload.bin").name.strip() or "upload.bin"
    if len(filename) > 240:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "material_filename_too_long", "max_length": 240},
        )
    normalized_labels = _parse_labels(labels)
    body = await file.read()
    try:
        document = await ingest_document(
            owner_id=current.id,
            original_name=filename,
            content=body,
            content_type=file.content_type,
            title=Path(filename).stem,
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_course_material", "message": str(exc)},
        ) from exc
    if document.status != "ready":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "knowledge_document_not_ready",
                "parse_status": document.status,
            },
        )
    try:
        material, created = library_repo.create_material(
            owner_id=current.id,
            document_id=document.id,
            filename=filename,
            category=category,
            labels=normalized_labels,
            course_id=course_id,
            group_id=group_id,
        )
    except library_repo.MaterialDocumentUnavailable as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "knowledge_document_not_ready", "parse_status": exc.status},
        ) from exc
    return {**_serialize_material(material), "created": created}


@router.get("/groups")
def list_material_groups(
    q: str = Query(default="", max_length=80),
    current: User = Depends(require_teacher),
):
    groups = library_repo.list_groups(current.id)
    query = q.strip()
    if not query:
        return {"items": [_serialize_group(item) for item in groups], "total": len(groups)}
    matches = match_catalog_items(
        query,
        groups,
        fields_for_item=lambda item: {"name": item.name},
    )
    return {
        "items": [
            {
                **_serialize_group(match.item),
                "match_kind": match.match_kind,
                "match_score": match.score,
                "match_reason": match.reason,
            }
            for match in matches
        ],
        "total": len(matches),
    }


@router.post("/groups")
def create_material_group(
    req: CreateMaterialGroupRequest,
    current: User = Depends(require_teacher),
):
    name, normalized_name = normalize_catalog_text(req.name)
    if not name:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Group name cannot be blank"
        )
    if req.course_id is not None:
        _owned_course_or_404(req.course_id, current.id)
    exact = library_repo.find_group_by_normalized_name(current.id, normalized_name)
    if exact is not None:
        return {**_serialize_group(exact), "created": False}
    owner_groups = library_repo.list_groups(current.id)
    related = [
        match for match in match_catalog_items(
            name,
            owner_groups,
            fields_for_item=lambda item: {"name": item.name},
        )
        if match.match_kind == "related"
    ][:5]
    if related and not req.force_create:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "similar_items",
                "resource": "course_material_group",
                "candidates": [
                    {
                        **_serialize_group(match.item),
                        "match_kind": match.match_kind,
                        "score": match.score,
                        "reason": match.reason,
                    }
                    for match in related
                ],
            },
        )
    group, created = library_repo.create_group(
        owner_id=current.id,
        name=name,
        normalized_name=normalized_name,
        course_id=req.course_id,
    )
    return {**_serialize_group(group), "created": created}


@router.put("/groups/{group_id}")
def update_material_group(
    group_id: str,
    req: UpdateMaterialGroupRequest,
    current: User = Depends(require_teacher),
):
    _owned_group_or_404(group_id, current.id)
    name = None
    normalized_name = None
    if "name" in req.model_fields_set and req.name is not None:
        name, normalized_name = normalize_catalog_text(req.name)
        if not name:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Group name cannot be blank",
            )
    set_course = "course_id" in req.model_fields_set
    if set_course and req.course_id is not None:
        _owned_course_or_404(req.course_id, current.id)
    try:
        group = library_repo.update_group(
            group_id,
            current.id,
            name=name,
            normalized_name=normalized_name,
            set_course=set_course,
            course_id=req.course_id,
        )
    except library_repo.DuplicateGroupName as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "material_group_name_exists", "group_id": exc.group_id},
        ) from exc
    if group is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Material group not found")
    return _serialize_group(group)


@router.delete("/groups/{group_id}")
def delete_material_group(
    group_id: str,
    current: User = Depends(require_teacher),
):
    affected = library_repo.delete_group(group_id, current.id)
    if affected is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Material group not found")
    return {
        "status": "success",
        "group_id": group_id,
        "moved_to_ungrouped": affected,
    }


@router.put("/{material_id}")
def update_course_material(
    material_id: str,
    req: UpdateCourseMaterialRequest,
    current: User = Depends(require_teacher),
):
    material = _owned_material_or_404(material_id, current.id)
    fields: dict = {}
    effective_course_id = (
        req.course_id if "course_id" in req.model_fields_set else material.course_id
    )
    effective_group_id = (
        req.group_id if "group_id" in req.model_fields_set else material.group_id
    )
    if effective_course_id is not None:
        _owned_course_or_404(effective_course_id, current.id)
    if effective_group_id is not None:
        group = _owned_group_or_404(effective_group_id, current.id)
        if effective_course_id is None:
            effective_course_id = group.course_id
        elif group.course_id is not None and group.course_id != effective_course_id:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "material_group_course_mismatch"},
            )
    if "filename" in req.model_fields_set and req.filename is not None:
        filename = Path(req.filename).name.strip()
        if not filename:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Filename cannot be blank"
            )
        fields["filename"] = filename
    if "course_id" in req.model_fields_set or (
        "group_id" in req.model_fields_set
        and req.group_id is not None
        and material.course_id is None
    ):
        fields["course_id"] = effective_course_id
    if "group_id" in req.model_fields_set:
        fields["group_id"] = req.group_id
    if "category" in req.model_fields_set and req.category is not None:
        fields["category"] = req.category
    if "labels" in req.model_fields_set and req.labels is not None:
        fields["labels"] = _normalize_labels(req.labels)
    try:
        updated = library_repo.update_material(
            material_id, current.id, **fields
        ) if fields else material
    except library_repo.GroupCourseMismatch as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "material_group_course_mismatch"},
        ) from exc
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Course material not found")
    return _serialize_material(updated)


@router.delete("/{material_id}")
def delete_course_material(
    material_id: str,
    confirm_referenced: bool = Query(default=False),
    current: User = Depends(require_teacher),
):
    try:
        deleted = library_repo.delete_material(
            material_id,
            current.id,
            confirm_referenced=confirm_referenced,
        )
    except library_repo.MaterialReferenced as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "course_material_is_referenced",
                "task_reference_count": exc.reference_count,
            },
        ) from exc
    except DomainError as exc:
        return domain_error_response(exc)
    if deleted is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Course material not found")
    if deleted.storage_key is not None:
        get_storage().delete(deleted.storage_key)
    return {
        "status": "success",
        "material_id": material_id,
        "detached_task_references": deleted.detached_references,
    }
