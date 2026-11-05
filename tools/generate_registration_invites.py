"""Generate a private 50-code batch and a digest-only server environment file.

Usage: python tools/generate_registration_invites.py /private/path/new-batch
The output directory must not exist. No database or network access is made.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets


def generate_batch(destination: Path) -> None:
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    codes: set[str] = set()
    while len(codes) < 50:
        raw = "".join(secrets.choice(alphabet) for _ in range(24))
        codes.add("-".join(raw[i:i + 4] for i in range(0, 24, 4)))
    ordered = sorted(codes)
    digests = [hashlib.sha256(code.replace("-", "").encode()).hexdigest() for code in ordered]
    files = {
        "invitation-codes.txt": "\n".join(ordered) + "\n",
        "registration-invites.env": "SMARTAI_REGISTRATION_INVITE_DIGESTS=" + ",".join(digests) + "\n",
        "manifest.json": json.dumps({"count": 50, "code_bits": 24 * 5,
            "codes_sha256": hashlib.sha256(("\n".join(ordered) + "\n").encode()).hexdigest()}, indent=2) + "\n",
    }
    for name, content in files.items():
        fd = os.open(destination / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as output:
            output.write(content)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    generate_batch(args.destination)
    print("Created 50 private invitation codes; no server or database was changed.")
