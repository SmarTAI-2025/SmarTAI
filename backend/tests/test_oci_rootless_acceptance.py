"""Opt-in: dedicated disposable Linux account with a pinned, local Podman image."""
import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tempfile

import pytest

from backend.config import settings
from backend.models import TestCase as Case
from backend.tools.code_interpreter import run_sandbox
from backend.tools.oci_grading_runner import (
    IsolatedRunnerUnavailable,
    launcher_environment,
    run_python_oci,
)


pytestmark = [
    pytest.mark.skipif(os.getenv("SMARTAI_OCI_ROOTLESS_QA") != "1",
                       reason="Requires a disposable, dedicated rootless Podman account"),
    pytest.mark.asyncio,
]


def podman(*args):
    return subprocess.check_output(
        ["podman", *args], text=True, env=launcher_environment(), timeout=10
    ).strip()


def container_names(*, all_states=False):
    args = ["--all"] if all_states else []
    return podman("ps", *args, "--filter", "label=smartai.isolated-runner=true",
                  "--format", "{{.Names}}").splitlines()


@pytest.fixture(autouse=True)
def dedicated_runtime(monkeypatch):
    assert sys.platform == "linux" and os.geteuid() != 0
    assert "DOCKER_HOST" not in os.environ and "CONTAINER_HOST" not in os.environ
    pinned = os.environ["SMARTAI_PROGRAM_RUNNER_IMAGE"]
    assert re.fullmatch(r"sha256:[a-f0-9]{64}", pinned)
    assert podman("image", "inspect", pinned, "--format", "{{.Id}}").removeprefix("sha256:") == pinned[7:]
    assert not podman("ps", "-aq"), "Use an empty, dedicated Podman account"
    monkeypatch.setattr(settings, "runtime_environment", "production")
    monkeypatch.setattr(settings, "program_runner_mode", "oci")
    monkeypatch.setattr(settings, "program_runner_runtime", "podman")
    monkeypatch.setattr(settings, "program_runner_image", pinned)
    monkeypatch.setattr(settings, "program_runner_max_concurrent", 2)
    yield
    assert not container_names(all_states=True)


async def test_case_binding_debug_and_wrong_answer():
    source = "def first(): return 42\ndef scale(x):\n print('debug', x)\n return x * 3"
    report = await run_sandbox(source, [
        Case(function_name="scale", function_args=[4], expected_return="12"),
        Case(function_name="scale", function_args=[9], expected_return="100"),
        Case(function_name="first", function_args=[], expected_return="42"),
    ])
    assert (report.passed_count, report.total_count) == (2, 3)
    good, wrong, other = report.results
    assert wrong.exit_reason == "wrong_answer"
    assert (other.case_index, other.passed, other.return_value) == (2, True, "42")
    assert (good.case_index, good.passed, good.stdout, good.return_value) == (0, True, "debug 4\n", "12")
    assert (wrong.case_index, wrong.passed, wrong.stdout, wrong.return_value) == (1, False, "debug 9\n", "27")
    for case in report.results:
        assert case.isolation_mode == "oci_container" and case.executed
        assert re.fullmatch(r"[a-f0-9]{64}", case.request_digest)
    assert good.execution_id != wrong.execution_id and good.request_digest != wrong.request_digest
    # Exercise the transport's keyword-only ABI independently of upstream case preparation.
    direct = await run_python_oci("def f(xs, *, n): return sum(xs) * n",
                                  function_call={"name": "f", "args": [[2, 3]], "kwargs": {"n": 2}})
    assert direct.passed and direct.return_value == "10"


async def test_stdin_continues_after_student_error():
    report = await run_sandbox("print(20 // int(input()))", [
        Case(input="0", expected_output="0"), Case(input="4", expected_output="5"),
    ])
    bad, good = report.results
    assert (bad.passed, bad.executed, bad.exit_reason) == (False, True, "runtime_error")
    assert (good.passed, good.executed, good.stdout) == (True, True, "5\n")
    assert "/work/main.py" not in bad.error


async def test_kernel_and_mount_boundaries(tmp_path, monkeypatch, record_property):
    canary = tmp_path / "synthetic-guest-only"
    canary.write_text("QA-only file sentinel")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "synthetic-qa-only")
    # A listening host socket is a positive control even on a VM without a NIC.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        with socket.create_connection(listener.getsockname(), timeout=1):
            pass
        code = f"""
import json, os, pathlib, resource, socket
P = pathlib.Path
cg = P('/sys/fs/cgroup')
status = dict(line.split(':', 1) for line in P('/proc/self/status').read_text().splitlines())
def flags(path):
    return os.statvfs(path).f_flag
print(json.dumps(dict(
    uid=os.geteuid(), cap=int(status['CapEff'], 16), nnp=int(status['NoNewPrivs']),
    seccomp=int(status['Seccomp']), root_ro=bool(flags('/') & os.ST_RDONLY),
    input_ro=bool(flags('/input/request.json') & os.ST_RDONLY),
    work_flags=flags('/work') & (os.ST_NOEXEC | os.ST_NODEV | os.ST_NOSUID),
    work_bytes=os.statvfs('/work').f_blocks * os.statvfs('/work').f_frsize,
    host_file=P({str(canary)!r}).exists(), runtime_socket=P('/run/podman/podman.sock').exists(),
    env_canary='AWS_SECRET_ACCESS_KEY' in os.environ,
    net_ns=os.stat('/proc/self/ns/net').st_ino, interfaces=sorted(os.listdir('/sys/class/net')),
    host_port_reachable=socket.socket().connect_ex(('127.0.0.1', {listener.getsockname()[1]})) == 0,
    memory=int((cg/'memory.max').read_text()), swap=int((cg/'memory.swap.max').read_text()),
    pids=int((cg/'pids.max').read_text()), cpu=(cg/'cpu.max').read_text().split(),
    address_space=resource.getrlimit(resource.RLIMIT_AS)[0],
    file_limit=resource.getrlimit(resource.RLIMIT_FSIZE)[0],
    nofile=resource.getrlimit(resource.RLIMIT_NOFILE)[0], core=resource.getrlimit(resource.RLIMIT_CORE)[0],
)))
"""
        result = await run_python_oci(code, memory_mb=64, bind_request=True)
    assert result.passed, result.error
    observed = json.loads(result.stdout)
    record_property("kernel", json.dumps(observed, sort_keys=True))
    net_ns = observed.pop("net_ns")
    assert net_ns != os.stat("/proc/self/ns/net").st_ino
    quota, period = map(int, observed.pop("cpu"))
    assert 2 * quota == period
    assert observed == dict(uid=65534, cap=0, nnp=1, seccomp=2, root_ro=True, input_ro=True,
                            work_flags=os.ST_NOEXEC | os.ST_NODEV | os.ST_NOSUID, work_bytes=16 << 20,
                            host_file=False, runtime_socket=False, env_canary=False, interfaces=["lo"],
                            host_port_reachable=False, memory=64 << 20, swap=0, pids=16,
                            address_space=64 << 20, file_limit=4 * 8192 + 1, nofile=64, core=0)


async def test_limits_enforced_and_next_case_runs(record_property):
    # Both workloads are bounded even if a limit regresses.
    memory = await run_python_oci("bytearray(96 * 1024 * 1024)", memory_mb=64)
    assert not memory.passed and "MemoryError" in memory.error
    record_property("memory_projection", f"{memory.exit_reason}/{memory.resource_status}")
    pids = await run_python_oci("""
import errno, os, signal, time
children = []
try:
    for _ in range(22):
        try:
            pid = os.fork()
        except OSError as exc:
            assert exc.errno == errno.EAGAIN
            print('pid-limit', len(children))
            break
        if not pid:
            time.sleep(3)
            os._exit(0)
        children.append(pid)
finally:
    for pid in children:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
""")
    # The script catches EAGAIN and exits normally. The trusted cgroup counter
    # must still override that outcome while preserving the child's stdout.
    assert not pids.passed and pids.process_exit_code == 0
    assert (pids.exit_reason, pids.resource_status) == ("pid_limit", "pid_limit_exceeded")
    assert pids.cgroup_pid_denials > 0 and pids.cgroup_oom_kills == 0
    assert pids.stdout.startswith("pid-limit ")
    record_property("caught_pid_denials", pids.cgroup_pid_denials)
    assert 0 < int(pids.stdout.split()[1]) <= 14
    record_property("pid_children", int(pids.stdout.split()[1]))
    timeout = await run_python_oci("while True: pass", timeout=.25)
    assert (timeout.passed, timeout.exit_reason, timeout.resource_status) == (False, "timeout", "timeout")
    for stream in ["stdout", "stderr"]:
        flood = await run_python_oci(f"import sys; sys.{stream}.write('x'*8192)", max_output_chars=256)
        assert flood.exit_reason == flood.resource_status == "output_limit" and not flood.passed
        assert getattr(flood, stream + "_truncated") and max(len(flood.stdout), len(flood.error)) <= 256
    assert (await run_python_oci("print('recovered')")).stdout == "recovered\n"


async def test_concurrent_cases_keep_their_own_work_and_stdin(record_property, monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "synthetic-qa-only")
    code = """
from pathlib import Path
import time
p = Path('/work/private')
with p.open('x') as f:
    f.write(input())
time.sleep(1.5)
print(p.read_text())
"""
    markers = ["first", "second", "third", "fourth"]
    tasks = [asyncio.create_task(run_python_oci(code, mark, bind_request=True)) for mark in markers]
    peak = 0
    try:
        while not all(job.done() for job in tasks):
            names = await asyncio.to_thread(container_names)
            if names and not peak:
                metadata = json.loads(await asyncio.to_thread(podman, "inspect", names[0]))[0]
                assert metadata["Image"].removeprefix("sha256:") == settings.program_runner_image[7:]
                assert metadata["HostConfig"]["NetworkMode"] == "none"
                binds = [m for m in metadata["Mounts"] if m["Type"] == "bind"]
                assert [(m["Destination"], m["RW"]) for m in binds] == [("/input/request.json", False)]
                assert all(not e.startswith("AWS_SECRET_ACCESS_KEY=") for e in metadata["Config"]["Env"])
            peak = max(peak, len(names))
            assert peak <= 2
            await asyncio.sleep(.05)
        results = await asyncio.gather(*tasks)
    finally:
        for job in tasks:
            job.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    record_property("observed_peak_running", peak)
    assert peak == 2
    assert [r.stdout.strip() for r in results] == markers and all(r.passed for r in results)
    assert len({r.execution_id for r in results}) == len({r.request_digest for r in results}) == 4


async def test_independent_backends_share_one_slot(record_property):
    # Each fresh interpreter imports the real adapter and waits for the same start
    # signal. Compare student intervals using the VM's common monotonic clock.
    backend = """
import asyncio, json, sys
from backend.config import settings
from backend.tools.oci_grading_runner import run_python_oci
settings.runtime_environment = 'production'
settings.program_runner_mode = 'oci'
settings.program_runner_runtime = 'podman'
settings.program_runner_image = sys.argv[1]
settings.program_runner_max_concurrent = 1
print('ready', flush=True)
input()
source = "import time; marker=input(); start=time.monotonic_ns(); time.sleep(1); print(marker, start, time.monotonic_ns())"
result = asyncio.run(run_python_oci(source, sys.argv[2], bind_request=True))
assert result.passed and result.isolation_mode == 'oci_container'
print(json.dumps([result.execution_id, result.stdout.split()]))
"""
    workers = []
    marks = ["worker-a", "worker-b"]
    try:
        async with asyncio.timeout(45):
            for mark in marks:
                workers.append(await asyncio.create_subprocess_exec(
                    sys.executable, "-c", backend, settings.program_runner_image, mark,
                    cwd=Path(__file__).resolve().parents[2], env=launcher_environment(),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                ))
            ready = await asyncio.gather(*(p.stdout.readline() for p in workers))
            assert ready == [b"ready\n", b"ready\n"]
            outputs = await asyncio.gather(*(p.communicate(b"start\n") for p in workers))
            assert all(p.returncode == 0 for p in workers)
    finally:
        for p in workers:
            if p.returncode is None:
                p.kill()
                await p.wait()
    cases = [json.loads(out) for out, _ in outputs]
    assert len({case[0] for case in cases}) == 2
    assert [case[1][0] for case in cases] == marks
    intervals = sorted((int(start), int(end)) for _, (_, start, end) in cases)
    record_property("worker_intervals_ns", json.dumps(intervals))
    assert all(end > start for start, end in intervals)
    assert intervals[0][1] <= intervals[1][0]


@pytest.mark.parametrize("missing", ["image", "runtime"])
async def test_unavailable_does_not_run_on_host(missing, tmp_path, monkeypatch):
    if missing == "image":
        monkeypatch.setattr(settings, "program_runner_image", "sha256:" + "0" * 64)
    else:
        monkeypatch.setattr(settings, "program_runner_runtime", "smartai-qa-no-such-runtime")
    host_marker = tmp_path / "unexpected-host-execution"
    with pytest.raises(IsolatedRunnerUnavailable, match="^programming_execution_unavailable$"):
        await run_sandbox(f"open({str(host_marker)!r}, 'w').close()", [Case(expected_output="")])
    assert not host_marker.exists()


async def test_08_kernel_oom_is_not_within_limits(record_property):
    # Both allocators are below the per-process limit. Together they exceed 64 MiB.
    # Prioritize the synthetic child so that its parent can report kernel counters.
    code = """
import json, subprocess, sys
from pathlib import Path
events = Path('/sys/fs/cgroup/memory.events')
def oom_kills():
    return int(dict(line.split() for line in events.read_text().splitlines())['oom_kill'])
before = oom_kills()
hold = bytearray(22 * 1024 * 1024)
child = subprocess.run([sys.executable, '-I', '-c',
    "from pathlib import Path; Path('/proc/self/oom_score_adj').write_text('1000'); x=bytearray(42*1024*1024)"],
    timeout=5)
print(json.dumps([child.returncode, oom_kills() - before]))
"""
    control = await run_python_oci(code, memory_mb=128)
    assert control.passed and json.loads(control.stdout) == [0, 0]
    limited = await run_python_oci(code, memory_mb=64)
    witness = json.loads(limited.stdout)
    assert witness == [-signal.SIGKILL, 1]
    record_property("oom_witness_child_rc_and_kills", json.dumps(witness))
    assert limited.process_exit_code == 0 and limited.cgroup_oom_kills == witness[1]
    record_property("supervisor_oom_kills", limited.cgroup_oom_kills)
    record_property("oom_projection", f"{limited.exit_reason}/{limited.resource_status}")
    assert (limited.passed, limited.exit_reason, limited.resource_status) == (
        False, "memory_limit", "memory_limit_exceeded")


async def test_08_pid_exhaustion_has_distinct_result(record_property):
    code = """
import os, time
children = []
try:
    for _ in range(22):
        pid = os.fork()
        if pid == 0:
            time.sleep(.5)
            os._exit(0)
        children.append(pid)
finally:
    for pid in children:
        os.waitpid(pid, 0)
"""
    result = await run_python_oci(code)
    assert result.process_exit_code > 0 and result.cgroup_pid_denials > 0
    record_property("uncaught_pid_denials", result.cgroup_pid_denials)
    record_property("pid_projection", f"{result.exit_reason}/{result.resource_status}")
    assert (result.passed, result.exit_reason, result.resource_status) == (
        False, "pid_limit", "pid_limit_exceeded")


async def test_cancel_a_live_child_and_reuse_only_slot(monkeypatch, record_property):
    monkeypatch.setattr(settings, "program_runner_max_concurrent", 1)
    task = asyncio.create_task(run_python_oci("import time; time.sleep(18)", timeout=25))
    name = None
    try:
        async with asyncio.timeout(20):
            while True:
                if task.done():
                    await task
                    pytest.fail("Student finished before the cancellation witness")
                names = await asyncio.to_thread(container_names)
                if names and "/work/main.py" in await asyncio.to_thread(podman, "top", names[0], "args"):
                    name = names[0]
                    break
                await asyncio.sleep(.1)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError, IsolatedRunnerUnavailable):
            await task
    assert re.fullmatch(r"smartai-oci-[a-f0-9]{32}", name)
    slot = Path(tempfile.gettempdir()) / f"smartai-oci-slots-{os.getuid()}" / "0"
    at_return = (task.cancelled(), name in container_names(all_states=True), slot.read_text() == "")
    record_property("cancelled_still_listed_slot_empty", json.dumps(at_return))
    # Observe without deleting: keep the immediate failure and allow this bounded
    # program to finish naturally so a failed run does not poison the next test.
    async with asyncio.timeout(35):
        while name in await asyncio.to_thread(container_names, all_states=True):
            await asyncio.sleep(.3)
    assert (await run_python_oci("print('slot reused')")).stdout == "slot reused\n"
    assert at_return == (True, False, True)


@pytest.mark.parametrize(("source", "args", "kwargs", "reason"), [
    pytest.param("def helper(x): return x", [], {}, "target_function_missing", id="absent-entry"),
    pytest.param("def solve(x, /, *, shift): return x+shift", [4], {}, "signature_mismatch",
                 id="missing-keyword-only"),
    pytest.param("def solve(x, /): return x", [], {"x": 3}, "signature_mismatch",
                 id="positional-only-by-keyword"),
    pytest.param("def solve(x): raise TypeError('missing required positional argument')",
                 [1], {}, "runtime_error", id="body-typeerror"),
    pytest.param("raise KeyError('solve')\ndef helper(): return 0", [], {}, "runtime_error",
                 id="module-fails-before-lookup"),
    pytest.param("def solve(:", [], {}, "syntax_error", id="malformed-source"),
    pytest.param("from __future__ import annotations\ndef solve(xs: list[int]) -> int: return len(xs)",
                 [], {}, "signature_mismatch", id="deferred-annotation-missing-argument"),
    pytest.param("def solve(x, /, *tail, scale=2, **kw): return (x+sum(tail)+kw['extra'])*scale",
                 [1, 2, 3], {"extra": 4, "scale": 3}, "success", id="variadic-success"),
    pytest.param("import random\ndef helper(): return 2", [], {}, "target_function_missing",
                 id="stdlib-import-absent-entry"),
])
async def test_function_failure_phase(source, args, kwargs, reason, record_property):
    result = await run_python_oci(source, function_call={"name": "solve", "args": args, "kwargs": kwargs})
    record_property("observed_reason", result.exit_reason)
    assert result.executed and result.isolation_mode == "oci_container"
    assert result.cgroup_oom_kills == result.cgroup_pid_denials == 0
    assert result.termination_signal is None
    assert result.exit_reason == reason
    if reason == "success":
        assert result.passed and result.process_exit_code == 0 and result.return_value == "30"
    else:
        assert not result.passed and result.process_exit_code == 1 and result.return_value is None


async def test_real_oci_callbacks_follow_completion_order(monkeypatch):
    from backend.tools import code_interpreter

    cases = [Case(input="3 4", expected_output="7"), Case(input="1 2", expected_output="3")]
    real_case = code_interpreter._run_case
    second_checkpointed = asyncio.Event()
    callbacks = []

    async def finish_second_first(code, case, timeout, memory):
        # Both results come from real containers. Only delivery of the first
        # result is gated, so startup speed cannot decide callback order.
        row = await real_case(code, case, timeout, memory)
        if case is cases[0]:
            await asyncio.wait_for(second_checkpointed.wait(), timeout=30)
        return row

    async def checkpoint(row):
        callbacks.append((row.case_id, row.case_index, row.execution_id, row.actual_output))
        if row.case_index == 1:
            second_checkpointed.set()

    monkeypatch.setattr(code_interpreter, "_run_case", finish_second_first)
    report = await run_sandbox("print(sum(map(int, input().split())))", cases, on_case_complete=checkpoint)
    assert (report.passed_count, report.total_count) == (2, 2)
    first, second = report.results
    assert first.isolation_mode == second.isolation_mode == "oci_container"
    assert first.execution_id != second.execution_id
    assert [(r.case_id, r.case_index, r.actual_output) for r in report.results] == [
        ("case-1", 0, "7\n"), ("case-2", 1, "3\n")]
    assert callbacks == [
        ("case-2", 1, second.execution_id, "3\n"), ("case-1", 0, first.execution_id, "7\n")]
