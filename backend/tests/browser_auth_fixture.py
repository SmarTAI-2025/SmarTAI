"""Seed disposable browser accounts; the browser still uses real HTTP login."""
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path


def main():
    from sqlalchemy.engine import make_url
    url = os.environ.get("SMARTAI_E2E_DATABASE_URL", "sqlite:////tmp/smartai-e2e.db")
    parsed = make_url(url)
    database = Path(parsed.database or "").resolve()
    temporary_roots = [Path("/tmp").resolve(), Path(tempfile.gettempdir()).resolve()]
    if parsed.drivername != "sqlite" or not any(database.is_relative_to(root) for root in temporary_roots):
        raise ValueError("Browser fixtures require an explicit temporary SQLite database")
    if not database.is_file():
        raise ValueError("Start the disposable browser backend before seeding accounts")
    role, label = sys.argv[1:]
    if role not in {"teacher", "student", "admin"}:
        raise ValueError("Invalid test role")
    from backend.auth import hash_password
    from backend.db.models import UserRecord
    from sqlalchemy.orm import Session
    from backend.db.session import configure_database
    username = f"browser-{label}-{uuid.uuid4().hex[:8]}"
    password = "browser-fixture-password"
    with Session(configure_database(url)) as session:
        session.add(UserRecord(id=username, username=username, role=role,
                               password_hash=hash_password(password), is_active=True))
        session.commit()
    print(json.dumps(dict(username=username, password=password)))


if __name__ == "__main__":
    main()
