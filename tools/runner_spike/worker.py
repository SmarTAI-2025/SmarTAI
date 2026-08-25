#!/usr/bin/env python3
"""Small, dependency-free worker executed inside the spike container.

The host harness supplies one versioned runner request through a read-only
bind mount.  This worker deliberately does not import the web application, so
the container image has no reason to contain application settings or secrets.
"""
from __future__ import annotations

import json
import math
import os
import re
import signal
import subprocess
import sys
import time
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


class InvalidRequest(ValueError):
    """Raised when the mounted request is outside the frozen spike contract."""


def _bounded_int(value: Any, *, minimum: int, maximum: int, name: str) -> int:
    if isinstance(value, bool):
        raise InvalidRequest(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidRequest(f"{name} must be an integer") from exc
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
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise InvalidRequest(f"{name} must be numeric") from exc
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


def _read_output(path: Path, limit: int) -> tuple[str, bool]:
    byte_limit = limit * 4
    try:
        size = path.stat().st_size
        data = path.read_bytes()[: byte_limit + 1]
    except OSError:
        return "", False
    text = data.decode("utf-8", errors="replace")
    text, chars_truncated = _truncate_text(text, limit)
    return text, chars_truncated or size > byte_limit


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


def execute_request(payload: Any, *, workdir: Path = DEFAULT_WORKDIR) -> dict[str, Any]:
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
    stdout_path = workdir / ".stdout"
    stderr_path = workdir / ".stderr"
    max_output = request["max_output_chars"]
    # UTF-8 needs at most four bytes per character. One extra byte lets the
    # worker distinguish exactly-at-limit output from RLIMIT_FSIZE truncation.
    file_limit = max_output * 4 + 1
    started_at = time.perf_counter()
    timed_out = False
    returncode: int | None = None

    try:
        script_path.write_text(request["code"], encoding="utf-8", newline="\n")
        with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
            process = subprocess.Popen(
                [sys.executable, "-I", "-B", str(script_path)],
                cwd=workdir,
                stdin=subprocess.DEVNULL,
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

        stdout, stdout_truncated = _read_output(stdout_path, max_output)
        stderr, stderr_truncated = _read_output(stderr_path, max_output)
        stdout, sanitize_stdout_truncated = _sanitize(stdout, max_output)
        stderr, sanitize_stderr_truncated = _sanitize(stderr, max_output)
        stdout_truncated = stdout_truncated or sanitize_stdout_truncated
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
        if stdout_truncated or stderr_truncated:
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
        if returncode == 0:
            return _result(
                request["execution_id"],
                executed=True,
                exit_reason="success",
                stdout=stdout,
                stderr=stderr,
                duration_ms=(time.perf_counter() - started_at) * 1000,
                resource_status="within_limits",
            )
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
        for path in (script_path, stdout_path, stderr_path):
            try:
                path.unlink()
            except OSError:
                pass


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
            response = execute_request(payload)
    sys.stdout.write(json.dumps(response, ensure_ascii=False, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
