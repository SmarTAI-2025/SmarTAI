"""
Regression tests for XTS-DSY-W1-SYMPY-LOOP P0:
indefinite-integral +C false-mismatch when the reference is AI-computed.

Background (see help/context/w1-sympy-loop-report.md §3.1):
  When a calculation problem has no teacher reference answer, the skill asks
  the LLM to generate a SymPy program.  SymPy's ``integrate(2*x+3, x)`` returns
  ``x**2 + 3*x`` *without* the +C constant.  A student who correctly writes
  ``x**2 + 3*x + C`` would then be marked mismatched by strict symbolic
  comparison (``simplify((x²+3x+C)-(x²+3x)) = C ≠ 0``) and incorrectly lose
  marks.

Fix (option 4 — minimal scope):
  Only in the *no-reference* branch, when the LLM-generated program contains
  ``integrate(``, verify by comparing derivatives (``diff(student) -
  diff(ref) == 0``) so +C vanishes.  The teacher-reference branch is untouched
  so that a student who omits C against a teacher reference that includes C is
  still correctly marked wrong.

These tests mock the LLM and sandbox so no real provider / code execution is
needed.  Run with::

    SMARTAI_E2E_FAKE_PROVIDER=true \
    python -m pytest backend/tests/test_calculation_indefinite_integral_c.py -v
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock
from typing import Any

import pytest

from backend.models import ExpertResult, ProblemInfo, StudentAnswerInfo
from backend.skills.calculation import CalculationSkill
from backend.tools import numerical


# ─── Helpers (aligned with test_calculation_skill.py conventions) ────────────

def _fake_provider_returning(response_obj: Any = None, raw_content: str = "{}"):
    provider = MagicMock()
    provider.provider_id = "mock:test"
    response = MagicMock()
    response.content = raw_content
    response.duration_ms = 10.0
    provider.ainvoke = AsyncMock(return_value=response)
    return provider


def _make_problem(
    *,
    q_id: str = "q1",
    stem: str = "Compute 6 * 7.",
    criterion: str = "Final value must be 42.",
    reference_answer: str | None = None,
    max_score: float = 10.0,
) -> ProblemInfo:
    return ProblemInfo(
        q_id=q_id,
        number="1",
        type="计算题",
        stem=stem,
        criterion=criterion,
        reference_answer=reference_answer,
        max_score=max_score,
    )


def _make_answer(content: str) -> StudentAnswerInfo:
    return StudentAnswerInfo(q_id="q1", number="1", type="计算题", content=content)


def _llm_output(score=8.0, max_score=10.0, confidence=0.9, comment="OK.", steps=None):
    return MagicMock(
        score=score, max_score=max_score, confidence=confidence,
        comment=comment, steps=steps or [],
    ), MagicMock(content="{}", duration_ms=10.0)


def _patch_generate_and_run(monkeypatch, *, sympy_code, stdout):
    import backend.skills.calculation as calc

    async def fake_generate(provider, problem):
        return sympy_code

    async def fake_run(code, *, timeout=10.0):
        return stdout

    monkeypatch.setattr(calc, "_generate_sympy_program", fake_generate)
    monkeypatch.setattr(calc, "_run_sympy_in_sandbox", fake_run)


def _spy_verifiers(monkeypatch):
    """Wrap the numerical verifiers so tests can assert which one was used."""
    calls = {"derivative": 0, "equivalent": 0}
    orig_deriv = numerical.verify_derivative_equivalent
    orig_equiv = numerical.verify_equivalent

    async def spy_deriv(s, t, **kwargs):
        calls["derivative"] += 1
        return await orig_deriv(s, t, **kwargs)

    async def spy_equiv(s, t, **kwargs):
        calls["equivalent"] += 1
        return await orig_equiv(s, t, **kwargs)

    monkeypatch.setattr(numerical, "verify_derivative_equivalent", spy_deriv)
    # calculation.py does `from backend.tools import numerical` then calls
    # numerical.verify_equivalent, so patching the attribute on the module is
    # enough.
    monkeypatch.setattr(numerical, "verify_equivalent", spy_equiv)
    return calls


# ─── Unit-level: verify_derivative_equivalent ────────────────────────────────

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


# ─── Integration-level: CalculationSkill.grade() flow ────────────────────────

@pytest.mark.asyncio
async def test_no_reference_indefinite_integral_student_with_c_matches(monkeypatch):
    """s3 scenario: no teacher ref, LLM integrates (no +C), student writes +C.

    Before the fix this was marked mismatched (false negative) and the student
    lost marks.  After the fix the skill detects ``integrate(`` and verifies
    via derivatives, yielding ``matched`` -> full marks.
    """
    import backend.skills.calculation as calc

    _patch_generate_and_run(
        monkeypatch,
        sympy_code="import sympy\nx=sympy.Symbol('x')\nprint(sympy.integrate(2*x+3, x))",
        stdout="x**2 + 3*x",
    )
    calls = _spy_verifiers(monkeypatch)

    async def fake_call(*args, **kwargs):
        return _llm_output(score=7.0, comment="过程正确。")

    monkeypatch.setattr("backend.skills.calculation.structured_llm_call", fake_call)

    skill = CalculationSkill(provider=_fake_provider_returning())
    problem = _make_problem(
        stem="求不定积分 ∫ (2*x + 3) dx。",
        criterion="按步骤给分，最终答案正确得满分。",
        reference_answer=None,
    )
    answer = _make_answer("由幂函数积分公式逐项积分，2x 得 x²、3 得 3x，= x**2 + 3*x + C")

    result = await skill.grade(problem, answer, student_id="s1")

    assert calls["derivative"] == 1, "derivative verify should be used for integral"
    assert calls["equivalent"] == 0, "strict verify must not run for no-ref integral"
    # matched -> score forced to max_score
    assert result.score == 10.0
    assert "✓" in result.comment


@pytest.mark.asyncio
async def test_no_reference_non_integral_uses_strict_verify(monkeypatch):
    """A non-integral (e.g. derivative) problem must still use strict verify."""
    _patch_generate_and_run(
        monkeypatch,
        sympy_code="import sympy\nx=sympy.Symbol('x')\nprint(sympy.diff(3*x**2-12*x+9, x))",
        stdout="6*x - 12",
    )
    calls = _spy_verifiers(monkeypatch)

    async def fake_call(*args, **kwargs):
        return _llm_output(score=8.0, comment="正确。")

    monkeypatch.setattr("backend.skills.calculation.structured_llm_call", fake_call)

    skill = CalculationSkill(provider=_fake_provider_returning())
    problem = _make_problem(
        stem="求 f(x)=3*x**2-12*x+9 的导数。",
        criterion="按步骤给分。",
        reference_answer=None,
    )
    answer = _make_answer("= 6*x - 12")

    result = await skill.grade(problem, answer, student_id="s1")

    assert calls["equivalent"] == 1, "strict verify should be used for non-integral"
    assert calls["derivative"] == 0
    assert result.score == 10.0
    assert "✓" in result.comment


@pytest.mark.asyncio
async def test_teacher_reference_integral_omitting_c_still_mismatched(monkeypatch):
    """s5 scenario: teacher ref includes +C, student omits C -> must mismatch.

    This guards the teacher-reference branch against accidental extension of
    the derivative-verify fix.  A student who omits C against a teacher
    reference that includes C is genuinely wrong.
    """
    calls = _spy_verifiers(monkeypatch)

    async def fake_call(*args, **kwargs):
        return _llm_output(score=9.0, comment="基本正确。")

    monkeypatch.setattr("backend.skills.calculation.structured_llm_call", fake_call)

    skill = CalculationSkill(provider=_fake_provider_returning())
    problem = _make_problem(
        stem="求不定积分 ∫ (2*x + 3) dx。",
        criterion="按步骤给分。",
        reference_answer="x**2 + 3*x + C",
    )
    answer = _make_answer("由幂函数积分公式，2x 积分得 x²，3 积分得 3x，逐项相加得 = x**2 + 3*x")

    result = await skill.grade(problem, answer, student_id="s1")

    assert calls["equivalent"] == 1, "strict verify must be used with teacher ref"
    assert calls["derivative"] == 0, "derivative verify must NOT run with teacher ref"
    # mismatched -> process-credit cap at 70%
    assert result.score <= 7.0
    assert "✗" in result.comment


# ════════════════════════════════════════════════════════════════════════════
# PR #25 bugfix regression tests (review feedback on false-matches)
# ────────────────────────────────────────────────────────────────────────────
# Reproduces the two false-match bugs called out on PR #25 plus the
# definite/indefinite confusion, and locks the fixes:
#   A. different constants must NOT be equal (diff(const,x)=0 for both)
#   B. t**2 vs t**3 must NOT be equal (fixed var=x made diff wrt x = 0)
#   C. definite integrals must use STRICT compare, not derivative compare
#   D. non-x variables are honoured (var resolved from the integrate call)
#   E. LLM code that integrates over a substitution var then .subs() must not
#      fool us into differentiating w.r.t. the (now-absent) substitution var
# Plus unit tests for the _parse_integrate_call ast parser.
# ════════════════════════════════════════════════════════════════════════════


# ─── Unit: verify_derivative_equivalent with var resolution ─────────────────

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


# ─── Unit: _parse_integrate_call ast parser ──────────────────────────────────


def _parse_integrate(code):
    from backend.skills.calculation import _parse_integrate_call
    return _parse_integrate_call(code)


def test_parse_indefinite_integral():
    code = "import sympy\nx=sympy.Symbol('x')\nprint(sympy.integrate(2*x+3, x))"
    assert _parse_integrate(code) == (True, "x")


def test_parse_definite_integral():
    code = "import sympy\nx=sympy.Symbol('x')\nprint(sympy.integrate(x**2, (x, 0, 1)))"
    assert _parse_integrate(code) == (False, None)


def test_parse_no_integrate_returns_none():
    code = "import sympy\nx=sympy.Symbol('x')\nprint(sympy.diff(x**2, x))"
    assert _parse_integrate(code) is None


def test_parse_multivariate_integrate_returns_none():
    code = "import sympy\nx,y=sympy.symbols('x y')\nprint(sympy.integrate(x+y, x, y))"
    assert _parse_integrate(code) is None


def test_parse_attribute_call_sp_integrate():
    """``sp.integrate(...)`` (attribute form) must be recognised, not just bare
    ``integrate(...)``.  LLMs commonly ``import sympy as sp``."""
    code = "import sympy as sp\nx=sp.Symbol('x')\nprint(sp.integrate(2*x+3, x))"
    assert _parse_integrate(code) == (True, "x")


def test_parse_attribute_call_definite():
    """``sympy.integrate(f, (x,0,1))`` attribute form + definite."""
    code = "import sympy\nx=sympy.Symbol('x')\nprint(sympy.integrate(x**2, (x, 0, 1)))"
    assert _parse_integrate(code) == (False, None)


def test_parse_three_arg_legacy_definite_returns_none():
    """``integrate(f, x, 0, 1)`` - legacy 3-arg definite form.  We deliberately
    return None (len(args)!=2) so it falls to strict compare.  A definite
    integral under strict compare is still correct (no false match); we just
    lose the var.  This locks the current conservative behaviour.
    """
    code = "import sympy\nx=sympy.Symbol('x')\nprint(sympy.integrate(x**2, x, 0, 1))"
    assert _parse_integrate(code) is None


def test_parse_second_arg_is_call_returns_none():
    """``integrate(f, Symbol('x'))`` - 2nd arg is a Call, not a bare Name.
    Conservative: return None -> strict path.  (Known limitation: a genuine
    indefinite integral written this way would lose derivative-compare and a
    student's +C could be marked mismatched.  Rare in practice; tracked as a
    follow-up, not fixed here.)"""
    code = "import sympy\nprint(sympy.integrate(sympy.Symbol('x')**2, sympy.Symbol('x')))"
    assert _parse_integrate(code) is None


def test_parse_Integral_class_doit_returns_none():
    """``Integral(f, x).doit()`` uses the Integral *class*, not the integrate()
    *function* - the parser only looks for ``integrate`` calls, so returns None.
    Same known limitation as above: genuine indefinite integral falls to strict
    compare.  Locked here so the limitation is visible."""
    code = "import sympy\nx=sympy.Symbol('x')\nprint(sympy.Integral(x**2, x).doit())"
    assert _parse_integrate(code) is None


def test_parse_definite_with_parametric_limit():
    """``integrate(f, (x, 0, t))`` - definite integral with a symbolic upper
    limit.  2nd arg is a Tuple -> definite -> strict compare.  Must NOT be
    mistaken for indefinite (which would derivative-compare a scalar-ish
    result and false-match)."""
    code = "import sympy\nx,t=sympy.symbols('x t')\nprint(sympy.integrate(2*x, (x, 0, t)))"
    assert _parse_integrate(code) == (False, None)


def test_parse_nested_integrate_uses_outer():
    """``integrate(integrate(f, x), y)`` - only the *first* (outer) integrate
    call is considered; its 2nd arg ``y`` decides -> indefinite over y."""
    code = "import sympy\nx,y=sympy.symbols('x y')\nprint(sympy.integrate(sympy.integrate(x*y, x), y))"
    assert _parse_integrate(code) == (True, "y")


def test_parse_first_integrate_wins_in_mixed_code():
    """``a=integrate(f,x); b=integrate(g,(x,0,1)); print(a+b)`` - mixed indefinite
    + definite in one program.  The parser returns on the FIRST call it finds
    (here the indefinite one).  Known limitation: a mixed program is
    classified by its first integrate only.  Locked here."""
    code = (
        "import sympy\nx=sympy.Symbol('x')\n"
        "a=sympy.integrate(2*x+3, x)\n"
        "b=sympy.integrate(x**2, (x, 0, 1))\n"
        "print(a+b)"
    )
    assert _parse_integrate(code) == (True, "x")


# ─── Integration: CalculationSkill.grade() flow for the bugfix paths ─────────

@pytest.mark.asyncio
async def test_definite_integral_uses_strict_compare_not_derivative(monkeypatch):
    """Bug C: ``integrate(f, (x, 0, 1))`` is a DEFINITE integral.  Its result is
    a scalar (possibly with free params) and must use strict verify_equivalent,
    NOT derivative compare (which would make any two scalars 'match' since
    diff(scalar,·)=0).  A wrong student answer must be mismatched.
    """
    _patch_generate_and_run(
        monkeypatch,
        sympy_code="import sympy\nx=sympy.Symbol('x')\nprint(sympy.integrate(x**2, (x, 0, 1)))",
        stdout="1/3",
    )
    calls = _spy_verifiers(monkeypatch)

    async def fake_call(*args, **kwargs):
        return _llm_output(score=8.0, comment="计算过程有误。")

    monkeypatch.setattr("backend.skills.calculation.structured_llm_call", fake_call)

    skill = CalculationSkill(provider=_fake_provider_returning())
    problem = _make_problem(
        stem="求定积分 ∫₀¹ x² dx。",
        criterion="按步骤给分。",
        reference_answer=None,
    )
    answer = _make_answer("= 1/2")  # wrong value

    result = await skill.grade(problem, answer, student_id="s1")

    assert calls["equivalent"] == 1, "definite integral must use strict verify"
    assert calls["derivative"] == 0, "definite integral must NOT use derivative verify"
    assert result.score <= 7.0, "wrong definite integral must be capped"
    assert "✗" in result.comment


@pytest.mark.asyncio
async def test_non_x_variable_indefinite_integral_matches_with_plus_c(monkeypatch):
    """Bug B integration: integrate over t -> ref t**2+3*t, student writes +C.
    var is resolved to t (from the integrate call) and +C vanishes -> matched.
    """
    _patch_generate_and_run(
        monkeypatch,
        sympy_code="import sympy\nt=sympy.Symbol('t')\nprint(sympy.integrate(2*t+3, t))",
        stdout="t**2 + 3*t",
    )
    calls = _spy_verifiers(monkeypatch)

    async def fake_call(*args, **kwargs):
        return _llm_output(score=7.0, comment="过程正确。")

    monkeypatch.setattr("backend.skills.calculation.structured_llm_call", fake_call)

    skill = CalculationSkill(provider=_fake_provider_returning())
    problem = _make_problem(
        stem="求不定积分 ∫ (2*t + 3) dt。",
        criterion="按步骤给分。",
        reference_answer=None,
    )
    answer = _make_answer("逐项积分得 = t**2 + 3*t + C")

    result = await skill.grade(problem, answer, student_id="s1")

    assert calls["derivative"] == 1, "derivative verify should be used for t-integral"
    assert calls["equivalent"] == 0
    assert result.score == 10.0, "correct +C over t must get full marks"
    assert "✓" in result.comment


@pytest.mark.asyncio
async def test_substitution_var_in_llm_code_does_not_false_match(monkeypatch):
    """情况3 integration: LLM code integrates over u then .subs(u, x).
    The parsed var 'u' is discarded by the safety valve (u absent in ref), the
    reference's var x is inferred, and a correct student answer still matches.
    Without the safety valve, diff(·, u)=0 would match ANY answer.
    """
    _patch_generate_and_run(
        monkeypatch,
        sympy_code=(
            "import sympy\n"
            "u,x=sympy.symbols('u x')\n"
            "print(sympy.integrate(2*u+3, u).subs(u, x))"
        ),
        stdout="x**2 + 3*x",
    )
    calls = _spy_verifiers(monkeypatch)

    async def fake_call(*args, **kwargs):
        return _llm_output(score=7.0, comment="正确。")

    monkeypatch.setattr("backend.skills.calculation.structured_llm_call", fake_call)

    skill = CalculationSkill(provider=_fake_provider_returning())
    problem = _make_problem(
        stem="求不定积分 ∫ (2*x + 3) dx。",
        criterion="按步骤给分。",
        reference_answer=None,
    )
    # Student writes the WRONG answer (x**2, missing the 3*x term).  If the
    # safety valve failed and we differentiated wrt u, diff(x**2,u)=0 would
    # falsely match.  With the fix we differentiate wrt x -> 2x vs 2x+3 -> False.
    answer = _make_answer("= x**2 + C")

    result = await skill.grade(problem, answer, student_id="s1")

    assert calls["derivative"] == 1
    assert result.score <= 7.0, "wrong antiderivative must not false-match via u"
    assert "✗" in result.comment
