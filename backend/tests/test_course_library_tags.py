from __future__ import annotations

import time
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.api import materials, tags
from backend.auth import require_teacher
from backend.db import course_library_repository, grading_repository, tag_repository
from backend.db.knowledge_repository import set_task_documents
from backend.db.models import (
    AssignmentKnowledgeDocumentRecord,
    AssignmentRecord,
    AssignmentTagRecord,
    CourseMaterialRecord,
    CourseRecord,
    KnowledgeDocumentRecord,
    StoredFileRecord,
    UserRecord,
)
from backend.db.session import session_scope
from backend.models import User
from backend.storage import get_storage


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _teacher() -> User:
    suffix = uuid.uuid4().hex[:12]
    user = User(
        id=f"teacher_{suffix}",
        username=f"teacher-{suffix}",
        email=f"teacher-{suffix}@example.test",
        role="teacher",
        password_hash="test",
    )
    now = time.time()
    with session_scope() as session:
        session.add(UserRecord(
            id=user.id,
            username=user.username,
            email=user.email,
            role=user.role,
            password_hash=user.password_hash,
            is_active=True,
            created_at=now,
            updated_at=now,
        ))
    return user


def _course_and_assignment(owner: User) -> tuple[str, str]:
    course_id = _id("course")
    assignment_id = _id("assignment")
    now = time.time()
    with session_scope() as session:
        session.add(CourseRecord(
            id=course_id,
            name="Linear Algebra",
            code="MATH-201",
            description="",
            teacher_id=owner.id,
            created_at=now,
            updated_at=now,
        ))
        session.flush()
        session.add(AssignmentRecord(
            id=assignment_id,
            course_id=course_id,
            teacher_id=owner.id,
            name="Problem Set 1",
            description="",
            status="draft",
            created_at=now,
            updated_at=now,
            version=1,
        ))
    return course_id, assignment_id


def _client(owner: User, *, include_materials: bool = True, include_tags: bool = True) -> TestClient:
    app = FastAPI()
    if include_materials:
        app.include_router(materials.router)
    if include_tags:
        app.include_router(tags.router)
    app.dependency_overrides[require_teacher] = lambda: owner
    return TestClient(app)


def test_course_library_uses_canonical_file_storage_and_guards_references():
    owner = _teacher()
    other = _teacher()
    course_id, assignment_id = _course_and_assignment(owner)
    owner_client = _client(owner, include_tags=False)
    other_client = _client(other, include_tags=False)

    group_response = owner_client.post(
        "/course-materials/groups",
        json={"name": "Week 1", "course_id": course_id},
    )
    assert group_response.status_code == 200
    group = group_response.json()
    assert group["created"] is True

    body = b"Vectors span a subspace. Basis vectors are linearly independent."
    upload = owner_client.post(
        "/course-materials/",
        data={
            "group_id": group["group_id"],
            "category": "lecture",
            "labels": '["Vectors", " vectors ", "Week 1"]',
        },
        files={"file": ("notes.txt", body, "text/plain")},
    )
    assert upload.status_code == 200, upload.text
    material = upload.json()
    assert material["created"] is True
    assert material["course_id"] == course_id
    assert material["labels"] == ["Vectors", "Week 1"]
    assert material["parse_status"] == "ready"

    persisted = course_library_repository.get_material(material["material_id"], owner.id)
    assert persisted is not None
    assert persisted.stored_file_id is not None
    assert persisted.storage_key is not None
    assert get_storage().exists(persisted.storage_key)
    with session_scope() as session:
        assert session.get(KnowledgeDocumentRecord, persisted.document_id) is not None
        assert session.get(StoredFileRecord, persisted.stored_file_id) is not None
        assert session.get(CourseMaterialRecord, persisted.material_id) is not None

    listing = owner_client.get("/course-materials/")
    assert listing.status_code == 200
    assert listing.json()["capabilities"]["durable"] is True
    assert listing.json()["storage"] in {"local", "object"}
    assert other_client.get("/course-materials/").json()["items"] == []

    # Exact owner-local content is idempotent and keeps the first metadata row.
    duplicate = owner_client.post(
        "/course-materials/",
        data={"category": "answer"},
        files={"file": ("renamed.txt", body, "text/plain")},
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["created"] is False
    assert duplicate.json()["material_id"] == material["material_id"]

    # The same bytes belong to a different owner's document namespace and must
    # neither collide at the PK nor disclose/reuse the first owner's row.
    other_upload = other_client.post(
        "/course-materials/",
        data={"category": "other"},
        files={"file": ("notes.txt", body, "text/plain")},
    )
    assert other_upload.status_code == 200, other_upload.text
    other_persisted = course_library_repository.get_material(
        other_upload.json()["material_id"], other.id
    )
    assert other_persisted is not None
    assert other_persisted.document_id != persisted.document_id

    set_task_documents(
        assignment_id=assignment_id,
        owner_id=owner.id,
        document_ids=[persisted.document_id],
    )
    referenced = owner_client.get("/course-materials/").json()["items"][0]
    assert referenced["task_reference_count"] == 1
    assert referenced["last_used_at"] is not None

    run = grading_repository.create_run_bundle(
        assignment_id,
        teacher_id=owner.id,
        revision_ids=[],
        setup={},
        setup_fingerprint="course-library-frozen-kb",
        input_manifest={"knowledge_document_ids": [persisted.document_id]},
    )
    frozen_guard = owner_client.delete(
        f"/course-materials/{persisted.material_id}",
        params={"confirm_referenced": "true"},
    )
    assert frozen_guard.status_code == 409
    assert frozen_guard.json()["error"]["code"] == (
        "knowledge_document_in_active_grading_run"
    )
    grading_repository.cancel(run.id, teacher_id=owner.id)

    guarded = owner_client.delete(f"/course-materials/{persisted.material_id}")
    assert guarded.status_code == 409
    assert guarded.json()["detail"] == {
        "code": "course_material_is_referenced",
        "task_reference_count": 1,
    }
    deleted = owner_client.delete(
        f"/course-materials/{persisted.material_id}",
        params={"confirm_referenced": "true"},
    )
    assert deleted.status_code == 200
    assert deleted.json()["detached_task_references"] == 1
    assert not get_storage().exists(persisted.storage_key)
    with session_scope() as session:
        assert session.get(CourseMaterialRecord, persisted.material_id) is None
        assert session.get(KnowledgeDocumentRecord, persisted.document_id) is None
        assert session.get(StoredFileRecord, persisted.stored_file_id) is None
        assert session.scalar(select(AssignmentKnowledgeDocumentRecord).where(
            AssignmentKnowledgeDocumentRecord.assignment_id == assignment_id,
            AssignmentKnowledgeDocumentRecord.document_id == persisted.document_id,
        )) is None


def test_normalized_tags_are_owner_scoped_unique_and_detach_on_delete():
    owner = _teacher()
    other = _teacher()
    _, assignment_id = _course_and_assignment(owner)
    owner_client = _client(owner, include_materials=False)
    other_client = _client(other, include_materials=False)

    created = owner_client.post(
        "/tags/", json={"name": "  Algebra  ", "color": "blue"}
    )
    assert created.status_code == 200
    algebra = created.json()
    assert algebra["created"] is True
    assert algebra["name"] == "Algebra"
    assert algebra["normalized_name"] == "algebra"

    normalized_duplicate = owner_client.post(
        "/tags/", json={"name": "Ａｌｇｅｂｒａ", "color": "rose"}
    )
    assert normalized_duplicate.status_code == 200
    assert normalized_duplicate.json()["created"] is False
    assert normalized_duplicate.json()["id"] == algebra["id"]

    other_same_name = other_client.post(
        "/tags/", json={"name": "Algebra", "color": "green"}
    )
    assert other_same_name.status_code == 200
    assert other_same_name.json()["id"] != algebra["id"]

    geometry = owner_client.post(
        "/tags/", json={"name": "Geometry", "color": "teal"}
    ).json()
    conflict = owner_client.put(
        f"/tags/{geometry['id']}", json={"name": " algebra "}
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == {
        "code": "tag_name_exists",
        "tag_id": algebra["id"],
    }
    # A non-owner sees a 404-shaped response, not the existence of the row.
    assert other_client.put(
        f"/tags/{algebra['id']}", json={"color": "amber"}
    ).status_code == 404

    selected = tag_repository.set_assignment_tags(
        assignment_id=assignment_id,
        owner_id=owner.id,
        tag_ids=[algebra["id"], algebra["id"]],
    )
    assert [tag.id for tag in selected] == [algebra["id"]]
    assert tag_repository.list_assignment_tag_ids(
        assignment_id=assignment_id, owner_id=owner.id
    ) == [algebra["id"]]
    assert tag_repository.usage_count(algebra["id"]) == 1
    assert tag_repository.serialize_tag(selected[0], usage_count=1)["usage_count"] == 1
    listed = owner_client.get("/tags/").json()
    listed_algebra = next(tag for tag in listed if tag["id"] == algebra["id"])
    assert listed_algebra["usage_count"] == 1

    deleted = owner_client.delete(f"/tags/{algebra['id']}")
    assert deleted.status_code == 200
    assert deleted.json()["affected_tasks"] == 1
    assert tag_repository.get_tag(algebra["id"], owner.id) is None
    with session_scope() as session:
        assert session.scalar(select(AssignmentTagRecord).where(
            AssignmentTagRecord.assignment_id == assignment_id,
            AssignmentTagRecord.tag_id == algebra["id"],
        )) is None
    assert tag_repository.get_tag(other_same_name.json()["id"], other.id) is not None
