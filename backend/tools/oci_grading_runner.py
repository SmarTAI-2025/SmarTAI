"""Thin OCI transport: one container per test, existing grading comparisons unchanged.

Only a read-only request file enters the container; runtime stderr never crosses
this boundary. This does not grant the web process a rootful Docker socket.
Production requires a rootless runtime with seccomp and a pinned image digest.
"""
from __future__ import annotations

import asyncio
import contextlib
try:
    import fcntl
except ImportError:  # OCI execution is supported on Linux hosts only.
    fcntl = None
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid

from backend.config import settings
from tools.runner_spike.harness import build_container_command


class IsolatedRunnerUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("programming_execution_unavailable")


def launcher_environment() -> dict[str, str]:
    # Runtime discovery requires the service user's HOME/XDG path, not the
    # website's database credentials, provider keys or inherited proxy settings.
    env = {key: os.environ[key] for key in ("PATH", "HOME", "XDG_RUNTIME_DIR") if key in os.environ}
    docker_host = os.environ.get("DOCKER_HOST", "")
    if docker_host:
        if not docker_host.startswith("unix://"):
            raise IsolatedRunnerUnavailable()
        env["DOCKER_HOST"] = docker_host
    return env


async def _bounded_read(stream, limit=300_000):
    chunks, length = [], 0
    while chunk := await stream.read(8192):
        length += len(chunk)
        if length > limit:
            raise IsolatedRunnerUnavailable()
        chunks.append(chunk)
    return b"".join(chunks)


async def _command(args, *, timeout, env):
    process = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
    )
    reads = asyncio.gather(_bounded_read(process.stdout), _bounded_read(process.stderr))
    try:
        stdout, _stderr = await asyncio.wait_for(reads, timeout)
        await asyncio.wait_for(process.wait(), timeout)
        if process.returncode:
            raise IsolatedRunnerUnavailable()
        return stdout
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
        if not reads.done():
            reads.cancel()
        with contextlib.suppress(BaseException):
            await reads


async def _check_runtime(runtime, env):
    if settings.runtime_environment != "production":
        return
    if not re.fullmatch(r"(?:[a-zA-Z0-9._:/-]+@)?sha256:[a-f0-9]{64}", settings.program_runner_image):
        raise IsolatedRunnerUnavailable()
    info = json.loads(await _command([runtime, "info", "--format", "{{json .}}"], timeout=10, env=env))
    if Path(runtime).name == "podman":
        security = info.get("host", {}).get("security", {})
        safe = security.get("rootless") is True and security.get("seccompEnabled") is True and info.get("host", {}).get("cgroupVersion") == "v2"
    else:
        options = info.get("SecurityOptions", [])
        safe = any("name=rootless" in str(option) for option in options) and any("name=seccomp" in str(option) for option in options)
    if Path(runtime).name == "docker":
        safe = safe and info.get("CgroupVersion") == "2" and info.get("CgroupDriver") == "systemd" and all(info.get(key) is True for key in ("MemoryLimit", "CpuCfsQuota", "PidsLimit"))
    if not safe:
        raise IsolatedRunnerUnavailable()


@contextlib.asynccontextmanager
async def _host_slot():
    # flock spans API workers; only this service user's slots are shared. No
    # student identifiers or source data enter lock filenames. Kernel releases
    # a slot when a process exits; containers additionally get an inner timeout.
    if fcntl is None:
        raise IsolatedRunnerUnavailable()
    directory = Path(tempfile.gettempdir()) / f"smartai-oci-slots-{os.getuid()}"
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
        raise IsolatedRunnerUnavailable()
    handle = None
    deadline = asyncio.get_running_loop().time() + 120
    try:
        while handle is None:
            for index in range(settings.program_runner_max_concurrent):
                fd = os.open(directory / str(index), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                candidate = os.fdopen(fd, "a")
                try:
                    fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    handle = candidate
                    break
                except BlockingIOError:
                    candidate.close()
            if handle is None:
                if asyncio.get_running_loop().time() >= deadline:
                    raise IsolatedRunnerUnavailable()
                await asyncio.sleep(0.05)
        yield
    finally:
        if handle is not None:
            handle.close()


async def _remove(runtime, name, env):
    with contextlib.suppress(Exception):
        await _command([runtime, "rm", "--force", name], timeout=5, env=env)


async def run_python_oci(code, test_input="", *, timeout=10.0, memory_mb=256, max_output_chars=8192):
    from backend.models import TestCase
    from backend.tools.code_interpreter import TestResult
    from backend.tools.grading_runner import RunnerResult
    if settings.program_runner_mode != "oci":
        raise IsolatedRunnerUnavailable()
    runtime = shutil.which(settings.program_runner_runtime)
    if not runtime or not settings.program_runner_image:
        raise IsolatedRunnerUnavailable()
    if not 0.05 <= timeout <= 60 or not 64 <= memory_mb <= 512 or not 256 <= max_output_chars <= 65536:
        raise IsolatedRunnerUnavailable()
    execution_id = uuid.uuid4().hex
    name = f"smartai-oci-{execution_id}"
    payload = {"schema_version": 1, "execution_id": execution_id, "task_type": "programming",
               "language": "python", "code": code, "test_input": test_input, "test_cases": [],
               "limits": {"timeout_seconds": timeout, "memory_mb": memory_mb, "max_output_chars": max_output_chars}}
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(encoded) > 256 * 1024:
        raise IsolatedRunnerUnavailable()
    try:
        env = launcher_environment()
        await _check_runtime(runtime, env)
        async with _host_slot():
            with tempfile.TemporaryDirectory(prefix="smartai-oci-") as tmp:
                request = Path(tmp) / "request.json"
                request.write_bytes(encoded)
                request.chmod(0o444)
                args = build_container_command(runtime=runtime, image=settings.program_runner_image,
                    request_file=request, container_name=name, memory_mb=memory_mb)
                args[2:2] = ["--pull=never", "--log-driver=none", f"--memory-swap={memory_mb}m", "--label=smartai.isolated-runner=true"]
                try:
                    raw = await _command(args, timeout=timeout + 12, env=env)
                    # Strict model; do not accept a trailing forged result after
                    # malformed output, a different execution ID, or host mode.
                    result = RunnerResult.model_validate_json(raw)
                    if result.execution_id != execution_id or result.isolation_mode != "oci_container" or not result.executed:
                        raise IsolatedRunnerUnavailable()
                    if result.exit_reason not in {"success", "runtime_error", "timeout", "output_limit", "syntax_error"}:
                        raise IsolatedRunnerUnavailable()
                    if len(result.stdout) > max_output_chars or len(result.stderr) > max_output_chars:
                        raise IsolatedRunnerUnavailable()
                    return TestResult(test=TestCase(input=test_input), passed=result.exit_reason == "success",
                        actual_output=result.stdout, error=result.stderr, duration_ms=result.duration_ms,
                        exit_reason=result.exit_reason.value, stdout_truncated=result.stdout_truncated,
                        stderr_truncated=result.stderr_truncated, isolation_mode="oci_container")
                finally:
                    # Cancellation must not release capacity while a container
                    # still runs. Cleanup is shielded and awaited before return.
                    cleanup = asyncio.create_task(_remove(runtime, name, env))
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        await cleanup
                        raise
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise IsolatedRunnerUnavailable() from exc
