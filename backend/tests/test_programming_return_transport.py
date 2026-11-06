"""The return channel uses Python literal values, independently of JSON inputs."""
import pytest

from backend.models import TestCase as Case
from backend.tools.code_interpreter import _values_match, run_sandbox


LITERALS = [
    ("{1, 2}", "{2, 1}"),
    ("set()", "set()"),
    ("b'\\x00\\xff'", "b'\\x00\\xff'"),
    ("{(1, 2): b'ok'}", "{(1, 2): b'ok'}"),
    ("{b'key': [{1, 2}, (3, None)]}", "{b'key': [{2, 1}, (3, None)]}"),
    ("1 + 2j", "(1+2j)"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("value,expected", LITERALS)
async def test_python_literal_return_is_observed_and_compared(value, expected):
    report = await run_sandbox(f"def f(): return {value}", [
        Case(function_name="f", function_args=[], expected_return=expected),
        Case(function_name="f", function_args=[], expected_return="'wrong value'"),
    ])
    assert [row.passed for row in report.results] == [True, False]
    assert [row.exit_reason for row in report.results] == ["success", "wrong_answer"]
    assert all(row.return_value and not row.error and row.executed for row in report.results)


@pytest.mark.parametrize("actual,expected", [
    ("{True}", "{1}"), ("{(True,)}", "{(1,)}"),
    ("{(True,): 'x'}", "{(1,): 'x'}"),
    ("{((False,),): 'x'}", "{((0,),): 'x'}"),
    ("{1e999: 'x'}", "{1e999: 'x'}"), ("1e999j", "1e999j"),
])
def test_unordered_values_keep_nested_types_and_finite_numbers(actual, expected):
    assert not _values_match(actual, expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [
    "def f(): return float('nan')",
    "def f(): return frozenset({1, 2})",
    "def f():\n x=[]; x.append(x); return x",
    "class Value:\n def __repr__(self): raise AssertionError('must not call custom repr')\ndef f(): return Value()",
])
async def test_uncomparable_return_is_ungraded_instead_of_student_runtime_failure(code):
    with pytest.raises(ValueError, match="^programming_return_value_unavailable$"):
        await run_sandbox(code, [Case(function_name="f", function_args=[], expected_return="1")])
