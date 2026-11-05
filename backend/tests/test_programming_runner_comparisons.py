"""Execution/assessment boundaries independent of the LLM and grading skill."""
import pytest

from backend.models import TestCase as Case
from backend.tools import code_interpreter as runner


@pytest.mark.parametrize(("actual", "expected", "match"), [
    ("9007199254740992", "9007199254740993", False),
    ("-9007199254740993", "-9007199254740992", False),
    ("9007199254740993", "9007199254740993", True),
    ("[True]", "[1]", False), ("{True: 0}", "{1: 0}", False),
    ("[0.30000000000000004]", "[0.3]", True), ("1e999", "1e999", False),
    ("'five'", "'five'", True), ("None", "None", True),
])
def test_structural_and_precise_values(actual, expected, match):
    assert runner._values_match(actual, expected) is match


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "actual", "expected", "match"), [
    ("default", "unexpected", "", False), ("default", "\n", "", False),
    ("default", "", "", True), ("default", "5\n", "5", True),
    ("exact", " a\r\n", " a\n", True), ("exact", "5\n", "5", False),
    ("trimmed", " a \n b\n\n", " a\n b", True), ("trimmed", " a\n", "a", False),
    ("json", '{"a":1, "b":[true]}', '{"b":[true],"a":1}', True),
    ("json", '{"a":true}', '{"a":1}', False), ("json", 'bad', 'bad', False),
    ("numeric", "1.001", "1", True), ("numeric", "1.01", "1", False),
    ("numeric", "Infinity", "Infinity", False),
])
async def test_explicit_stdout_comparison(monkeypatch, mode, actual, expected, match):
    async def observed(*args, **kwargs):
        return runner.TestResult(Case(), True, actual, "", 1, exit_reason="success")
    monkeypatch.setattr(runner, "run_python_subprocess", observed)
    case = Case(input="", expected_output=expected).model_copy(
        update={"comparison": mode, "abs_tol": .0011, "rel_tol": 0.0})
    result = (await runner.run_sandbox("# fixed response", [case])).results[0]
    assert result.passed is match and result.actual_output == actual
    assert result.exit_reason == ("success" if match else "wrong_answer")


@pytest.mark.asyncio
async def test_no_expectation_is_not_an_empty_expected_value(monkeypatch):
    async def unexpected(*args, **kwargs):
        pytest.fail("Incomplete contract dispatched a runner")
    monkeypatch.setattr(runner, "run_python_subprocess", unexpected)
    with pytest.raises(ValueError, match="Missing expected"):
        await runner.run_sandbox("# fixed response", [Case(input="1")])


@pytest.mark.asyncio
async def test_function_channel_separates_keyword_only_return_and_printed_output():
    # Only this authored fixture is executed in the development subprocess.
    case = Case(function_name="f", function_args=[2], expected_return="7").model_copy(
        update={"function_kwargs": {"y": 5}})
    source = "print('module log')\ndef f(x, *, y):\n print('debug: 999'); return x+y"
    result = (await runner.run_sandbox(source, [case])).results[0]
    assert result.passed and result.actual_output == result.return_value == "7"
    assert result.stdout == "module log\ndebug: 999\n"
    assert result.execution_id and len(result.request_digest) == 64


@pytest.mark.asyncio
async def test_return_transport_preserves_python_literal_types():
    code = "def pair(): return (1, 2)"
    cases = [Case(function_name="pair", function_args=[], expected_return=v) for v in ("[1, 2]", "(1, 2)")]
    report = await runner.run_sandbox(code, cases)
    assert [r.passed for r in report.results] == [False, True]
