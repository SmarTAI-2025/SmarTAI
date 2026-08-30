from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.main import app


@pytest.mark.parametrize(
    ("path", "payload", "secret"),
    [
        (
            "/auth/register/request",
            {
                "username": "validation-user",
                "email": "validation@ustc.edu.cn",
                "password": "p" * 129,
            },
            "p" * 129,
        ),
        (
            "/auth/register/verify",
            {"token": "registration-token-secret-" + "x" * 512},
            "registration-token-secret-" + "x" * 512,
        ),
        (
            "/auth/password-reset/confirm",
            {"token": "valid-shape", "new_password": "n" * 129},
            "n" * 129,
        ),
    ],
    ids=["registration-password", "registration-token", "reset-password"],
)
def test_auth_validation_errors_never_echo_sensitive_input(
    path: str,
    payload: dict[str, str],
    secret: str,
):
    response = TestClient(app).post(path, json=payload)

    assert response.status_code == 422
    assert secret not in response.text
    assert all("input" not in error for error in response.json()["detail"])
