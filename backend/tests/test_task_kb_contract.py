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
