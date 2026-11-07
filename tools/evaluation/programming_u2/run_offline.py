"""Run selected existing backend tests without credentials, network, or subprocesses.

This launcher is an acceptance safety boundary, not an application change.
Use --repo PATH before pytest arguments. Existing subprocess tests are excluded
explicitly because this Mac baseline has no authorized real OCI environment.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import traceback

parser = argparse.ArgumentParser()
parser.add_argument("--repo", type=Path, required=True)
args, pytest_args = parser.parse_known_args()
repo = args.repo.resolve()
if (repo / ".env").exists() or (repo / "backend/.env").exists():
    raise SystemExit("Use an isolated checkout without .env")
runtime = Path(tempfile.mkdtemp(prefix="u2-offline-"))
safe_path = os.environ.get("PATH", "/usr/bin:/bin")
os.environ.clear()
os.environ.update({
    "PATH": safe_path, "LANG": "en_US.UTF-8", "TMPDIR": str(runtime),
    "SMARTAI_RUNTIME_ENVIRONMENT": "test", "SMARTAI_GRADING_ENGINE": "v2",
    "SMARTAI_DATABASE_URL": f"sqlite:///{runtime / 'test.db'}",
    "SMARTAI_DATABASE_AUTO_CREATE": "true", "SMARTAI_SEED_TEST_USERS": "false",
    "SMARTAI_STORAGE_ROOT": str(runtime / "uploads"),
    "SMARTAI_PROGRAM_RUNNER_MODE": "disabled", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
})
sys.dont_write_bytecode = True
blocked = []
blocked_process_names = []
blocked_process_callers = []

def guard(event, audit_args):
    if event in {"subprocess.Popen", "os.system", "os.posix_spawn"}:
        blocked.append(event)
        blocked_process_names.append(Path(str(audit_args[0])).name)
        blocked_process_callers.append([f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
            for frame in traceback.extract_stack(limit=7)[:-1]])
        raise RuntimeError("U2 baseline forbids child-process execution")
    if event == "socket.connect" and audit_args[0].family in (socket.AF_INET, socket.AF_INET6):
        blocked.append(event)
        raise RuntimeError("U2 baseline forbids network connections")
    if event in {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"} and audit_args[0] not in (None, "localhost", "127.0.0.1", "::1"):
        blocked.append(event)
        raise RuntimeError("U2 baseline forbids external DNS lookups")

sys.addaudithook(guard)
os.chdir(repo)
sys.path.insert(0, str(repo))
import pytest
pytest_code = int(pytest.main(["-p", "pytest_asyncio.plugin", "-p", "no:cacheprovider", *pytest_args]))
# Some product paths catch errors. A swallowed guard rejection must never make
# an offline test appear to pass, e.g. a confidence cap assertion after failure.
code = pytest_code or (3 if blocked else 0)
print("U2_OFFLINE_RUN " + json.dumps({"repo": str(repo), "runtime": str(runtime),
    "network_or_process_attempts_blocked": len(blocked), "blocked_events": blocked,
    "blocked_process_names": blocked_process_names,
    "blocked_process_callers": blocked_process_callers,
    "real_model_calls": 0, "real_oci_executions": 0,
    "pytest_exit_code": pytest_code, "exit_code": code}))
raise SystemExit(code)
