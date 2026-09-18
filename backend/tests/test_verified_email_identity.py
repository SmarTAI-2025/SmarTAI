from __future__ import annotations

from fastapi.testclient import TestClient

from backend.auth import create_token, hash_password
from backend.db.models import UserRecord
from backend.db.session import session_scope
from backend.main import app


def test_existing_user_cannot_bypass_verification_by_patching_email():
    with session_scope() as session:
        session.add(UserRecord(
            id="verified-email-owner",
            username="verified-email-owner",
            email="owner@ustc.edu.cn",
            role="teacher",
            password_hash=hash_password("safe-password-123"),
            is_active=True,
        ))

    response = TestClient(app).patch(
        "/users/verified-email-owner",
        headers={
            "Authorization": f"Bearer {create_token('verified-email-owner', 'teacher')}"
        },
        json={"email": "unverified@mail.ustc.edu.cn"},
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "Email cannot be changed"}
    with session_scope() as session:
        assert session.get(UserRecord, "verified-email-owner").email == "owner@ustc.edu.cn"
