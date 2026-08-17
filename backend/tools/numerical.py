"""
Numerical engine tool using SymPy for symbolic/numerical verification.

Per docs §4.2.1, calculation grading should use a numerical engine to avoid
LLM hallucination on arithmetic. This tool wraps SymPy's equivalence checks
and can verify student answers against a target answer.
"""
from __future__ import annotations

import logging
from typing import Optional, Union

logger = logging.getLogger(__name__)


def _try_import_sympy():
    """Lazy import so the module doesn't hard-require SymPy."""
    try:
        import sympy
        return sympy
    except ImportError:
        logger.warning("sympy not installed; numerical verification disabled")
        return None


async def verify_equivalent(student_expr: str, target_expr: str) -> Optional[bool]:
    """
    Check symbolic equivalence of two expressions.

    Returns:
        True  if expressions are equivalent
        False if expressions are different
        None  if verification cannot be performed (parse error, sympy missing, etc.)
    """
    sympy = _try_import_sympy()
    if sympy is None:
        return None

    try:
        s = sympy.sympify(student_expr, convert_xor=True)
        t = sympy.sympify(target_expr, convert_xor=True)
        diff = sympy.simplify(s - t)
        return diff == 0
    except Exception as e:
        logger.debug(f"verify_equivalent failed: {e}")
        return None


async def verify_value(
    student_value: Union[str, float],
    target_value: Union[str, float],
    *,
    rel_tol: float = 1e-6,
) -> Optional[bool]:
    """
    Check numerical closeness with relative tolerance.

    Handles:
      - Integer / float comparison
      - Fractions (e.g. "3/4" vs "0.75")
      - Simple expressions (e.g. "pi/2" vs "1.5707963")
    """
    sympy = _try_import_sympy()
    if sympy is None:
        return None

    try:
        s = float(sympy.sympify(str(student_value)))
        t = float(sympy.sympify(str(target_value)))
        if t == 0:
            return abs(s) < rel_tol
        return abs((s - t) / t) < rel_tol
    except Exception as e:
        logger.debug(f"verify_value failed: {e}")
        return None


async def simplify_expression(expr: str) -> Optional[str]:
    """Return the simplified form of an expression, or None on failure."""
    sympy = _try_import_sympy()
    if sympy is None:
        return None
    try:
        return str(sympy.simplify(sympy.sympify(expr, convert_xor=True)))
    except Exception:
        return None


# Symbols that students commonly use to denote the arbitrary constant of
# integration.  These are excluded when inferring the integration variable
# from an expression's free symbols, so that e.g. ``x**2 + 3*x + C`` (free
# symbols {x, C}) is still attributed the variable ``x``.
_INTEGRATION_CONSTANT_SYMBOLS = frozenset({"C", "c", "C1", "C2", "c1", "c2"})


def _resolve_integration_variable(
    sympy,
    s_expr,
    t_expr,
    *,
    var: Optional[str] = None,
) -> Optional[str]:
    """Determine the variable to differentiate w.r.t.

    Priority:
      1. ``var`` (parsed from the LLM-generated ``integrate(f, var)`` call) is
         used only when it actually appears in either expression — this is the
         *safety valve* against LLM code that integrates over a substitution
         variable and then ``.subs()`` it away (e.g.
         ``integrate(2*u+3, u).subs(u, x)``).  Without this check, ``u`` would
         be used and both derivatives w.r.t. ``u`` would be 0 → false match.
      2. Fallback: infer from the reference expression's free symbols, after
         stripping common integration-constant symbols (``C``/``c``/…).  The
         reference (SymPy's ``integrate`` output) never carries +C, so its free
         symbols are exactly the genuine variables.  Exactly one must remain.
      3. Otherwise return None (caller degrades to LLM_ONLY rather than risk a
         false match).
    """
    try:
        t_free = set(t_expr.free_symbols)
        s_free = set(s_expr.free_symbols)
        all_free = t_free | s_free
        free_names = {getattr(sym, "name", None) for sym in all_free}

        # (1) explicit var, validated against the symbols that actually appear.
        if var and var in free_names:
            return var

        # (2) infer from the reference's free symbols, minus constant symbols.
        ref_vars = {
            getattr(sym, "name", None)
            for sym in t_free
            if getattr(sym, "name", None) not in _INTEGRATION_CONSTANT_SYMBOLS
        }
        ref_vars.discard(None)
        if len(ref_vars) == 1:
            return next(iter(ref_vars))
    except Exception as e:
        logger.debug(f"_resolve_integration_variable failed: {e}")
    return None


async def verify_derivative_equivalent(
    student_expr: str,
    target_expr: str,
    *,
    var: Optional[str] = None,
) -> Optional[bool]:
    """For indefinite-integral answers, compare derivatives instead of raw
    expressions.

    SymPy's ``integrate`` returns an antiderivative *without* the +C constant.
    A student who correctly writes ``+C`` would therefore be marked mismatched
    by :func:`verify_equivalent` (``simplify(s - t) == C != 0``).  Comparing
    derivatives instead makes the constant vanish, so both ``x**2 + 3*x`` and
    ``x**2 + 3*x + C`` verify against the AI-computed reference.

    Only used for the *no-reference* branch (LLM-generated SymPy), where the
    reference itself lacks +C.  When the teacher supplies a reference that
    includes +C, the strict :func:`verify_equivalent` is used so that a student
    who omits C is correctly marked wrong.

    The integration variable is resolved by :func:`_resolve_integration_variable`:
    an explicit ``var`` (parsed from the ``integrate(f, var)`` call) is preferred
    but only trusted if it appears in either expression; otherwise the variable
    is inferred from the reference's free symbols (constants like ``C`` excluded).
    When the variable cannot be determined, returns ``None`` (caller degrades to
    LLM_ONLY) rather than risk a false match.

    Returns:
        True  if the derivatives w.r.t. the resolved variable are equivalent
        False if they differ
        None  if the variable cannot be determined or verification fails
    """
    sympy = _try_import_sympy()
    if sympy is None:
        return None
    try:
        s = sympy.sympify(student_expr, convert_xor=True)
        t = sympy.sympify(target_expr, convert_xor=True)
        var_name = _resolve_integration_variable(sympy, s, t, var=var)
        if var_name is None:
            return None
        x = sympy.Symbol(var_name)
        diff = sympy.simplify(sympy.diff(s, x) - sympy.diff(t, x))
        return diff == 0
    except Exception as e:
        logger.debug(f"verify_derivative_equivalent failed: {e}")
        return None
