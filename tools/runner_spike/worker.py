#!/usr/bin/env python3
"""Small, dependency-free worker executed inside the spike container.

The host harness supplies one versioned runner request through a read-only
bind mount.  This worker deliberately does not import the web application, so
the container image has no reason to contain application settings or secrets.
"""
from __future__ import annotations

import ast
import hashlib
import inspect
import __future__
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


# A second NUL cannot be a Python repr value; it marks an uncomparable return.
RETURN_UNAVAILABLE = "\x00"


class FunctionReturnUnavailable(ValueError):
    def __init__(self):
        super().__init__("programming_return_value_unavailable")


# Only this supervisor serializes runner status. The child can supply its
# return value and stdout, but never receives an expected answer or verdict.
_CALL_PROGRAM = """
import json, math, os, runpy, sys
def literal_value(value):
    kind = type(value)
    if kind in (type(None), bool, int, str, bytes):
        return True
    if kind in (float, complex):
        return math.isfinite(value.real) and math.isfinite(value.imag)
    if kind in (list, tuple, set):
        return all(literal_value(item) for item in value)
    if kind is dict:
        return all(literal_value(key) and literal_value(item) for key, item in value.items())
    return False
source, target = sys.argv[1:3]
call = json.loads(sys.stdin.read())
namespace = runpy.run_path(source, run_name="__smartai_submission__")
with open(int(target) if target.isdecimal() else target, "wb") as channel:
    # A NUL byte cannot occur unescaped in repr() of the supported values.
    # Record lookup phase after a successful module load, before invocation.
    channel.write(bytes([0]))
    channel.flush()
    value = namespace[call["name"]](*call["args"], **call["kwargs"])
    # Input arguments are JSON, but returned values use Python literals.
    # Keep sets, bytes and literal dictionary keys while rejecting objects
    # with arbitrary repr methods and non-finite numbers.
    try:
        comparable = literal_value(value)
    except RecursionError:
        comparable = False
    # Unsupported return representations are an evaluator limitation, not a
    # Python exception raised by the submitted function.
    try:
        payload = repr(value).encode("utf-8") if comparable else bytes([0])
    except (ValueError, OverflowError, RecursionError, UnicodeError):
        payload = bytes([0])
    channel.write(payload)
"""


def child_command(source, return_channel=None):
    if return_channel is None:
        return [sys.executable, "-I", "-B", str(source)]
    return [sys.executable, "-I", "-B", "-c", _CALL_PROGRAM, str(source), str(return_channel)]


def _static_failure_reason(code, call, module_loaded):
    """Inspect only the original source. Never import or evaluate a submission."""
    try:
        tree = ast.parse(code, "<submission>")
        compiled = compile(tree, "<submission>", "exec", dont_inherit=True)
    except SyntaxError:
        return "syntax_error"
    if call is None or not module_loaded:
        return "runtime_error"
    annotations_deferred = bool(compiled.co_flags & __future__.annotations.compiler_flag)

    # Dynamic bindings, decorator side effects, imports or executable module
    # statements can change the target or fail before invocation. A phase we
    # cannot prove must not be blamed on the student's declared signature.
    functions = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.ImportFrom) and stmt.module == "__future__" and not stmt.level:
            continue
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            # The actual harness advanced past module initialization. Check
            # static name bindings without re-importing any student-selected
            # module. Unknown/wildcard bindings cannot prove a missing target.
            for alias in stmt.names:
                module = alias.name if isinstance(stmt, ast.Import) else stmt.module
                if (not module or module.split(".", 1)[0] not in sys.stdlib_module_names
                        or getattr(stmt, "level", 0) or alias.name == "*"):
                    return "runtime_error"
                bound = alias.asname or (module.split(".", 1)[0] if isinstance(stmt, ast.Import) else alias.name)
                if bound == call["name"]:
                    return "runtime_error"
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue
        if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant) and all(
            isinstance(target, ast.Name) and target.id != call["name"] for target in stmt.targets
        ):
            continue
        if not isinstance(stmt, ast.FunctionDef) or stmt.decorator_list or getattr(stmt, "type_params", []):
            return "runtime_error"
        args = stmt.args
        declared = [*args.posonlyargs, *args.args, *args.kwonlyargs]
        declared += [arg for arg in (args.vararg, args.kwarg) if arg]
        annotations = [stmt.returns, *(arg.annotation for arg in declared)]
        builtins = {"int", "float", "complex", "str", "bool", "list", "tuple", "dict", "set", "bytes"}
        if not annotations_deferred and any(annotation is not None and not isinstance(annotation, ast.Constant)
               and not (isinstance(annotation, ast.Name) and annotation.id in builtins) for annotation in annotations):
            return "runtime_error"
        if any(not isinstance(value, ast.Constant) for value in (*args.defaults, *args.kw_defaults) if value is not None):
            return "runtime_error"
        functions[stmt.name] = args
    args = functions.get(call["name"])
    if args is None:
        return "target_function_missing"

    param = inspect.Parameter
    positional = [*args.posonlyargs, *args.args]
    required = len(positional) - len(args.defaults)
    params = [param(arg.arg, param.POSITIONAL_ONLY if i < len(args.posonlyargs) else param.POSITIONAL_OR_KEYWORD,
                    default=param.empty if i < required else None)
              for i, arg in enumerate(positional)]
    if args.vararg:
        params.append(param(args.vararg.arg, param.VAR_POSITIONAL))
    params.extend(param(arg.arg, param.KEYWORD_ONLY, default=param.empty if default is None else None)
                  for arg, default in zip(args.kwonlyargs, args.kw_defaults))
    if args.kwarg:
        params.append(param(args.kwarg.arg, param.VAR_KEYWORD))
    try:
        inspect.Signature(params).bind(*call["args"], **call["kwargs"])
    except TypeError:
        return "signature_mismatch"
    return "runtime_error"


_DIAGNOSES = {"syntax_error", "target_function_missing", "signature_mismatch", "runtime_error"}


def _diagnose_failure(code, call, module_loaded=False):
    # Only failed programming cases pay for this bounded compiler check. A
    # separate trusted process receives the original request through stdin,
    # never stdout, traceback or a path writable by the student's process.
    if len(code) > MAX_CODE_CHARS:
        return "runtime_error"
    try:
        check = subprocess.run([sys.executable, "-I", "-B", __file__, "--diagnose"],
                               input=json.dumps([code, call, module_loaded]).encode(), capture_output=True,
                               timeout=1.5, env=_SAFE_CHILD_ENV)
        reason = check.stdout.decode("ascii")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return "runtime_error"
    return reason if check.returncode == 0 and reason in _DIAGNOSES else "runtime_error"


def _cgroup_events():
    """Read only the private cgroup-v2 root of this single-case container."""
    def counter(filename, key):
        try:
            rows = (Path("/sys/fs/cgroup") / filename).read_text().splitlines()
            return int(dict(line.split() for line in rows)[key])
        except (OSError, ValueError, KeyError):
            return None
    try:
        if Path("/proc/self/cgroup").read_text().strip() != "0::/":
            return (None, None)
    except OSError:
        return (None, None)
    return counter("memory.events", "oom_kill"), counter("pids.events", "max")


def _resource_failure(before, after):
    for index, reason in enumerate(("memory_limit", "pid_limit")):
        if before[index] is not None and after[index] is not None and after[index] > before[index]:
            return reason
    return None


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
    language = payload.get("language")
    if language != "python" and not (language in ("c", "cpp") and payload.get("task_type") == "programming"
                                     and payload.get("bind_request") is True and payload.get("function_call") is None):
        raise InvalidRequest("unsupported language or execution mode")
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
        limits.get("timeout_seconds", 5.0),
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
        "language": language,
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


def split_function_result(value):
    # A marker alone (e.g. os._exit(0) inside a function) is not a return.
    # Student code can write the private value channel: phase alone is never
    # proof. The separate inspector must independently verify a declarative
    # source and an incompatible binding before reporting an entry error.
    loaded = value is not None and value.startswith("\0")
    return loaded, value[1:] if loaded else None


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


class NativeBuildUnavailable(Exception):
    def __init__(self, reason="toolchain_unavailable"):
        self.reason = reason


def _build_native(language, source, memory_mb):
    """Compile and link separately inside the disposable case container.

    Only recognized source diagnostics vote. Missing tools, exhausted compiler
    resources, abnormal exits and ambiguous linker failures stay ungraded.
    """
    compiler = "/usr/bin/gcc" if language == "c" else "/usr/bin/g++"
    standard = "-std=c11" if language == "c" else "-std=c++17"
    obj = source.with_suffix(".o")
    stages = [
        ([compiler, standard, "-fdiagnostics-format=json", "-fmax-errors=2", "-c", str(source), "-o", str(obj)], "compile_error"),
        ([compiler, str(obj), "-o", "/binary/submission", "-lm"], "link_error"),
    ]
    def confirm_toolchain():
        # A damaged image must not turn its missing headers/startup libraries
        # into a student error. Verify a known-good build and run in this case.
        header = "iostream" if language == "cpp" else "stdio.h"
        probe = source.with_name("probe" + source.suffix)
        probe.write_text(f"#include <{header}>\nint main(void) {{return 0;}}", encoding="ascii")
        for args in ([compiler, standard, str(probe), "-o", "/binary/probe", "-lm"], ["/binary/probe"]):
            subprocess.run(args, check=True, timeout=6, env=_SAFE_CHILD_ENV, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           preexec_fn=lambda: _apply_child_limits(memory_mb, 6, 4 * 1024 * 1024))

    for command, reason in stages:
        with tempfile.TemporaryFile(dir=source.parent) as diagnostic:
            process = subprocess.run(command, cwd=source.parent, env=_SAFE_CHILD_ENV, timeout=12,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=diagnostic,
                                     preexec_fn=lambda: _apply_child_limits(memory_mb, 12, 4 * 1024 * 1024))
            if process.returncode == 0:
                continue
            diagnostic.seek(0)
            detail = diagnostic.read(32768).decode("utf-8", errors="replace")
        if process.returncode == 1:
            if reason == "compile_error":
                try:
                    diagnostics = json.loads(detail.rstrip().removesuffix("compilation terminated.").rstrip())
                except ValueError:
                    diagnostics = []
                for item in diagnostics:
                    if item.get("kind") not in {"error", "fatal error"}:
                        continue
                    caret = next((loc["caret"] for loc in item.get("locations", [])
                                  if loc.get("caret", {}).get("file") == str(source)), {})
                    line = caret.get("line", 0)
                    if type(line) is not int or not 1 <= line <= MAX_CODE_CHARS:
                        continue
                    confirm_toolchain()
                    # Keep only fixed categories and a bounded source line.
                    # The diagnostic can contain student identifiers, directives and paths.
                    message = item.get("message", "")
                    category = "source_error"
                    if re.search(r"expected [‘'];[’']", message):
                        category = "missing_semicolon"
                    elif item["kind"] == "fatal error" and message.endswith(": No such file or directory"):
                        # A missing include can be a submission dependency or a
                        # partially damaged image. Neither proves a student failure.
                        raise NativeBuildUnavailable("native_header_unavailable")
                    elif "undeclared" in message or "not declared in this scope" in message:
                        category = "unknown_identifier"
                    return reason, f"{category} line {line}"
            elif re.search(r"undefined reference to [`']main'", detail):
                raise NativeBuildUnavailable("native_main_required")
            elif "undefined reference to" in detail or "multiple definition of" in detail:
                confirm_toolchain()
                return reason, "unresolved_symbol" if "undefined reference to" in detail else "duplicate_symbol"
        raise NativeBuildUnavailable()
    return None


def _execute_request(payload: Any, *, workdir: Path = DEFAULT_WORKDIR, track_resources=False) -> dict[str, Any]:
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
    language = request["language"]
    extension = {"python": "py", "c": "c", "cpp": "cpp"}[language]
    script_path = workdir / f"main.{extension}"
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
            before = _cgroup_events() if track_resources else (None, None)
            if language == "python":
                command = child_command(script_path, return_fd)
            else:
                try:
                    failure = _build_native(language, script_path, request["memory_mb"])
                except (OSError, subprocess.SubprocessError) as exc:
                    raise NativeBuildUnavailable() from exc
                if failure:
                    reason, diagnostic = failure
                    return _result(request["execution_id"], executed=True, exit_reason=reason, stderr=diagnostic,
                                   duration_ms=(time.perf_counter() - started_at) * 1000)
                command = ["/binary/submission"]
            process = subprocess.Popen(
                command,
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

            after = _cgroup_events() if track_resources else (None, None)
            stdout, stdout_truncated = _read_output(stdout_file, max_output)
            stderr, stderr_truncated = _read_output(stderr_file, max_output)
            returned, return_truncated = _read_output(return_file, max_output + 1) if return_file is not None else (None, False)
            module_loaded, return_value = split_function_result(returned)
        # Student stdout is comparison data; sanitizing path-like literals
        # before comparison changes correct answers. Keep diagnostics separate.
        if not request["bind_request"]:
            stdout, sanitized = _sanitize(stdout, max_output)
            stdout_truncated = stdout_truncated or sanitized
        stderr, sanitize_stderr_truncated = _sanitize(stderr, max_output)
        stderr_truncated = stderr_truncated or sanitize_stderr_truncated

        def case_result(**values):
            result = _result(request["execution_id"], executed=True, **values)
            if request["bind_request"]:
                result["process_exit_code"] = returncode if returncode >= 0 else None
                result["termination_signal"] = -returncode if returncode < 0 else None
                for idx, key in enumerate(("cgroup_oom_kills", "cgroup_pid_denials")):
                    if before[idx] is not None and after[idx] is not None and after[idx] >= before[idx]:
                        result[key] = after[idx] - before[idx]
            return result

        if timed_out:
            stderr, timeout_message_truncated = _truncate_text(
                f"{stderr.rstrip()}\nTimeout after {request['timeout_seconds']}s".lstrip(),
                max_output,
            )
            return case_result(
                exit_reason="timeout",
                stdout=stdout,
                stderr=stderr,
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated or timeout_message_truncated,
                duration_ms=(time.perf_counter() - started_at) * 1000,
                resource_status="timeout",
            )
        if stdout_truncated or stderr_truncated or return_truncated:
            return case_result(
                exit_reason="output_limit",
                stdout=stdout,
                stderr=stderr,
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
                duration_ms=(time.perf_counter() - started_at) * 1000,
                resource_status="output_limit",
            )
        resource_failure = _resource_failure(before, after) if request["bind_request"] else None
        if resource_failure:
            # A child can swallow EAGAIN or lose an OOM-killed grandchild and
            # still exit 0. The single-container controller owns this evidence.
            return case_result(exit_reason=resource_failure, resource_status=resource_failure + "_exceeded",
                               stdout=stdout, stderr=stderr, stdout_truncated=stdout_truncated,
                               stderr_truncated=stderr_truncated, duration_ms=(time.perf_counter() - started_at) * 1000)
        if returncode == 0 and return_value == RETURN_UNAVAILABLE:
            return _result(request["execution_id"], executed=False, exit_reason="internal_error",
                           stderr="programming_return_value_unavailable", resource_status="not_executed")
        if returncode == 0 and (not request["function_call"] or return_value):
            result = case_result(
                exit_reason="success",
                stdout=stdout,
                stderr=stderr,
                duration_ms=(time.perf_counter() - started_at) * 1000,
                resource_status="within_limits",
            )
            if return_value is not None:
                result["return_value"] = return_value
            return result
        if request["bind_request"] and returncode == -signal.SIGKILL:
            return case_result(exit_reason="internal_error", resource_status="unknown", stdout=stdout,
                               stderr="The process was killed; resource attribution is unavailable.",
                               duration_ms=(time.perf_counter() - started_at) * 1000)
        reason = (
            _diagnose_failure(request["code"], payload.get("function_call"), module_loaded)
            if language == "python" and request["bind_request"] and returncode is not None and returncode > 0 else "runtime_error"
        )
        return case_result(
            exit_reason=reason, stdout=stdout, stderr=stderr,
            stdout_truncated=stdout_truncated, stderr_truncated=stderr_truncated,
            duration_ms=(time.perf_counter() - started_at) * 1000,
            resource_status="unknown",
        )
    except NativeBuildUnavailable as exc:
        return _result(request["execution_id"], executed=False, exit_reason=exc.reason,
                       resource_status="not_executed")
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


def execute_request(payload: Any, *, workdir: Path = DEFAULT_WORKDIR, track_resources=False) -> dict[str, Any]:
    result = _execute_request(payload, workdir=workdir, track_resources=track_resources)
    if isinstance(payload, dict) and payload.get("bind_request") is True:
        result["request_digest"] = request_digest(payload)
    return result


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["--diagnose"]:
        # Bound the compiler process separately. A pathological source cannot
        # crash the website or the supervisor that owns the result protocol.
        if sys.platform == "linux":
            import ctypes
            if ctypes.CDLL(None).prctl(4, 0, 0, 0, 0) != 0:
                return 1
            _apply_child_limits(128, 1, 8192)
        code, call, module_loaded = json.loads(sys.stdin.buffer.read(MAX_REQUEST_BYTES * 6 + 1))
        sys.stdout.write(_static_failure_reason(code, call, module_loaded))
        return 0
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
                response = execute_request(payload, track_resources=isinstance(payload, dict) and payload.get("bind_request") is True)
            else:
                response = _result("unavailable", executed=False, exit_reason="internal_error",
                                   stderr="supervisor protection unavailable", resource_status="not_executed")
    sys.stdout.write(json.dumps(response, ensure_ascii=True, sort_keys=True, allow_nan=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
