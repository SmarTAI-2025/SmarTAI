"""A deployment must never mistake development subprocess limits for OCI."""
from unittest.mock import AsyncMock

import pytest

from backend.tools import code_interpreter
from backend.skills.base import classify_skill_error
from backend.skills.programming import ProgrammingSkill
from backend.tests.test_programming_skill import _fake_provider, _make_problem, _make_answer


@pytest.mark.asyncio
async def test_production_host_execution_is_rejected_before_any_process_or_file(monkeypatch):
    monkeypatch.setattr(code_interpreter.settings, "runtime_environment", "production")
    launch = AsyncMock(side_effect=AssertionError("must not start a host process"))
    monkeypatch.setattr(code_interpreter.asyncio, "create_subprocess_exec", launch)
    with pytest.raises(code_interpreter.HostCodeExecutionDisabled):
        await code_interpreter.run_python_subprocess("print(1)")
    launch.assert_not_called()
    kind, message = classify_skill_error(code_interpreter.HostCodeExecutionDisabled())
    assert kind == "general" and "未执行" in message


@pytest.mark.asyncio
async def test_programming_unavailable_is_a_failed_expert_not_student_test_failure(monkeypatch):
    monkeypatch.setattr(code_interpreter.settings, "runtime_environment", "production")
    problem = _make_problem(test_cases=[{"input": "1 2", "expected_output": "3"}])
    provider = _fake_provider()
    result = await ProgrammingSkill(provider).grade(problem, _make_answer("print(sum(map(int, input().split())))"))
    assert result.confidence == 0
    assert result.error_kind == "general"
    assert "未执行" in result.comment
    assert "0/" not in result.comment
    provider.ainvoke.assert_not_called()
