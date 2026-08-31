import pytest
from fastapi.testclient import TestClient


def test_task_kb_save_to_library_and_attachment_provenance_persist():
    from backend.auth import create_token
    from backend.db import course_library_repository
    from backend.db.models import UserRecord
    from backend.db.session import session_scope
    from backend.main import app
    from backend.services import task_facade

    owner_id = "demo_task-kb-owner"
    with session_scope() as session:
        session.add(UserRecord(
            id=owner_id, username="task-kb-owner", password_hash="hash",
            role="teacher", is_active=True,
        ))
    task = task_facade.create_task(
        owner_id=owner_id,
        name="KB Contract",
        semester_id=None,
        course_id=None,
        idempotency_key="task-kb-contract",
    )
    task_id = task["task_id"]
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {create_token(owner_id, 'teacher')}"}

    uploaded = client.post(
        f"/tasks/{task_id}/kb",
        headers=headers,
        data={
            "save_to_library": "true",
            "expected_workflow_revision": str(task["workflow_revision"]),
        },
        files={"file": ("teacher-notes.txt", b"frozen knowledge", "text/plain")},
    )
    assert uploaded.status_code == 200, uploaded.text
    payload = uploaded.json()
    assert payload["source_kind"] == "upload"
    assert payload["saved_to_library"] is True
    assert payload["saved_material_id"] == payload["library_material_id"]
    assert payload["saved_material_created"] is True
    material = course_library_repository.get_material(
        payload["library_material_id"], owner_id
    )
    assert material is not None
    assert material.document_id == payload["doc_id"]

    listed = client.get(f"/tasks/{task_id}/kb", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["docs"] == [{
        "doc_id": payload["doc_id"],
        "filename": "teacher-notes.txt",
        "chunk_count": 1,
        "uploaded_at": listed.json()["docs"][0]["uploaded_at"],
        "source_kind": "upload",
        "library_material_id": payload["library_material_id"],
        "saved_to_library": True,
    }]

    removed = client.delete(
        f"/tasks/{task_id}/kb/{payload['doc_id']}",
        headers=headers,
        params={"expected_workflow_revision": payload["workflow_revision"]},
    )
    assert removed.status_code == 200
    attached = client.post(
        f"/tasks/{task_id}/kb",
        headers=headers,
        data={
            "library_material_id": payload["library_material_id"],
            "expected_workflow_revision": str(
                removed.json()["workflow_revision"]
            ),
        },
    )
    assert attached.status_code == 200, attached.text
    assert attached.json()["source_kind"] == "library"
    relisted = client.get(f"/tasks/{task_id}/kb", headers=headers).json()
    assert relisted["docs"][0]["source_kind"] == "library"
    assert relisted["docs"][0]["library_material_id"] == payload[
        "library_material_id"
    ]


def test_task_only_upload_is_hidden_globally_and_same_hash_promotes_retained():
    """Task-only knowledge stays task-scoped until an explicit library upload.

    A later personal-library upload of identical bytes promotes the canonical
    owner/hash document in place.  Detaching it from the task must therefore
    retain the promoted document rather than enqueue task-only cleanup.
    """
    from backend.auth import create_token
    from backend.db.models import UserRecord
    from backend.db.session import session_scope
    from backend.main import app
    from backend.services import task_facade

    owner_id = "demo_task-kb-promotion-owner"
    with session_scope() as session:
        session.add(UserRecord(
            id=owner_id,
            username="task-kb-promotion-owner",
            password_hash="hash",
            role="teacher",
            is_active=True,
        ))
    task = task_facade.create_task(
        owner_id=owner_id,
        name="Task-only KB promotion",
        semester_id=None,
        course_id=None,
        idempotency_key="task-kb-promotion",
    )
    task_id = task["task_id"]
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {create_token(owner_id, 'teacher')}"}
    content = b"same owner knowledge promoted without a second object"

    task_upload = client.post(
        f"/tasks/{task_id}/kb",
        headers=headers,
        data={
            "save_to_library": "false",
            "expected_workflow_revision": str(task["workflow_revision"]),
        },
        files={"file": ("task-only.txt", content, "text/plain")},
    )
    assert task_upload.status_code == 200, task_upload.text
    task_payload = task_upload.json()
    assert task_payload["saved_to_library"] is False
    assert client.get(f"/tasks/{task_id}/kb", headers=headers).json()["docs"][0][
        "doc_id"
    ] == task_payload["doc_id"]
    assert client.get("/knowledge/documents", headers=headers).json()[
        "documents"
    ] == []
    assert client.get(
        f"/knowledge/documents/{task_payload['doc_id']}", headers=headers
    ).status_code == 404
    assert client.get(
        f"/knowledge/documents/{task_payload['doc_id']}/download", headers=headers
    ).status_code == 404
    assert client.get("/course-materials/", headers=headers).json()["items"] == []

    promoted = client.post(
        "/knowledge/documents",
        headers=headers,
        files={"file": ("personal-copy.txt", content, "text/plain")},
    )
    assert promoted.status_code == 201, promoted.text
    assert promoted.json()["id"] == task_payload["doc_id"]
    assert [
        item["id"]
        for item in client.get("/knowledge/documents", headers=headers).json()[
            "documents"
        ]
    ] == [task_payload["doc_id"]]

    removed = client.delete(
        f"/tasks/{task_id}/kb/{task_payload['doc_id']}",
        headers=headers,
        params={
            "expected_workflow_revision": task_payload["workflow_revision"],
        },
    )
    assert removed.status_code == 200, removed.text
    detail = client.get(
        f"/knowledge/documents/{task_payload['doc_id']}", headers=headers
    )
    assert detail.status_code == 200


def test_task_only_document_cleanup_waits_for_the_last_task_reference():
    from backend.auth import create_token
    from backend.db.knowledge_storage_repository import get_document_storage
    from backend.db.models import UserRecord
    from backend.db.session import session_scope
    from backend.domain.knowledge_storage import (
        KNOWLEDGE_CLEANUP_TASK_UNREFERENCED,
    )
    from backend.main import app
    from backend.services import task_facade

    owner_id = "demo_task-kb-last-reference-owner"
    with session_scope() as session:
        session.add(UserRecord(
            id=owner_id,
            username="task-kb-last-reference-owner",
            password_hash="hash",
            role="teacher",
            is_active=True,
        ))
    tasks = [
        task_facade.create_task(
            owner_id=owner_id,
            name=f"Task-only reference {index}",
            semester_id=None,
            course_id=None,
            idempotency_key=f"task-kb-last-reference-{index}",
        )
        for index in (1, 2)
    ]
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {create_token(owner_id, 'teacher')}"}
    content = b"one task-only document selected by two tasks"

    uploads = []
    for task in tasks:
        response = client.post(
            f"/tasks/{task['task_id']}/kb",
            headers=headers,
            data={
                "save_to_library": "false",
                "expected_workflow_revision": str(task["workflow_revision"]),
            },
            files={"file": ("shared-task-only.txt", content, "text/plain")},
        )
        assert response.status_code == 200, response.text
        uploads.append(response.json())
    assert uploads[0]["doc_id"] == uploads[1]["doc_id"]
    document_id = uploads[0]["doc_id"]
    available = get_document_storage(document_id, owner_id)
    assert available is not None
    assert available.retention_policy == "task_only"
    assert available.state == "available"

    first_removed = client.delete(
        f"/tasks/{tasks[0]['task_id']}/kb/{document_id}",
        headers=headers,
        params={
            "expected_workflow_revision": uploads[0]["workflow_revision"],
        },
    )
    assert first_removed.status_code == 200, first_removed.text
    still_available = get_document_storage(document_id, owner_id)
    assert still_available is not None
    assert still_available.state == "available"
    assert client.get(
        f"/tasks/{tasks[1]['task_id']}/kb", headers=headers
    ).json()["docs"][0]["doc_id"] == document_id

    last_removed = client.delete(
        f"/tasks/{tasks[1]['task_id']}/kb/{document_id}",
        headers=headers,
        params={
            "expected_workflow_revision": uploads[1]["workflow_revision"],
        },
    )
    assert last_removed.status_code == 200, last_removed.text
    pending = get_document_storage(document_id, owner_id)
    assert pending is not None
    assert pending.state == "cleanup_pending"
    assert pending.cleanup_reason == KNOWLEDGE_CLEANUP_TASK_UNREFERENCED
    assert pending.cleanup_operation_id


def test_failed_task_only_attachment_enqueues_cleanup_instead_of_leaking_quota():
    import hashlib

    from backend.auth import create_token
    from backend.db.knowledge_repository import (
        get_document_by_hash,
        set_task_documents,
    )
    from backend.db.knowledge_storage_repository import get_document_storage
    from backend.db.models import UserRecord
    from backend.db.session import session_scope
    from backend.domain.knowledge_storage import (
        KNOWLEDGE_CLEANUP_TASK_ATTACH_FAILED,
    )
    from backend.main import app
    from backend.services import task_facade

    owner_id = "demo_task-kb-attach-failure-owner"
    with session_scope() as session:
        session.add(UserRecord(
            id=owner_id,
            username="task-kb-attach-failure-owner",
            password_hash="hash",
            role="teacher",
            is_active=True,
        ))
    task = task_facade.create_task(
        owner_id=owner_id,
        name="Task-only attach failure",
        semester_id=None,
        course_id=None,
        idempotency_key="task-kb-attach-failure",
    )
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {create_token(owner_id, 'teacher')}"}
    retained_ids = []
    for index in range(3):
        uploaded = client.post(
            "/knowledge/documents",
            headers=headers,
            files={
                "file": (
                    f"retained-{index}.txt",
                    f"retained knowledge {index}".encode(),
                    "text/plain",
                )
            },
        )
        assert uploaded.status_code == 201, uploaded.text
        retained_ids.append(uploaded.json()["id"])
    set_task_documents(
        assignment_id=task["task_id"],
        owner_id=owner_id,
        document_ids=retained_ids,
    )

    orphan_body = b"fourth task-only attachment cannot fit the selection"
    failed = client.post(
        f"/tasks/{task['task_id']}/kb",
        headers=headers,
        data={
            "save_to_library": "false",
            "expected_workflow_revision": str(task["workflow_revision"]),
        },
        files={"file": ("orphan.txt", orphan_body, "text/plain")},
    )
    assert failed.status_code == 400, failed.text
    document = get_document_by_hash(
        owner_id, hashlib.sha256(orphan_body).hexdigest()
    )
    assert document is not None
    pending = get_document_storage(document.id, owner_id)
    assert pending is not None
    assert pending.state == "cleanup_pending"
    assert pending.cleanup_reason == KNOWLEDGE_CLEANUP_TASK_ATTACH_FAILED


def test_tombstoned_task_cannot_late_attach_and_cancel_task_only_grace(tmp_path):
    from backend.db.knowledge_repository import set_task_documents
    from backend.db.knowledge_storage_repository import get_document_storage
    from backend.db.models import UserRecord
    from backend.db.session import session_scope
    from backend.services import task_facade
    from backend.services.knowledge_storage import persist_knowledge_upload
    from backend.storage.local import LocalStorage

    owner_id = "task-kb-tombstone-owner"
    with session_scope() as session:
        session.add(UserRecord(
            id=owner_id,
            username=owner_id,
            password_hash="hash",
            role="teacher",
            is_active=True,
        ))
    task = task_facade.create_task(
        owner_id=owner_id,
        name="Tombstoned task knowledge",
        semester_id=None,
        course_id=None,
        idempotency_key="task-kb-tombstone",
    )
    upload = persist_knowledge_upload(
        storage=LocalStorage(tmp_path / "tombstoned-task-knowledge"),
        owner_id=owner_id,
        original_name="late.txt",
        content=b"late task-only upload",
        content_type="text/plain",
        retention_policy="task_only",
        origin_assignment_id=task["task_id"],
    )
    before = get_document_storage(upload.document_id, owner_id)
    assert before is not None
    assert before.unattached_expires_at is not None

    task_facade.delete_task(task_id=task["task_id"], owner_id=owner_id)
    with pytest.raises(ValueError, match="Assignment not found"):
        set_task_documents(
            assignment_id=task["task_id"],
            owner_id=owner_id,
            document_ids=[upload.document_id],
        )

    after = get_document_storage(upload.document_id, owner_id)
    assert after is not None
    assert after.state == "available"
    assert after.unattached_expires_at == before.unattached_expires_at
