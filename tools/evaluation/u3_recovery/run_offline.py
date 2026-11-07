"""Run U3 probes or existing backend tests on a disposable local environment.

Example: python tools/evaluation/u3_recovery/run_offline.py -p backend.tests.conftest
         -q tools/evaluation/u3_recovery/probes
The two known-gap probes deliberately fail on the audited main.
"""
import argparse
import json
import os
from pathlib import Path
import platform
import secrets
import sys
import tempfile

from safety import block_network, require_isolated_run


def main():
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--allow-loopback", action="store_true",
        help="Allow only literal loopback IP connections for the existing local HTTP regression")
    args, pytest_args = parser.parse_known_args()
    source = args.source_root.resolve()
    if not (source / "backend/tests/conftest.py").is_file() or (source / ".env").exists():
        raise SystemExit("Use a clean SmarTAI checkout/archive without .env")
    root = Path(tempfile.mkdtemp(prefix="smartai-u3-audit-")).resolve()
    token = secrets.token_hex(16)
    (root / ".audit-token").write_text(token)
    os.environ.clear()
    os.environ.update({
        "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(root), "TMPDIR": str(root), "LANG": "en_US.UTF-8",
        "SMARTAI_DATABASE_URL": f"sqlite:///{root / 'audit.db'}",
        "SMARTAI_DATABASE_URL_LIGHT": f"sqlite:///{root / 'audit.db'}",
        "SMARTAI_DATABASE_HEAVY": "false", "SMARTAI_DATABASE_AUTO_CREATE": "true",
        "SMARTAI_SEED_TEST_USERS": "false", "SMARTAI_STORAGE_ROOT": str(root / "uploads"),
        "SMARTAI_STORAGE_BACKEND": "local", "SMARTAI_RUNTIME_ENVIRONMENT": "test",
        "SMARTAI_GRADING_ENGINE": "v2", "PYTHONPATH": str(source),
        "SMARTAI_U3_AUDIT_ROOT": str(root), "SMARTAI_U3_AUDIT_TOKEN": token,
        "SMARTAI_U3_SOURCE_ROOT": str(source), "SMARTAI_U3_EVIDENCE": str(root / "observations"),
    })
    os.chdir(source)
    sys.path.insert(0, str(source))
    require_isolated_run()
    attempts = block_network(allow_loopback=args.allow_loopback)
    import pytest
    code = int(pytest.main(["--basetemp", str(root / "pytest"),
        "-o", f"cache_dir={root / 'pytest-cache'}", *pytest_args]))
    if attempts and code == 0:
        code = 1
    result = {"source": str(source), "runtime": str(root), "python": sys.version,
        "platform": platform.platform(), "pytest_args": pytest_args,
        "allow_loopback": args.allow_loopback,
        "blocked_network_attempts": len(attempts), "exit_code": code}
    (root / "run.json").write_text(json.dumps(result, indent=2))
    print("U3_OFFLINE_RUN " + json.dumps(result))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
