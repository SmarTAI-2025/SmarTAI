#!/usr/bin/env python3
"""Small, dependency-free worker executed inside the spike container.

The host harness supplies one versioned runner request through a read-only
bind mount.  This worker deliberately does not import the web application, so
the container image has no reason to contain application settings or secrets.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import time
import tempfile
from contextlib import ExitStack
from pathlib import Path
from typing import Any

if sys.platform != "win32":
    import resource


SCHEMA_VERSION = 1
MAX_CODE_CHARS = 64 * 1024
MAX_REQUEST_BYTES = 256 * 1024
MAX_OUTPUT_CHARS = 65_536
MAX_MEMORY_MB = 512
MAX_TIMEOUT_SECONDS = 60.0
DEFAULT_WORKDIR = Path("/work")

_SAFE_CHILD_ENV = {
    "HOME": "/work",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONHASHSEED": "0",
    "PYTHONNOUSERSITE": "1",
    "TMPDIR": "/work",
}
_ABSOLUTE_PATH_RE = re.compile(
    r"(?:[A-Za-z]:\\(?:[^\\\s\"']+\\)*[^\\\s\"']+|"
    r"(?<![\w.])/(?:[^/\s\"'():,]+/)+[^/\s\"'():,]+)"
)


# Only this supervisor serializes runner status. The child can supply its
# return value and stdout, but never receives an expected answer or verdict.
_CALL_PROGRAM = """
import json, os, runpy, sys
source, target = sys.argv[1:3]
call = json.loads(sys.stdin.read())
namespace = runpy.run_path(source, run_name="__smartai_submission__")
value = namespace[call["name"]](*call["args"], **call["kwargs"])
with open(int(target) if target.isdecimal() else target, "wb") as channel:
    # Use JSON to reject tensors, class instances, handles and non-finite
    # floats before exposing the legacy repr-compatible comparison value.
    json.dumps(value, ensure_ascii=True, allow_nan=False)
    # Preserve tuple/list and numeric/string key distinctions for comparison.
    channel.write(repr(value).encode("utf-8"))
"""


def child_command(source, return_channel=None):
    if return_channel is None:
        return [sys.executable, "-I", "-B", str(source)]
    return [sys.executable, "-I", "-B", "-c", _CALL_PROGRAM, str(source), str(return_channel)]


def request_digest(payload):
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


class InvalidRequest(ValueError):
    """Raised when the mounted request is outside the frozen spike contract."""


def _bounded_int(value: Any, *, minimum: int, maximum: int, name: str) -> int:
    if type(value) is not int:
        raise InvalidRequest(f"{name} must be an integer")
    parsed = value
    if not minimum <= parsed <= maximum:
        raise InvalidRequest(f"{name} is outside the allowed range")
    return parsed


def _bounded_float(
    value: Any,
    *,
    minimum: float,
    maximum: float,
    name: str,
) -> float:
    if type(value) not in (float, int):
        raise InvalidRequest(f"{name} must be numeric")
    parsed = value
    if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
        raise InvalidRequest(f"{name} is outside the allowed range")
    return parsed


def _validate_request(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise InvalidRequest("request must be a JSON object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise InvalidRequest("unsupported schema_version")

    execution_id = payload.get("execution_id")
    if not isinstance(execution_id, str) or not 1 <= len(execution_id) <= 128:
        raise InvalidRequest("execution_id is invalid")
    if payload.get("language") != "python":
        raise InvalidRequest("only python is supported by this spike")
    if payload.get("task_type") not in {"sympy", "programming"}:
        raise InvalidRequest("task_type is invalid")

    code = payload.get("code")
    if not isinstance(code, str) or not code or len(code) > MAX_CODE_CHARS:
        raise InvalidRequest("code is empty or too large")

    test_input = payload.get("test_input", "")
    if not isinstance(test_input, str) or len(test_input.encode("utf-8")) > 65536:
        raise InvalidRequest("test_input is invalid or too large")
    call = payload.get("function_call")
    if call is not None:
        if (not isinstance(call, dict) or set(call) != {"name", "args", "kwargs"}
                or not isinstance(call["name"], str) or not call["name"].isidentifier()
                or not isinstance(call["args"], list) or not isinstance(call["kwargs"], dict)
                or payload["task_type"] != "programming" or test_input):
            raise InvalidRequest("invalid function invocation")
        test_input = json.dumps(call, allow_nan=False)
    limits = payload.get("limits")
    if not isinstance(limits, dict):
        raise InvalidRequest("limits must be a JSON object")
    timeout_seconds = _bounded_float(
        limits.get("timeout_seconds", 10.0),
        minimum=0.05,
        maximum=MAX_TIMEOUT_SECONDS,
        name="timeout_seconds",
    )
    memory_mb = _bounded_int(
        limits.get("memory_mb", 256),
        minimum=64,
        maximum=MAX_MEMORY_MB,
        name="memory_mb",
    )
    max_output_chars = _bounded_int(
        limits.get("max_output_chars", 8192),
        minimum=256,
        maximum=MAX_OUTPUT_CHARS,
        name="max_output_chars",
    )
    return {
        "execution_id": execution_id,
        "code": code,
        "test_input": test_input,
        "function_call": call is not None,
        "bind_request": payload.get("bind_request") is True,
        "timeout_seconds": timeout_seconds,
        "memory_mb": memory_mb,
        "max_output_chars": max_output_chars,
    }


def _apply_child_limits(memory_mb: int, timeout_seconds: float, file_bytes: int) -> None:
    """Apply limits that complement the container's cgroup controls."""

    if sys.platform == "win32":  # pragma: no cover - container is Linux
        return
    memory_bytes = memory_mb * 1024 * 1024
    cpu_seconds = max(1, int(math.ceil(timeout_seconds)) + 1)
    for limit_name, value in (
        (resource.RLIMIT_AS, memory_bytes),
        (resource.RLIMIT_CPU, cpu_seconds),
        (resource.RLIMIT_FSIZE, file_bytes),
        (resource.RLIMIT_NOFILE, 64),
        (resource.RLIMIT_CORE, 0),
    ):
        resource.setrlimit(limit_name, (value, value))


def _truncate_text(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    marker = "\n...[truncated]"
    if limit <= len(marker):
        return value[:limit], True
    return value[: limit - len(marker)] + marker, True


def _read_output(stream, limit):
    stream.seek(0)
    data = stream.read(limit * 4 + 1)
    text, truncated = _truncate_text(data.decode("utf-8", errors="replace"), limit)
    return text, truncated or len(data) > limit * 4


def _sanitize(value: str, limit: int) -> tuple[str, bool]:
    value = value.replace("/work/main.py", "<runner>/main.py")
    value = value.replace("/input/request.json", "<runner>/request.json")
    value = _ABSOLUTE_PATH_RE.sub("<redacted-path>", value)
    return _truncate_text(value, limit)


def _result(
    execution_id: str,
    *,
    executed: bool,
    exit_reason: str,
    stdout: str = "",
    stderr: str = "",
    stdout_truncated: bool = False,
    stderr_truncated: bool = False,
    duration_ms: float = 0.0,
    resource_status: str = "unknown",
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "execution_id": execution_id,
        "executed": executed,
        "exit_reason": exit_reason,
        "passed_count": 1 if executed and exit_reason == "success" else 0,
        "total_count": 1 if executed else 0,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "duration_ms": max(0.0, duration_ms),
        "resource_status": resource_status,
        "isolation_mode": "oci_container",
    }


def _execute_request(payload: Any, *, workdir: Path = DEFAULT_WORKDIR) -> dict[str, Any]:
    """Execute one validated request; container teardown owns directory cleanup."""

    try:
        request = _validate_request(payload)
    except InvalidRequest as exc:
        execution_id = "invalid"
        if isinstance(payload, dict) and isinstance(payload.get("execution_id"), str):
            execution_id = payload["execution_id"][:128] or "invalid"
        return _result(
            execution_id,
            executed=False,
            exit_reason="static_rejected",
            stderr=str(exc),
            resource_status="not_executed",
        )

    workdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    script_path = workdir / "main.py"
    stdin_path = workdir / ".stdin"
    max_output = request["max_output_chars"]
    # UTF-8 needs at most four bytes per character. One extra byte lets the
    # worker distinguish exactly-at-limit output from RLIMIT_FSIZE truncation.
    file_limit = max_output * 4 + 1
    started_at = time.perf_counter()
    timed_out = False
    returncode: int | None = None

    try:
        script_path.write_text(request["code"], encoding="utf-8", newline="\n")
        stdin_path.write_text(request["test_input"], encoding="utf-8")
        with ExitStack() as stack:
            stdin_file = stack.enter_context(stdin_path.open("rb"))
            stdout_file = stack.enter_context(tempfile.TemporaryFile(dir=workdir))
            stderr_file = stack.enter_context(tempfile.TemporaryFile(dir=workdir))
            return_file = stack.enter_context(tempfile.TemporaryFile(dir=workdir)) if request["function_call"] else None
            return_fd = return_file.fileno() if return_file is not None else None
            process = subprocess.Popen(
                child_command(script_path, return_fd),
                pass_fds=(return_fd,) if return_fd is not None else (),
                cwd=workdir,
                stdin=stdin_file,
                stdout=stdout_file,
                stderr=stderr_file,
                env=dict(_SAFE_CHILD_ENV),
                start_new_session=True,
                preexec_fn=lambda: _apply_child_limits(
                    request["memory_mb"],
                    request["timeout_seconds"],
                    file_limit,
                ),
            )
            try:
                returncode = process.wait(timeout=request["timeout_seconds"])
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                returncode = process.wait()

            stdout, stdout_truncated = _read_output(stdout_file, max_output)
            stderr, stderr_truncated = _read_output(stderr_file, max_output)
            return_value, return_truncated = _read_output(return_file, max_output) if return_file is not None else (None, False)
        # Student stdout is comparison data; sanitizing path-like literals
        # before comparison changes correct answers. Keep diagnostics separate.
        if not request["bind_request"]:
            stdout, sanitized = _sanitize(stdout, max_output)
            stdout_truncated = stdout_truncated or sanitized
        stderr, sanitize_stderr_truncated = _sanitize(stderr, max_output)
        stderr_truncated = stderr_truncated or sanitize_stderr_truncated

        if timed_out:
            stderr, timeout_message_truncated = _truncate_text(
                f"{stderr.rstrip()}\nTimeout after {request['timeout_seconds']}s".lstrip(),
                max_output,
            )
            return _result(
                request["execution_id"],
                executed=True,
                exit_reason="timeout",
                stdout=stdout,
                stderr=stderr,
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated or timeout_message_truncated,
                duration_ms=(time.perf_counter() - started_at) * 1000,
                resource_status="timeout",
            )
        if stdout_truncated or stderr_truncated or return_truncated:
            return _result(
                request["execution_id"],
                executed=True,
                exit_reason="output_limit",
                stdout=stdout,
                stderr=stderr,
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
                duration_ms=(time.perf_counter() - started_at) * 1000,
                resource_status="output_limit",
            )
        if returncode == 0 and return_value != "":
            result = _result(
                request["execution_id"],
                executed=True,
                exit_reason="success",
                stdout=stdout,
                stderr=stderr,
                duration_ms=(time.perf_counter() - started_at) * 1000,
                resource_status="within_limits",
            )
            if return_value is not None:
                result["return_value"] = return_value
            return result
        return _result(
            request["execution_id"],
            executed=True,
            exit_reason="runtime_error",
            stdout=stdout,
            stderr=stderr,
            duration_ms=(time.perf_counter() - started_at) * 1000,
            resource_status="unknown",
        )
    except Exception as exc:  # pragma: no cover - defensive container boundary
        return _result(
            request["execution_id"],
            executed=False,
            exit_reason="internal_error",
            stderr=f"runner internal error: {type(exc).__name__}",
            duration_ms=(time.perf_counter() - started_at) * 1000,
            resource_status="not_executed",
        )
    finally:
        for path in (script_path, stdin_path):
            try:
                path.unlink()
            except OSError:
                pass


def execute_request(payload: Any, *, workdir: Path = DEFAULT_WORKDIR) -> dict[str, Any]:
    result = _execute_request(payload, workdir=workdir)
    if isinstance(payload, dict) and payload.get("bind_request") is True:
        result["request_digest"] = request_digest(payload)
    return result


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        response = _result(
            "invalid",
            executed=False,
            exit_reason="static_rejected",
            stderr="expected one request file",
            resource_status="not_executed",
        )
    else:
        try:
            request_path = Path(argv[0])
            if request_path.stat().st_size > MAX_REQUEST_BYTES:
                raise InvalidRequest("request file is too large")
            payload = json.loads(request_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, InvalidRequest) as exc:
            response = _result(
                "invalid",
                executed=False,
                exit_reason="static_rejected",
                stderr=f"invalid request file: {type(exc).__name__}",
                resource_status="not_executed",
            )
        else:
            # The supervisor and untrusted child share an unprivileged uid.
            # Disallow access via /proc/<supervisor>/fd/1 (the outer protocol
            # stream), ptrace and process_vm_writev before starting the child.
            import ctypes
            if sys.platform == "linux" and ctypes.CDLL(None).prctl(4, 0, 0, 0, 0) == 0:
                response = execute_request(payload)
            else:
                response = _result("unavailable", executed=False, exit_reason="internal_error",
                                   stderr="supervisor protection unavailable", resource_status="not_executed")
    sys.stdout.write(json.dumps(response, ensure_ascii=True, sort_keys=True, allow_nan=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
