"""Source-binding and automatic-scope regressions; no LLM or network calls."""
from types import SimpleNamespace

import pytest

from backend.services.math_tool_planning import (
    _asserted_condition, _tolerance_values, automatic_checks, binding_error, expression,
)
from backend.skills.math_grading import Claim
from backend.tools.math_verification import MathVerificationRequest, verify_math


def inputs(reference="1", stem="Compare the submitted result.", criterion=""):
    return (SimpleNamespace(stem=stem, reference_answer=reference),
            SimpleNamespace(original_criterion=criterion, effective_criterion=criterion))


def bound(student_text, student_expression, reference="1", **kwargs):
    problem, plan = inputs(reference)
    request = MathVerificationRequest(student_expression=student_expression,
                                      reference_expression=reference, **kwargs)
    return binding_error(request, student_text, reference, problem=problem, plan=plan)


@pytest.mark.parametrize("source,replacement", [
    ("0.10000000000000001", "0.1"),
    ("9007199254740993.0", "9007199254740992.0"),
    ("0.123456789012345678901234567891", "0.123456789012345678901234567892"),
])
def test_decimal_source_identity_does_not_use_rounded_ast_float(source, replacement):
    assert bound(source, replacement) == "student_expression_not_bound"
    assert bound(source, source) is None


@pytest.mark.parametrize("source,replacement", [("0.10", ".1"), ("1e-3", "0.001"), ("1.0", "1")])
def test_exact_numeric_renderings_can_bind_without_algebraic_rewriting(source, replacement):
    assert bound(source, replacement) is None
    assert bound("1+1", "2") == "student_expression_not_bound"


@pytest.mark.parametrize("text", ["0x10", "0b11", "1_000", "True", "'3'"])
def test_unsupported_numeric_spellings_are_not_classified_as_supported_arithmetic(text):
    assert expression(text) is None


@pytest.mark.parametrize("text,expected", [
    ("Absolute tolerance 1e-3.", {0.001}),
    ("Absolute tolerance 0.001.", {0.001}),
    ("Absolute tolerance .001;", {0.001}),
    ("绝对误差不超过1e-3。", {0.001}),
    ("absolute tolerance 1e-3.0", set()),
    ("absolute tolerance 1e-", set()),
    ("absolute tolerance 1.2.3", set()),
    ("absolute tolerance 1..2", set()),
    ("absolute tolerance 1%", set()),
    ("absolute tolerance 1 %", set()),
    ("absolute tolerance 1/1000", set()),
    ("absolute tolerance 1 × 10^-3", set()),
    ("absolute tolerance 1e999", set()),
    ("absolute tolerance 1e-999", set()),
])
def test_tolerance_uses_a_complete_representable_number_token(text, expected):
    assert _tolerance_values(text, "absolute_tolerance") == expected


@pytest.mark.parametrize("text", [
    "x>0", "Given x>0.", "Assume x > 0; calculate.", r"Given \(x>0\).",
    "已知x>0。求值。", "设 x>0；计算。",
])
def test_only_complete_simple_affirmative_condition_clauses_are_bound(text):
    assert _asserted_condition("x>0", text)


@pytest.mark.parametrize("text", [
    "x>0 is false.", "x>0不正确。", "不要假设x>0。", "已知x>0或y>0。",
    "Given x>0 or y>0.", "Suppose x>0 for a contradiction.",
    "Assume x>0 for a contradiction.", "Show x>0.", "If x>0, simplify.",
    "x>0 and y>0", "x>0.1", "x>0 is not required.",
])
def test_mentions_disjunctions_and_local_contradiction_assumptions_are_not_premises(text):
    assert not _asserted_condition("x>0", text)


def test_unbound_condition_never_becomes_a_cas_assumption():
    problem, plan = inputs("x", "x>0 is false. Simplify sqrt(x^2).")
    request = MathVerificationRequest(student_expression="sqrt(x^2)", reference_expression="x",
                                      assumptions={"x": {"positive": True}})
    assert binding_error(request, "sqrt(x^2)", "x", problem=problem, plan=plan) == "assumption_not_bound"
    problem.stem = "Given x>0. Simplify sqrt(x^2)."
    assert binding_error(request, "sqrt(x^2)", "x", problem=problem, plan=plan) is None


@pytest.mark.parametrize("student,student_unit,reference_unit,expected", [
    ("3 s", "s", "m", None),
    ("3 s", None, None, "student_expression_not_bound"),
    ("3", None, None, "reference_expression_not_bound"),
    ("$3 s$; $4 m$", "m", "m", "student_expression_not_bound"),
    ("$3 s$; $3 m$", "m", "m", "student_expression_not_bound"),
])
def test_units_are_bound_to_the_corresponding_quantity(student, student_unit, reference_unit, expected):
    problem, plan = inputs("3 m")
    request = MathVerificationRequest(student_expression="3", reference_expression="3",
                                      student_unit=student_unit, reference_unit=reference_unit)
    assert binding_error(request, student, "3 m", problem=problem, plan=plan) == expected


@pytest.mark.asyncio
async def test_grounded_dimension_mismatch_reaches_the_real_tool_as_a_mismatch():
    problem, plan = inputs("3 m")
    request = MathVerificationRequest(student_expression="3", reference_expression="3",
                                      student_unit="s", reference_unit="m")
    assert binding_error(request, "3 s", "3 m", problem=problem, plan=plan) is None
    assert (await verify_math(request)).status == "refuted"


def claim(text):
    return Claim(id="c", quote=text, verdict="supported", reason="Synthetic submitted statement.")


@pytest.mark.parametrize("stem,text", [
    ("Let i=2. Find i+1.", "i=2"),
    ("Let e be the identity of G.", "e^2=e"),
    ("变量 i 为整数。", "i=2"),
    ("Let I be a matrix.", "I^2=I"),
])
def test_reserved_constant_used_as_variable_is_not_automatically_interpreted_as_a_number(stem, text):
    problem, plan = inputs("3", stem)
    assert automatic_checks([claim(text)], problem, plan, student_answer=text) == []


def test_explicit_variable_shadowing_also_blocks_a_model_proposed_constant_check():
    problem, plan = inputs("3", "Let i=2. Find i+1.")
    request = MathVerificationRequest(student_expression="2", reference_expression="i")
    assert binding_error(request, "i=2", problem.stem, problem=problem, plan=plan) == "ambiguous_constant_symbol"


@pytest.mark.parametrize("text", ["pi/2", "e", "E", "i", "I"])
def test_constants_remain_available_when_not_explicitly_shadowed(text):
    problem, plan = inputs(text)
    checks = automatic_checks([claim(text)], problem, plan, student_answer=text)
    assert len(checks) == 1 and checks[0]["origin"] == "explicit_reference"


def test_reference_comparison_requires_the_entire_answer_not_an_intermediate_quantity():
    problem, plan = inputs("6", "Calculate 2*3.")
    assert automatic_checks([claim("2")], problem, plan, student_answer="Take 2. Then 2*3=6.") == []
    assert automatic_checks([claim("2")], problem, plan) == []
    checks = automatic_checks([claim("2*3=6")], problem, plan, student_answer="Take 2. Then 2*3=6.")
    assert len(checks) == 1 and checks[0]["origin"] == "explicit_relation"


def test_equation_reference_also_requires_a_whole_answer():
    problem, plan = inputs("x=2")
    assert automatic_checks([claim("x=1")], problem, plan, student_answer="Suppose x=1. Later x=2.") == []
    checks = automatic_checks([claim("x=1")], problem, plan, student_answer=" $x=1$ ")
    assert len(checks) == 1 and checks[0]["origin"] == "explicit_equations"


@pytest.mark.asyncio
async def test_sentence_final_scientific_tolerance_does_not_approve_an_excessive_error():
    problem, plan = inputs("1", "Absolute tolerance 1e-3.")
    checks = automatic_checks([claim("1.5")], problem, plan, student_answer="1.5")
    request = MathVerificationRequest.model_validate(checks[0]["request"])
    assert request.absolute_tolerance == 0.001
    assert binding_error(request, "1.5", "1", problem=problem, plan=plan) is None
    assert (await verify_math(request)).status == "refuted"
