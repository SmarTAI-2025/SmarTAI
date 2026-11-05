"""
Sandboxed code interpreter tool for grading programming questions.

Run student code against test cases. Configured production uses the rootless
OCI transport; development/test can retain the historical subprocess path.

SAFETY: Host execution is disabled when runtime_environment=production.
Development/test subprocesses are not isolation against hostile code. Formal
production execution must use the separately implemented OCI adapter.

CONCURRENCY: Every subprocess invocation is gated by a global asyncio.Semaphore
from :mod:`backend.tools.sandbox_runtime` — without it, the grading pipeline's
nested ``asyncio.gather`` (students × questions × test cases) can fork bomb the
host. Default cap is 8.

EXECUTION MODES:
  - stdin/stdout (default): student code is a complete program; we feed
    `tc.input` on stdin and compare stdout to `tc.expected_output`.
  - function-call: a fixed harness loads the unchanged module, invokes the
    named function with positional/keyword arguments and writes its result to
    a separate channel. The supervisor keeps stdout as diagnostics. The
    expected result and the comparison run outside the student process.
"""
from __future__ import annotations

import ast
import asyncio
import json
import keyword
import math
import uuid
from decimal import Decimal, InvalidOperation
import logging
import os
import subprocess
import signal
import sys
import tempfile
import time

# `resource` is Unix-only; on Windows the rlimit-based sandbox is skipped
# (see preexec_fn guard in run_python_subprocess).
if sys.platform != "win32":
    import resource
from dataclasses import dataclass, field
from typing import List, Optional

# TestCase is the canonical Pydantic model in backend.models — keep one shape
# for upload parsing, storage, and execution. Re-export under the same name so
# legacy imports `from backend.tools.code_interpreter import TestCase` keep
# working.
from backend.config import settings
from backend.models import TestCase
from backend.tools.sandbox_runtime import get_sandbox_semaphore

from tools.runner_spike.worker import child_command, request_digest, _diagnose_failure, split_function_result

logger = logging.getLogger(__name__)


class HostCodeExecutionDisabled(RuntimeError):
    """Production must not execute untrusted code in the website process host."""

    def __init__(self) -> None:
        super().__init__("host_code_execution_disabled")



@dataclass
class TestResult:
    test: TestCase
    passed: bool
    actual_output: str
    error: str
    duration_ms: float
    exit_reason: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    isolation_mode: str = "host_subprocess"
    stdout: str = ""
    return_value: str | None = None
    execution_id: str = ""
    request_digest: str = ""
    case_id: str = ""
    case_index: int | None = None
    resource_status: str = "unknown"
    process_exit_code: int | None = None
    termination_signal: int | None = None
    cgroup_oom_kills: int | None = None
    cgroup_pid_denials: int | None = None
    executed: bool = True


@dataclass
class ExecutionReport:
    passed_count: int = 0
    total_count: int = 0
    pass_rate: float = 0.0
    results: List[TestResult] = field(default_factory=list)
    summary: str = ""


# ─── Resource limits for child process ───────────────────────────────────────

def _apply_limits(memory_mb: int = 256, cpu_seconds: int = 10) -> None:
    """Applied in child process to cap resources."""
    mem_bytes = memory_mb * 1024 * 1024
    try:
        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    except (ValueError, OSError) as e:
        # Some platforms (macOS) don't support all limits
        logger.debug(f"Could not apply full resource limits: {e}")


async def run_python_subprocess(
    code: str,
    test_input: str = "",
    *,
    timeout: float = 10.0,
    memory_mb: int = 256,
    max_output_chars: Optional[int] = None,
    function_call: dict | None = None,
    bind_request: bool = False,
) -> TestResult:
    """Run a single test case in a subprocess.

    Acquires the global sandbox semaphore so that no matter how many callers
    fan out via ``asyncio.gather``, at most ``get_sandbox_limit()`` subprocesses
    are alive at once. Without this guard the grading pipeline can spawn
    hundreds of processes (students × questions × test cases) and OOM the host.
    """
    # Resource limits alone are not filesystem/network isolation. Production
    # uses OCI or reports an infrastructure error, never a host fallback or 0/N.
    if settings.program_runner_mode == "oci":
        from backend.tools.oci_grading_runner import run_python_oci
        return await run_python_oci(code, test_input, timeout=timeout,
                                    memory_mb=memory_mb, max_output_chars=max_output_chars or 8192,
                                    function_call=function_call, bind_request=bind_request)
    if settings.runtime_environment == "production":
        raise HostCodeExecutionDisabled()
    execution_id = uuid.uuid4().hex
    digest = request_digest({"execution_id": execution_id, "code": code, "stdin": test_input,
                             "function_call": function_call, "timeout": timeout, "memory_mb": memory_mb,
                             "max_output_chars": max_output_chars})
    sem = get_sandbox_semaphore()
    async with sem:
        started_at = time.perf_counter()
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", suffix=".py", delete=False
        ) as f:
            f.write(code)
            code_path = f.name

        result_dir = tempfile.TemporaryDirectory(prefix="smartai-function-") if function_call is not None else None
        return_path = os.path.join(result_dir.name, "return") if result_dir else None
        try:
            preexec_fn = (
                (lambda: _apply_limits(memory_mb, int(timeout) + 2))
                if sys.platform != "win32"
                else None
            )
            command = child_command(code_path, return_path) if bind_request or function_call is not None else [sys.executable, code_path]
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=preexec_fn,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate((json.dumps(function_call, allow_nan=False) if function_call is not None else test_input).encode()),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return TestResult(
                    test=TestCase(input=test_input),
                    passed=False,
                    actual_output="",
                    error=f"Timeout after {timeout}s",
                    duration_ms=(time.perf_counter() - started_at) * 1000,
                    exit_reason="timeout", resource_status="timeout", execution_id=execution_id, request_digest=digest,
                    process_exit_code=proc.returncode if proc.returncode >= 0 else None,
                    termination_signal=-proc.returncode if proc.returncode < 0 else None,
                )

            except BaseException:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
                raise
            stdout = stdout_b.decode(errors="replace")
            stderr = stderr_b.decode(errors="replace").replace(
                code_path,
                "<runner>/main.py",
            )
            stdout_truncated = False
            stderr_truncated = False
            if max_output_chars is not None:
                stdout, stdout_truncated = _truncate_output(
                    stdout,
                    max_output_chars,
                )
                stderr, stderr_truncated = _truncate_output(
                    stderr,
                    max_output_chars,
                )
            return_value = None
            module_loaded = False
            return_truncated = False
            if return_path and os.path.isfile(return_path):
                limit = max_output_chars or 8192
                with open(return_path, "rb") as returned:
                    value = returned.read((limit + 1) * 4 + 1).decode(errors="replace")
                value, return_truncated = _truncate_output(value, limit + 1)
                module_loaded, return_value = split_function_result(value)
            output_limited = stdout_truncated or stderr_truncated or return_truncated
            if output_limited:
                limit_error = f"Output limit exceeded ({max_output_chars} chars)"
                stderr = f"{stderr.rstrip()}\n{limit_error}".lstrip()

            if bind_request and proc.returncode == -getattr(signal, "SIGKILL", 9):
                from backend.tools.oci_grading_runner import IsolatedRunnerUnavailable
                raise IsolatedRunnerUnavailable()
            missing_return = return_path is not None and not return_value
            reason = "success"
            if output_limited:
                reason = "output_limit"
            elif proc.returncode or missing_return:
                reason = (await asyncio.to_thread(_diagnose_failure, code, function_call, module_loaded)
                          if bind_request and proc.returncode > 0 else "runtime_error")

            return TestResult(
                test=TestCase(input=test_input),
                passed=(proc.returncode == 0 and not output_limited and not missing_return),
                stdout=stdout, return_value=return_value, execution_id=execution_id, request_digest=digest,
                resource_status="output_limit" if output_limited else "within_limits" if proc.returncode == 0 else "unknown",
                process_exit_code=proc.returncode if proc.returncode >= 0 else None,
                termination_signal=-proc.returncode if proc.returncode < 0 else None,
                actual_output=stdout,
                error=stderr if proc.returncode != 0 or output_limited else "",
                duration_ms=(time.perf_counter() - started_at) * 1000,
                exit_reason=reason,
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
            )
        finally:
            if result_dir:
                result_dir.cleanup()
            try:
                os.unlink(code_path)
            except OSError:
                pass


def _truncate_output(value: str, limit: int) -> tuple[str, bool]:
    """Bound one captured subprocess stream while preserving a clear marker."""

    if limit < 1 or len(value) <= limit:
        return value, False
    marker = "\n...[truncated]"
    if limit <= len(marker):
        return value[:limit], True
    return value[: limit - len(marker)] + marker, True


async def run_sandbox(
    code: str,
    test_cases: List[TestCase],
    *,
    language: str = "python",
    per_case_timeout: float = 10.0,
    memory_mb: int = 256,
) -> ExecutionReport:
    """
    Run student code against a list of test cases. Returns ExecutionReport.

    With no test cases, record zero executions.

    Each TestCase chooses its own mode: if `function_name` is set, function-
    call mode is used (LeetCode style — wrap student code, call fn(*args),
    print repr); otherwise stdin/stdout mode.
    """
    if language != "python":
        logger.warning(f"Language {language!r} not yet supported; returning skip result")
        return ExecutionReport(summary=f"Language {language} not supported in sandbox")

    if not test_cases:
        return ExecutionReport(summary="No test cases; execution not attempted")

    # Validate all executable contracts before dispatching even one case.
    if len(test_cases) > 50:
        raise ValueError("Programming test case count exceeds 50")
    for tc in test_cases:
        mode = getattr(tc, "comparison", "default")
        if mode not in {"default", "exact", "trimmed", "tokens", "json", "numeric"}:
            raise ValueError("Unsupported comparison mode")
        for tol in (getattr(tc, "abs_tol", 1e-9), getattr(tc, "rel_tol", 0.0)):
            if not isinstance(tol, (int, float)) or not math.isfinite(tol) or tol < 0:
                raise ValueError("Invalid numeric tolerance")
        if tc.function_name:
            if tc.function_args is None and getattr(tc, "function_kwargs", None) is None:
                raise ValueError("Missing function invocation arguments")
            if not tc.function_name.isidentifier() or keyword.iskeyword(tc.function_name):
                raise ValueError("Invalid function entry")
            if tc.expected_return is None:
                raise ValueError("Missing expected function return")
            if mode in {"tokens", "trimmed"}:
                raise ValueError("Token comparison requires standard output")
        elif "expected_output" not in tc.model_fields_set:
            raise ValueError("Missing expected standard output")

    case_ids = [getattr(tc, "case_id", None) or f"case-{i + 1}" for i, tc in enumerate(test_cases)]
    if len(set(case_ids)) != len(case_ids):
        raise ValueError("Duplicate test case ID")
    jobs = [asyncio.create_task(_run_case(code, tc, per_case_timeout, memory_mb)) for tc in test_cases]
    try:
        results = await asyncio.wait_for(asyncio.gather(*jobs), timeout=60)
    except BaseException:
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        raise

    for index, result in enumerate(results):
        result.case_index = index
        result.case_id = case_ids[index]
    passed = sum(1 for r in results if r.passed)
    total = len(results)
    return ExecutionReport(
        passed_count=passed,
        total_count=total,
        pass_rate=passed / total if total else 0.0,
        results=results,
        summary=f"Passed {passed}/{total} tests",
    )


async def _run_case(code: str, tc: TestCase, timeout: float, memory_mb: int) -> TestResult:
    call = None
    if tc.function_name:
        call = {"name": tc.function_name, "args": tc.function_args or [], "kwargs": getattr(tc, "function_kwargs", None) or {}}
    raw = await run_python_subprocess(code, "" if call else tc.input, timeout=timeout, memory_mb=memory_mb,
                                      max_output_chars=8192, function_call=call, bind_request=True)
    raw.test = tc
    raw.stdout = raw.actual_output
    if not raw.passed:
        return raw
    mode = getattr(tc, "comparison", "default")
    abs_tol, rel_tol = getattr(tc, "abs_tol", 1e-9), getattr(tc, "rel_tol", 0.0)
    if call:
        if raw.return_value is None:
            raise ValueError("Missing function result channel")
        raw.actual_output = raw.return_value
        raw.passed = _values_match(raw.return_value, tc.expected_return, mode, abs_tol, rel_tol)
    elif mode == "numeric":
        raw.passed = _numeric_text_match(raw.actual_output, tc.expected_output, abs_tol, rel_tol)
    elif mode == "trimmed":
        def trimmed(value):
            return "\n".join(line.rstrip() for line in value.replace("\r\n", "\n").split("\n")).rstrip("\n")
        raw.passed = trimmed(raw.actual_output) == trimmed(tc.expected_output)
    elif mode == "json":
        raw.passed = _json_match(raw.actual_output, tc.expected_output)
    elif mode == "tokens":
        raw.passed = raw.actual_output.split() == tc.expected_output.split()
    elif mode == "exact" or tc.expected_output == "":
        raw.passed = raw.actual_output.replace("\r\n", "\n") == tc.expected_output.replace("\r\n", "\n")
    elif mode == "default":
        raw.passed = raw.actual_output.strip() == tc.expected_output.strip()
    else:
        raise ValueError("Unsupported comparison mode")
    if not raw.passed:
        raw.exit_reason = "wrong_answer"
    return raw


def _json_match(actual_text, expected_text):
    def reject(value):
        raise ValueError("Non-finite JSON number")
    try:
        actual = json.loads(actual_text, parse_constant=reject)
        expected = json.loads(expected_text, parse_constant=reject)
        return _literal_values_match(actual, expected, True, 0, 0)
    except (TypeError, ValueError, RecursionError, OverflowError):
        return False


def _numeric_text_match(actual, expected, abs_tol, rel_tol):
    try:
        a, b = Decimal(actual.strip()), Decimal(expected.strip())
    except InvalidOperation:
        return False
    if not a.is_finite() or not b.is_finite():
        return False
    # If both inputs are integers, compare them exactly. Decimal avoids
    # collapsing adjacent large integers to the same machine float.
    if a == b:
        return True
    if a.as_tuple().exponent >= 0 and b.as_tuple().exponent >= 0:
        return False
    try:
        return abs(a - b) <= max(Decimal(str(abs_tol)), Decimal(str(rel_tol)) * max(abs(a), abs(b)))
    except ArithmeticError:
        return False


def _values_match(actual_repr: str, expected_repr: str, mode="default", abs_tol=1e-9, rel_tol=0.0) -> bool:
    if mode == "numeric":
        return _numeric_text_match(actual_repr, expected_repr, abs_tol, rel_tol)
    if mode == "json":
        try:
            # Function results are represented as Python literals at this layer.
            value = ast.literal_eval(actual_repr.strip())
            return _json_match(json.dumps(value, allow_nan=False), expected_repr)
        except (TypeError, ValueError, SyntaxError, RecursionError):
            return False
    if mode not in {"default", "exact"}:
        raise ValueError("Unsupported function result comparison")
    try:
        actual = ast.literal_eval(actual_repr.strip())
        expected = ast.literal_eval(expected_repr.strip())
        return _literal_values_match(actual, expected, mode == "exact", abs_tol, rel_tol)
    except (ValueError, SyntaxError, OverflowError, RecursionError):
        return False


def _literal_values_match(actual, expected, exact, abs_tol, rel_tol):
    if type(actual) in (int, float) and type(expected) in (int, float):
        if exact and type(actual) is not type(expected):
            return False
        if any(type(v) is float and not math.isfinite(v) for v in (actual, expected)):
            return False
        if (type(actual) is int and type(expected) is int) or exact or actual == expected:
            return actual == expected
        return _numeric_text_match(repr(actual), repr(expected), abs_tol, rel_tol)
    if type(actual) is not type(expected):
        return False
    if isinstance(actual, (list, tuple)):
        return len(actual) == len(expected) and all(
            _literal_values_match(a, b, exact, abs_tol, rel_tol) for a, b in zip(actual, expected)
        )
    if isinstance(actual, dict):
        return ({(type(k), k) for k in actual} == {(type(k), k) for k in expected}
                and all(_literal_values_match(actual[k], expected[k], exact, abs_tol, rel_tol) for k in actual))
    return actual == expected
