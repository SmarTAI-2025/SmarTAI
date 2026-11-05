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
import logging
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid

from backend.config import settings
from tools.runner_spike.harness import build_container_command
from tools.runner_spike.worker import request_digest


logger = logging.getLogger(__name__)


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


def _reject_duplicate_fields(pairs):
    fields = {}
    for key, value in pairs:
        if key in fields:
            raise IsolatedRunnerUnavailable()
        fields[key] = value
    return fields


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
    # Worker JSON can escape each Unicode scalar or control character in all
    # three bounded channels (stdout, stderr and a function return).
    reads = asyncio.gather(_bounded_read(process.stdout, 3 * 65_536 * 12 + 4096), _bounded_read(process.stderr))
    try:
        stdout, _stderr = await asyncio.wait_for(reads, timeout)
        await asyncio.wait_for(process.wait(), timeout)
        if process.returncode:
            logger.warning("OCI command failed: operation=%s exit=%s", args[1], process.returncode)
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
        host = info.get("host", {})
        safe = (security.get("rootless") is True and security.get("seccompEnabled") is True
                and host.get("cgroupVersion") == "v2" and host.get("cgroupManager") == "systemd"
                and {"cpu", "memory", "pids"}.issubset(host.get("cgroupControllers", [])))
    else:
        options = info.get("SecurityOptions", [])
        safe = any("name=rootless" in str(option) for option in options) and any("name=seccomp" in str(option) for option in options)
    if Path(runtime).name == "docker":
        safe = safe and info.get("CgroupVersion") == "2" and info.get("CgroupDriver") == "systemd" and all(info.get(key) is True for key in ("MemoryLimit", "CpuCfsQuota", "PidsLimit"))
    if not safe:
        raise IsolatedRunnerUnavailable()


def _store_slot(handle, container_name):
    handle.seek(0)
    handle.truncate()
    handle.write(container_name)
    handle.flush()
    os.fsync(handle.fileno())


async def _remove(runtime, name, env):
    try:
        # A cancelled grader must terminate the student immediately. Podman
        # otherwise waits its container stop timeout, longer than our CLI
        # deadline, and can leave the container stuck in Stopping.
        stop = ["--time=0"] if Path(runtime).name == "podman" else []
        await _command([runtime, "rm", "--force", *stop, name], timeout=10, env=env)
    except Exception:
        # --rm may already have removed it. Only a successful, exact absence
        # query counts as cleanup evidence; a daemon failure is not absence.
        listed = await _command([runtime, "ps", "--all", "--filter", f"name={name}",
                                 "--format", "{{.Names}}"], timeout=5, env=env)
        if listed.strip():
            raise IsolatedRunnerUnavailable()


@contextlib.asynccontextmanager
async def _host_slot(runtime, env, name):
    if fcntl is None:
        raise IsolatedRunnerUnavailable()
    directory = Path(tempfile.gettempdir()) / f"smartai-oci-slots-{os.getuid()}"
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
        raise IsolatedRunnerUnavailable()
    deadline = asyncio.get_running_loop().time() + 120
    quarantined = set()
    while True:
        for index in range(settings.program_runner_max_concurrent):
            if index in quarantined:
                continue
            fd = os.open(directory / str(index), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "r+") as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                await _check_runtime(runtime, env)
                previous = handle.read(128)
                if previous:
                    if not re.fullmatch(r"smartai-oci-[0-9a-f]{32}", previous):
                        quarantined.add(index)
                        continue
                    try:
                        await _remove(runtime, previous, env)
                    except Exception:
                        quarantined.add(index)
                        continue
                # Durable before launch, including on process death. Failure to
                # remove the old container keeps this capacity quarantined.
                _store_slot(handle, name)
                try:
                    yield
                finally:
                    cleanup = asyncio.create_task(_remove(runtime, name, env))
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        await cleanup
                        _store_slot(handle, "")
                        raise
                    _store_slot(handle, "")
                return
        if len(quarantined) >= settings.program_runner_max_concurrent or asyncio.get_running_loop().time() >= deadline:
            raise IsolatedRunnerUnavailable()
        await asyncio.sleep(.05)


async def run_python_oci(code, test_input="", *, timeout=10.0, memory_mb=256, max_output_chars=8192,
                         function_call=None, bind_request=False):
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
    if function_call is not None:
        payload["function_call"] = function_call
        bind_request = True
    if bind_request:
        payload["bind_request"] = True
    digest = request_digest(payload)
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) > 256 * 1024:
        raise IsolatedRunnerUnavailable()
    try:
        env = launcher_environment()
        async with _host_slot(runtime, env, name):
            with tempfile.TemporaryDirectory(prefix="smartai-oci-") as tmp:
                request = Path(tmp) / "request.json"
                request.write_bytes(encoded)
                request.chmod(0o444)
                args = build_container_command(runtime=runtime, image=settings.program_runner_image,
                    request_file=request, container_name=name, memory_mb=memory_mb)
                args[2:2] = ["--pull=never", "--log-driver=none", f"--memory-swap={memory_mb}m", "--label=smartai.isolated-runner=true"]
                raw = await _command(args, timeout=timeout + 12, env=env)
                # Strict model; do not accept a trailing forged result after
                # malformed output, a different execution ID, or host mode.
                # Pydantic accepts repeated keys by choosing the last. Reject
                # ambiguity in evidence before validating the strict wire types.
                json.loads(raw, object_pairs_hook=_reject_duplicate_fields)
                result = RunnerResult.model_validate_json(raw, strict=True)
                mandatory = {"schema_version", "execution_id", "executed", "exit_reason", "passed_count", "total_count",
                             "stdout", "stderr", "duration_ms", "resource_status", "isolation_mode"}
                if not mandatory.issubset(result.model_fields_set):
                    raise IsolatedRunnerUnavailable()
                if result.execution_id != execution_id or result.isolation_mode != "oci_container" or not result.executed:
                    raise IsolatedRunnerUnavailable()
                if result.exit_reason not in {"success", "runtime_error", "timeout", "output_limit", "syntax_error"}:
                    raise IsolatedRunnerUnavailable()
                success = result.exit_reason == "success"
                if (result.total_count != 1 or result.passed_count != int(success)
                        or (bind_request and result.request_digest != digest)
                        or (success and (result.stdout_truncated or result.stderr_truncated))
                        or ((function_call is not None and success) != (result.return_value is not None))
                        or (result.return_value is not None and len(result.return_value) > max_output_chars)):
                    raise IsolatedRunnerUnavailable()
                expected_status = {"success": "within_limits", "runtime_error": "unknown", "syntax_error": "unknown",
                                   "timeout": "timeout", "output_limit": "output_limit"}
                if result.resource_status != expected_status[result.exit_reason.value]:
                    raise IsolatedRunnerUnavailable()
                if len(result.stdout) > max_output_chars or len(result.stderr) > max_output_chars:
                    raise IsolatedRunnerUnavailable()
                return TestResult(test=TestCase(input=test_input), passed=success, resource_status=result.resource_status.value,
                    actual_output=result.stdout, stdout=result.stdout, return_value=result.return_value,
                    execution_id=execution_id, request_digest=digest, error=result.stderr, duration_ms=result.duration_ms,
                    exit_reason=result.exit_reason.value, stdout_truncated=result.stdout_truncated,
                    stderr_truncated=result.stderr_truncated, isolation_mode="oci_container")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise IsolatedRunnerUnavailable() from exc
