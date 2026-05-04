"""Seed pre-baked test accounts into the user store on startup.

Reads `settings.test_users_file` (default: data/test_users.json), which is
gitignored so credentials never reach the public repo. Each entry is one
{username, password, role} record. We bcrypt-hash on load — only the file
on disk holds plaintext, and only on machines where it has been generated.

To create the file, run:
    python scripts/generate_test_users.py

To distribute credentials to invited testers, send them the username +
password from that file via a private channel (1Password, signal, etc.).

Render / cloud deployments
──────────────────────────
The repo-level file is gitignored and therefore won't reach Render. Two
supported alternatives — both pickup is automatic, no code change needed:

  1. Render Secret Files: upload the JSON in the dashboard, then set
       SMARTAI_TEST_USERS_FILE=/etc/secrets/test_users.json
     The file is mounted at runtime and we read it normally.

  2. Inline env var: paste the entire JSON as a single env var:
       SMARTAI_TEST_USERS_JSON='{"users":[{"username":"...","password":"..."}]}'
     We parse it directly without touching the filesystem. Handy when you
     don't want to deal with secret files.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any

from backend.auth import hash_password
from backend.config import settings
from backend.models import User
from backend.state import find_user_by_username, register_user

logger = logging.getLogger(__name__)


def _load_payload() -> dict[str, Any] | None:
    """Resolve the test-users payload from env-var (preferred for cloud) or file.

    Precedence: SMARTAI_TEST_USERS_JSON > settings.test_users_file
    """
    inline = os.getenv("SMARTAI_TEST_USERS_JSON")
    if inline:
        try:
            return json.loads(inline)
        except json.JSONDecodeError as e:
            logger.warning(f"SMARTAI_TEST_USERS_JSON is set but not valid JSON: {e}")
            return None

    path = settings.test_users_file
    if not path or not os.path.exists(path):
        logger.info(f"No test users file at {path!r}; skipping seed.")
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Could not load test users from {path}: {e}")
        return None


def seed_test_users() -> int:
    """Load test accounts from env or the configured JSON file. Returns the
    number of accounts seeded. Idempotent — usernames already in the store
    are skipped so reloads don't duplicate users.
    """
    payload = _load_payload()
    if payload is None:
        return 0

    users = payload.get("users") if isinstance(payload, dict) else None
    if not isinstance(users, list):
        logger.warning("test users payload: expected top-level {'users': [...]}")
        return 0

    seeded = 0
    for entry in users:
        if not isinstance(entry, dict):
            continue
        username = entry.get("username")
        password = entry.get("password")
        role = entry.get("role", "teacher")
        if not username or not password:
            continue
        if find_user_by_username(username) is not None:
            continue
        if role not in ("teacher", "student", "admin"):
            role = "teacher"
        user = User(
            id=f"u_{uuid.uuid4().hex[:10]}",
            username=username,
            email=entry.get("email", f"{username}@test.local"),
            role=role,  # type: ignore[arg-type]
            password_hash=hash_password(password),
        )
        register_user(user)
        seeded += 1
    logger.info(f"Seeded {seeded} test users")
    return seeded
