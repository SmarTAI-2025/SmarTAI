"""Explicit synthetic invitation inputs for existing registration regressions."""
import uuid
from threading import Lock
from backend.config import settings
from backend.services.email_registration import digest_registration_invite

_lock = Lock()


def invite_code():
    code = uuid.uuid4().hex
    with _lock:
        settings.registration_invite_digests += "," + digest_registration_invite(code)
    return code


def invite_digest():
    return digest_registration_invite(invite_code())
