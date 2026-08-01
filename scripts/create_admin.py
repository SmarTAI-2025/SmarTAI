"""Create or update the first administrator without storing plaintext credentials."""
from __future__ import annotations

import argparse
import getpass
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.auth import hash_password
from backend.db.session import create_schema
from backend.models import User
from backend.state import find_user_by_username, register_user


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a SmarTAI administrator")
    parser.add_argument("username")
    parser.add_argument("--email", default="")
    args = parser.parse_args()
    password = os.getenv("SMARTAI_BOOTSTRAP_ADMIN_PASSWORD") or getpass.getpass("Admin password: ")
    if len(password) < 10:
        raise SystemExit("Admin password must contain at least 10 characters")
    create_schema()
    existing = find_user_by_username(args.username)
    user = existing or User(id=f"u_{uuid.uuid4().hex[:10]}", username=args.username)
    user.email = args.email
    user.role = "admin"
    user.password_hash = hash_password(password)
    user.is_active = True
    register_user(user)
    print(f"Administrator {args.username!r} is ready.")


if __name__ == "__main__":
    main()
