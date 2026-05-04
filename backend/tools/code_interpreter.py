"""
Sandboxed code interpreter tool for grading programming questions.

Per docs §4.2.1, should use Docker or subprocess sandbox to run student code
against test cases. This initial implementation uses subprocess with strict
resource limits. Docker support can be added later via the same interface.

SAFETY: This code runs UNTRUSTED student input. The subprocess runner applies
timeout, memory limits, and CPU limits via resource.setrlimit. For production
use with hostile input, wrap in Docker with --network=none --read-only.

CONCURRENCY: Every subprocess invocation is gated by a global asyncio.Semaphore
from :mod:`backend.tools.sandbox_runtime` — without it, the grading pipeline's
nested ``asyncio.gather`` (students × questions × test cases) can fork bomb the
host. Default cap is 8.

EXECUTION MODES:
  - stdin/stdout (default): student code is a complete program; we feed
    `tc.input` on stdin and compare stdout to `tc.expected_output`.
  - function-call (LeetCode style): student code is just a function definition
    (no top-level I/O). When `tc.function_name` is set we wrap the student
    code with an auto-generated harness that loads `tc.function_args` from
    stdin (JSON), calls the function, and prints repr(result). The harness
    output is compared to `tc.expected_return` after ast.literal_eval
    normalization so e.g. "1" and "1.0" with int rounding still match.
"""
from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile

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
from backend.models import TestCase
from backend.tools.sandbox_runtime import get_sandbox_semaphore

logger = logging.getLogger(__name__)


@dataclass
class TestResult:
    test: TestCase
    passed: bool
    actual_output: str
    error: str
    duration_ms: float


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
) -> TestResult:
    """Run a single test case in a subprocess.

    Acquires the global sandbox semaphore so that no matter how many callers
    fan out via ``asyncio.gather``, at most ``get_sandbox_limit()`` subprocesses
    are alive at once. Without this guard the grading pipeline can spawn
    hundreds of processes (students × questions × test cases) and OOM the host.
    """
    sem = get_sandbox_semaphore()
    async with sem:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(code)
            code_path = f.name

        try:
            preexec_fn = (
                (lambda: _apply_limits(memory_mb, int(timeout) + 2))
                if sys.platform != "win32"
                else None
            )
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                code_path,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=preexec_fn,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(test_input.encode()),
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
                    duration_ms=timeout * 1000,
                )

            stdout = stdout_b.decode(errors="replace")
            stderr = stderr_b.decode(errors="replace")

            return TestResult(
                test=TestCase(input=test_input),
                passed=(proc.returncode == 0),
                actual_output=stdout,
                error=stderr if proc.returncode != 0 else "",
                duration_ms=0.0,  # TODO: measure
            )
        finally:
            try:
                os.unlink(code_path)
            except OSError:
                pass


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

    If test_cases is empty, runs code once with no stdin to check it at least
    compiles / doesn't crash.

    Each TestCase chooses its own mode: if `function_name` is set, function-
    call mode is used (LeetCode style — wrap student code, call fn(*args),
    print repr); otherwise stdin/stdout mode.
    """
    if language != "python":
        logger.warning(f"Language {language!r} not yet supported; returning skip result")
        return ExecutionReport(summary=f"Language {language} not supported in sandbox")

    if not test_cases:
        # Empty run to at least check for syntax errors
        result = await run_python_subprocess(
            code, "", timeout=per_case_timeout, memory_mb=memory_mb
        )
        return ExecutionReport(
            passed_count=int(result.passed),
            total_count=1,
            pass_rate=1.0 if result.passed else 0.0,
            results=[result],
            summary="Syntax check only (no test cases provided)",
        )

    # Run each test case concurrently
    coros = []
    for tc in test_cases:
        if tc.function_name:
            coros.append(_run_function_call(code, tc, timeout=per_case_timeout, memory_mb=memory_mb))
        else:
            coros.append(run_python_subprocess(code, tc.input, timeout=per_case_timeout, memory_mb=memory_mb))
    results = await asyncio.gather(*coros)

    # Compare actual vs expected for stdin/stdout cases (function-call cases
    # have already populated `passed` inside _run_function_call).
    for r, tc in zip(results, test_cases):
        r.test = tc
        if tc.function_name:
            continue  # already compared
        if r.passed and tc.expected_output:
            # Normalize trailing whitespace for comparison
            r.passed = r.actual_output.strip() == tc.expected_output.strip()

    passed = sum(1 for r in results if r.passed)
    total = len(results)
    return ExecutionReport(
        passed_count=passed,
        total_count=total,
        pass_rate=passed / total if total else 0.0,
        results=results,
        summary=f"Passed {passed}/{total} tests",
    )


# ─── Function-call (LeetCode-style) mode ──────────────────────────────────────

_FUNCTION_HARNESS = """
# === SmarTAI auto-injected harness — do not modify ===
import json, sys
__smartai_args = json.loads(sys.stdin.read() or "[]")
print(repr({fn}(*__smartai_args)))
"""


def _values_match(actual_repr: str, expected_repr: str) -> bool:
    """Compare two repr-style strings, falling back to literal_eval normalization.

    LLM-generated `expected_return` may be "1" while student returns 1
    (`repr(1) == '1'`), or LLM may use single quotes vs double — literal_eval
    canonicalizes both sides through Python's own AST so equivalent values
    compare equal. Numeric values are also compared with a small tolerance
    so floating-point formatting drift (``0.30000000000000004`` vs ``0.3``)
    does not cause spurious failures.
    """
    a = (actual_repr or "").strip()
    b = (expected_repr or "").strip()
    if a == b:
        return True
    # Numeric tolerance — covers float formatting drift that literal_eval
    # would otherwise honor as "not equal" (Python: 0.3 == 0.30000000000000004
    # is False).
    try:
        return abs(float(a) - float(b)) < 1e-9
    except ValueError:
        pass
    # Structural equality via literal_eval (lists, tuples, strings with
    # different quotes, etc).
    try:
        return ast.literal_eval(a) == ast.literal_eval(b)
    except (ValueError, SyntaxError):
        return False


async def _run_function_call(
    student_code: str,
    tc: TestCase,
    *,
    timeout: float,
    memory_mb: int,
) -> TestResult:
    """Wrap student code with a call harness, then run + compare."""
    fn = tc.function_name or ""
    if not fn or not fn.replace("_", "").isalnum():
        return TestResult(
            test=tc, passed=False, actual_output="",
            error=f"Invalid function_name: {fn!r}",
            duration_ms=0.0,
        )
    args = tc.function_args if tc.function_args is not None else []
    try:
        stdin_payload = json.dumps(args)
    except (TypeError, ValueError) as e:
        return TestResult(
            test=tc, passed=False, actual_output="",
            error=f"function_args not JSON-serializable: {e}",
            duration_ms=0.0,
        )

    wrapped = student_code.rstrip() + "\n" + _FUNCTION_HARNESS.format(fn=fn)
    raw = await run_python_subprocess(
        wrapped, stdin_payload, timeout=timeout, memory_mb=memory_mb,
    )
    if not raw.passed:
        # subprocess failure (timeout / non-zero exit / runtime error)
        raw.test = tc
        return raw
    raw.passed = _values_match(raw.actual_output, tc.expected_return or "")
    raw.test = tc
    return raw
