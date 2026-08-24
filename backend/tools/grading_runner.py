"""Versioned runner contract for small grading agents.

Week 1 intentionally keeps execution on the existing host subprocess runner.
The contract makes that limitation explicit through ``isolation_mode``; it is
not a production sandbox claim.  A later isolated runner can implement the
same request/result models without changing the SymPy agent loop.
"""
from __future__ import annotations

import ast
import re
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.models import TestCase
from backend.tools.code_interpreter import run_python_subprocess


RUNNER_SCHEMA_VERSION = 1
MAX_SYMPY_CODE_CHARS = 16 * 1024


class RunnerExitReason(str, Enum):
    SUCCESS = "success"
    STATIC_REJECTED = "static_rejected"
    SYNTAX_ERROR = "syntax_error"
    RUNTIME_ERROR = "runtime_error"
    TIMEOUT = "timeout"
    OUTPUT_LIMIT = "output_limit"
    UNSUPPORTED_TASK = "unsupported_task"
    INTERNAL_ERROR = "internal_error"


class RunnerResourceStatus(str, Enum):
    WITHIN_LIMITS = "within_limits"
    NOT_EXECUTED = "not_executed"
    TIMEOUT = "timeout"
    OUTPUT_LIMIT = "output_limit"
    UNKNOWN = "unknown"


class RunnerLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_seconds: float = Field(default=10.0, ge=0.05, le=60.0)
    memory_mb: int = Field(default=256, ge=64, le=1024)
    max_output_chars: int = Field(default=8192, ge=256, le=65_536)


class RunnerRequest(BaseModel):
    """Stable input shape shared by the SymPy and future programming loops."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = RUNNER_SCHEMA_VERSION
    execution_id: str = Field(min_length=1, max_length=128)
    task_type: Literal["sympy", "programming"]
    language: Literal["python"] = "python"
    code: str = Field(min_length=1, max_length=MAX_SYMPY_CODE_CHARS)
    test_cases: list[TestCase] = Field(default_factory=list)
    limits: RunnerLimits = Field(default_factory=RunnerLimits)


class RunnerResult(BaseModel):
    """Sanitized runner output safe to pass to an Agent or public serializer."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = RUNNER_SCHEMA_VERSION
    execution_id: str
    executed: bool
    exit_reason: RunnerExitReason
    passed_count: int = Field(default=0, ge=0)
    total_count: int = Field(default=0, ge=0)
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    duration_ms: float = Field(default=0.0, ge=0)
    resource_status: RunnerResourceStatus = RunnerResourceStatus.UNKNOWN
    isolation_mode: Literal["host_subprocess", "oci_container"] = "host_subprocess"


class SympyStaticCheck(BaseModel):
    allowed: bool
    exit_reason: RunnerExitReason
    message: str = ""


_FORBIDDEN_CALLS = {
    "open",
    "input",
    "eval",
    "exec",
    "compile",
    "__import__",
    "breakpoint",
    "help",
    "globals",
    "locals",
    "vars",
    "dir",
    "getattr",
    "setattr",
    "delattr",
}
_FORBIDDEN_NAMES = {
    "__builtins__",
    "os",
    "sys",
    "subprocess",
    "socket",
    "pathlib",
    "shutil",
    "requests",
    "urllib",
}
_FORBIDDEN_ATTRIBUTES = {
    "system",
    "popen",
    "fork",
    "spawn",
    "connect",
    "urlopen",
    "read_text",
    "read_bytes",
    "write_text",
    "write_bytes",
    "unlink",
    "remove",
    "rmdir",
    "mkdir",
}


class _SympyPolicyVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.violations: list[str] = []
        self.print_calls = 0

    def _reject(self, node: ast.AST, message: str) -> None:
        line = getattr(node, "lineno", None)
        suffix = f" at line {line}" if line else ""
        self.violations.append(f"{message}{suffix}")

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        for alias in node.names:
            if alias.name != "sympy" and not alias.name.startswith("sympy."):
                self._reject(node, f"import {alias.name!r} is not allowed")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        module = node.module or ""
        if node.level or (module != "sympy" and not module.startswith("sympy.")):
            self._reject(node, f"import from {module!r} is not allowed")
        if any(alias.name == "*" for alias in node.names):
            self._reject(node, "wildcard imports are not allowed")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:  # noqa: N802
        if node.id in _FORBIDDEN_NAMES:
            self._reject(node, f"name {node.id!r} is not allowed")
        if isinstance(node.ctx, ast.Store) and node.id == "print":
            self._reject(node, "overwriting print is not allowed")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802
        if node.attr.startswith("_") or node.attr in _FORBIDDEN_ATTRIBUTES:
            self._reject(node, f"attribute {node.attr!r} is not allowed")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        if isinstance(node.func, ast.Name):
            if node.func.id == "print":
                self.print_calls += 1
            if node.func.id in _FORBIDDEN_CALLS:
                self._reject(node, f"call to {node.func.id!r} is not allowed")
        self.generic_visit(node)


def check_sympy_code(code: str) -> SympyStaticCheck:
    """Check the frozen Week 1 SymPy allow/deny policy without executing code."""

    if len(code) > MAX_SYMPY_CODE_CHARS:
        return SympyStaticCheck(
            allowed=False,
            exit_reason=RunnerExitReason.STATIC_REJECTED,
            message=f"code exceeds {MAX_SYMPY_CODE_CHARS} characters",
        )
    try:
        tree = ast.parse(code, filename="<sympy-runner>", mode="exec")
    except SyntaxError as exc:
        line = exc.lineno or 0
        return SympyStaticCheck(
            allowed=False,
            exit_reason=RunnerExitReason.SYNTAX_ERROR,
            message=f"SyntaxError at line {line}: {exc.msg}",
        )

    visitor = _SympyPolicyVisitor()
    visitor.visit(tree)
    if visitor.print_calls != 1:
        visitor.violations.append(
            f"exactly one print() call is required; found {visitor.print_calls}"
        )
    if visitor.violations:
        return SympyStaticCheck(
            allowed=False,
            exit_reason=RunnerExitReason.STATIC_REJECTED,
            message="; ".join(visitor.violations[:8]),
        )
    return SympyStaticCheck(
        allowed=True,
        exit_reason=RunnerExitReason.SUCCESS,
    )


_ABSOLUTE_PATH_RE = re.compile(
    r"(?:[A-Za-z]:\\(?:[^\\\s\"']+\\)*[^\\\s\"']+|"
    r"(?<![\w.])/(?:[^/\s\"'():,]+/)+[^/\s\"'():,]+)"
)


def _truncate_text(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    marker = "\n...[truncated]"
    if limit <= len(marker):
        return value[:limit], True
    return value[: limit - len(marker)] + marker, True


def sanitize_runner_text(value: str, *, limit: int) -> tuple[str, bool]:
    """Remove host paths and enforce the public runner output bound."""

    sanitized = _ABSOLUTE_PATH_RE.sub("<redacted-path>", value or "")
    return _truncate_text(sanitized, limit)


def _not_executed_result(
    request: RunnerRequest,
    *,
    reason: RunnerExitReason,
    message: str,
) -> RunnerResult:
    stderr, truncated = sanitize_runner_text(
        message,
        limit=request.limits.max_output_chars,
    )
    return RunnerResult(
        execution_id=request.execution_id,
        executed=False,
        exit_reason=reason,
        stderr=stderr,
        stderr_truncated=truncated,
        resource_status=RunnerResourceStatus.NOT_EXECUTED,
    )


async def run_grading_request(request: RunnerRequest) -> RunnerResult:
    """Validate and execute one request through the frozen Week 1 contract."""

    if request.task_type != "sympy":
        return _not_executed_result(
            request,
            reason=RunnerExitReason.UNSUPPORTED_TASK,
            message="programming task execution is reserved for the Week 2 loop",
        )
    if request.test_cases:
        return _not_executed_result(
            request,
            reason=RunnerExitReason.STATIC_REJECTED,
            message="sympy requests do not accept programming test cases",
        )

    check = check_sympy_code(request.code)
    if not check.allowed:
        return _not_executed_result(
            request,
            reason=check.exit_reason,
            message=check.message,
        )

    try:
        raw = await run_python_subprocess(
            request.code,
            "",
            timeout=request.limits.timeout_seconds,
            memory_mb=request.limits.memory_mb,
            max_output_chars=request.limits.max_output_chars,
        )
    except Exception as exc:  # pragma: no cover - defensive process boundary
        return _not_executed_result(
            request,
            reason=RunnerExitReason.INTERNAL_ERROR,
            message=f"runner internal error: {type(exc).__name__}",
        )

    stdout, stdout_truncated = sanitize_runner_text(
        raw.actual_output,
        limit=request.limits.max_output_chars,
    )
    stderr, stderr_truncated = sanitize_runner_text(
        raw.error,
        limit=request.limits.max_output_chars,
    )
    if raw.exit_reason == "timeout":
        reason = RunnerExitReason.TIMEOUT
        resource_status = RunnerResourceStatus.TIMEOUT
    elif raw.exit_reason == "output_limit":
        reason = RunnerExitReason.OUTPUT_LIMIT
        resource_status = RunnerResourceStatus.OUTPUT_LIMIT
    elif raw.passed:
        reason = RunnerExitReason.SUCCESS
        resource_status = RunnerResourceStatus.WITHIN_LIMITS
    else:
        reason = RunnerExitReason.RUNTIME_ERROR
        resource_status = RunnerResourceStatus.UNKNOWN

    return RunnerResult(
        execution_id=request.execution_id,
        executed=True,
        exit_reason=reason,
        passed_count=1 if reason == RunnerExitReason.SUCCESS else 0,
        total_count=1,
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=stdout_truncated or raw.stdout_truncated,
        stderr_truncated=stderr_truncated or raw.stderr_truncated,
        duration_ms=raw.duration_ms,
        resource_status=resource_status,
    )
