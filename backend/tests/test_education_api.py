"""Current course-label API authorization; retired enrollment routes stay absent."""
from __future__ import annotations

from fastapi.testclient import TestClient

from backend.auth import hash_password
from backend.main import app
from backend.models import User
from backend.state import get_user_store


def _seed(username: str, role: str) -> str:
    uid = f"u_{username}"
    get_user_store()[uid] = User(id=uid, username=username, role=role, password_hash=hash_password("pass-pass"))
    return uid


def _login(client: TestClient, username: str) -> str:
    resp = client.post("/auth/login", json={"username": username, "password": "pass-pass"})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _teacher_client() -> tuple[TestClient, str]:
    _seed("t_api", "teacher")
    c = TestClient(app)
    return c, _login(c, "t_api")


def _other_teacher_client() -> tuple[TestClient, str]:
    _seed("t_other_api", "teacher")
    c = TestClient(app)
    return c, _login(c, "t_other_api")


def _student_client() -> tuple[TestClient, str]:
    _seed("s_api", "student")
    c = TestClient(app)
    return c, _login(c, "s_api")


# ─── course ───────────────────────────────────────────────────────────────────


def test_teacher_creates_course():
    c, tok = _teacher_client()
    resp = c.post("/courses", headers=_headers(tok), json={"name": "Algebra", "code": "M101"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "Algebra"
    assert body["teacher_id"].startswith("u_")
    assert "student_ids" not in body and "student_count" not in body


def test_student_cannot_create_course():
    c, tok = _student_client()
    resp = c.post("/courses", headers=_headers(tok), json={"name": "x"})
    assert resp.status_code == 403


def test_other_teacher_gets_404_for_course_without_leak():
    c, tok = _teacher_client()
    course = c.post("/courses", headers=_headers(tok), json={"name": "Private"}).json()
    c2, tok2 = _other_teacher_client()
    resp = c2.get(f"/courses/{course['id']}", headers=_headers(tok2))
    assert resp.status_code == 404
    assert resp.json().get("error", {}).get("code") == "not_found"
