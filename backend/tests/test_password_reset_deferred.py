"""Public reset response must precede account lookup and SMTP delivery."""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from fastapi import FastAPI

from backend.api import auth as auth_api
from backend.db.models import PasswordResetRequestRecord, UserRecord
from backend.db.session import session_scope
from backend.services import password_reset


def _user(active=True):
    with session_scope() as session:
        session.add(UserRecord(id="deferred-user", username="deferred-user",
            email="deferred@ustc.edu.cn", password_hash="synthetic-hash",
            role="teacher", is_active=active))


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["active", "inactive", "missing"])
async def test_public_response_is_sent_before_deferred_account_processing(monkeypatch, account):
    if account != "missing":
        _user(active=account == "active")
    monkeypatch.setattr(password_reset.settings, "allowed_email_domains", "ustc.edu.cn")
    events = []
    real_delivery = password_reset._deliver_password_reset
    def observed_delivery(**kwargs):
        events.append("account-processing")
        return real_delivery(**kwargs)
    monkeypatch.setattr(password_reset, "_deliver_password_reset", observed_delivery)
    class Sender:
        def send(self, *args):
            events.append("smtp")
    monkeypatch.setattr(auth_api, "get_email_sender", lambda: Sender())
    app = FastAPI()
    app.include_router(auth_api.router)
    body = json.dumps({"email": "deferred@ustc.edu.cn"}).encode()
    messages = []
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    async def send(message):
        messages.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body"):
            events.append("response-sent")
    await app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": "/auth/password-reset/request",
        "raw_path": b"/auth/password-reset/request", "query_string": b"",
        "root_path": "", "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 12345), "server": ("test", 80)}, receive, send)
    assert events[:2] == ["response-sent", "account-processing"]
    assert events.count("smtp") == int(account == "active")
    assert next(m for m in messages if m["type"] == "http.response.start")["status"] == 202
    payload = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    assert json.loads(payload) == {"status": "reset_link_requested",
        "expires_in_seconds": 1800, "resend_after_seconds": 60}


def test_deferred_attempt_predating_password_change_does_not_mint_a_new_link(monkeypatch):
    _user()
    monkeypatch.setattr(password_reset.settings, "allowed_email_domains", "ustc.edu.cn")
    callbacks = []
    class Sender:
        def send(self, *args):
            raise AssertionError("stale reset attempt must not send mail")
    result = password_reset.request_password_reset(
        email="deferred@ustc.edu.cn", source_ip="127.0.0.1", sender=Sender(),
        delivery_scheduler=callbacks.append)
    assert result["status"] == "reset_link_requested"
    assert len(callbacks) == 1
    with session_scope() as session:
        assert session.query(PasswordResetRequestRecord).count() == 0
        session.get(UserRecord, "deferred-user").auth_invalid_before = time.time() + 1
    callbacks[0]()
    with session_scope() as session:
        assert session.query(PasswordResetRequestRecord).count() == 0
