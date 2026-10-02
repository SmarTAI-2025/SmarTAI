"""Actual export HTTP paths protected by the suite's multi-layer zero-OCR guard."""
from fastapi.testclient import TestClient

from backend.auth import create_token
from backend.main import app
from backend.tests.test_task_finalization_contract import _prepared_task


def test_finalize_generate_download_and_refresh_never_recognize_again():
    seeded = _prepared_task()
    client = TestClient(app)
    base = "/tasks/" + seeded["task_id"]
    headers = {"Authorization":"Bearer " + create_token(seeded["owner_id"], "teacher")}
    confirmed = client.post(base + "/finalization/confirm", headers=headers, json={"expected_workflow_revision":0})
    assert confirmed.status_code == 200, confirmed.text
    generated = client.post(base + "/artifacts/generate", headers=headers,
        json={"expected_workflow_revision":confirmed.json()["workflow_revision"]})
    assert generated.status_code == 200, generated.text
    for _ in range(2):
        assert client.get(base + "/artifacts", headers=headers).status_code == 200
        downloaded = client.get(base + "/artifacts/1/bundle", headers=headers)
        assert downloaded.status_code == 200 and downloaded.content.startswith(b"PK")
        assert client.get(base + "/state", headers=headers).status_code == 200
        assert client.get(base + "/result", headers=headers).status_code == 200
    assert client.get(base + "/artifacts/1/bundle").status_code == 401


def test_course_material_capabilities_match_public_upload_scope():
    from backend.tests.test_knowledge_ingestion import owner
    from backend.knowledge.service import KNOWLEDGE_UPLOAD_EXTENSIONS
    who = owner()
    result = TestClient(app).get("/course-materials/", headers={"Authorization":"Bearer " + create_token(who, "teacher")})
    assert result.status_code == 200
    assert set(result.json()["capabilities"]["accepted_types"]) == {ext[1:] for ext in KNOWLEDGE_UPLOAD_EXTENSIONS}
