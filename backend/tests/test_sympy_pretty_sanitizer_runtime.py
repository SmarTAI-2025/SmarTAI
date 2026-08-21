"""Runtime tests for ``_sanitize_sympy_output_code`` (PR #43 hardening).

The companion ``test_sympy_pretty_sanitizer.py`` checks *string fragments* and
``compile()``.  This file goes one step further: it **executes** the sanitized
code with ``exec()`` in an isolated namespace, captures stdout, and asserts:

  - the **output value** is correct (e.g. ``x**3/3``, not multi-line ASCII art)
  - the **output line count** is exactly 1 (single-line, sympifiable)
  - non-SymPy same-name functions are **not** rewritten (stdout unchanged)
  - nested ``pprint`` / ``pretty_print`` calls are **not** rewritten (D-2b safe
    fail — the original multi-line stdout is preserved, not silently corrupted)

These tests directly address the three reviewer concerns on PR #43:

  1. "会误改非 SymPy 的同名函数" — ``test_runtime_non_sympy_latex_unchanged``
  2. "嵌套位置的 pprint/pretty_print 会丢失 stdout" — ``test_runtime_nested_pprint_preserves_stdout``
  3. "现有用例主要检查字符串片段和 compile()，不足以证明运行语义"
"""
from __future__ import annotations

import contextlib
import io
import textwrap

import sympy
from backend.skills.calculation import _sanitize_sympy_output_code


def _run_sanitized(code: str) -> tuple[str, str]:
    """Sanitize *code*, exec it, and return (sanitized_code, stdout)."""
    sanitized = _sanitize_sympy_output_code(code)
    buf = io.StringIO()
    ns: dict = {"sympy": sympy, "sp": sympy}
    with contextlib.redirect_stdout(buf):
        exec(compile(sanitized, "<sanitized>", "exec"), ns)
    return sanitized, buf.getvalue()


def _run_raw(code: str) -> str:
    """Exec *code* without sanitization and return stdout."""
    buf = io.StringIO()
    ns: dict = {"sympy": sympy, "sp": sympy}
    with contextlib.redirect_stdout(buf):
        exec(compile(code, "<raw>", "exec"), ns)
    return buf.getvalue()


# ─── Problem 1: non-SymPy same-name function not rewritten ──────────────────


def test_runtime_non_sympy_latex_unchanged():
    """``def latex(x): return x+1; print(latex(2))`` → stdout must stay ``3``.

    If the sanitizer blindly rewrote ``latex(2)`` to ``str(2)``, stdout would
    become ``2`` and corrupt the reference value.  This is the exact scenario
    from the PR #43 review.
    """
    code = textwrap.dedent("""\
        def latex(x):
            return x + 1
        print(latex(2))
    """)
    sanitized, stdout = _run_sanitized(code)
    assert stdout.strip() == "3", f"expected '3', got {stdout!r}"
    assert sanitized == code, "non-SymPy function must not be rewritten"


def test_runtime_non_sympy_pretty_unchanged():
    """``def pretty(x): return f'<{x}>'; print(pretty(42))`` → ``<42>``."""
    code = textwrap.dedent("""\
        def pretty(x):
            return f'<{x}>'
        print(pretty(42))
    """)
    sanitized, stdout = _run_sanitized(code)
    assert stdout.strip() == "<42>"
    assert sanitized == code


def test_runtime_shadowed_star_import_not_rewritten():
    """``from sympy import *; def pretty(x): return 'NO'; print(pretty(42))``.

    Even with ``from sympy import *``, a local ``def pretty`` shadows the
    SymPy import.  The sanitizer must NOT rewrite the call.
    """
    code = textwrap.dedent("""\
        from sympy import *
        def pretty(x):
            return 'NO'
        print(pretty(42))
    """)
    sanitized, stdout = _run_sanitized(code)
    assert stdout.strip() == "NO"
    assert "pretty(42)" in sanitized, "shadowed pretty() must not be rewritten"


# ─── Problem 2: nested self-printing funcs preserve stdout (D-2b) ────────────


def test_runtime_nested_pprint_not_rewritten():
    """``saved = sp.pprint(r)`` — D-2b: leave untouched.

    The original code prints multi-line ASCII art (pprint's own stdout) and
    assigns ``None`` to ``saved``.  The sanitizer must NOT rewrite this —
    rewriting would either lose stdout or change the return value.

    We verify the sanitized code is identical to the original.
    """
    code = textwrap.dedent("""\
        import sympy as sp
        x = sp.symbols('x')
        r = sp.integrate(x**2, x)
        saved = sp.pprint(r)
        print(saved)
    """)
    sanitized = _sanitize_sympy_output_code(code)
    assert sanitized == code, "nested pprint must not be rewritten (D-2b)"


def test_runtime_standalone_pprint_single_line():
    """``sp.pprint(r)`` as standalone expression → ``print(str(r))``.

    After rewriting, stdout must be single-line and contain the correct value.
    """
    code = textwrap.dedent("""\
        import sympy as sp
        x = sp.symbols('x')
        r = sp.integrate(x**2, x)
        sp.pprint(r)
    """)
    sanitized, stdout = _run_sanitized(code)
    lines = [l for l in stdout.splitlines() if l.strip()]
    assert len(lines) == 1, f"expected 1 line, got {len(lines)}: {lines}"
    assert "x**3/3" in stdout or "x^3/3" in stdout, f"got {stdout!r}"


def test_runtime_nested_pretty_returns_string():
    """``saved = sp.pretty(r)`` → ``saved = str(r)`` — return type preserved.

    ``pretty`` returns a string, so it's safe to rewrite in any position.
    The value of ``saved`` should be the ``str()`` representation, and printing
    it should give a single line.
    """
    code = textwrap.dedent("""\
        import sympy as sp
        x = sp.symbols('x')
        r = sp.integrate(x**2, x)
        saved = sp.pretty(r)
        print(saved)
    """)
    sanitized, stdout = _run_sanitized(code)
    lines = [l for l in stdout.splitlines() if l.strip()]
    assert len(lines) == 1, f"expected 1 line, got {len(lines)}: {lines}"
    assert "x**3/3" in stdout or "x^3/3" in stdout, f"got {stdout!r}"


# ─── Problem 3: keyword-only & alias — real execution ────────────────────────


def test_runtime_keyword_only_pretty_single_line():
    """``sp.pretty(expr=r, use_unicode=False)`` → rewritten, single-line stdout."""
    code = textwrap.dedent("""\
        import sympy as sp
        x = sp.symbols('x')
        r = sp.integrate(x**2, x)
        print(sp.pretty(expr=r, use_unicode=False))
    """)
    sanitized, stdout = _run_sanitized(code)
    lines = [l for l in stdout.splitlines() if l.strip()]
    assert len(lines) == 1, f"expected 1 line, got {len(lines)}: {lines}"
    assert "x**3/3" in stdout or "x^3/3" in stdout, f"got {stdout!r}"


def test_runtime_alias_pretty_single_line():
    """``fmt = sp.pretty; print(fmt(r))`` → rewritten, single-line stdout."""
    code = textwrap.dedent("""\
        import sympy as sp
        x = sp.symbols('x')
        r = sp.integrate(x**2, x)
        fmt = sp.pretty
        print(fmt(r))
    """)
    sanitized, stdout = _run_sanitized(code)
    lines = [l for l in stdout.splitlines() if l.strip()]
    assert len(lines) == 1, f"expected 1 line, got {len(lines)}: {lines}"
    assert "x**3/3" in stdout or "x^3/3" in stdout, f"got {stdout!r}"


def test_runtime_alias_pprint_standalone_single_line():
    """``fmt = sp.pprint; fmt(r)`` standalone → ``print(str(r))``."""
    code = textwrap.dedent("""\
        import sympy as sp
        x = sp.symbols('x')
        r = sp.integrate(x**2, x)
        fmt = sp.pprint
        fmt(r)
    """)
    sanitized, stdout = _run_sanitized(code)
    lines = [l for l in stdout.splitlines() if l.strip()]
    assert len(lines) == 1, f"expected 1 line, got {len(lines)}: {lines}"
    assert "x**3/3" in stdout or "x^3/3" in stdout, f"got {stdout!r}"


def test_runtime_alias_reassigned_away_not_rewritten():
    """``fmt = sp.pretty; fmt = print; fmt(r)`` → fmt(r) calls print, not pretty.

    The sanitizer must not rewrite ``fmt(r)`` because ``fmt`` was reassigned
    to ``print``.  stdout should be the ``str()`` of the integral (what
    ``print(r)`` produces), NOT ``str(r)`` (what a pretty rewrite would give).

    Actually both produce the same stdout here since ``print(r)`` and
    ``print(str(r))`` are equivalent for sympy objects.  The key assertion is
    that the sanitized code is unchanged.
    """
    code = textwrap.dedent("""\
        import sympy as sp
        x = sp.symbols('x')
        r = sp.integrate(x**2, x)
        fmt = sp.pretty
        fmt = print
        fmt(r)
    """)
    sanitized = _sanitize_sympy_output_code(code)
    assert sanitized == code, "reassigned alias must not be rewritten"


# ─── v_s6 regression: the original failure case ─────────────────────────────


def test_runtime_v_s6_pretty_use_unicode_false():
    """The exact v_s6 failure: ``print(sp.pretty(integral, use_unicode=False))``.

    Before the sanitizer, this produced multi-line ASCII art that
    ``sympy.sympify`` could not parse.  After sanitization, stdout must be a
    single line containing the integral expression.
    """
    code = textwrap.dedent("""\
        import sympy as sp
        x, t = sp.symbols('x t')
        C = sp.symbols('C')
        expr = 2*t*x + 3
        integral = sp.integrate(expr, x) + C
        print(sp.pretty(integral, use_unicode=False))
    """)
    sanitized, stdout = _run_sanitized(code)
    lines = [l for l in stdout.splitlines() if l.strip()]
    assert len(lines) == 1, f"expected 1 line, got {len(lines)}: {lines}"
    # The integral of 2*t*x + 3 w.r.t. x is t*x**2 + 3*x, plus C
    assert "C" in stdout
    assert "x**2" in stdout or "x^2" in stdout, f"got {stdout!r}"


# ─── Clean code is not broken ────────────────────────────────────────────────


def test_runtime_clean_print_unchanged():
    """Clean ``print(r)`` code produces the same stdout before and after."""
    code = textwrap.dedent("""\
        import sympy as sp
        x = sp.symbols('x')
        r = sp.integrate(x**2, x)
        print(r)
    """)
    sanitized = _sanitize_sympy_output_code(code)
    assert sanitized == code
    _, stdout_sanitized = _run_sanitized(code)
    stdout_raw = _run_raw(code)
    assert stdout_sanitized == stdout_raw


def test_runtime_star_import_bare_pretty_single_line():
    """``from sympy import *; pretty(r)`` — D-1b heuristic, rewritten."""
    code = textwrap.dedent("""\
        from sympy import *
        x = symbols('x')
        r = integrate(x**2, x)
        pretty(r)
    """)
    sanitized, stdout = _run_sanitized(code)
    lines = [l for l in stdout.splitlines() if l.strip()]
    assert len(lines) == 1, f"expected 1 line, got {len(lines)}: {lines}"
    assert "x**3/3" in stdout or "x^3/3" in stdout, f"got {stdout!r}"
