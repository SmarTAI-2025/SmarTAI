import json

from pydantic import BaseModel

from backend.tools.structured_llm import (
    _clean_strings,
    extract_and_parse_json,
    format_math_and_quotes,
)


class _MathPayload(BaseModel):
    prose: str
    solution_code: str


def test_wraps_formula_only_bare_latex_as_one_expression():
    value = "Evaluate\n\\int_{0}^{1} x e^{x^2} dx.\nShow the substitution."

    assert format_math_and_quotes(value) == (
        "Evaluate\n$\\int_{0}^{1} x e^{x^2} dx$.\nShow the substitution."
    )


def test_wraps_bare_inline_latex_atoms_without_swallowing_prose():
    value = r"For a 30^\circ incline with \mu_k=0.20, use m \times n matrices."

    assert format_math_and_quotes(value) == (
        "For a $30^\\circ$ incline with $\\mu_k$=0.20, use m $\\times$ n matrices."
    )


def test_preserves_existing_math_and_code_verbatim():
    value = "Already $\\mu_k=0.2$.\n```python\npattern = r'\\mu'\n```\nUse `r'\\theta'`."

    assert format_math_and_quotes(value) == value


def test_normalizes_standard_latex_delimiters_before_bare_fallback():
    assert format_math_and_quotes(r"Solve \(x^2=1\) now.") == "Solve $x^2=1$ now."


def test_preserves_unfenced_source_code_and_code_fields():
    implementation = "def parse(value):\n    return r'\\mu:' + value"

    assert format_math_and_quotes(implementation) == implementation
    assert _clean_strings({"solution_code": implementation}) == {
        "solution_code": implementation
    }


def test_json_output_normalizes_prose_but_preserves_solution_code():
    parsed = extract_and_parse_json(
        json.dumps(
            {
                "prose": r"Use \mu_k on a 30^\circ incline.",
                "solution_code": r"pattern = r'\mu'",
            }
        ),
        _MathPayload,
    )

    assert parsed.prose == "Use $\\mu_k$ on a $30^\\circ$ incline."
    assert parsed.solution_code == "pattern = r'\\mu'"


def test_repairs_double_escaped_generated_markdown_without_touching_math_meaning():
    value = (
        r"1. **Substitution:** Let $u=x^2$.\\n\\n"
        r"2. Evaluate $$$\\frac{1}{2}\\left(e-1\\right)$$$."
    )

    assert format_math_and_quotes(value) == (
        "1. **Substitution:** Let $u=x^2$.\n\n"
        r"2. Evaluate $$\frac{1}{2}\left(e-1\right)$$."
    )


def test_does_not_decode_real_latex_nu_or_nabla_as_newlines():
    value = r"Use \nu and \nabla f in the proof."

    assert format_math_and_quotes(value) == r"Use $\nu$ and $\nabla$ f in the proof."
