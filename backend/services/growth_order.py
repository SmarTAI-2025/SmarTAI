"""Asymptotic ordering of a bounded list of elementary positive functions, n -> infinity."""

from fractions import Fraction
import re


def _normalize(expr):
    value = re.sub(r"\s+|\$|\\(?:left|right|quad|,|;|!)", "", expr.strip("。.,，；; "))
    for _ in range(3):
        value = re.sub(r"\\(?:d?frac)\{([^{}]*)\}\{([^{}]*)\}", r"((\1)/(\2))", value)
    value = value.replace(r"\log", "log").replace(r"\sqrt", "sqrt").replace("√", "sqrt")
    value = value.replace("{", "(").replace("}", ")").replace(r"\cdot", "*")
    value = re.sub(r"\(([0-9]+|n)\)", r"\1", value)
    while value.startswith("(") and value.endswith(")") and _outer(value):
        value = value[1:-1]
    return value


def _outer(value):
    depth = 0
    for i, c in enumerate(value):
        depth += (c == "(") - (c == ")")
        if depth == 0 and i != len(value) - 1:
            return False
    return depth == 0


def _order(raw):
    t = _normalize(raw)
    depth = 0
    for char in t:
        depth += (char == "(") - (char == ")")
        if depth < 0:
            return None
    if depth:
        return None
    rational = r"(\d{1,5}(?:/\d{1,5})?)"
    if re.fullmatch(r"\d{1,6}(?:\^\d{1,5})?", t) and not t.startswith("0"):
        return (1, 0, 0, 0)
    if t == "n!":
        return (4, 0, 0, 0)
    if t == "n^n":
        return (5, 0, 0, 0)
    if t == "n":
        return (1, 1, 0, 0)
    if t == "sqrtn":
        return (1, Fraction(1, 2), 0, 0)
    m = re.fullmatch(r"n\^\(?" + rational + r"\)?", t)
    if m:
        return (1, Fraction(m[1]), 0, 0)
    m = re.fullmatch(r"\(?" + rational + r"\)?\^n", t)
    if m:
        base = Fraction(m[1])
        if base <= 0:
            return None
        return (3 if base > 1 else 0, base, 0, 0) if base != 1 else (1, 0, 0, 0)
    log = r"log_([2-9]|[1-9][0-9]{1,4})n"
    if re.fullmatch(log, t):
        return (1, 0, 1, 0)
    if re.fullmatch(r"log_([2-9]|[1-9][0-9]{1,4})\(?" + log + r"\)?", t):
        return (1, 0, 0, 1)
    m = re.fullmatch(r"\(" + log + r"\)\^(\d{1,4})", t) or re.fullmatch(
        r"log_(\d+)\^(\d{1,4})n", t
    )
    if m and int(m[1]) > 1:
        return (1, 0, int(m[2]), 0)
    if re.fullmatch(r"n\*?" + log, t) or re.fullmatch(
        r"n/\(?log_n(?:[2-9]|[1-9][0-9]{1,4})\)?", t
    ):
        return (1, 1, 1, 0)
    m = re.fullmatch(r"n\^\(?" + log + r"\)?", t)
    if m:
        return (2, Fraction(1, int(m[1])), 0, 0)
    return None


def ordered_growth_classes(stem):
    if (
        not stem
        or len(stem) > 12000
        or not re.search(r"增长率|order of growth|asymptotic growth", stem, re.I)
    ):
        return []
    if not re.search(r"由小[到至]大|递增|increasing|ascending", stem, re.I):
        return []
    body = re.split(r"[:：]\s*", stem, maxsplit=1)
    if len(body) != 2:
        return []
    terms = [
        term.strip().strip("$。.")
        for term in re.split(r"[,，、;；]", body[1])
        if term.strip()
    ]
    if not 3 <= len(terms) <= 60:
        return []
    try:
        keys = [_order(term) for term in terms]
    except (ValueError, ZeroDivisionError):
        return []
    if any(key is None for key in keys):
        return []
    groups = {}
    for key, term in zip(keys, terms):
        groups.setdefault(key, []).append(term)
    return [groups[key] for key in sorted(groups)]
