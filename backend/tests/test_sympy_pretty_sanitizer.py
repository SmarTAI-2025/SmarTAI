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


def test_nested_formatter_preserves_assignment_and_outer_call():
    """Nested formatter replacement must not delete its assignment target."""
    code = (
        "import sympy as sp\n"
        "x = sp.symbols('x')\n"
        "r = sp.simplify(sp.pretty(x))\n"
        "print(r)\n"
    )
    out = _sanitize_sympy_output_code(code)
    assert "r = sp.simplify(str(x))" in out
    assert "print(r)" in out
    compile(out, "<sanitized>", "exec")


def test_multiline_formatter_inside_print_is_not_double_printed():
    """A multiline wrapped call becomes one print, not print(print(...))."""
    code = (
        "import sympy as sp\n"
        "x = sp.symbols('x')\n"
        "print(\n"
        "    sp.pretty(x, use_unicode=False)\n"
        ")\n"
    )
    out = _sanitize_sympy_output_code(code)
    assert "print(str(x))" in out
    assert "print(print" not in out
    compile(out, "<sanitized>", "exec")


def test_multiple_format_calls_on_one_line_preserve_all_arguments():
    """Span overlap must not drop either formatter call or the outer print."""
    code = (
        "import sympy as sp\n"
        "a, b = sp.symbols('a b')\n"
        "print(sp.pretty(a), sp.latex(b))\n"
    )
    out = _sanitize_sympy_output_code(code)
    assert "print(str(a), str(b))" in out
    assert "pretty" not in out
    assert "latex" not in out
    compile(out, "<sanitized>", "exec")


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


# ─── PR #43 hardening: source confirmation (problem 1) ───────────────────────


def test_non_sympy_same_name_function_not_rewritten():
    """A locally-defined ``def latex(x)`` must NOT be rewritten.

    This is the exact scenario from the PR #43 review: if we blindly rewrite
    any call named ``latex``, ``def latex(x): return x+1; print(latex(2))``
    would change stdout from ``3`` to ``2``, corrupting the reference value.
    """
    code = (
        "def latex(x):\n"
        "    return x + 1\n"
        "print(latex(2))\n"
    )
    out = _sanitize_sympy_output_code(code)
    assert out == code, "must not rewrite a non-SymPy function with the same name"


def test_shadowed_import_not_rewritten():
    """``from sympy import latex; latex = my_func`` shadows the import."""
    code = (
        "from sympy import latex\n"
        "latex = lambda x: x + 1\n"
        "print(latex(2))\n"
    )
    out = _sanitize_sympy_output_code(code)
    assert out == code, "shadowed SymPy name must not be rewritten"


def test_wrong_module_alias_not_rewritten():
    """``import numpy as sp; sp.pretty(r)`` — sp is NOT sympy here."""
    code = (
        "import numpy as sp\n"
        "r = 42\n"
        "print(sp.pretty(r))\n"
    )
    out = _sanitize_sympy_output_code(code)
    assert out == code, "must not rewrite when the module alias is not sympy"


def test_star_import_bare_name_rewritten():
    """``from sympy import *; pretty(r)`` — D-1b heuristic, unshadowed."""
    code = (
        "from sympy import *\n"
        "x = symbols('x')\n"
        "r = integrate(x**2, x)\n"
        "pretty(r)\n"
    )
    out = _sanitize_sympy_output_code(code)
    assert "print(str(r))" in out


def test_star_import_shadowed_bare_name_not_rewritten():
    """``from sympy import *; def pretty(x): ...`` — shadowed, no rewrite."""
    code = (
        "from sympy import *\n"
        "def pretty(x):\n"
        "    return str(x)\n"
        "print(pretty(42))\n"
    )
    out = _sanitize_sympy_output_code(code)
    assert out == code, "shadowed star-import name must not be rewritten"


# ─── PR #43 hardening: nested self-printing funcs (problem 2, D-2b) ──────────


def test_nested_pprint_in_assignment_not_rewritten():
    """``saved = sp.pprint(r)`` — pprint returns None; rewriting loses stdout.

    D-2b: leave it untouched so the sandbox fails safely (→ LLM_ONLY) rather
    than silently changing the return value / losing stdout.
    """
    code = (
        "import sympy as sp\n"
        "x = sp.symbols('x')\n"
        "r = sp.integrate(x**2, x)\n"
        "saved = sp.pprint(r)\n"
        "print(saved)\n"
    )
    out = _sanitize_sympy_output_code(code)
    assert "pprint" in out, "nested pprint must NOT be rewritten"
    assert out == code


def test_nested_pprint_inside_print_not_rewritten():
    """``print(sp.pprint(r))`` — nested pprint, leave untouched (D-2b)."""
    code = (
        "import sympy as sp\n"
        "x = sp.symbols('x')\n"
        "r = sp.integrate(x**2, x)\n"
        "print(sp.pprint(r))\n"
    )
    out = _sanitize_sympy_output_code(code)
    assert "pprint" in out
    assert out == code


def test_nested_pretty_in_assignment_still_rewritten():
    """``saved = sp.pretty(r)`` — pretty returns a string, safe to rewrite."""
    code = (
        "import sympy as sp\n"
        "x = sp.symbols('x')\n"
        "r = sp.integrate(x**2, x)\n"
        "saved = sp.pretty(r)\n"
        "print(saved)\n"
    )
    out = _sanitize_sympy_output_code(code)
    assert "saved = str(r)" in out
    assert "pretty" not in out
    compile(out, "<sanitized>", "exec")


def test_standalone_pprint_still_rewritten():
    """``sp.pprint(r)`` as a standalone expression IS rewritten (unchanged)."""
    code = (
        "import sympy as sp\n"
        "x = sp.symbols('x')\n"
        "r = sp.integrate(x**2, x)\n"
        "sp.pprint(r)\n"
    )
    out = _sanitize_sympy_output_code(code)
    assert "print(str(r))" in out


# ─── PR #43 hardening: keyword-only & alias (problem 3) ──────────────────────


def test_keyword_only_pretty_rewritten():
    """``sp.pretty(expr=r, use_unicode=False)`` — keyword-only arg, no positionals."""
    code = (
        "import sympy as sp\n"
        "x = sp.symbols('x')\n"
        "r = sp.integrate(x**2, x)\n"
        "print(sp.pretty(expr=r, use_unicode=False))\n"
    )
    out = _sanitize_sympy_output_code(code)
    assert "print(str(r))" in out
    assert "pretty" not in out


def test_alias_pretty_rewritten():
    """``fmt = sp.pretty; fmt(r)`` — alias tracked via assignment (D-3a)."""
    code = (
        "import sympy as sp\n"
        "x = sp.symbols('x')\n"
        "r = sp.integrate(x**2, x)\n"
        "fmt = sp.pretty\n"
        "print(fmt(r))\n"
    )
    out = _sanitize_sympy_output_code(code)
    assert "print(str(r))" in out
    assert "fmt = sp.pretty" in out  # the alias assignment itself is preserved


def test_alias_to_non_sympy_not_rewritten():
    """``fmt = numpy.pretty; fmt(r)`` — alias to a non-sympy attr, no rewrite."""
    code = (
        "import numpy as np\n"
        "r = 42\n"
        "fmt = np.pretty\n"
        "print(fmt(r))\n"
    )
    out = _sanitize_sympy_output_code(code)
    assert out == code


def test_alias_reassigned_away_not_rewritten():
    """``fmt = sp.pretty; fmt = print; fmt(r)`` — alias reassigned, no rewrite."""
    code = (
        "import sympy as sp\n"
        "x = sp.symbols('x')\n"
        "r = sp.integrate(x**2, x)\n"
        "fmt = sp.pretty\n"
        "fmt = print\n"
        "fmt(r)\n"
    )
    out = _sanitize_sympy_output_code(code)
    # The last assignment ``fmt = print`` shadows the alias, so fmt(r) is not
    # a confirmed format call and must not be rewritten.
    assert "print(str(r))" not in out
    assert "fmt(r)" in out
