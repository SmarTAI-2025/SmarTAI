"""Real function execution against independently checkable mathematical cases."""
import asyncio
import json
import time

import pytest
from pydantic import ValidationError

from backend.tools import math_verification as mv


def verify(student, reference, **kwargs):
    evidence = asyncio.run(mv.verify_math(mv.MathVerificationRequest(
        student_expression=student, reference_expression=reference, **kwargs,
    )))
    # Wire evidence may contain no SymPy instances, NaNs or infinities.
    json.dumps(evidence.model_dump(mode="json"), allow_nan=False)
    return evidence


@pytest.mark.parametrize("student,reference,kwargs,status", [
    ("1.5707963", "pi/2", {}, "refuted"),
    ("1.5707963", "pi/2", {"allow_approximation": True, "absolute_tolerance": 1e-7}, "supported"),
    ("1.5707963", "pi/2", {"allow_approximation": True, "absolute_tolerance": 1e-9}, "refuted"),
    ("1.5707963", "pi/2", {"allow_approximation": True}, "unknown"),
    ("1.5707963", "pi/2", {"absolute_tolerance": 1}, "refuted"),
    ("0.1 + 0.2", "0.3", {}, "supported"),
    ("0.000001", "0", {"allow_approximation": True, "absolute_tolerance": 1e-6}, "supported"),
    ("0.000001", "0", {"allow_approximation": True, "relative_tolerance": 1e-6}, "refuted"),
    ("1.001", "1", {"allow_approximation": True, "relative_tolerance": .001}, "supported"),
    ("pi", "3.14159", {"comparison": "numeric", "allow_approximation": True, "absolute_tolerance": 1e-5}, "supported"),
    ("e", "E", {"comparison": "numeric"}, "supported"),
    ("I", "i", {"comparison": "numeric"}, "supported"),
    ("sqrt(-1)", "I", {"comparison": "numeric"}, "supported"),
    ("log(-1)", "I*pi", {"comparison": "numeric"}, "supported"),
    ("exp(I*pi)", "-1", {"comparison": "numeric"}, "supported"),
    ("1 + 2*I", "1.00000001 + 2*I", {"comparison": "numeric", "allow_approximation": True, "absolute_tolerance": 1e-6}, "supported"),
    ("1 + 2*I", "1 + I", {"comparison": "numeric"}, "refuted"),
    ("x", "x", {"comparison": "numeric"}, "unknown"),
    ("x-x+1", "1", {"comparison": "numeric"}, "unknown"),
    ("x/x", "1", {"comparison": "numeric"}, "unknown"),
    ("1/0", "1/0", {}, "unknown"),
    ("0/0", "0", {}, "unknown"),
    ("log(0)", "0", {}, "unknown"),
    ("sqrt(-1)", "I", {"domain": "real"}, "unknown"),
    ("x+0.001", "x", {"allow_approximation": True, "absolute_tolerance": 0.1}, "unknown"),
])
def test_constants_and_precision(student, reference, kwargs, status):
    assert verify(student, reference, **kwargs).status == status


@pytest.mark.parametrize("student,reference,kwargs,status", [
    ("(x+1)^2", "x^2+2*x+1", {}, "supported"),
    ("sin(x)^2+cos(x)^2", "1", {}, "supported"),
    ("sqrt(x^2)", "x", {"domain": "real"}, "refuted"),
    ("sqrt(x^2)", "x", {"assumptions": {"x": {"nonnegative": True}}}, "supported"),
    ("sqrt(x^2)", "x", {"domain": "x >= 0"}, "supported"),
    ("sqrt(x^2)", "x", {"domain": "x < 0"}, "refuted"),
    ("log(x^2)", "2*log(x)", {"assumptions": {"x": {"positive": True}}}, "supported"),
    ("(x^2-1)/(x-1)", "x+1", {}, "refuted"),
    ("(x^2-1)/(x-1)", "x+1", {"domain": "x != 1"}, "supported"),
    ("(x^2-1)/(x-1)", "x+1", {"domain": "x > 1"}, "supported"),
    ("(x*(x-1))/x", "x-1", {}, "refuted"),
    ("(x/x)-(x/x)", "0", {}, "refuted"),
    ("1/x", "x^(-1)", {}, "supported"),
    ("sqrt(x)^2", "x", {"domain": "real"}, "refuted"),
    ("sqrt(x)^2", "x", {"domain": "x >= 0"}, "supported"),
    ("log(x)-log(x)", "0", {"domain": "real"}, "refuted"),
    ("x", "x", {"domain": "x > 1 and x < 0"}, "unknown"),
    ("x", "x", {"assumptions": {"x": {"positive": True, "negative": True}}}, "unknown"),
    ("x", "x", {"domain": "x is a positive number"}, "unknown"),
    ("x", "x", {"domain": "x > y and y > x"}, "unknown"),
    ("1/x", "1/x", {"domain": "x == 0"}, "unknown"),
    ("x", "x", {"assumptions": {"x": {"finite": False}}}, "unknown"),
])
def test_symbolic_domains(student, reference, kwargs, status):
    evidence = verify(student, reference, **kwargs)
    assert evidence.status == status, evidence
    if status == "refuted":
        assert evidence.counterexample
        assert evidence.details["witness_kind"] in {"domain", "value"}


def test_counterexample_respects_assumptions_and_domain():
    evidence = verify("sqrt(x*x)", "x", domain="x < -1")
    assert evidence.status == "refuted"
    assert evidence.details["assignment"] == {"x": "-2"}
    assert evidence.details["student_value"] == "2"
    assert evidence.details["reference_value"] == "-2"
    # These are equal at every candidate in the bounded sampler, but distinct
    # polynomials. A matching sample set must never become supported.
    evidence = verify("x*(x-1)*(x+1)*(x-2)*(x+2)*(x-1/2)*(x-3)*(x-I)*(x+I)", "0")
    assert evidence.status == "unknown"


@pytest.mark.parametrize("student,reference,kwargs,status", [
    ("x^2+C", "x^2+C1", {"variable": "x"}, "supported"),
    ("x^2+C", "x^2", {"variable": "x"}, "supported"),
    ("x^2+4", "x^2", {"variable": "x"}, "supported"),
    ("x^2", "x^2+C", {"variable": "x"}, "unknown"),
    ("x^2", "x^3", {"variable": "x"}, "refuted"),
    ("x^2", "x^3", {"variable": "u"}, "unknown"),
    ("x+y", "x+y", {}, "unknown"),
    ("2", "1", {"variable": "x"}, "unknown"),
    ("log(x)", "log(x)+C", {"variable": "x", "domain": "x > 0"}, "unknown"),
    ("log(x)+C1", "log(x)+C", {"variable": "x", "domain": "x > 0"}, "supported"),
    ("log(x)", "log(x)", {"variable": "x"}, "unknown"),
    ("Abs(x)", "x", {"variable": "x", "domain": "real"}, "unknown"),
    ("x^2+sin(C)", "x^2+C1", {"variable": "x", "domain": "real"}, "unknown"),
    ("x^2+C", "x^2+C1", {"variable": "x", "assumptions": {"C": {"positive": True}}}, "unknown"),
    ("x^2+I*C", "x^2+C1", {"variable": "x", "domain": "real"}, "unknown"),
    ("x^2+C", "x^2+C1", {"variable": "x", "assumptions": {"C": {"real": False}}}, "unknown"),
])
def test_primitives_and_constants(student, reference, kwargs, status):
    assert verify(student, reference, comparison="antiderivative", **kwargs).status == status


@pytest.mark.parametrize("student,reference,kwargs,status", [
    ("100", "1", {"student_unit": "cm", "reference_unit": "m"}, "supported"),
    ("1", "1", {"student_unit": "m", "reference_unit": "cm"}, "refuted"),
    ("1", "1", {"student_unit": "N", "reference_unit": "kg*m/s^2"}, "supported"),
    ("1", "1", {"student_unit": "m", "reference_unit": "s"}, "refuted"),
    ("1", "1", {"student_unit": "m"}, "unknown"),
    ("0", "273.15", {"student_unit": "degC", "reference_unit": "K"}, "unknown"),
    ("1", "1", {"student_unit": "__import__('os')", "reference_unit": "m"}, "unknown"),
    ("1.0002", "100", {"student_unit": "m", "reference_unit": "cm",
                      "allow_approximation": True, "absolute_tolerance": .01}, "refuted"),
    ("1.00005", "100", {"student_unit": "m", "reference_unit": "cm",
                       "allow_approximation": True, "absolute_tolerance": .01}, "supported"),
])
def test_units(student, reference, kwargs, status):
    assert verify(student, reference, **kwargs).status == status


@pytest.mark.parametrize("student,reference,comparison,status", [
    ("Integral(sin(x), (x, 0, pi))", "2", "numeric", "supported"),
    ("integrate(exp(x), (x, 0, 1))", "E-1", "numeric", "supported"),
    ("Integral(I*x, (x, 0, 2))", "2*I", "numeric", "supported"),
    ("Integral(x^2, (x, 1, 0))", "-1/3", "numeric", "supported"),
    ("Integral(sin(x), (x, 0, pi))+x-x", "2", "numeric", "unknown"),
    ("Integral(1/x, (x, -1, 1))", "0", "numeric", "unknown"),
    ("Integral(x/x, (x, -1, 1))", "2", "numeric", "unknown"),
    ("Integral(exp(-x*x), (x, 0, 1))", "0", "numeric", "refuted"),
    ("Integral(x, (x, 0, y))", "y^2/2", "equivalence", "unknown"),
    ("Derivative(x^3,x)", "3*x^2", "equivalence", "supported"),
    ("diff(sin(x),x)", "cos(x)", "equivalence", "supported"),
    ("Derivative(x,x)", "1", "numeric", "supported"),
    ("diff(log(x),x)", "1/x", "equivalence", "unknown"),
])
def test_bounded_calculus(student, reference, comparison, status):
    evidence = verify(student, reference, comparison=comparison)
    assert evidence.status == status, evidence


def test_definite_integral_does_not_extend_variable_assumptions_past_bounds():
    evidence = verify(
        "Integral(sqrt(x^2), (x, -1, 1))", "0", comparison="numeric",
        assumptions={"x": {"positive": True}},
    )
    assert evidence.status == "unknown"


@pytest.mark.parametrize("student,reference,kwargs,status", [
    ("Eq(x-1,0)", "Eq(x*(x-1),0)", {}, "refuted"),
    ("x-1 == 0", "x*(x-1) == 0", {"domain": "x != 0"}, "supported"),
    ("Eq(x-1,0)", "Eq(x*(x-1),0)", {"domain": "x > 0"}, "supported"),
    ("Eq(x^2,1)", "Eq((x-1)*(x+1),0)", {}, "supported"),
    ("Eq(x^2,-1)", "Eq(x,I)", {}, "refuted"),
    ("Eq(x^2,-1)", "Eq(x^2,-2)", {"domain": "real"}, "supported"),
    ("Eq((x^2-1)/(x-1),0)", "Eq(x^2-1,0)", {}, "refuted"),
    ("Eq(x^5-x,0)", "Eq(x,0)", {}, "unknown"),
    ("Eq(sin(x),0)", "Eq(x,0)", {}, "unknown"),
    ("Eq(x,y)", "Eq(x,1)", {}, "unknown"),
    ("Eq(x-x,0)", "Eq(0,0)", {}, "unknown"),
    ("Eq(x,0)", "Eq(x,0)", {"domain": "x > 1 and x < 0"}, "unknown"),
])
def test_equation_solution_sets(student, reference, kwargs, status):
    evidence = verify(student, reference, **kwargs)
    assert evidence.status == status, evidence
    assert evidence.method == "solution_sets"


def test_dividing_by_x_loses_a_checked_root():
    evidence = verify("Eq(x-1,0)", "Eq(x*(x-1),0)")
    assert evidence.counterexample == "x=0"
    assert evidence.details["witness_kind"] == "lost_solution"
    assert evidence.details["student_solutions"] == ["1"]
    assert evidence.details["reference_solutions"] == ["0", "1"]


@pytest.mark.parametrize("expression", [
    "__import__('os').system('false')", "(1).__class__", "x[0]",
    "[x for x in (1,2)]", "(lambda: 1)()", "sin(x, evaluate=True)",
    "Symbol('x')", "open('/tmp/unsafe','w')", "exec('raise Exception')",
    "True", "'1'", "[1,2]", "0xff", "1e999", "10**9999999",
    "((((10**128)**128)**128)**128)",
    "0**0", "x**0", "nan", "oo",
    "x**x", "factorial(1000000)", "f(x)", "Eq(x,1)",
    "x(x-1)=0", "\\frac{1}{2}", "sin(" * 40 + "x" + ")" * 40,
])
def test_unsupported_inputs_are_unknown(expression):
    assert verify(expression, "1").status == "unknown"


def test_schema_is_bounded_and_json_safe():
    with pytest.raises(ValidationError):
        mv.MathVerificationRequest(student_expression="x" * 2049, reference_expression="1")
    with pytest.raises(ValidationError):
        mv.MathVerificationRequest(student_expression="1", reference_expression="1", absolute_tolerance=float("nan"))
    with pytest.raises(ValidationError):
        mv.MathVerificationRequest(student_expression="1", reference_expression="1", absolute_tolerance=-1)
    with pytest.raises(ValidationError):
        mv.MathVerificationRequest(student_expression="x", reference_expression="x", assumptions={"x": {"real": "yes"}})
    with pytest.raises(ValidationError):
        mv.MathVerificationRequest(student_expression="x", reference_expression="x", assumptions={"x" * 1000: {"real": True}})
    with pytest.raises(ValidationError):
        mv.MathVerificationRequest(student_expression="x", reference_expression="x", assumptions={"x": {f"flag{i}": True for i in range(20)}})


def test_hostile_input_cannot_write_files(tmp_path):
    sentinel = tmp_path / "sentinel"
    expression = f"__import__('pathlib').Path({str(sentinel)!r}).write_text('bad')"
    assert verify(expression, "1").status == "unknown"
    assert not sentinel.exists()


def test_real_worker_is_killed_on_deadline_and_cancellation(monkeypatch):
    original = asyncio.create_subprocess_exec
    children = []

    async def slow_worker(*args, **kwargs):
        # Real OS process, busy CPU loop, no mocked mathematical result.
        process = await original(args[0], "-I", "-c", "while True: pass", **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_worker)
    monkeypatch.setattr(mv, "WALL_SECONDS", 0.2)
    started = time.monotonic()
    evidence = verify("1", "1")
    assert evidence.status == "unknown" and evidence.method == "timeout"
    assert time.monotonic() - started < 2
    assert children[0].returncode is not None

    async def cancel():
        task = asyncio.create_task(mv.verify_math(mv.MathVerificationRequest(student_expression="1", reference_expression="1")))
        while len(children) < 2:
            await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert children[-1].returncode is not None

    asyncio.run(cancel())


def test_resource_failure_and_malformed_output_are_unknown(monkeypatch):
    original = asyncio.create_subprocess_exec

    async def failed_worker(*args, **kwargs):
        return await original(args[0], "-I", "-c", "raise SystemExit(1)", **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", failed_worker)
    assert verify("1", "1").status == "unknown"

    async def invalid_worker(*args, **kwargs):
        return await original(args[0], "-I", "-c", "print('not JSON')", **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", invalid_worker)
    assert verify("1", "1").status == "unknown"
