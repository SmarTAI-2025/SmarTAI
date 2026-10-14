"""Public HTTP routing must not revive a retired workflow in any environment."""
import pytest
from fastapi.testclient import TestClient

from backend.auth import get_optional_user
from backend.config import settings
from backend.main import create_app
from backend.models import User


RETIRED_REQUESTS = [
    ("GET", "/courses/old/students"),
    ("POST", "/courses/old/enroll"),
    ("GET", "/assignments"),
    ("POST", "/assignments"),
    ("POST", "/assignments/old/publish"),
    ("POST", "/assignments/old/close"),
    ("GET", "/assignments/old/questions"),
    ("POST", "/submissions/submit"),
    ("POST", "/submissions/upload"),
    ("POST", "/submissions/teacher-import"),
    ("POST", "/submissions/teacher-upload"),
    ("POST", "/submissions/detail/old/correct"),
    ("GET", "/submissions/assignment/old"),
    ("POST", "/grading-runs"),
    ("POST", "/grading-runs/old/release"),
    ("POST", "/grading-runs/results/old/review"),
    ("GET", "/results/assignment/old/me"),
    ("GET", "/results/assignment/old/summary"),
    ("POST", "/prob_preview/"),
    ("POST", "/hw_preview/"),
    ("POST", "/ai_grading/"),
    ("POST", "/auth/register"),
]


@pytest.mark.parametrize("environment", ["development", "test", "production"])
def test_retired_workflows_are_unmounted_for_every_role(monkeypatch, environment):
    monkeypatch.setattr(settings, "runtime_environment", environment)
    app = create_app()
    client = TestClient(app)
    paths = app.openapi()["paths"]
    retired_prefixes = ("/submissions", "/grading-runs", "/results", "/prob_preview", "/hw_preview", "/ai_grading")
    assert not any(path.startswith(retired_prefixes) for path in paths)
    assert "/courses/{course_id}/students" not in paths
    assert "/courses/{course_id}/enroll" not in paths
    # Assignment-scoped knowledge is a shared internal adapter, not an LMS flow.
    assert not any(path.startswith("/assignments") and "/knowledge" not in path for path in paths)
    assert "/tasks/{task_id}/result" in paths
    assert "/courses" in paths and "/courses/search" in paths
    assert "/auth/register/request" in paths

    for role in (None, "teacher", "student", "admin"):
        user = User(id=f"surface-{role}", username=f"surface-{role}", role=role, password_hash="unused") if role else None
        app.dependency_overrides[get_optional_user] = lambda: user
        for method, path in RETIRED_REQUESTS:
            response = client.request(method, path, json={})
            assert response.status_code == 404, (environment, role, method, path, response.text)


def test_public_course_labels_remain_teacher_only_and_omit_members():
    from backend.db.course_repository import create_course
    from backend.state import get_user_store

    teacher = User(id="surface-teacher", username="surface-teacher", role="teacher", password_hash="unused")
    student = User(id="surface-student", username="surface-student", role="student", password_hash="unused")
    get_user_store()[teacher.id] = teacher
    get_user_store()[student.id] = student
    course = create_course(teacher_id=teacher.id, name="Algebra", code="M101")
    app = create_app()
    client = TestClient(app)
    app.dependency_overrides[get_optional_user] = lambda: teacher
    for path in ("/courses", f"/courses/{course.id}", "/courses/search?q=Algebra"):
        response = client.get(path)
        assert response.status_code == 200, response.text
        assert "student_ids" not in response.text and "student_count" not in response.text
    created = client.post("/courses", json={"name": "Geometry"})
    assert created.status_code == 200 and created.json()["created"] is True
    assert "student_ids" not in created.json()
    similar = client.post("/courses", json={"name": "Algebra II"})
    assert similar.status_code == 409, similar.text
    assert "student_ids" not in similar.text and "student_count" not in similar.text
    assert client.delete(f"/courses/{created.json()['id']}").status_code == 200
    app.dependency_overrides[get_optional_user] = lambda: student
    assert client.get("/courses").status_code == 403
    assert client.get(f"/courses/{course.id}").status_code == 403
