"""Unit tests for _sanitize_sympy_output_code (PR #25 延伸: pretty() 防御).

Background — v_s6 regression sample (w1-sympy-loop-regression-report.md §3.1):
the LLM sometimes emits ``sp.pretty(expr, use_unicode=False)`` or ``pprint(...)``
to print the answer, producing multi-line ASCII art that ``sympy.sympify``
cannot parse → SymPy verification silently fails → grading degrades to
LLM_ONLY and loses the deterministic anchor.

These tests lock the sanitiser behaviour:
  - pretty / pprint / srepr / latex / pretty_print  → rewritten to print(str(...))
  - keyword args (e.g. use_unicode=False) are dropped
  - nested calls inside print(...) are still rewritten
  - bare-name and attribute (sp.pretty) forms are both handled
  - clean code is returned unchanged
  - unparseable code is returned unchanged
"""
from __future__ import annotations

from backend.skills.calculation import _sanitize_sympy_output_code


# ─── Rewriting ────────────────────────────────────────────────────────────────


def test_pretty_with_kwarg_rewritten():
    """``print(sp.pretty(integral, use_unicode=False))`` → ``print(str(integral))``.

    This is the exact v_s6 failure case.
    """
    code = (
        "import sympy as sp\n"
        "x, t = sp.symbols('x t')\n"
        "C = sp.symbols('C')\n"
        "expr = 2*t*x + 3\n"
        "integral = sp.integrate(expr, x) + C\n"
        "print(sp.pretty(integral, use_unicode=False))\n"
    )
    out = _sanitize_sympy_output_code(code)
    assert "print(str(integral))" in out
    assert "pretty" not in out


def test_pprint_rewritten():
    """``sp.pprint(result)`` → ``print(str(result))``."""
    code = (
        "import sympy as sp\n"
        "x = sp.symbols('x')\n"
        "result = sp.integrate(x**2, x)\n"
        "sp.pprint(result)\n"
    )
    out = _sanitize_sympy_output_code(code)
    assert "print(str(result))" in out
    assert "pprint" not in out


def test_bare_pretty_inside_print_rewritten():
    """``print(pretty(r))`` (bare name, no sp. prefix) → ``print(str(r))``."""
    code = (
        "from sympy import *\n"
        "x = symbols('x')\n"
        "r = integrate(x**2, x)\n"
        "print(pretty(r))\n"
    )
    out = _sanitize_sympy_output_code(code)
    assert "print(str(r))" in out
    assert "pretty" not in out


def test_srepr_rewritten():
    """``print(sp.srepr(r))`` → ``print(str(r))``."""
    code = (
        "import sympy as sp\n"
        "x = sp.symbols('x')\n"
        "r = sp.integrate(x**2, x)\n"
        "print(sp.srepr(r))\n"
    )
    out = _sanitize_sympy_output_code(code)
    assert "print(str(r))" in out
    assert "srepr" not in out


def test_latex_rewritten():
    """``print(sp.latex(r))`` → ``print(str(r))``."""
    code = (
        "import sympy as sp\n"
        "x = sp.symbols('x')\n"
        "r = sp.integrate(x**2, x)\n"
        "print(sp.latex(r))\n"
    )
    out = _sanitize_sympy_output_code(code)
    assert "print(str(r))" in out
    assert "latex" not in out


def test_pretty_print_alias_rewritten():
    """``pretty_print(r)`` (alias of pprint) -> ``print(str(r))``.

    The import line still mentions ``pretty_print`` (that's fine — it's not a
    call), so we only assert the *call* was rewritten, not that the name
    vanished entirely.
    """
    code = (
        "from sympy import pretty_print, symbols, integrate\n"
        "x = symbols('x')\n"
        "r = integrate(x**2, x)\n"
        "pretty_print(r)\n"
    )
    out = _sanitize_sympy_output_code(code)
    assert "print(str(r))" in out
    # the call line ``pretty_print(r)`` must be gone
    assert "pretty_print(r)" not in out


# ─── No-op cases ──────────────────────────────────────────────────────────────


def test_clean_print_unchanged():
    """Clean ``print(r)`` code is returned unchanged."""
    code = (
        "import sympy as sp\n"
        "x = sp.symbols('x')\n"
        "r = sp.integrate(x**2, x)\n"
        "print(r)\n"
    )
    assert _sanitize_sympy_output_code(code) == code


def test_clean_print_str_unchanged():
    """``print(str(r))`` (already good) is returned unchanged."""
    code = (
        "import sympy as sp\n"
        "x = sp.symbols('x')\n"
        "r = sp.integrate(x**2, x)\n"
        "print(str(r))\n"
    )
    assert _sanitize_sympy_output_code(code) == code


def test_unparseable_code_unchanged():
    """Code with a syntax error is returned unchanged (sandbox will report it)."""
    code = "import sympy as sp\nx = sp.symbols('x'\nprint(x"  # broken
    assert _sanitize_sympy_output_code(code) == code


def test_no_print_at_all_unchanged():
    """Code that only computes (no print, no pretty) is unchanged."""
    code = "import sympy as sp\nx = sp.symbols('x')\nr = sp.integrate(x**2, x)\n"
    assert _sanitize_sympy_output_code(code) == code


# ─── Preserved semantics ──────────────────────────────────────────────────────


def test_indentation_preserved():
    """Rewritten call keeps the original line's indentation."""
    code = (
        "import sympy as sp\n"
        "x = sp.symbols('x')\n"
        "r = sp.integrate(x**2, x)\n"
        "if r is not None:\n"
        "    sp.pprint(r)\n"
    )
    out = _sanitize_sympy_output_code(code)
    assert "    print(str(r))" in out


def test_multiple_format_calls_all_rewritten():
    """If the LLM uses pretty() twice, both are rewritten."""
    code = (
        "import sympy as sp\n"
        "x = sp.symbols('x')\n"
        "a = sp.integrate(x**2, x)\n"
        "print(sp.pretty(a))\n"
        "b = sp.integrate(x**3, x)\n"
        "print(sp.pretty(b))\n"
    )
    out = _sanitize_sympy_output_code(code)
    assert "print(str(a))" in out
    assert "print(str(b))" in out
    assert "pretty" not in out


def test_non_format_function_not_touched():
    """``print(sp.simplify(r))`` is NOT rewritten — simplify is fine."""
    code = (
        "import sympy as sp\n"
        "x = sp.symbols('x')\n"
        "r = sp.integrate(x**2, x)\n"
        "print(sp.simplify(r))\n"
    )
    out = _sanitize_sympy_output_code(code)
    # simplify should remain — we don't touch it
    assert "simplify" in out
    assert out == code
