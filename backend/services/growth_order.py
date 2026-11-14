"""Asymptotic ordering of a bounded list of elementary positive functions, n -> infinity."""

from fractions import Fraction
import re


# Accept a conventional ordering instruction, optionally with n -> positive
# infinity. Residual conditions (including unfamiliar notation) are outside
# the theorem implemented by _order.
_INFINITY = re.compile(
    r"(?:当|as|when)?\s*\bn\s*(?:\\(?:to|rightarrow)\b|→|->|"
    r"趋(?:于|近于?|向)|tends?\s+to|approaches?)\s*"
    r"(?:\+\s*)?(?:\\infty\b|∞|(?:正)?无穷(?:大)?|(?:positive\s+)?infinity)\s*(?:时)?",
    re.I | re.ASCII,
)
_INSTRUCTION_WORDS = re.compile(
    r"增长率|增长速度|从小到大|由小[到至]大|递增|升序|从低到高|排列|排序|顺序|"
    r"下面|下列|以下|下述|各个|函数|表达式|各式|渐近|增长|按照|依照|次序|大小|非降|依次|分别|进行|"
    r"[按将把请给出的各个依为和并序]|"
    r"\b(?:rank|arrange|sort|order|the|following|these|functions?|expressions?|in|by|of|"
    r"their|asymptotic|growth|rates?|increasing|ascending|please)\b",
    re.I,
)


def _infinity_ordering(instruction):
    header = re.sub(r"^\s*(?:第\s*)?\d+(?:\.\d+)*(?:题|[.)、])?\s*(?:[①-⑳]|[(（]\d{1,2}[)）])?\s*", "", instruction)
    header = _INFINITY.sub(" ", header.replace("$", ""))
    return re.fullmatch(r"[\s，,。.;；()（）:：]*", _INSTRUCTION_WORDS.sub("", header)) is not None


def _normalize(expr):
    value = re.sub(r"\\(?:left|right|quad|,|;|!)", "", expr)
    # A space between numeric tokens is not permission to concatenate them.
    if re.search(r"[\d.]\s+[\d.]", value):
        return ""
    value = re.sub(r"\s+", "", value)
    for _ in range(3):
        value = re.sub(r"\\(?:d?frac)\{([^{}]+)\}\{([^{}]+)\}", r"((\1)/(\2))", value)
    value = value.replace(r"\log", "log").replace(r"\sqrt", "sqrt").replace("√", "sqrt")
    value = value.replace("{", "(").replace("}", ")").replace(r"\cdot", "*")
    value = re.sub(r"\^\((\d+|n)\)(?![\d.])", r"^\1", value)
    value = re.sub(r"log_\((\d+)\)(?![\d.])", r"log_\1", value)
    value = value.replace("log_(n)", "log_n")
    value = re.sub(r"(log_\d+|sqrt)\(n\)", r"\1n", value)
    value = re.sub(r"log_n\((\d+)\)(?![\w.])", r"log_n\1", value)
    value = re.sub(r"(?<![\w.)])\((n|\d+)\)(?![\w.(])", r"\1", value)
    return _ungroup(value)


def _ungroup(value):
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
    if len(raw) > 512:
        return None
    t = _normalize(raw)
    depth = 0
    for char in t:
        depth += (char == "(") - (char == ")")
        if depth < 0:
            return None
    if depth:
        return None
    number = r"(?:\d{1,5}(?:\.\d{1,5})?|\.\d{1,5})"

    # A slash belongs to a power operand only inside a grouped fraction.
    # Outside the power a positive constant factor does not change its order.
    quotient = re.fullmatch(rf"(n\^{number})/({number})", t)
    if quotient and Fraction(quotient[2]) > 0:
        t = quotient[1]
    reciprocal = re.fullmatch(rf"({number})/({number})\^n", t)
    if reciprocal and min(Fraction(value) for value in reciprocal.groups()) > 0:
        t = f"({1 / Fraction(reciprocal[2])})^n"

    def power_operand(token):
        value = _ungroup(token)
        pattern = rf"{number}(?:/{number})?" if token != value else number
        return Fraction(value) if re.fullmatch(pattern, value) else None

    if re.fullmatch(r"\d{1,6}(?:\^\d{1,5})?", t) and not t.startswith("0"):
        return (1, 0, 0, 0)
    if t == "n!":
        return (4, 0, 0, 0)
    if t in {"n^n", "n^(n)"}:
        return (5, 0, 0, 0)
    if t == "n":
        return (1, 1, 0, 0)
    if t == "sqrtn":
        return (1, Fraction(1, 2), 0, 0)
    exponent = power_operand(t[2:]) if t.startswith("n^") else None
    if exponent is not None:
        return (1, exponent, 0, 0)
    base = power_operand(t[:-2]) if t.endswith("^n") else None
    if base is not None:
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
    if not re.search(r"(?:由|从)小[到至]大|递增|升序|increasing|ascending", stem, re.I):
        return []
    body = re.split(r"[:：]\s*", stem, maxsplit=1)
    if len(body) != 2 or not _infinity_ordering(body[0]):
        return []
    listing = body[1].strip().removesuffix("。").removesuffix(".").rstrip()
    if listing.startswith("$") and listing.endswith("$") and listing.count("$") == 2:
        listing = listing[1:-1]
    terms = []
    for raw in re.split(r"[,，、;；]", listing):
        term = raw.strip()
        if term.startswith("$") and term.endswith("$"):
            term = term[1:-1]
        terms.append(term)
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
