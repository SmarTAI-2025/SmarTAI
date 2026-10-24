import pytest

from backend.services.math_source_evidence import match_source_quote


@pytest.mark.parametrize("source,quote", [
    (r"Thus \(x^2=1\).", "$x^2=1$"),
    (r"Thus \[x^2=1\].", "$$x^2 = 1$$"),
    ("First line\n second line", "First line second line"),
    (r"Use \(\frac{a}{b}=1\)", r"$\frac {a} {b} = 1$"),
    ("Result $x = 1$ follows.", r"\(x=1\)"),
])
def test_presentation_differences_locate_the_actual_original_span(source, quote):
    result = match_source_quote(source, quote)
    assert result is not None and result.method == "format"
    assert result.text == source[result.start:result.end]
    assert result.text in source


@pytest.mark.parametrize("source,quote", [
    ("x^2=1", "x^3=1"), ("x^2=1", "x^2=-1"),
    ("forall x", "exists x"), (r"$x \neq 0$", "$x=0$"),
    ("x/x", "1"), ("A=B=C", "A=C"), ("A=B=C", "A=...=C"),
    ("xy=1", "x y = 1"), ("12=12", "1 2 = 12"),
    (r"\thetax=1", r"\theta x = 1"), ("x>0", "x>=0"),
    (r"$x \in R$", r"$x \in \mathbb{R}$"),
    ("$x=1$", r"\( x=1"), ("x=1", "$$x=1$"),
    (r"Cost \$5", "Cost 5"), ("x=1 and x=1", "$x = 1$"),
    ("x=10", "x=1"), ("x=1.25", "x=1"), ("x=1e3", "x=1"),
    (r"\alpha", "alpha"), ("123", "23"), ("alpha=1", "pha=1"),
    ("αβ=1", "β=1"),
])
def test_changed_or_ambiguous_evidence_is_not_accepted(source, quote):
    assert match_source_quote(source, quote) is None


def test_exact_match_and_limits():
    result = match_source_quote('It says "x=1".', '"x=1"')
    assert result.text == '"x=1"' and result.method == "exact"
    assert match_source_quote("x=1 and x=1", "x=1") is None
    assert match_source_quote("abc", " ") is None
    assert match_source_quote("abc\0def", "abc") is None
    assert match_source_quote("a" * 500001, "a") is None


def test_invalid_substrings_do_not_make_a_valid_token_occurrence_ambiguous():
    assert match_source_quote("ENERGY E result", "E").text == "E"
    assert match_source_quote("x=10 then x=1", "x=1").text == "x=1"
    assert match_source_quote("IDENTITY I result", "I").text == "I"


def test_chinese_rule_can_quote_a_phrase_and_punctuation_is_not_decimal():
    assert match_source_quote("每个独立错误扣1分", "独立错误扣1分").text == "独立错误扣1分"
    assert match_source_quote("x=1. Therefore...", "x=1").text == "x=1"


@pytest.mark.parametrize("source,quote,expected", [
    ("Hence $x=1$.", r"Hence \(x = 1\)", "Hence $x=1$"),
    ("$x=1$, done.", r"\(x = 1\), done.", "$x=1$, done."),
    ("Hence $x=1$.", "Hence $x=1", "Hence $x=1$"),
    ("$a+x=1$", "x = 1", "x=1"),
])
def test_complete_formula_retains_paired_delimiters_without_adding_content(source, quote, expected):
    assert match_source_quote(source, quote).text == expected
