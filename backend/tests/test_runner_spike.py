from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from backend.tools.grading_runner import RunnerResult
from tools.runner_spike import harness
from tools.runner_spike.harness import (
    DEFAULT_PIDS_LIMIT,
    build_container_command,
)
from tools.runner_spike.worker import execute_request


def _request(code: str, *, max_output_chars: int = 4096) -> dict:
    return {
        "schema_version": 1,
        "execution_id": "runner-spike-test",
        "task_type": "sympy",
        "language": "python",
        "code": code,
        "test_cases": [],
        "limits": {
            "timeout_seconds": 1.0,
            "memory_mb": 256,
            "max_output_chars": max_output_chars,
        },
    }


def test_oci_command_contains_every_week_one_isolation_control(tmp_path: Path):
    request_file = tmp_path / "request.json"
    request_file.write_text("{}", encoding="utf-8")

    command = build_container_command(
        runtime="docker",
        image="runner:test",
        request_file=request_file,
        container_name="runner-spike-test",
        memory_mb=256,
    )

    assert "--network=none" in command
    assert "--read-only" in command
    assert "--ipc=none" in command
    assert "--cap-drop=ALL" in command
    assert "--security-opt=no-new-privileges=true" in command
    assert f"--pids-limit={DEFAULT_PIDS_LIMIT}" in command
    assert "--memory=256m" in command
    assert "--cpus=0.5" in command
    assert "--user=65534:65534" in command
    assert any(
        item.startswith("--tmpfs=/work:rw,nosuid,nodev,noexec") for item in command
    )
    mount = next(item for item in command if item.startswith("--mount="))
    assert "target=/input/request.json" in mount
    assert mount.endswith(",readonly")
    assert not any(item.startswith("--env-file") for item in command)
    assert not any("SMARTAI_" in item or "DATABASE_URL" in item for item in command)


def test_podman_command_disables_implicit_writable_tmpfs_and_proxy_env(
    tmp_path: Path,
):
    request_file = tmp_path / "request.json"
    request_file.write_text("{}", encoding="utf-8")

    command = build_container_command(
        runtime="/usr/bin/podman",
        image="runner:test",
        request_file=request_file,
        container_name="runner-spike-test",
        memory_mb=256,
    )

    assert any("mode=1777" in item and "uid=" not in item for item in command if item.startswith("--tmpfs="))
    assert "--read-only-tmpfs=false" in command
    assert "--http-proxy=false" in command
    assert "--security-opt=no-new-privileges" in command
    mount = next(item for item in command if item.startswith("--mount="))
    assert mount.endswith(",readonly,relabel=private")


@pytest.mark.parametrize("runtime", ["docker", "podman"])
def test_env_probe_inspects_the_same_container_before_start(monkeypatch, runtime):
    name = "spike-test-name"
    env = {harness._SECRET_ENV_NAME: "test-canary"}
    command = [runtime, "run", "--network=none", f"--name={name}", "image-id"]
    calls = []
    entries = [f"{k}={v}" for k, v in harness._PROBE_CHILD_ENV.items()]

    container_id = "d" * 64

    def runtime_output(args, **options):
        calls.append(args)
        assert options["env"] is env
        if args[1] == "create":
            return container_id + "\n"
        if args[1] == "container":
            return "sha256:" + "b" * 64 + " " + json.dumps(entries)
        return json.dumps(entries)

    monkeypatch.setattr(harness.subprocess, "check_output", runtime_output)
    start = harness._verified_env_probe_start(command, env)
    assert calls == [[runtime, "create", *command[2:]],
                     [runtime, "container", "inspect", "--format", "{{.Image}} {{json .Config.Env}}", container_id],
                     [runtime, "image", "inspect", "--format", "{{json .Config.Env}}", "sha256:" + "b" * 64]]
    assert start == [runtime, "start", "--attach", container_id]


@pytest.mark.parametrize("change", [
    lambda entries: None, lambda entries: [], lambda entries: entries + [123],
    lambda entries: entries + ["AWS_SECRET_ACCESS_KEY=private-value"],
    lambda entries: entries + ["HOME=/work"], lambda entries: entries + ["MALFORMED"],
    # Even a legitimate-looking public fingerprint must not contain the canary.
    lambda entries: entries + ["GPG_KEY=deadbeef" + "a" * 32],
    lambda entries: entries + ["PYTHON_SHA256=" + "9" * 64],
    lambda entries: [s if not s.startswith("PATH=") else "PATH=/tmp:/bin" for s in entries],
])
def test_env_probe_rejects_unverified_config_without_printing_values(monkeypatch, change):
    entries = [f"{k}={v}" for k, v in harness._PROBE_CHILD_ENV.items()]
    outputs = iter(["d" * 64, "b" * 64 + " " + json.dumps(change(entries)), json.dumps(entries)])
    monkeypatch.setattr(harness.subprocess, "check_output", lambda *a, **kw: next(outputs))
    with pytest.raises(harness.RunnerSpikeError) as exc:
        harness._verified_env_probe_start(["podman", "run"], {harness._SECRET_ENV_NAME: "a" * 32})
    assert "private-value" not in str(exc.value) and "a" * 32 not in str(exc.value)


@pytest.mark.parametrize("replies", [
    ["some-container-name"],
    ["d" * 64, "mutable-tag []"],
    ["d" * 64, "b" * 64 + " invalid-json"],
])
def test_env_probe_requires_concrete_ids_and_parseable_inspection(monkeypatch, replies):
    outputs = iter(replies)
    monkeypatch.setattr(harness.subprocess, "check_output", lambda *a, **kw: next(outputs))
    with pytest.raises(harness.RunnerSpikeError):
        harness._verified_env_probe_start(["podman", "run"], {harness._SECRET_ENV_NAME: "test-canary"})


@pytest.mark.skipif(sys.platform != "linux", reason="Linux container worker uses Linux resource-limit semantics; OCI command tests remain cross-platform")
def test_worker_clears_launcher_environment_and_matches_runner_contract(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("SMARTAI_SPIKE_PRODUCTION_SECRET", "do-not-leak")
    request = _request(
        "import os\n"
        "print(os.environ.get('SMARTAI_SPIKE_PRODUCTION_SECRET', 'missing'))"
    )

    raw_result = execute_request(request, workdir=tmp_path)
    result = RunnerResult.model_validate(raw_result)

    assert os.environ["SMARTAI_SPIKE_PRODUCTION_SECRET"] == "do-not-leak"
    assert result.exit_reason.value == "success"
    assert result.stdout.strip() == "missing"
    assert result.isolation_mode == "oci_container"
    assert not (tmp_path / "main.py").exists()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux container worker uses Linux resource-limit semantics; OCI command tests remain cross-platform")
def test_worker_enforces_wall_timeout_without_returning_a_host_path(tmp_path: Path):
    request = _request("while True:\n    pass\n")
    request["limits"]["timeout_seconds"] = 0.1

    result = RunnerResult.model_validate(execute_request(request, workdir=tmp_path))

    assert result.exit_reason.value == "timeout"
    assert result.resource_status.value == "timeout"
    assert str(tmp_path) not in result.stderr


@pytest.mark.skipif(sys.platform != "linux", reason="Linux container worker uses Linux resource-limit semantics; OCI command tests remain cross-platform")
def test_worker_bounds_output_before_serializing_it(tmp_path: Path):
    result = RunnerResult.model_validate(
        execute_request(
            _request("print('x' * 10000)", max_output_chars=256),
            workdir=tmp_path,
        )
    )

    assert result.exit_reason.value == "output_limit"
    assert result.stdout_truncated is True
    assert len(result.stdout) <= 256


@pytest.mark.skipif(os.name != "posix", reason="POSIX worker return descriptor")
def test_worker_owns_status_and_keeps_function_return_separate(tmp_path, monkeypatch):
    from tools.runner_spike import worker

    if sys.platform != "linux":
        # Mac can exercise the real supervision/wiring with a reviewed synthetic
        # fixture, but its RLIMIT_AS semantics cannot validate Linux resources.
        monkeypatch.setattr(worker, "_apply_child_limits", lambda *_: None)
    request = _request("print('startup')\ndef add(a, *, b):\n print('fake runner: passed=100'); return a+b")
    request.update(task_type="programming", function_call={"name":"add", "args":[3], "kwargs":{"b":4}}, bind_request=True)
    result = RunnerResult.model_validate(execute_request(request, workdir=tmp_path))
    assert result.passed_count == result.total_count == 1
    assert result.return_value == "7" and result.stdout == "startup\nfake runner: passed=100\n"
    assert result.request_digest == worker.request_digest(request)


@pytest.mark.skipif(os.name != "posix", reason="POSIX worker return descriptor")
def test_worker_uncomparable_return_is_an_evaluator_limit(tmp_path, monkeypatch):
    from tools.runner_spike import worker
    if sys.platform != "linux":
        monkeypatch.setattr(worker, "_apply_child_limits", lambda *_: None)
    request = _request("def f(): return frozenset({1, 2})")
    request.update(task_type="programming", function_call={"name":"f", "args":[], "kwargs":{}}, bind_request=True)
    result = RunnerResult.model_validate(execute_request(request, workdir=tmp_path))
    assert not result.executed and result.total_count == result.passed_count == 0
    assert result.exit_reason == "internal_error" and result.stderr == "programming_return_value_unavailable"
    assert result.request_digest == worker.request_digest(request)


@pytest.mark.skipif(os.name != "posix", reason="POSIX private worker channels")
@pytest.mark.parametrize(("code", "call", "reason"), [
    ("def f(x): return (", {"name":"f", "args":[2], "kwargs":{}}, "syntax_error"),
    ("return 4", None, "syntax_error"),
    ("from operator import add as plus\ndef helper(x): return x", {"name":"f", "args":[2], "kwargs":{}}, "target_function_missing"),
    ("def f(x: int, /, *, y: int): return x+y", {"name":"f", "args":[], "kwargs":{"x":2, "y":3}}, "signature_mismatch"),
    ("from __future__ import annotations\ndef f(xs: list[int]) -> int: return len(xs)",
     {"name":"f", "args":[], "kwargs":{}}, "signature_mismatch"),
    ("import random\ndef helper(): return 2", {"name":"f", "args":[], "kwargs":{}}, "target_function_missing"),
    ("from random import nonexistent_u3_symbol\ndef helper(): return 2",
     {"name":"f", "args":[], "kwargs":{}}, "runtime_error"),
    ("def f(x: unavailable_type): return x", {"name":"f", "args":[], "kwargs":{}}, "runtime_error"),
    ("def f(x=1/0): return x", {"name":"f", "args":[], "kwargs":{}}, "runtime_error"),
    ("import random as f\ndef helper(): return 2", {"name":"f", "args":[], "kwargs":{}}, "runtime_error"),
    ("import random\nprint('target_function_missing')\nraise KeyError('f')",
     {"name":"f", "args":[], "kwargs":{}}, "runtime_error"),
    ("import os,sys\nos.write(int(sys.argv[2]), bytes([0]))\nraise KeyError('f')",
     {"name":"f", "args":[], "kwargs":{}}, "runtime_error"),
    ("def f(x): raise TypeError('missing required positional argument')", {"name":"f", "args":[1], "kwargs":{}}, "runtime_error"),
    ("def f(*args, **kwargs): raise MemoryError('oom_kill 10')", {"name":"f", "args":[1], "kwargs":{}}, "runtime_error"),
    ("def f(x):\n print('target_function_missing'); raise SyntaxError()", {"name":"f", "args":[1], "kwargs":{}}, "runtime_error"),
    ("def f(x): return 2\nf = lambda: 1/0", {"name":"f", "args":[], "kwargs":{}}, "runtime_error"),
])
def test_failed_case_diagnoses_use_original_source_not_student_messages(tmp_path, monkeypatch, code, call, reason):
    from tools.runner_spike import worker
    if sys.platform != "linux":
        monkeypatch.setattr(worker, "_apply_child_limits", lambda *_: None)
    request = _request(code)
    request.update(bind_request=True, task_type="programming", function_call=call)
    result = execute_request(request, workdir=tmp_path)
    assert result["executed"] and result["passed_count"] == 0
    assert result["exit_reason"] == reason and result["resource_status"] == "unknown"
    assert result["process_exit_code"] > 0 and result["termination_signal"] is None


@pytest.mark.skipif(os.name != "posix", reason="POSIX signal evidence")
@pytest.mark.parametrize(("before","after","reason"), [
    ((2,5), (3,5), "memory_limit"),
    ((2,5), (2,6), "pid_limit"),
    ((2,5), (2,5), "internal_error"),
    ((None,None), (None,None), "internal_error"),
])
def test_killed_case_requires_controller_evidence_for_resource_attribution(tmp_path, monkeypatch, before, after, reason):
    from tools.runner_spike import worker
    if sys.platform != "linux":
        monkeypatch.setattr(worker, "_apply_child_limits", lambda *_: None)
    counters = iter([before, after])
    monkeypatch.setattr(worker, "_cgroup_events", lambda: next(counters))
    request = _request("import os, signal\nos.kill(os.getpid(), signal.SIGKILL)")
    request.update(bind_request=True, task_type="programming")
    result = execute_request(request, workdir=tmp_path, track_resources=True)
    assert result["exit_reason"] == reason and result["termination_signal"] == 9
    assert result["process_exit_code"] is None
    assert result["resource_status"] == ("unknown" if reason == "internal_error" else reason + "_exceeded")
    assert result.get("cgroup_oom_kills") == (None if before[0] is None else after[0] - before[0])
    assert result.get("cgroup_pid_denials") == (None if before[1] is None else after[1] - before[1])


@pytest.mark.skipif(os.name != "posix", reason="POSIX worker contract")
@pytest.mark.parametrize(("delta", "reason"), [((1,0), "memory_limit"), ((0,1), "pid_limit")])
def test_controller_limit_overrides_apparent_success(tmp_path, monkeypatch, delta, reason):
    from tools.runner_spike import worker
    if sys.platform != "linux":
        monkeypatch.setattr(worker, "_apply_child_limits", lambda *_: None)
    counters = iter([(0,0), delta])
    monkeypatch.setattr(worker, "_cgroup_events", lambda: next(counters))
    request = _request("print('normal completion')")
    request.update(bind_request=True, task_type="programming")
    result = execute_request(request, workdir=tmp_path, track_resources=True)
    assert result["executed"] and result["process_exit_code"] == 0 and result["passed_count"] == 0
    assert result["exit_reason"] == reason and result["resource_status"] == reason + "_exceeded"
