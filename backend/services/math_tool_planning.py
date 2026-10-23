"""Conservative source binding and automatic, bounded math opportunities.

This is a syntax bridge, not a second grader or a LaTeX theorem prover. Only
explicit arithmetic/quantities are scheduled automatically. Unsupported text
is left to the model; no tool failure or parsing decision assigns a mark.
"""
from __future__ import annotations

import ast
from decimal import Decimal, InvalidOperation
import math
import re

from backend.services.math_source_evidence import match_source_quote
from backend.tools.math_verification import MathVerificationRequest, _tree, _FUNCTIONS


_NUMBER = r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"


def _unfence(text):
    text = text.strip()
    for left, right in (("$$", "$$"), ("\\(", "\\)"), ("\\[", "\\]"), ("$", "$")):
        if text.startswith(left) and text.endswith(right) and len(text) >= len(left) + len(right):
            inner = text[len(left):-len(right)]
            if left not in inner and right not in inner:
                return inner.strip()
    return text


def expression(text, *, allow_unknown_functions=False):
    """Return supported-looking arithmetic without evaluating user text."""
    text = _unfence(text)
    if not text or len(text) > 2048:
        return None
    # Constant/rendering aliases are syntactic, never algebraic equivalence.
    text = re.sub(r"\\pi\b", "pi", text).replace("π", "pi")
    text = text.replace("\\cdot", "*").replace("\\times", "*")
    try:
        tree = _tree(text)
    except ValueError:
        return None
    except Exception:
        return None
    allowed = (ast.Expression, ast.Constant, ast.Name, ast.Load, ast.BinOp,
               ast.UnaryOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow,
               ast.UAdd, ast.USub, ast.Call, ast.Tuple)
    for node in ast.walk(tree):
        if not isinstance(node, allowed):
            return None
        if isinstance(node, ast.Constant):
            token = ast.get_source_segment(tree.source, node)
            if (type(node.value) not in (int, float) or token is None
                    or len(token) > 100 or not re.fullmatch(_NUMBER, token)):
                return None
            try:
                Decimal(token)
            except InvalidOperation:
                return None
        if isinstance(node, ast.Call) and (not isinstance(node.func, ast.Name)
                or (node.func.id not in _FUNCTIONS and not allow_unknown_functions) or node.keywords):
            return None
        if isinstance(node, ast.Name) and node.id not in _FUNCTIONS and not re.fullmatch(
                r"[A-Za-z][A-Za-z0-9]{0,2}", node.id):
            return None
    return text


def _identity(text):
    parsed = expression(text, allow_unknown_functions=True)
    if parsed is None:
        return None
    tree = _tree(parsed)
    # AST float values have already rounded the original decimal token. Bind
    # numeric leaves exactly, including digits beyond binary64 precision.
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant):
            value = Decimal(ast.get_source_segment(tree.source, node))
            sign, digits, exponent = value.as_tuple()
            digits = list(digits)
            while digits and digits[-1] == 0:
                digits.pop()
                exponent += 1
            node.value = (sign, tuple(digits), exponent if digits else 0)
    return ast.dump(tree, include_attributes=False)


def quantity(text, *, allow_unknown_functions=False):
    parsed = expression(text, allow_unknown_functions=allow_unknown_functions)
    if parsed is not None:
        return parsed, None
    # Do not split on an operator space (e.g. "x + y") or invent units.
    match = re.fullmatch(r"(.+?)\s+([A-Za-zµΩ][A-Za-z0-9µΩ*/^(). -]*)", _unfence(text))
    if match and expression(match[1]) is not None and not re.search(r"\s", match[2].strip()):
        return expression(match[1]), match[2]
    return None


def _statements(text):
    # These prefixes assert a following relation; arbitrary prose, negations,
    # questions and local hypotheses are not discarded to expose a formula.
    prefix = (r"^(?:(?:(?:逐项)?(?:计算|化简|求导|积分|展开)(?:结果)?|答案|结果)\s*[:：]\s*|"
              r"(?:答案|结果)\s*(?:为|是)\s*|"
              r"(?:计算得|可得|由此可得)\s*[:：]?\s*|(?:因此|所以)\s*[,，:：]?\s*|"
              r"(?:thus|hence|therefore)\s*[:,]?\s+)")
    for part in re.split(r"[;；。\n]", _unfence(text)):
        part = part.strip()
        if part.endswith(".") and not part.endswith(".."):
            part = part[:-1].rstrip()
        part = re.sub(prefix, "", part, count=1, flags=re.I)
        if part:
            yield _unfence(part)


def _relation_parts(text):
    """Complete operands only, with their units and relation operators.

    A derivative label can identify its RHS, but is not sent to CAS as an
    arithmetic expression. Reject a whole malformed relation instead of
    salvaging a convenient numeric suffix or an operand inside a function.
    """
    for part in _statements(text):
        if re.search(r"[<>!≠≤≥]|==", part):
            continue
        split = re.split(r"([=≈])", part)
        if len(split) < 3:
            continue
        terms, operators = [term.strip() for term in split[::2]], split[1::2]
        values = [quantity(term, allow_unknown_functions=True) for term in terms]
        if all(value is not None or (index == 0 and re.fullmatch(
                r"[A-Za-z][A-Za-z0-9]{0,2}'\([A-Za-z][A-Za-z0-9]{0,2}\)", term))
                for index, (term, value) in enumerate(zip(terms, values))):
            yield terms, operators, values


def _pieces(text):
    for part in _statements(text):
        if quantity(part, allow_unknown_functions=True) is not None:
            yield part
    for terms, operators, values in _relation_parts(text):
        for term, value in zip(terms, values):
            if value is not None:
                yield term
        for index, operator in enumerate(operators):
            left, right = values[index:index + 2]
            if (operator == "=" and left and right and left[1] is None and right[1] is None
                    and expression(left[0]) is not None and expression(right[0]) is not None):
                yield f"Eq({left[0]},{right[0]})"


def _adjacent_relation(request, text):
    target = ((_identity(request.student_expression), request.student_unit),
              (_identity(request.reference_expression), request.reference_unit))
    for _, operators, values in _relation_parts(text):
        for index in range(len(operators)):
            left, right = values[index:index + 2]
            if left and right:
                pair = ((_identity(left[0]), left[1]), (_identity(right[0]), right[1]))
                if target in (pair, pair[::-1]):
                    return True
    return False


def _bound_units(expr, sources):
    target = _identity(expr)
    if target is None:
        return set()
    units = set()
    for source in sources:
        for part in _pieces(source):
            # A syntactically bound unknown function is still genuine source
            # text. The worker decides support and returns unknown; binding
            # must not confuse unavailable mathematics with invented input.
            value = quantity(part, allow_unknown_functions=True)
            if value and _identity(value[0]) == target:
                units.add(value[1])
    return units


def _bound(expr, sources, unit=None):
    # A unit must belong to this quantity, not merely occur elsewhere. Missing
    # units must not turn an explicitly dimensional answer into a bare number.
    return _bound_units(expr, sources) == {unit}


def _tolerance_values(source, key):
    labels = (("absolute_tolerance", "absolute tolerance", "absolute error", "atol", "绝对误差", "绝对容差")
              if key.startswith("absolute") else
              ("relative_tolerance", "relative tolerance", "relative error", "rtol", "相对误差", "相对容差"))
    # Do not backtrack from 1e-3. to the prefix 1. Sentence punctuation is
    # allowed; a following digit, exponent, decimal continuation or % is not.
    pattern = ("(?:" + "|".join(map(re.escape, labels))
               + r")\s*(?:[:=：≤]|不超过|不大于)?\s*(" + _NUMBER
               + r")(?![\w%％]|\.(?=[\d.]))(?!\s*(?:[%％*/^×·÷]|[+-]\s*\d))")
    values = set()
    for match in re.finditer(pattern, source, re.I):
        try:
            precise = Decimal(match[1])
            value = float(precise)
            if math.isfinite(value) and Decimal(str(value)) == precise:
                values.add(value)
        except (InvalidOperation, OverflowError, ValueError):
            continue
    return values


def _asserted_condition(condition, source):
    """Accept simple affirmative clauses, not mentions or logical alternatives.

    Complex natural language remains unverified rather than being silently
    turned into an unconditional CAS assumption.
    """
    for clause in re.split(r"[;；。\n]|\.(?!\d)", source):
        clause = clause.strip()
        clause = re.sub(r"^(?:(?:given|assume|let|where|with)\s+|(?:已知|假设|设|其中|条件为)\s*)",
                        "", clause, count=1, flags=re.I)
        clause = _unfence(clause)
        found = match_source_quote(clause, condition)
        if found and not clause[:found.start].strip() and not clause[found.end:].strip():
            return True
    return False


def _shadowed_constants(source):
    """Reserved CAS constants explicitly used as assignment/variable names.

    This detects a small syntax, not every possible meaning of i or e. Such
    symbols cannot be safely sent to a verifier that reserves their names.
    """
    names = set()
    patterns = (
        r"(?<![A-Za-z0-9_\\])([iIeE])\s*=(?!=)",
        r"\b(?:variable|parameter|index)\s+([iIeE])\b",
        r"(?<![A-Za-z0-9_\\])([iIeE])\s+(?:is|be)\s+(?:(?:an?|the)\s+)?(?:integer|real|index|variable|parameter|identity|matrix|vector|element)\b",
        r"(?:变量|参数|下标)\s*([iIeE])(?![A-Za-z0-9_])",
        r"(?<![A-Za-z0-9_\\])([iIeE])\s*(?:为|是)(?:整数|实数|变量|参数|下标|单位元|矩阵|向量)",
    )
    for pattern in patterns:
        names.update(match[1] for match in re.finditer(pattern, source, re.I))
    return names


def _uses_names(expr, names):
    parsed = expression(expr)
    return parsed is not None and any(isinstance(node, ast.Name) and node.id in names
                                     for node in ast.walk(_tree(parsed)))


def binding_error(request, claim_quote, context_quote, *, problem, plan):
    """Reject ungrounded expressions/extra assumptions before invoking CAS.

    The finite phrase support is deliberately conservative. A missed natural
    language condition yields unknown, not a false or unconditional verdict.
    """
    sources = (problem.stem, problem.reference_answer or "", plan.effective_criterion)
    if not any(match_source_quote(source, context_quote) for source in sources):
        return "unmatched_context"
    claim_relation = _adjacent_relation(request, claim_quote)
    if not _bound(request.student_expression, [claim_quote], request.student_unit) and not claim_relation:
        return "student_expression_not_bound"
    # Prefer an actual reference quantity over the student's equal-valued
    # quantity. Otherwise a missing reference unit could bind back to the
    # bare student value and erase a dimensional mismatch.
    reference_units = set()
    reference_from_claim = False
    for index, source in enumerate((problem.reference_answer or "", context_quote, claim_quote)):
        reference_units = _bound_units(request.reference_expression, [source])
        if reference_units:
            reference_from_claim = index == 2
            break
    if reference_units != {request.reference_unit} and not (reference_from_claim and claim_relation):
        return "reference_expression_not_bound"
    if reference_from_claim and not claim_relation:
        return "relation_not_bound"
    shadowed = _shadowed_constants(problem.stem + "\n" + claim_quote)
    if any(_uses_names(expr, shadowed) for expr in
           (request.student_expression, request.reference_expression)):
        return "ambiguous_constant_symbol"
    # A reference solution using a convenient assumption does not establish
    # that the original question permits it.
    conditions = "\n".join((problem.stem, plan.original_criterion))
    for name, flags in request.assumptions.items():
        for flag, enabled in flags.items():
            alternatives = {
                "positive": [f"{name}>0", f"{name}为正数", f"{name} is positive"],
                "negative": [f"{name}<0", f"{name}为负数", f"{name} is negative"],
                "nonzero": [f"{name}!=0", f"{name}≠0", f"{name} is nonzero"],
                "nonnegative": [f"{name}>=0", f"{name}≥0"],
                "nonpositive": [f"{name}<=0", f"{name}≤0"],
                "real": [f"{name}∈R", f"{name}为实数", f"{name} is real", f"real {name}"],
                "integer": [f"{name}∈Z", f"{name}为整数", f"{name} is integer"],
            }.get(flag, [])
            if not enabled or not any(_asserted_condition(text, problem.stem) for text in alternatives):
                return "assumption_not_bound"
    if request.domain and not _asserted_condition(request.domain, problem.stem):
        return "domain_not_bound"
    for key in ("absolute_tolerance", "relative_tolerance"):
        value = getattr(request, key)
        if value is not None:
            if _tolerance_values(conditions, key) != {value}:
                return "tolerance_not_bound"
    if request.allow_approximation and not re.search(
            r"近似|约为|有效数字|小数|容差|误差|approx|tolerance|decimal|significant", conditions, re.I):
        return "approximation_not_bound"
    return None


def automatic_checks(claims, problem, plan=None, *, student_answer=None):
    """Discover explicit claim equations and simple final-value comparisons.

    This deterministic pass is independent of the model returning checks=[];
    it cannot guarantee every semantic opportunity has been found.
    Reference comparisons require a claim covering the entire student_answer;
    intermediate statements may only schedule their own explicit relation.
    """
    checks = []
    claims = list(claims)
    reference = quantity(problem.reference_answer or "")
    shadowed = _shadowed_constants(problem.stem + "\n" + (student_answer if student_answer is not None
                                    else "\n".join(claim.quote for claim in claims)))
    precision = {}
    if plan is not None:
        source = "\n".join((problem.stem, plan.original_criterion))
        for key in ("absolute_tolerance", "relative_tolerance"):
            values = _tolerance_values(source, key)
            if len(values) == 1:
                precision[key] = values.pop()
        if precision:
            precision.update(allow_approximation=True, comparison="numeric")
    for claim in claims:
        if claim.kind != "statement":
            continue
        text = _unfence(claim.quote)
        located = match_source_quote(student_answer, claim.quote) if student_answer is not None else None
        whole_answer = bool(located and not student_answer[:located.start].strip()
                            and not student_answer[located.end:].strip())
        for _, operators, values in _relation_parts(claim.quote):
            for index, operator in enumerate(operators):
                left, right = values[index:index + 2]
                if not left or not right or bool(left[1]) != bool(right[1]):
                    continue
                if operator == "≈" and not precision:
                    continue
                expressions = [expression(value[0]) for value in (left, right)]
                if (not all(expressions)
                        or (_identity(expressions[0]) == _identity(expressions[1]) and left[1] == right[1])
                        or any(_uses_names(side, shadowed) for side in expressions)
                        or any(isinstance(node, ast.Name) and node.id not in _FUNCTIONS | {"pi", "E", "e", "I", "i"}
                               for side in expressions for node in ast.walk(_tree(side)))):
                    continue
                checks.append({"claim_id": claim.id, "context_quote": problem.stem,
                    "request": MathVerificationRequest(student_expression=expressions[1],
                        reference_expression=expressions[0], student_unit=right[1], reference_unit=left[1],
                        **(precision if operator == "≈" else {})).model_dump(mode="json"), "origin": "explicit_relation"})
        if text.count("=") == 1 and not re.search(r"[<>!]", text):
            left, right = text.split("=")
            if any(_uses_names(side, shadowed) for side in (left, right)):
                continue
            # x=3 can be a solution/constraint, not an identity for every x.
            # Compare equations as solution sets only when both are explicit.
            ref = _unfence(problem.reference_answer or "")
            if whole_answer and ref.count("=") == 1 and not re.search(r"[<>!]", ref):
                ref_left, ref_right = ref.split("=")
                if (all(expression(side) is not None for side in (left, right, ref_left, ref_right))
                        and not any(_uses_names(side, shadowed) for side in (ref_left, ref_right))):
                    checks.append({"claim_id": claim.id, "context_quote": problem.reference_answer,
                        "request": MathVerificationRequest(student_expression=f"Eq({left},{right})",
                            reference_expression=f"Eq({ref_left},{ref_right})").model_dump(mode="json"),
                        "origin": "explicit_equations"})
        value = quantity(text)
        if (whole_answer and value and reference
                and not any(_uses_names(part, shadowed) for part in (value[0], reference[0]))):
            checks.append({"claim_id": claim.id, "context_quote": problem.reference_answer,
                "request": MathVerificationRequest(student_expression=value[0], reference_expression=reference[0],
                    student_unit=value[1], reference_unit=reference[1], **precision).model_dump(mode="json"), "origin": "explicit_reference"})
    return checks
