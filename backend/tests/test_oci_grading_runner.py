import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from backend.tools import oci_grading_runner as runner
from backend.tools import code_interpreter
from backend.models import TestCase as Case


@pytest.fixture
def configured(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.settings, "program_runner_mode", "oci")
    monkeypatch.setattr(runner.settings, "program_runner_image", "sha256:" + "a" * 64)
    monkeypatch.setattr(runner.settings, "program_runner_max_concurrent", 2)
    monkeypatch.setattr(runner.settings, "runtime_environment", "test")
    monkeypatch.setattr(runner.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(runner.tempfile, "gettempdir", lambda: str(tmp_path))


def worker_result(request, **changes):
    return json.dumps({"schema_version": 1, "execution_id": request["execution_id"], "executed": True,
        "exit_reason": "success", "passed_count": 1, "total_count": 1, "stdout": "3\n", "stderr": "",
        "duration_ms": 1, "resource_status": "within_limits", "isolation_mode": "oci_container", **changes}).encode()


def request_from(args):
    mount = next(x for x in args if x.startswith("--mount="))
    return json.loads(Path(mount.split("source=")[1].split(",target=")[0]).read_text())


@pytest.mark.asyncio
async def test_transport_reuses_two_case_comparison_without_host_fallback(configured, monkeypatch):
    commands = []
    async def command(args, **kwargs):
        commands.append(args)
        if args[1] == "rm": return b""
        request = request_from(args)
        assert request["test_input"] in ["1 2", "2 2"]
        return worker_result(request)
    monkeypatch.setattr(runner, "_command", command)
    report = await code_interpreter.run_sandbox("print(sum(map(int,input().split())))", [Case(input="1 2", expected_output="3"), Case(input="2 2", expected_output="4")])
    assert report.passed_count == 1 and report.total_count == 2
    assert len([a for a in commands if a[1] == "rm"]) == 2
    run = next(a for a in commands if a[1] == "run")
    for flag in ["--network=none", "--read-only", "--cap-drop=ALL", "--pids-limit=16", "--user=65534:65534", "--pull=never", "--memory-swap=256m"]:
        assert flag in run
    assert not any("DATABASE" in a or "--env-file" in a or "docker.sock" in a for a in run)


@pytest.mark.asyncio
async def test_explicit_function_mapping_and_args_remain_exact(configured, monkeypatch):
    async def command(args, **kwargs):
        if args[1] == "rm": return b""
        request = request_from(args)
        assert "print(repr(second(*__smartai_args)))" in request["code"]
        assert request["test_input"] == "[2]"
        return worker_result(request, stdout="4\n")
    monkeypatch.setattr(runner, "_command", command)
    report = await code_interpreter.run_sandbox("def first(x): return 0\ndef second(x): return x*2", [Case(function_name="second", function_args=[2], expected_return="4")])
    assert report.passed_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"execution_id":"wrong"}, {"schema_version":2}, {"isolation_mode":"host_subprocess"}, {"executed":False}, {"exit_reason":"internal_error"}])
async def test_invalid_result_never_becomes_student_zero(configured, monkeypatch, changes):
    removed = []
    async def command(args, **kwargs):
        if args[1] == "rm": removed.append(args); return b""
        return worker_result(request_from(args), **changes)
    monkeypatch.setattr(runner, "_command", command)
    with pytest.raises(runner.IsolatedRunnerUnavailable, match="^programming_execution_unavailable$"):
        await runner.run_python_oci("print(3)")
    assert len(removed) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["timeout", "runtime_error", "output_limit"])
async def test_student_errors_stay_per_case(configured, monkeypatch, reason):
    async def command(args, **kwargs):
        return b"" if args[1] == "rm" else worker_result(request_from(args), exit_reason=reason, stdout="", stderr="Safe test error")
    monkeypatch.setattr(runner, "_command", command)
    result = await runner.run_python_oci("print(3)")
    assert not result.passed and result.exit_reason == reason


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("secret /etc/private"), asyncio.TimeoutError(), asyncio.CancelledError()])
async def test_cleanup_on_error_timeout_and_cancel(configured, monkeypatch, failure):
    removed = []
    async def command(args, **kwargs):
        if args[1] == "rm": removed.append(args); return b""
        raise failure
    monkeypatch.setattr(runner, "_command", command)
    with pytest.raises(asyncio.CancelledError if isinstance(failure, asyncio.CancelledError) else runner.IsolatedRunnerUnavailable):
        await runner.run_python_oci("print(3)")
    assert len(removed) == 1


@pytest.mark.asyncio
async def test_host_wide_capacity_is_bounded(configured, monkeypatch):
    active = peak = 0
    async def command(args, **kwargs):
        nonlocal active, peak
        if args[1] == "rm": return b""
        active += 1; peak = max(active, peak)
        await asyncio.sleep(.02)
        active -= 1
        return worker_result(request_from(args))
    monkeypatch.setattr(runner, "_command", command)
    await asyncio.gather(*(runner.run_python_oci("print(3)") for _ in range(6)))
    assert peak == 2


@pytest.mark.asyncio
async def test_production_requires_rootless_seccomp_cgroups_and_pinned_image(configured, monkeypatch):
    monkeypatch.setattr(runner.settings, "runtime_environment", "production")
    safe = {"SecurityOptions":["name=rootless", "name=seccomp,profile=builtin"], "CgroupVersion":"2", "CgroupDriver":"systemd", "MemoryLimit":True,"CpuCfsQuota":True,"PidsLimit":True}
    command = AsyncMock(return_value=json.dumps(safe).encode())
    monkeypatch.setattr(runner, "_command", command)
    await runner._check_runtime("/usr/bin/docker", {})
    for key, invalid in [("SecurityOptions", ["name=seccomp"]), ("CgroupDriver", "none"), ("MemoryLimit", False), ("PidsLimit", False)]:
        command.return_value = json.dumps({**safe, key:invalid}).encode()
        with pytest.raises(runner.IsolatedRunnerUnavailable): await runner._check_runtime("/usr/bin/docker", {})
    monkeypatch.setattr(runner.settings, "program_runner_image", "mutable:latest")
    with pytest.raises(runner.IsolatedRunnerUnavailable): await runner._check_runtime("/usr/bin/docker", {})


def test_runtime_process_does_not_inherit_application_secrets(configured, monkeypatch):
    monkeypatch.setenv("SMARTAI_SECRET", "do-not-copy")
    monkeypatch.setenv("DATABASE_URL", "do-not-copy")
    monkeypatch.setenv("HTTPS_PROXY", "do-not-copy")
    assert not any(key in runner.launcher_environment() for key in ["SMARTAI_SECRET", "DATABASE_URL", "HTTPS_PROXY"])
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote:2375")
    with pytest.raises(runner.IsolatedRunnerUnavailable): runner.launcher_environment()
