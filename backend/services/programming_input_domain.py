"""Check proposed inputs against a small, deterministic subset of the frozen question.

An executable reference is an output oracle, not an input-domain oracle. Unknown
constraints abstain; the submitted implementation never participates in this decision.
"""

from __future__ import annotations

import ast
import json
from decimal import Decimal, InvalidOperation
import operator
import re
import unicodedata

from backend.services.programming_test_contract import _teacher_signature, bind_stdin_cases, required_function_name

IDENT = r"[A-Za-z_][A-Za-z_0-9]*"
NUM = r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)"
OPS = r"(?:<=|>=|!=|==|<|>|=)"
_OPERATORS = {
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "=": operator.eq,
    "==": operator.eq,
    "!=": operator.ne,
}
SIGN = (
    r"(?:(?:strictly\s+)?(?:positive|negative)|non[- ]?(?:negative|positive|zero)|"
    r"严格正|严格负|非负|非正|非零|正|负)"
)
BASIC = r"(?:integers?|ints?|浮点数|实数|整数|strings?|str|字符串|numbers?|floats?|数值|数)"
TYPE = rf"(?:lists?\s+of\s+(?:{SIGN}\s*)?{BASIC}|(?:{SIGN}\s*)?{BASIC}\s*(?:列表|数组)|lists?|列表|数组|(?:{SIGN}\s*)?{BASIC})"
DECL = r"(?:is(?:\s+guaranteed\s+to\s+be)?|are|must\s+be|can\s+be|为|是|均为|必须(?:是|为)|保证(?:是|为)|属于|取值为|只能取)"
_CONDITIONAL = re.compile(r"^(?:if\b|when\b|unless\b|otherwise\b|若|当|如果|否则)", re.I)
_UNSUPPORTED = re.compile(
    r"\b(?:sorted|distinct|unique|prime|even|odd|nonempty|non-empty|matrix|graph|tree|permutation|"
    r"divisib\w*|multiple of|coprime|balanced|each row|except|excluding|provided that|subject to|satisf\w*|"
    r"constraint\w*|must|guaranteed|only|at least|at most|range|between|length|size)\b|"
    r"矩阵|图结构|排列|互异|不重复|质数|素数|偶数|奇数|非空|有序|升序|降序|整除|互质|满足|限定|约束|不得|只能|至多|至少|除外|范围|长度",
    re.I,
)


def _normal(text):
    return (
        unicodedata.normalize("NFKC", text or "")
        .replace("≤", "<=")
        .replace("≥", ">=")
        .replace("≠", "!=")
        .replace("−", "-")
        .replace("`", "")
        .strip()
    )


def _clauses(text, *, commas=True):
    # Preserve commas in interval/set literals and decimals in numeric bounds.
    clauses, start, depth = [], 0, 0
    for i, ch in enumerate(text):
        if ch in "{[(":
            depth += 1
        elif ch in "}])":
            depth = max(0, depth - 1)
        elif depth == 0 and (
            (ch in ";；。\n" or (commas and ch in ",，"))
            or (ch == "." and (i + 1 == len(text) or text[i + 1].isspace()))
        ):
            clauses.append(text[start:i].strip())
            start = i + 1
    return [s for s in (*clauses, text[start:].strip()) if s]


def _typed(value, spec, stdin):
    spec = re.sub(r"\s+", " ", spec).strip().lower()
    if re.search(r"\blists?\b|列表|数组", spec):
        if not isinstance(value, list):
            return False
        item = re.sub(r"^(?:lists?\s+of\s+)|(?:列表|数组)$", "", spec)
        return (
            True if item in {"list", "lists", "列表", "数组", ""} else all(_typed(v, item, False) for v in value)
        )
    if re.search(r"\b(?:strings?|str)\b|字符串", spec):
        return isinstance(value, str)
    integer = bool(re.search(r"\b(?:integers?|ints?)\b|整数", spec))
    if stdin:
        if integer and not re.fullmatch(r"[+-]?\d+", value.strip()):
            return False
    elif type(value) not in ((int,) if integer else (int, float)):
        return False
    try:
        n = Decimal(str(value).strip())
        if not n.is_finite():
            return False
    except (ValueError, InvalidOperation):
        return False
    sign = re.match(SIGN, spec, re.I)
    if not sign:
        return True
    label = re.sub(r"[- ]|strictly|严格", "", sign[0].lower())
    return {
        "positive": n > 0,
        "正": n > 0,
        "negative": n < 0,
        "负": n < 0,
        "nonnegative": n >= 0,
        "非负": n >= 0,
        "nonpositive": n <= 0,
        "非正": n <= 0,
        "nonzero": n != 0,
        "非零": n != 0,
    }[label]


def _input_clauses(text):
    for sentence in _clauses(text, commas=False):
        conditional = re.search(r"\b(?:if|when|unless)\b|如果|若|当", sentence, re.I)
        if conditional:
            # In "if a > 0 then return …" the condition is part of the algorithm.
            # In "if a > 0, b must be …" it is an unsupported conditional domain.
            branch = sentence[conditional.start() :]
            sentence = sentence[: conditional.start()]
            if not re.search(r"\b(?:return|print|output)\b|返回|输出", branch, re.I):
                yield "unresolved input constraint"
        yield from _clauses(sentence)


def _declarations(stem, criterion):
    # Remove the call/formal signature before splitting; retain simple annotations.
    annotations = []

    def signature(m):
        for name, typ in re.findall(
            rf"({IDENT})\s*:\s*(int|str|float|list\[int\]|list\[str\])(?=\s*[,)=]|\s*$)", m["args"]
        ):
            annotations.append(
                f"{name} is " + {"list[int]": "list of integers", "list[str]": "list of strings"}.get(typ, typ)
            )
        return ";"

    text = re.sub(
        rf"((?:read|given|输入|给定)\s*(?:two|两个)\s*{TYPE}\s+{IDENT})\s*[,，]\s*({IDENT})(?=\s|[;。.]|$)",
        r"\1 and \2",
        _normal(stem),
        flags=re.I,
    )
    text = re.sub(
        rf"(?:\bdef\s+|\b(?:implement|define|write)\s+(?:the\s+)?(?:function\s+)?|实现\s*(?:函数\s*)?)"
        rf"{IDENT}\s*\((?P<args>[^()]*)\)\s*(?:->\s*{IDENT})?\s*:?",
        signature,
        text,
        flags=re.I,
    )
    result = []
    for clause, rubric in [(s, False) for s in annotations + list(_input_clauses(text))] + [
        (s, True) for s in _input_clauses(_normal(criterion))
    ]:
        if not _CONDITIONAL.match(clause) and not re.match(r"^(?:return|print|output|返回|输出)", clause, re.I):
            pieces = re.split(
                rf"\s+and\s+(?={IDENT}\s+(?:is|must))|\s*且\s*(?={IDENT}\s*(?:是|为|必须))", clause, flags=re.I
            )
            if len(pieces) > 1 and re.search(rf"(?:{TYPE}|{OPS})", pieces[0], re.I):
                result.extend((piece, rubric) for piece in pieces)
                continue
        clause = re.sub(r"\s+with\s+(?=(?:[a-z_]+|[+-]?\d+)\s*(?:<=|>=|<|>))", ";", clause, flags=re.I)
        typed_bound = re.fullmatch(
            rf"({IDENT})\s+(?:is|must be)\s+(?:an?\s+)?({TYPE})\s+(in\s*[\[(].+[\])])", clause, re.I
        )
        if typed_bound:
            result.extend(
                [(f"{typed_bound[1]} is {typed_bound[2]}", rubric), (f"{typed_bound[1]} {typed_bound[3]}", rubric)]
            )
        else:
            result.extend((part.strip(), rubric) for part in clause.split(";") if part.strip())
    return result


def _value_constraint(part, values, names, stdin):
    # Entire mathematical constraints only. Never eval teacher/model text.
    expr = re.sub(r"^(?:where\b|such that\b|满足|其中|保证)\s*", "", part, flags=re.I)
    negative = re.fullmatch(
        rf"(?:inputs?|values?)\s+(?:with\s+)?(.+?)\s+(?:are|is)\s+(?:outside (?:the )?(?:task|domain)|out of scope)",
        expr,
        re.I,
    )
    if negative:
        expr = negative[1]
    interval = re.fullmatch(rf"({names})\s*(?:∈|in|属于)\s*([\[(])\s*({NUM})\s*,\s*({NUM})\s*([\])])", expr, re.I)
    verbal = re.fullmatch(
        rf"({names})\s+(?:is|must be)\s+between\s+({NUM})\s+and\s+({NUM})\s+(inclusive|exclusive)", expr, re.I
    )
    if verbal:
        op = "<=" if verbal[4].lower() == "inclusive" else "<"
        expr = f"{verbal[2]} {op} {verbal[1]} {op} {verbal[3]}"
    if interval:
        expr = f"{interval[3]} {'<=' if interval[2]=='[' else '<'} {interval[1]} {'<=' if interval[5]==']' else '<'} {interval[4]}"
    atoms = rf"(?:{names}|{NUM})"
    if re.fullmatch(rf"{atoms}\s*{OPS}\s*{atoms}(?:\s*{OPS}\s*{atoms})?", expr):
        parts = re.split(rf"\s*({OPS})\s*", expr)
        variables = [a for a in parts[::2] if a in values]
        if not variables:
            return "unknown", ()
        if not stdin and any(type(values[n]) not in (int, float) for n in variables):
            return "outside", ()
        try:
            numbers = [Decimal(str(values.get(a, a)).strip()) for a in parts[::2]]
            if not all(v.is_finite() for v in numbers):
                return "outside", ()
            match = all(_OPERATORS[op](a, b) for op, a, b in zip(parts[1::2], numbers, numbers[1:]))
            if match == bool(negative):
                return "outside", ()
        except (InvalidOperation, ValueError):
            return "outside", ()
        return "allowed", variables
    choices = re.fullmatch(
        rf"({names})\s*(?:∈|in|must be one of|is one of|只能取|取值为)\s*(\{{[^{{}}]+\}})", expr, re.I
    )
    if choices and choices[1] in values:
        if len(choices[2]) > 512:
            return "unknown", ()
        try:
            permitted = ast.literal_eval("[" + choices[2][1:-1] + "]")
        except (SyntaxError, TypeError, ValueError, RecursionError):
            return "unknown", ()
        value = values[choices[1]]
        if not all(type(x) in (int, str) for x in permitted):
            return "unknown", ()
        if not any(
            (str(x) == value.strip() if stdin and type(x) is int else type(x) is type(value) and x == value)
            for x in permitted
        ):
            return "outside", ()
        return "allowed", [choices[1]]

    return None


def input_domain(problem, case):
    """Return 'allowed', 'outside', or 'unknown' for a supplemental case only."""
    stem = problem.stem or ""
    if len(stem) + len(problem.criterion or "") > 16384:
        return "unknown"
    try:
        name = required_function_name(stem)
        sig = _teacher_signature(stem, name) if name else None
    except ValueError:
        return "unknown"
    stdin = case.io_mode != "function" and not case.function_name
    if stdin:
        (case,) = bind_stdin_cases(stem, [case])
        # Multiple unnamed tokens are bound only by an explicit scalar-count declaration.
        values = {"_input": case.input}
    elif sig:
        try:
            values = dict(sig.bind(*(case.function_args or []), **(case.function_kwargs or {})).arguments)
        except TypeError:
            return "outside"
    else:
        args, kwargs = case.function_args or [], case.function_kwargs or {}
        values = {**{f"_arg{i}": v for i, v in enumerate(args)}, **kwargs}
    if not values:
        return "allowed" if sig is not None and not sig.parameters else "unknown"
    names = "|".join(re.escape(n) for n in sorted(values, key=len, reverse=True))
    tested, unknown = set(), False
    for raw, rubric in _declarations(stem, problem.criterion):
        # A comparison inside an instruction, example or conditional branch is
        # not a restriction on callers. Separate independently declared inputs.
        part = re.split(
            r"\s+and\s+(?=(?:print|return|output)\b)|\s+(?=(?:if|when)\b)", raw, maxsplit=1, flags=re.I
        )[0]
        if _CONDITIONAL.match(part) or re.match(r"^(?:else\b|then\b)", part, re.I):
            continue
        part = re.sub(
            r"^(?:the\s+)?(?:constraints?|input\s+domain|valid\s+inputs?|输入范围|输入约束|其中)\s*[:：]\s*",
            "",
            part,
            flags=re.I,
        )
        part = part.strip(" :：")
        if len(values) == 1 and next(iter(values)) in {"_arg0", "_input"}:
            alias = re.fullmatch(
                rf"(?:(?:the )?(?:input|argument|parameter)\s+|(?:参数|输入)\s*)?({IDENT})\s*{DECL}\s*(?:(?:an?|any)\s+)?(?:{TYPE}|{SIGN})",
                part,
                re.I,
            )
            if alias:
                original = next(iter(values))
                values = {alias[1]: values[original]}
                names = re.escape(alias[1])
                if original in tested:
                    tested = {alias[1]}
        if stdin:
            record = re.fullmatch(
                r"(?:read|input) (?:a |one )?(json list of integers|whitespace-separated integers)(?: from stdin)?",
                part,
                re.I,
            )
            if record:
                try:
                    items = json.loads(case.input) if record[1].lower().startswith("json") else case.input.split()
                except (ValueError, RecursionError):
                    return "outside"
                if not isinstance(items, list) or not all(
                    _typed(item, "integer", not record[1].lower().startswith("json")) for item in items
                ):
                    return "outside"
                tested.update(values)
                continue
            part = re.sub(r"\s+separated by (?:whitespace|spaces)(?: from stdin)?$", "", part, flags=re.I)
        checks = []
        # Named, scalar declarations; grouped names may have one shared type.
        named = re.fullmatch(
            rf"(?:(?:the\s+)?(?:input|parameter|argument)\s+|(?:输入|参数)\s*)?"
            rf"(?P<names>(?:{names})(?:(?:\s+and\s+|、)(?:{names}))*)\s*{DECL}\s*(?:(?:a|an|any)\s+)?(?P<type>{TYPE}|{SIGN})",
            part,
            re.I,
        )
        if named:
            targets = re.split(r"\s+and\s+|、", named["names"], flags=re.I)
            if any(n not in values for n in targets):
                unknown = True
                continue
            checks = [(n, named["type"]) for n in targets]
        else:
            decl = re.fullmatch(
                rf"(?:(?:read|given?|input|takes?)\s+|输入\s*|给定\s*)"
                rf"(?:(?P<count>an?|one|two|three|一个|两个|三个|一行|一列)\s*)?(?P<type>{TYPE})"
                rf"(?:\s+(?:from|on)\s+stdin|\s*(?P<names>{IDENT}(?:(?:\s+and\s+|、){IDENT})*))?",
                part,
                re.I,
            )
            if decl:
                count = {"two": 2, "两个": 2, "three": 3, "三个": 3}.get((decl["count"] or "").lower(), 1)
                targets = re.split(r"\s+and\s+|、", decl["names"], flags=re.I) if decl["names"] else list(values)
                if (
                    not stdin
                    and decl["names"]
                    and len(values) == len(targets) == count
                    and all(n.startswith("_arg") for n in values)
                ):
                    values = dict(zip(targets, values.values()))
                    names = "|".join(re.escape(n) for n in sorted(values, key=len, reverse=True))
                if stdin and (count > 1 or decl["names"]):
                    # Only read a literal scalar sequence here. Don't trim string-valued records.
                    if re.search(r"string|str|字符串", decl["type"], re.I):
                        if count != 1:
                            return "unknown"
                        values = {targets[0]: case.input}
                    else:
                        tokens = case.input.split()
                        if len(tokens) != count:
                            return "outside"
                        keys = targets if decl["names"] else [f"_input{i}" for i in range(count)]
                        if len(keys) != count:
                            return "unknown"
                        values = dict(zip(keys, tokens))
                    names = "|".join(re.escape(n) for n in sorted(values, key=len, reverse=True))
                    targets = list(values)
                if len(targets) != count or any(n not in values for n in targets):
                    unknown = True
                else:
                    checks = [(n, decl["type"]) for n in targets]
        if checks:
            for n, typ in checks:
                if stdin and re.search(r"lists?|列表|数组", typ, re.I):
                    unknown = True  # No declared encoding for a list in a byte stream.
                    continue
                if not _typed(values[n], typ, stdin):
                    return "outside"
                tested.add(n)
            continue

        constraint = _value_constraint(part, values, names, stdin)
        if constraint:
            status, checked = constraint
            if status == "outside":
                return status
            unknown |= status == "unknown"
            tested.update(checked)
            continue
        if re.fullmatch(r"read\s+(?:stdin|the input|from standard input)(?:\s+exactly)?", part, re.I) and stdin:
            tested.update(values)
            continue
        if re.fullmatch(r"(?:return|print) (?:its|the) (?:length|size)", part, re.I):
            continue
        # Output tests ("return whether n is positive") never constrain the caller.
        if re.match(r"^(?:return whether|return true if|判断|判断是否)", part, re.I):
            continue
        if _UNSUPPORTED.search(part) or re.search(
            rf"(?:\b(?:{names})\b\s*(?:{DECL}|{OPS}|∈)|输入(?:是|为|的)|input\s+(?:is|must|consists))", part, re.I
        ):
            unknown = True
        elif not rubric and re.search(
            r"\bfor\b.*\b(?:input|arguments?|values?)s?\b|对.+(?:输入|参数)|对于", part, re.I
        ):
            unknown = True
        elif not rubric and not re.match(
            r"^(?:implement(?: the)?(?: required| python)?(?: function)?$|(?:a )?function$|return\b|print\b|echo\b|"
            r"compute\b|calculate\b|output\b|返回|输出|计算|求|实现$|函数$|\.\.\.$)",
            part,
            re.I,
        ):
            # Unparsed descriptive input clauses may state additional requirements.
            unknown = True
    if unknown or tested != set(values):
        return "unknown"
    return "allowed"
