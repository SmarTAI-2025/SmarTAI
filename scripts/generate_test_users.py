"""Generate a test_users.json file with N pre-baked teacher credentials.

Default: 50 accounts named tester01..tester50 with random 12-char passwords.
The output file is gitignored — it never lands in the repo. Distribute the
credentials privately to invited testers, who then log in via /login.

Usage:
    python scripts/generate_test_users.py                   # 50 → data/test_users.json
    python scripts/generate_test_users.py --count 20        # 20 accounts
    python scripts/generate_test_users.py --out /tmp/u.json # custom path
    python scripts/generate_test_users.py --prefix beta_    # custom username prefix

After generating, restart the backend — accounts are seeded on app startup
(see backend/auth/seed.py).
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import string


PASSWORD_ALPHABET = string.ascii_letters + string.digits


def gen_password(length: int = 12) -> str:
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument("--prefix", type=str, default="tester")
    parser.add_argument("--role", type=str, default="teacher", choices=["teacher", "student", "admin"])
    parser.add_argument("--out", type=str, default="data/test_users.json")
    parser.add_argument("--password-length", type=int, default=12)
    args = parser.parse_args()

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)

    width = max(2, len(str(args.count)))
    users = [
        {
            "username": f"{args.prefix}{str(i + 1).zfill(width)}",
            "password": gen_password(args.password_length),
            "role": args.role,
            "email": f"{args.prefix}{str(i + 1).zfill(width)}@test.local",
        }
        for i in range(args.count)
    ]

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"users": users}, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.chmod(args.out, 0o600)

    print(f"✓ wrote {args.count} accounts to {args.out} (mode 0600)")
    print(f"  add the file to .gitignore (already covered by `data/` if used)")
    print(f"  restart backend to seed: SMARTAI_TEST_USERS_FILE={args.out} python -m backend.main")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
