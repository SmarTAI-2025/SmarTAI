#!/usr/bin/env python
"""Generate a NEW maintenance hash locally; no existing secret is read."""
import getpass
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.auth import hash_password

def main():
    password = getpass.getpass("New independent maintenance password (20–128 characters): ")
    if not 20 <= len(password) <= 128 or password != getpass.getpass("Confirm maintenance password: "):
        raise SystemExit("Password length or confirmation invalid")
    print(hash_password(password))
if __name__ == "__main__":
    main()
