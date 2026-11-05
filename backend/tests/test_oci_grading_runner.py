import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from backend.tools import oci_grading_runner as runner
from backend.tools import code_interpreter
from backend.models import TestCase as Case
from tools.runner_spike.worker import request_digest


@pytest.fixture
def configured(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.settings, "program_runner_mode", "oci")
    monkeypatch.setattr(runner.settings, "program_runner_image", "sha256:" + "a" * 64)
    monkeypatch.setattr(runner.settings, "program_runner_max_concurrent", 2)
    monkeypatch.setattr(runner.settings, "runtime_environment", "test")
    monkeypatch.setattr(runner.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(runner.tempfile, "gettempdir", lambda: str(tmp_path))


def worker_result(request, **changes):
    reason = changes.get("exit_reason", "success")
    return json.dumps({"schema_version": 1, "execution_id": request["execution_id"], "executed": True,
        "exit_reason": "success", "passed_count": int(reason == "success"), "total_count": 1, "stdout": "3\n", "stderr": "",
        "duration_ms": 1, "resource_status": {"success":"within_limits", "timeout":"timeout", "output_limit":"output_limit"}.get(reason, "unknown"),
        "request_digest": request_digest(request), "isolation_mode": "oci_container", **changes}).encode()


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
        assert request["code"] == "def first(x): return 0\ndef second(x): return x*2"
        assert request["function_call"] == {"name": "second", "args": [2], "kwargs": {}}
        assert request["test_input"] == "" and "expected_return" not in json.dumps(request)
        return worker_result(request, return_value="4", stdout="debug: 99\n")
    monkeypatch.setattr(runner, "_command", command)
    report = await code_interpreter.run_sandbox("def first(x): return 0\ndef second(x): return x*2", [Case(function_name="second", function_args=[2], expected_return="4")])
    assert report.passed_count == 1
    assert report.results[0].stdout == "debug: 99\n"
    assert report.results[0].return_value == "4"


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


@pytest.mark.asyncio
async def test_production_podman_requires_delegated_resource_controllers(configured, monkeypatch):
    monkeypatch.setattr(runner.settings, "runtime_environment", "production")
    host = {"security": {"rootless": True, "seccompEnabled": True}, "cgroupVersion": "v2", "cgroupManager": "systemd", "cgroupControllers": ["cpu", "memory", "pids"]}
    async def info(*args, **kwargs): return json.dumps({"host": host}).encode()
    monkeypatch.setattr(runner, "_command", info)
    await runner._check_runtime("/usr/bin/podman", {})
    host["cgroupControllers"] = ["cpu", "pids"]
    with pytest.raises(runner.IsolatedRunnerUnavailable):
        await runner._check_runtime("/usr/bin/podman", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [
    {"duration_ms": float("inf")}, {"duration_ms": float("nan")},
    {"passed_count": 0}, {"total_count": 2}, {"resource_status": "timeout"},
    {"request_digest": "0" * 64}, {"return_value": "forged"},
    {"executed": "true"}, {"stdout_truncated": True},
])
async def test_bound_result_rejects_nonfinite_inconsistent_and_mismatched_evidence(configured, monkeypatch, mutation):
    async def command(args, **kwargs):
        if args[1] == "rm":
            return b""
        return worker_result(request_from(args), **mutation)
    monkeypatch.setattr(runner, "_command", command)
    with pytest.raises(runner.IsolatedRunnerUnavailable):
        await code_interpreter.run_sandbox("print(1)", [Case(input="", expected_output="1")])


@pytest.mark.asyncio
async def test_unconfirmed_cleanup_quarantines_slot_until_recovered(configured, monkeypatch):
    monkeypatch.setattr(runner.settings, "program_runner_max_concurrent", 1)
    launches = []
    unavailable = True

    async def command(args, **kwargs):
        if args[1] == "rm":
            if unavailable:
                raise runner.IsolatedRunnerUnavailable()
            return b""
        if args[1] == "ps":
            return launches[-1].encode()
        launches.append(next(arg.split("=", 1)[1] for arg in args if arg.startswith("--name=")))
        if unavailable:
            raise asyncio.TimeoutError()
        return worker_result(request_from(args))

    monkeypatch.setattr(runner, "_command", command)
    for _ in range(2):
        with pytest.raises(runner.IsolatedRunnerUnavailable):
            await runner.run_python_oci("print(3)")
    assert len(launches) == 1
    unavailable = False
    assert (await runner.run_python_oci("print(3)")).passed
    assert len(set(launches)) == 2


@pytest.mark.asyncio
async def test_exited_autoremoved_container_releases_slot_only_after_verified_absence(configured, monkeypatch):
    calls = []
    async def command(args, **kwargs):
        calls.append(args[1])
        if args[1] == "rm":
            raise runner.IsolatedRunnerUnavailable()
        if args[1] == "ps":
            assert "--all" in args and any(str(item).startswith("name=smartai-oci-") for item in args)
            return b""
        return worker_result(request_from(args))
    monkeypatch.setattr(runner, "_command", command)
    assert (await runner.run_python_oci("print(3)")).passed
    assert calls == ["run", "rm", "ps"]


@pytest.mark.asyncio
async def test_cancellation_during_cleanup_keeps_capacity_until_removal(configured, monkeypatch):
    monkeypatch.setattr(runner.settings, "program_runner_max_concurrent", 1)
    deleting, removed = asyncio.Event(), asyncio.Event()
    launches = []
    async def command(args, **kwargs):
        if args[1] == "rm":
            deleting.set()
            await removed.wait()
            return b""
        launches.append(request_from(args)["execution_id"])
        return worker_result(request_from(args))
    monkeypatch.setattr(runner, "_command", command)
    first = asyncio.create_task(runner.run_python_oci("print(3)"))
    await deleting.wait()
    first.cancel()
    second = asyncio.create_task(runner.run_python_oci("print(4)"))
    await asyncio.sleep(.02)
    assert len(launches) == 1
    removed.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    assert len(set(launches)) == 2


@pytest.mark.asyncio
async def test_repeated_wire_field_is_not_valid_evidence(configured, monkeypatch):
    async def command(args, **kwargs):
        if args[1] == "rm":
            return b""
        original = worker_result(request_from(args))
        return b'{"executed":false,' + original[1:]
    monkeypatch.setattr(runner, "_command", command)
    with pytest.raises(runner.IsolatedRunnerUnavailable):
        await runner.run_python_oci("print(3)")


@pytest.mark.asyncio
@pytest.mark.parametrize(("reason","resource","metadata","valid"), [
    ("memory_limit","memory_limit_exceeded",{"cgroup_oom_kills":1,"termination_signal":9}, True),
    ("pid_limit","pid_limit_exceeded",{"cgroup_pid_denials":2,"process_exit_code":1}, True),
    ("memory_limit","memory_limit_exceeded",{"termination_signal":9},False),
    ("pid_limit","pid_limit_exceeded",{"cgroup_pid_denials":0},False),
    ("signature_mismatch","unknown",{"process_exit_code":1},True),
    ("target_function_missing","unknown",{"process_exit_code":1},True),
    ("syntax_error","unknown",{"process_exit_code":1},True),
])
async def test_oci_accepts_only_evidenced_programming_failure(configured, monkeypatch, reason, resource, metadata, valid):
    async def command(args, **kwargs):
        if args[1] == "rm":
            return b""
        return worker_result(request_from(args), exit_reason=reason, resource_status=resource, **metadata)
    monkeypatch.setattr(runner, "_command", command)
    case = Case(function_name="f", function_args=[], expected_return="1")
    if not valid:
        with pytest.raises(runner.IsolatedRunnerUnavailable):
            await code_interpreter.run_sandbox("def f(): return 0", [case])
        return
    raw = (await code_interpreter.run_sandbox("def f(): return 0", [case])).results[0]
    assert raw.executed and not raw.passed and raw.exit_reason == reason and raw.resource_status == resource
    assert raw.cgroup_oom_kills == metadata.get("cgroup_oom_kills")
    assert raw.cgroup_pid_denials == metadata.get("cgroup_pid_denials")
