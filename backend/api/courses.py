"""Courses API router — resource-oriented endpoints over the course service.

All authorization and transition logic lives in the service/repository; this
router only validates request shape, calls the service, and maps DomainError
to the stable ``{"error": {"code": ..., "message": ...}}`` envelope.
"""
from __future__ import annotations

from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from backend.auth import get_current_user, require_teacher
from backend.domain.errors import DomainError
from backend.api.errors import domain_error_response
from backend.models import User
from backend.services import courses as course_service
from backend.tools.catalog_matching import (
    CatalogMatch,
    match_catalog_items,
    normalize_catalog_text,
)

router = APIRouter(prefix="/courses", tags=["courses"])


class CreateCourseRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    code: str = Field(default="", max_length=80)
    description: str = Field(default="", max_length=4000)
    force_create: bool = False


class EnrollRequest(BaseModel):
    student_ids: List[str] = []


@router.post("")
def create_course(req: CreateCourseRequest, current: User = Depends(require_teacher)):
    name, normalized_name = normalize_catalog_text(req.name)
    code, normalized_code = normalize_catalog_text(req.code)
    if not name:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Course name cannot be blank",
        )
    try:
        owner_courses = course_service.list_courses_for(
            actor_id=current.id,
            role=current.role,
        )
        exact = next(
            (
                course
                for course in owner_courses
                if normalize_catalog_text(course.name)[1] == normalized_name
                or (
                    normalized_code
                    and normalize_catalog_text(course.code)[1] == normalized_code
                )
            ),
            None,
        )
        if exact is not None:
            return {**_serialize(exact), "created": False}

        related = [
            match
            for match in _merge_course_matches(name, code, owner_courses)
            if match.match_kind == "related"
        ][:5]
        if related and not req.force_create:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "code": "similar_items",
                    "resource": "course",
                    "candidates": [_serialize_match(match) for match in related],
                },
            )
        course = course_service.create_course(
            teacher_id=current.id,
            name=name,
            code=code,
            description=req.description,
        )
    except DomainError as exc:
        return domain_error_response(exc)
    return {**_serialize(course), "created": True}


@router.get("")
def list_courses(current: User = Depends(get_current_user)):
    try:
        items = course_service.list_courses_for(actor_id=current.id, role=current.role)
    except DomainError as exc:
        return domain_error_response(exc)
    return [_serialize(c) for c in items]


@router.get("/search")
def search_courses(
    q: str = Query(min_length=1, max_length=120),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current: User = Depends(require_teacher),
):
    try:
        owner_courses = course_service.list_courses_for(
            actor_id=current.id,
            role=current.role,
        )
    except DomainError as exc:
        return domain_error_response(exc)
    matches = match_catalog_items(q, owner_courses, fields_for_item=_course_fields)
    start = (page - 1) * page_size
    return {
        "items": [
            _serialize_match(match)
            for match in matches[start:start + page_size]
        ],
        "total": len(matches),
        "page": page,
        "page_size": page_size,
    }


@router.get("/{course_id}")
def get_course(course_id: str, current: User = Depends(get_current_user)):
    try:
        course = course_service.get_course(course_id=course_id, actor_id=current.id, role=current.role)
    except DomainError as exc:
        return domain_error_response(exc)
    return _serialize(course)


@router.get("/{course_id}/students")
def get_course_students(course_id: str, current: User = Depends(require_teacher)):
    try:
        course = course_service.get_course(course_id=course_id, actor_id=current.id, role=current.role)
    except DomainError as exc:
        return domain_error_response(exc)
    return [{"id": sid} for sid in course.student_ids]


@router.post("/{course_id}/enroll")
def enroll_students(course_id: str, req: EnrollRequest, current: User = Depends(require_teacher)):
    try:
        course = course_service.enroll_students(
            course_id=course_id, teacher_id=current.id, student_ids=req.student_ids
        )
    except DomainError as exc:
        return domain_error_response(exc)
    return {"status": "success", "student_ids": course.student_ids, "student_count": len(course.student_ids)}


@router.delete("/{course_id}")
def delete_course(course_id: str, current: User = Depends(require_teacher)):
    try:
        course_service.delete_course(course_id=course_id, teacher_id=current.id)
    except DomainError as exc:
        return domain_error_response(exc)
    return {"status": "success"}


def _serialize(c) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "code": c.code,
        "description": c.description,
        "teacher_id": c.teacher_id,
        "student_ids": list(c.student_ids),
        "student_count": len(c.student_ids),
        "created_at": c.created_at,
        "updated_at": c.updated_at,
    }


def _course_fields(course: Any) -> dict[str, str]:
    return {"name": course.name, "code": course.code}


def _serialize_match(match: CatalogMatch[Any]) -> dict:
    return {
        "item": _serialize(match.item),
        "match_kind": match.match_kind,
        "score": match.score,
        "reason": match.reason,
    }


def _merge_course_matches(
    name: str,
    code: str,
    courses: list[Any],
) -> list[CatalogMatch[Any]]:
    by_id: dict[str, CatalogMatch[Any]] = {}
    for query in (name, code):
        if not query:
            continue
        for match in match_catalog_items(query, courses, fields_for_item=_course_fields):
            current = by_id.get(match.item.id)
            if current is None or match.score > current.score:
                by_id[match.item.id] = match
    return sorted(
        by_id.values(),
        key=lambda match: (
            0 if match.match_kind == "exact" else 1,
            -match.score,
            normalize_catalog_text(match.item.name)[1],
            match.item.id,
        ),
    )
