"""Admission and transaction regressions using synthetic codes and fake mail."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.config import settings
from backend.db.models import EmailVerificationRequestRecord, RegistrationInviteRecord, UserRecord
from backend.db.session import session_scope
from backend.main import app
from backend.services import email_registration as registration
from backend.tests.registration_invite_helpers import invite_code


class Sender:
    def __init__(self, fail=False):
        self.tokens = []
        self.fail = fail

    def send(self, _email, _subject, body, _html):
        if self.fail:
            raise RuntimeError("synthetic delivery failure")
        self.tokens.append(body.split("#token=", 1)[1].split()[0])


def request(code, username="invite-teacher", sender=None):
    return registration.request_registration(
        username=username, email=f"{username}@ustc.edu.cn", password="synthetic-password",
        source_ip=None, invite_code=code, sender=sender or Sender(),
    )


@pytest.fixture(autouse=True)
def school_domains(monkeypatch):
    monkeypatch.setattr(settings, "allowed_email_domains", "ustc.edu.cn")


@pytest.mark.parametrize("code,error", [(None, "registration_invite_required"), ("", "registration_invite_required"),
    ("   ", "registration_invite_required"), ("wrong-code", "registration_invite_invalid")])
def test_api_requires_a_valid_code_before_mail_or_account(monkeypatch, code, error):
    sender = Sender()
    monkeypatch.setattr("backend.api.auth.get_email_sender", lambda: sender)
    payload = {"username": "invite-user", "email": "invite-user@ustc.edu.cn", "password": "synthetic-password"}
    if code is not None:
        payload["invite_code"] = code
    response = TestClient(app).post("/auth/register/request", json=payload)
    assert response.status_code == 400
    assert response.json() == {"detail": {"code": error}}
    assert sender.tokens == []
    with session_scope() as session:
        assert session.query(EmailVerificationRequestRecord).count() == 0
        assert session.query(UserRecord).count() == 0
        assert session.query(RegistrationInviteRecord).count() == 0


def test_code_is_spent_only_on_registration_and_replay_does_not_spend_again():
    code = invite_code()
    sender = Sender()
    pending = request("  " + code.upper() + "  ", sender=sender)
    with session_scope() as session:
        row = session.get(EmailVerificationRequestRecord, pending["request_id"])
        assert row.registration_invite_digest == registration.digest_registration_invite(code)
        assert code not in vars(row).values()
        assert session.query(RegistrationInviteRecord).count() == 0
        assert session.query(UserRecord).count() == 0
    assert registration.verify_registration(sender.tokens[0]) == {"status": "registered"}
    assert registration.verify_registration(sender.tokens[0]) == {"status": "already_verified"}
    with pytest.raises(registration.RegistrationError, match="registration_invite_invalid"):
        request(code, username="other-teacher")
    with session_scope() as session:
        assert session.query(RegistrationInviteRecord).count() == 1
        user = session.scalar(select(UserRecord))
        assert user.role == "teacher"
        session.delete(user)
    # Account closure must not replenish the invitation.
    with pytest.raises(registration.RegistrationError, match="registration_invite_invalid"):
        request(code, username="third-teacher")


def test_resend_preserves_code_and_only_latest_link_can_consume_it():
    code = invite_code()
    sender = Sender()
    first = request(code, sender=sender)
    with session_scope() as session:
        session.get(EmailVerificationRequestRecord, first["request_id"]).resend_available_at = 0
    resent = registration.resend_registration(request_id=first["request_id"], source_ip=None, sender=sender)
    with session_scope() as session:
        assert session.get(EmailVerificationRequestRecord, resent["request_id"]).registration_invite_digest == registration.digest_registration_invite(code)
    with pytest.raises(registration.RegistrationError, match="verification_link_already_used"):
        registration.verify_registration(sender.tokens[0])
    assert registration.verify_registration(sender.tokens[1])["status"] == "registered"


@pytest.mark.parametrize("reason", ["missing", "used", "revoked"])
def test_unavailable_invite_blocks_resend_and_verify_but_new_code_recovers(monkeypatch, reason):
    code = invite_code()
    sender = Sender()
    pending = request(code, sender=sender)
    with session_scope() as session:
        row = session.get(EmailVerificationRequestRecord, pending["request_id"])
        row.resend_available_at = 0
        if reason == "missing":
            row.registration_invite_digest = None
    if reason == "used":
        winner = Sender()
        request(code, username="invite-winner", sender=winner)
        assert registration.verify_registration(winner.tokens[0])["status"] == "registered"
    elif reason == "revoked":
        monkeypatch.setattr(settings, "registration_invite_digests", "")
    expected = "registration_invite_required" if reason == "missing" else "registration_invite_invalid"
    monkeypatch.setattr("backend.api.auth.get_email_sender", lambda: sender)
    client = TestClient(app)
    for _ in range(2):
        response = client.post("/auth/register/resend", json={"request_id": pending["request_id"]})
        assert response.status_code == 400
        assert response.json() == {"detail": {"code": expected}}
    with pytest.raises(registration.RegistrationError, match=expected):
        registration.verify_registration(sender.tokens[0])
    assert len(sender.tokens) == 1
    with session_scope() as session:
        assert session.query(EmailVerificationRequestRecord).count() == (2 if reason == "used" else 1)
        assert session.query(UserRecord).count() == (1 if reason == "used" else 0)
        assert session.query(RegistrationInviteRecord).count() == (1 if reason == "used" else 0)
        row = session.get(EmailVerificationRequestRecord, pending["request_id"])
        assert row.verified_at is None and row.superseded_at is None
    # Returning to the form and submitting a fresh code can complete registration.
    replacement = Sender()
    request(invite_code(), sender=replacement)
    assert registration.verify_registration(replacement.tokens[0])["status"] == "registered"


def test_failed_mail_and_failed_account_creation_do_not_spend_code(monkeypatch):
    code = invite_code()
    with pytest.raises(registration.RegistrationError, match="registration_email_delivery_failed"):
        request(code, sender=Sender(fail=True))
    sender = Sender()
    request(code, sender=sender)
    original = registration._persist_user
    def fail_after_insert(**kwargs):
        original(**kwargs)
        kwargs["session"].flush()
        raise RuntimeError("synthetic failure after account insert")
    monkeypatch.setattr(registration, "_persist_user", fail_after_insert)
    with pytest.raises(RuntimeError, match="synthetic failure"):
        registration.verify_registration(sender.tokens[0])
    with session_scope() as session:
        assert session.query(UserRecord).count() == 0
        assert session.query(RegistrationInviteRecord).count() == 0
    monkeypatch.setattr(registration, "_persist_user", original)
    assert registration.verify_registration(sender.tokens[0])["status"] == "registered"


def test_independent_confirmations_with_one_code_have_one_winner(monkeypatch):
    code = invite_code()
    first, second = Sender(), Sender()
    request(code, username="invite-one", sender=first)
    request(code, username="invite-two", sender=second)
    # Force both connections past the advisory check, without process locks.
    # The unique database insert must remain the arbiter across workers.
    barrier = threading.Barrier(2)
    original = registration._require_registration_invite
    def checked(session, digest):
        original(session, digest)
        barrier.wait(timeout=10)
    monkeypatch.setattr(registration, "_process_flow_locks", lambda *a, **kw: nullcontext())
    monkeypatch.setattr(registration, "_require_registration_invite", checked)
    def verify(token):
        try:
            return registration.verify_registration(token)["status"]
        except registration.RegistrationError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(verify, [first.tokens[0], second.tokens[0]]))
    assert sorted(results) == ["registered", "registration_invite_invalid"]
    with session_scope() as session:
        assert session.query(UserRecord).count() == 1
        assert session.query(RegistrationInviteRecord).count() == 1
        assert session.query(EmailVerificationRequestRecord).filter(EmailVerificationRequestRecord.verified_at.is_not(None)).count() == 1


def test_preexisting_unverified_link_cannot_bypass_invitation():
    sender = Sender()
    pending = request(invite_code(), sender=sender)
    with session_scope() as session:
        session.get(EmailVerificationRequestRecord, pending["request_id"]).registration_invite_digest = None
    with pytest.raises(registration.RegistrationError, match="registration_invite_required"):
        registration.verify_registration(sender.tokens[0])


def test_batch_generator_has_50_unique_codes_private_modes_and_no_overwrite(tmp_path):
    from tools.generate_registration_invites import generate_batch
    destination = tmp_path / "batch"
    generate_batch(destination)
    codes = (destination / "invitation-codes.txt").read_text().splitlines()
    assert len(codes) == len(set(codes)) == 50
    assert all(len(code.replace("-", "")) == 24 for code in codes)
    env = (destination / "registration-invites.env").read_text()
    assert set(env.strip().split("=", 1)[1].split(",")) == {registration.digest_registration_invite(code) for code in codes}
    assert all(code not in env for code in codes)
    assert destination.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in destination.iterdir())
    with pytest.raises(FileExistsError):
        generate_batch(destination)
