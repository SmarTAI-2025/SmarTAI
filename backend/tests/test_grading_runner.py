from __future__ import annotations

import pytest

from backend.tools.grading_runner import (
    RunnerExitReason,
    RunnerLimits,
    RunnerRequest,
    RunnerResourceStatus,
    check_sympy_code,
    run_grading_request,
    sanitize_runner_text,
)


def _request(code: str, **limit_overrides) -> RunnerRequest:
    return RunnerRequest(
        execution_id="test-execution:a1",
        task_type="sympy",
        code=code,
        limits=RunnerLimits(**limit_overrides),
    )


def test_runner_contract_is_versioned_and_forbids_unknown_fields():
    request = _request("from sympy import Integer\nprint(Integer(42))")

    assert request.model_dump(mode="json")["schema_version"] == 1
    with pytest.raises(ValueError):
        RunnerRequest(
            execution_id="bad",
            task_type="sympy",
            code="print(1)",
            unexpected=True,
        )


def test_sympy_static_policy_allows_math_and_rejects_host_capabilities():
    allowed = check_sympy_code(
        "from sympy import integrate, symbols\n"
        "x = symbols('x')\n"
        "print(integrate(x**2, (x, 0, 3)))"
    )
    forbidden_import = check_sympy_code("import os\nprint(os.environ)")
    forbidden_file = check_sympy_code("print(open('secret.txt').read())")

    assert allowed.allowed is True
    assert forbidden_import.exit_reason == RunnerExitReason.STATIC_REJECTED
    assert forbidden_file.exit_reason == RunnerExitReason.STATIC_REJECTED


def test_sympy_static_policy_classifies_syntax_errors_before_execution():
    result = check_sympy_code("from sympy import Integer\nprint(Integer(42)")

    assert result.allowed is False
    assert result.exit_reason == RunnerExitReason.SYNTAX_ERROR
    assert "SyntaxError" in result.message


@pytest.mark.asyncio
async def test_real_sympy_program_runs_through_contract():
    result = await run_grading_request(_request(
        "from sympy import integrate, symbols\n"
        "x = symbols('x')\n"
        "print(integrate(x**2, (x, 0, 3)))"
    ))

    assert result.executed is True
    assert result.exit_reason == RunnerExitReason.SUCCESS
    assert result.resource_status == RunnerResourceStatus.WITHIN_LIMITS
    assert result.stdout.strip() == "9"
    assert result.passed_count == result.total_count == 1
    assert result.duration_ms > 0


@pytest.mark.asyncio
async def test_programming_task_is_explicitly_reserved_for_week_two():
    request = RunnerRequest(
        execution_id="future-programming",
        task_type="programming",
        code="print(42)",
    )

    result = await run_grading_request(request)

    assert result.executed is False
    assert result.exit_reason == RunnerExitReason.UNSUPPORTED_TASK
    assert result.resource_status == RunnerResourceStatus.NOT_EXECUTED


@pytest.mark.asyncio
async def test_runner_timeout_is_stable_and_does_not_leak_a_path():
    result = await run_grading_request(_request(
        "while True:\n    pass\nprint(42)",
        timeout_seconds=0.1,
    ))

    assert result.executed is True
    assert result.exit_reason == RunnerExitReason.TIMEOUT
    assert result.resource_status == RunnerResourceStatus.TIMEOUT
    assert "/tmp/" not in result.stderr


@pytest.mark.asyncio
async def test_runner_truncates_excessive_output_and_marks_the_limit():
    result = await run_grading_request(_request(
        "print('x' * 2000)",
        max_output_chars=256,
    ))

    assert result.exit_reason == RunnerExitReason.OUTPUT_LIMIT
    assert result.resource_status == RunnerResourceStatus.OUTPUT_LIMIT
    assert result.stdout_truncated is True
    assert len(result.stdout) <= 256
    assert len(result.stderr) <= 256


@pytest.mark.asyncio
async def test_runtime_traceback_replaces_the_temporary_script_path():
    result = await run_grading_request(_request(
        "from sympy import Integer\nprint(missing_name)"
    ))

    assert result.exit_reason == RunnerExitReason.RUNTIME_ERROR
    assert "/tmp/" not in result.stderr
    assert "<runner>/main.py" in result.stderr


def test_sanitizer_removes_host_paths_without_returning_unbounded_text():
    text, truncated = sanitize_runner_text(
        'File "/home/service/private/main.py", line 2; read /etc/passwd\n'
        + "x" * 500,
        limit=128,
    )

    assert "/home/service" not in text
    assert "/etc/passwd" not in text
    assert text.count("<redacted-path>") == 2
    assert len(text) <= 128
    assert truncated is True
