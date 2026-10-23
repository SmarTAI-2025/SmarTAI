"""Regression coverage for shared numerical derivative comparison utilities.

The grading pipeline uses the conditional evidence API tested separately in
test_math_verification; these helpers do not determine mathematical grades.
"""
import asyncio

from backend.tools import numerical


def test_derivative_verify_makes_plus_c_equivalent():
    """Student's correct +C should match the AI ref (which lacks C)."""
    ok = asyncio.run(
        numerical.verify_derivative_equivalent("x**2 + 3*x + C", "x**2 + 3*x")
    )
    assert ok is True


def test_derivative_verify_catches_wrong_answer():
    """A genuinely wrong antiderivative must still mismatch."""
    ok = asyncio.run(
        numerical.verify_derivative_equivalent("x**2", "x**2 + 3*x")
    )
    assert ok is False


def test_strict_verify_still_flags_plus_c_difference():
    """verify_equivalent (strict) must still see +C as a difference — this is
    the behaviour relied upon by the teacher-reference branch (s5)."""
    ok = asyncio.run(
        numerical.verify_equivalent("x**2 + 3*x", "x**2 + 3*x + C")
    )
    assert ok is False


def test_different_constants_not_false_match():
    """Bug A: diff(2, x) == diff(1, x) == 0 used to make 2 and 1 'equal'.

    After the fix the integration variable must actually appear in one of the
    expressions; for two bare constants it does not, so we conservatively
    return None (→ caller degrades to LLM_ONLY) instead of a false True.
    """
    result = asyncio.run(
        numerical.verify_derivative_equivalent("2", "1", var="x")
    )
    assert result is not True, "distinct constants must never match"


def test_non_x_variable_wrong_answer_not_false_match():
    """Bug B: t**2 vs t**3 used to match because diff(·, x) treated t as const.

    After the fix the variable is resolved to ``t`` (passed explicitly from the
    integrate call), so derivatives are taken wrt t and the difference is
    non-zero -> False (mismatched).
    """
    ok = asyncio.run(
        numerical.verify_derivative_equivalent("t**2", "t**3", var="t")
    )
    assert ok is False, "t**2 and t**3 must mismatch when var=t"


def test_non_x_variable_plus_c_matches():
    """Positive control for Bug B: with var=t, +C still vanishes correctly."""
    ok = asyncio.run(
        numerical.verify_derivative_equivalent("t**2 + 3*t + C", "t**2 + 3*t", var="t")
    )
    assert ok is True


def test_var_safety_valve_discards_absent_substitution_var():
    """情况3: LLM code ``integrate(2*u+3, u).subs(u, x)`` -> ref is x**2+3*x.
    The parser passes var='u', but u appears in neither expression after subs,
    so the safety valve discards it and falls back to inferring from the
    reference (free symbols {x}) -> uses x -> +C still matches.
    """
    ok = asyncio.run(
        numerical.verify_derivative_equivalent(
            "x**2 + 3*x + C", "x**2 + 3*x", var="u"
        )
    )
    assert ok is True, "absent substitution var must fall back to reference's var"


def test_parametric_indefinite_integral_uses_explicit_var():
    """integrate(2*t*x + 3, x) -> ref t*x**2 + 3*x (free symbols {t, x}).
    Reference free-symbols count is 2, so D1b inference would bail; the explicit
    var='x' from the integrate call must be trusted (it IS in the free symbols)
    so derivative compare works and +C on x vanishes.
    """
    ok = asyncio.run(
        numerical.verify_derivative_equivalent(
            "t*x**2 + 3*x + C", "t*x**2 + 3*x", var="x"
        )
    )
    assert ok is True
