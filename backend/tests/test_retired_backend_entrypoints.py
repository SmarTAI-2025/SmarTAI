"""Retired operations must be unreachable even without using the frontend."""
import pytest
from fastapi.testclient import TestClient

from backend.main import app


@pytest.fixture(params=["public", "private"])
def surface(request, monkeypatch, tmp_path):
    if request.param == "public":
        return TestClient(app), ""
    monkeypatch.setenv("SMARTAI_ADMIN_PRIVATE_ENABLED", "true")
    monkeypatch.setenv("SMARTAI_ADMIN_DIST", str(tmp_path))
    (tmp_path / "admin.html").write_text("<html>synthetic admin</html>")
    from backend.private_main import create_private_app
    return TestClient(create_private_app()), "/api"


@pytest.mark.parametrize("method,path", [
    ("POST", "/tasks/retired-task/extract_problems"),
    ("POST", "/tasks/retired-task/problem-sources/preflight"),
    ("GET", "/analytics/retired-task/per_question/Q1"),
    ("DELETE", "/analytics/retired-task/cache"),
    ("DELETE", "/analytics/retired-task/per_question/Q1/cache"),
    ("PATCH", "/admin/users/retired-user/status"),
    ("GET", "/admin/metrics/catalog"),
    ("POST", "/admin/metrics/query"),
    ("POST", "/knowledge/documents"),
    ("GET", "/knowledge/documents"),
    ("GET", "/knowledge/documents/retired-document"),
    ("DELETE", "/knowledge/documents/retired-document"),
    ("GET", "/knowledge/documents/retired-document/download"),
    ("GET", "/assignments/retired-task/knowledge-documents"),
    ("PUT", "/assignments/retired-task/knowledge-documents"),
])
@pytest.mark.parametrize("authorization", [None, "Bearer invalid-credential"])
def test_retired_backend_request_has_no_handler(surface, method, path, authorization):
    headers = {"Authorization": authorization} if authorization else {}
    client, prefix = surface
    response = client.request(method, prefix + path, headers=headers)
    assert response.status_code in {404, 405}, response.text
