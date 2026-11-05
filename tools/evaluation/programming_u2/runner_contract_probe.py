"""Observe existing OCI contract gaps with a fake command, never real OCI.

Run via run_offline.py. Assertions intentionally reproduce current behavior;
passing these probes is not a product safety acceptance. No student code runs.
"""
import asyncio
import json
import math
from pathlib import Path

import pytest
from backend.tools import oci_grading_runner as runner


@pytest.fixture
def configured(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.settings, "program_runner_mode", "oci")
    monkeypatch.setattr(runner.settings, "program_runner_image", "sha256:" + "a" * 64)
    monkeypatch.setattr(runner.settings, "program_runner_max_concurrent", 1)
    monkeypatch.setattr(runner.settings, "runtime_environment", "test")
    monkeypatch.setattr(runner.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(runner.tempfile, "gettempdir", lambda: str(tmp_path))


def response(args, **changes):
    mount = next(arg for arg in args if arg.startswith("--mount="))
    request = json.loads(Path(mount.split("source=")[1].split(",target=")[0]).read_text())
    return json.dumps({"schema_version": 1, "execution_id": request["execution_id"],
        "executed": True, "exit_reason": "success", "passed_count": 1, "total_count": 1,
        "stdout": "fixed response", "stderr": "", "duration_ms": 1,
        "resource_status": "within_limits", "isolation_mode": "oci_container", **changes}).encode()


@pytest.mark.asyncio
async def test_nonfinite_duration_is_currently_accepted(configured, monkeypatch):
    calls = []
    async def fake_command(args, **kwargs):
        calls.append(args[1])
        return b"" if args[1] == "rm" else response(args, duration_ms=float("inf"))
    monkeypatch.setattr(runner, "_command", fake_command)
    result = await runner.run_python_oci("# synthetic; never executed")
    assert result.passed and math.isinf(result.duration_ms)
    assert calls == ["run", "rm"]
    print("U2_OCI_OBSERVATION " + json.dumps({"id": "D06", "evidence": "real parser/adapter; fixed command response",
        "duration_from_fixture": "Infinity", "observed_duration_finite": math.isfinite(result.duration_ms),
        "observed_passed": result.passed, "real_oci_executions": 0}))


@pytest.mark.asyncio
async def test_cleanup_failure_currently_releases_slot(configured, monkeypatch):
    events, requested_names = [], []
    async def fake_command(args, **kwargs):
        events.append(args[1])
        if args[1] == "rm":
            raise runner.IsolatedRunnerUnavailable()
        requested_names.append(next(arg for arg in args if arg.startswith("--name=")))
        raise asyncio.TimeoutError()
    monkeypatch.setattr(runner, "_command", fake_command)
    for _ in range(2):
        with pytest.raises(runner.IsolatedRunnerUnavailable):
            await runner.run_python_oci("# synthetic; never executed")
    assert events == ["run", "rm", "run", "rm"] and len(set(requested_names)) == 2
    print("U2_OCI_OBSERVATION " + json.dumps({"id": "D07", "evidence": "real host-slot logic; injected run timeout and rm failure",
        "max_concurrent": 1, "command_operations": events, "second_run_request_admitted": True,
        "cleanup_successes": 0, "real_oci_executions": 0,
        "limitation": "No actual containers exist; does not prove production leakage or full runtime cleanup behavior"}))
