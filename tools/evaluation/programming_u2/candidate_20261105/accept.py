"""Run U2 candidate checks in a fresh process against a separate checkout."""
import argparse
import asyncio
import os
from pathlib import Path
import secrets
import socket
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--only", choices=("retry", "browser"))
    args = parser.parse_args()
    if not __debug__:
        parser.error("run with Python assertions enabled")
    candidate = args.candidate.resolve()
    if not (candidate / "backend/services/programming_runs.py").is_file():
        parser.error("expected a programming candidate checkout")
    if any((candidate / name).exists() for name in (".env", "backend/.env")):
        parser.error("use a clean checkout without .env files")
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(candidate))
    os.chdir(candidate)
    with tempfile.TemporaryDirectory(prefix="smartai-u2-") as directory:
        # Keep installed runtime discovery, discard provider/proxy/database settings.
        cache_root = Path.home() / "Library/Caches" if sys.platform == "darwin" else Path.home() / ".cache"
        keep = {"PATH": os.environ["PATH"], "PLAYWRIGHT_BROWSERS_PATH":
                os.environ.get("PLAYWRIGHT_BROWSERS_PATH", str(cache_root / "ms-playwright"))}
        tempfile.tempdir = directory
        os.environ.clear()
        os.environ.update(keep, HOME=directory, TMPDIR=directory, SMARTAI_RUNTIME_ENVIRONMENT="test", SMARTAI_GRADING_ENGINE="v2",
                          SMARTAI_DATABASE_URL=f"sqlite:///{directory}/test.db",
                          SMARTAI_STORAGE_ROOT=f"{directory}/uploads", SMARTAI_DATABASE_AUTO_CREATE="true",
                          SMARTAI_SEED_TEST_USERS="false", SMARTAI_JWT_SECRET=secrets.token_hex(32),
                          SMARTAI_PROVIDER_ENCRYPTION_KEY=secrets.token_hex(32))
        denied = []

        def local_only(event, values):
            if event == "socket.connect" and values[0].family in (socket.AF_INET, socket.AF_INET6):
                if values[1][0] not in ("127.0.0.1", "::1"):
                    denied.append(event)
                    raise RuntimeError("U2 denied non-loopback connection")
            if event in ("socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr") and values[0] not in (None, "localhost", "127.0.0.1", "::1"):
                denied.append(event)
                raise RuntimeError("U2 denied external DNS")

        sys.addaudithook(local_only)
        if args.only != "browser":
            from retry_cases import verify_retry
            for interruption in ("second_case_failure", "cancel_after_last"):
                asyncio.run(verify_retry(interruption))
                print(f"PASS retry/{interruption}", flush=True)
        if args.only != "retry":
            from browser_flow import verify_browser
            verify_browser(candidate)
        assert not denied, denied


if __name__ == "__main__":
    main()
