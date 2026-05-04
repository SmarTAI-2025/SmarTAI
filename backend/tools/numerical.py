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
