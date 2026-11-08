"""Bounded witnesses for textbook questions asking for a marked statement's count.

Parse a small scalar C subset as data. Never compile or execute uploaded code.
A concrete trace can refute a proposed formula; agreement at sample inputs
alone cannot prove a general formula or a student's derivation.
"""

import ast
from fractions import Fraction
from math import isqrt
import operator
import re

from backend.services.question_structure import build_major_question_structure


class Unsupported(ValueError):
    pass


_ARITH = {
    "+": operator.add,
    "-": operator.sub,
    "*": operator.mul,
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "==": operator.eq,
    "!=": operator.ne,
}
_COUNTS = re.compile(
    r"频度|执行次数|执行.*?多少次|execution count|how many times|frequency", re.I
)


class _Trace:
    def __init__(self, n):
        self.values = {"n": n}
        self.count = 0
        self.fuel = 35_000

    def visit(self, node):
        from pycparser import c_ast as c

        self.fuel -= 1
        if self.fuel <= 0:
            raise Unsupported()
        if isinstance(node, c.Compound):
            for child in node.block_items or []:
                self.visit(child)
        elif isinstance(node, c.Label) and node.name == "smartai_count":
            self.count += 1
            self.visit(node.stmt)
        elif (
            isinstance(node, c.Constant)
            and node.type == "int"
            and re.fullmatch(r"0|[1-9][0-9]{0,8}", node.value)
        ):
            return int(node.value)
        elif isinstance(node, c.ID):
            return self.values.get(node.name)
        elif isinstance(node, c.BinaryOp) and node.op in _ARITH:
            left, right = self.visit(node.left), self.visit(node.right)
            if left is None or right is None:
                return None
            return self.bound(_ARITH[node.op](left, right))
        elif isinstance(node, c.ExprList):
            for child in node.exprs:
                self.visit(child)
        elif isinstance(node, c.Assignment) and isinstance(node.lvalue, c.ID):
            right = self.visit(node.rvalue)
            if node.op != "=":
                if node.op not in {"+=", "-=", "*="}:
                    raise Unsupported()
                left = self.visit(node.lvalue)
                right = (
                    None
                    if left is None or right is None
                    else self.bound(_ARITH[node.op[0]](left, right))
                )
            self.values[node.lvalue.name] = right
        elif (
            isinstance(node, c.UnaryOp)
            and node.op in {"p++", "++", "p--", "--"}
            and isinstance(node.expr, c.ID)
        ):
            old = self.visit(node.expr)
            self.values[node.expr.name] = (
                None if old is None else self.bound(old + (1 if "+" in node.op else -1))
            )
            return old if node.op.startswith("p") else self.values[node.expr.name]
        elif isinstance(node, c.If):
            child = node.iftrue if self.condition(node.cond) else node.iffalse
            if child is not None:
                self.visit(child)
        elif isinstance(node, (c.While, c.DoWhile, c.For)):
            if isinstance(node, c.For):
                self.visit(node.init)
            first = isinstance(node, c.DoWhile)
            while first or self.condition(node.cond):
                first = False
                self.visit(node.stmt)
                if isinstance(node, c.For):
                    self.visit(node.next)
        elif not isinstance(node, c.EmptyStatement):
            raise Unsupported()
        return None

    def condition(self, node):
        value = self.visit(node)
        if value is None:
            raise Unsupported()
        return bool(value)

    @staticmethod
    def bound(value):
        if abs(value) > 2**31 - 1:
            raise Unsupported()
        return value


def _closed_count(block):
    """Prove two monotone loop forms by matching initialization, guard and every body path."""
    from pycparser import c_ast as c

    statements = block.block_items or []
    if not statements or not isinstance(statements[-1], c.While):
        return None
    values = {}

    def initial(node):
        if isinstance(node, c.Constant) and node.value.isdecimal():
            return int(node.value)
        if isinstance(node, c.ID):
            return values.get(node.name, "n" if node.name == "n" else None)
        return None

    for item in statements[:-1]:
        assignments = item.exprs if isinstance(item, c.ExprList) else [item]
        for assign in assignments:
            if (
                not isinstance(assign, c.Assignment)
                or assign.op != "="
                or not isinstance(assign.lvalue, c.ID)
            ):
                return None
            values[assign.lvalue.name] = initial(assign.rvalue)
    loop = statements[-1]
    if not isinstance(loop.cond, c.BinaryOp) or loop.cond.op not in ("<=", ">="):
        return None
    left, right = (
        (loop.cond.left, loop.cond.right)
        if loop.cond.op == "<="
        else (loop.cond.right, loop.cond.left)
    )
    if initial(right) != "n":
        return None

    def one(node):
        if isinstance(node, c.Compound):
            return (
                node.block_items[0]
                if node.block_items and len(node.block_items) == 1
                else None
            )
        return node

    body = one(loop.stmt)
    if not isinstance(body, c.Label) or body.name != "smartai_count":
        return None
    target = one(body.stmt)

    def increment(node):
        node = one(node)
        return (
            node.expr.name
            if isinstance(node, c.UnaryOp)
            and node.op in ("p++", "++")
            and isinstance(node.expr, c.ID)
            else None
        )

    def offset_var(node):
        if (
            isinstance(node, c.BinaryOp)
            and node.op == "+"
            and isinstance(node.left, c.ID)
            and initial(node.right) == 1
        ):
            return node.left.name
        return None

    if (
        isinstance(left, c.BinaryOp)
        and left.op == "*"
        and isinstance(target, c.UnaryOp)
    ):
        name = increment(target)
        if (
            name
            and values.get(name) == 0
            and offset_var(left.left) == name
            and offset_var(left.right) == name
        ):
            return "isqrt(n)"
    if (
        isinstance(left, c.BinaryOp)
        and left.op == "+"
        and isinstance(left.left, c.ID)
        and isinstance(left.right, c.ID)
        and isinstance(target, c.If)
    ):
        names = {left.left.name, left.right.name}
        if len(names) != 2 or not all(isinstance(values.get(k), int) for k in names):
            return None
        condition = target.cond
        if not (
            isinstance(condition, c.BinaryOp)
            and condition.op in {"<", "<=", ">", ">=", "==", "!="}
            and isinstance(condition.left, c.ID)
            and isinstance(condition.right, c.ID)
            and {condition.left.name, condition.right.name} <= names
        ):
            return None
        if {increment(target.iftrue), increment(target.iffalse)} <= names:
            start = sum(values[k] for k in names)
            return "n" if start == 1 else f"max(0,n-({start})+1)"
    return None


def marked_counts(stem):
    if not stem or len(stem) > 32_000 or "@" not in stem or not _COUNTS.search(stem):
        return {}
    try:
        from pycparser import CParser
        from pycparser.c_parser import ParseError
    except ImportError:
        return {}

    structure = build_major_question_structure({"stem": stem}, major_order=0)
    context = structure.shared_stem.replace("$", "")
    parts = [(p.label, p.stem) for p in structure.subparts]
    if not parts:
        code = re.fullmatch(r"(.*?)```(?:c|cpp|C)?\s*\n(.*?)\n```\s*", stem, re.S)
        if not code or not _COUNTS.search(code[1]) or "```" in code[2]:
            return {}
        context = code[1].replace("$", "")
        parts = [("question", code[2])]
    if len(parts) > 24:
        return {}
    results = {}
    parser = CParser()
    for label, body in parts:
        source = re.sub(r"//[^\n]*|/\*.*?\*/", "", body, flags=re.S)
        # A faithful scan may put math delimiters around individual C tokens.
        # Strip only paired presentation markup; preserve comparisons and order.
        source = re.sub(r"\$([^$\n]+)\$", r"\1", source)
        source = re.sub(r"\\le(?:q)?(?![a-zA-Z])", "<=", source)
        source = re.sub(r"\\ge(?:q)?(?![a-zA-Z])", ">=", source)
        source = source.replace("≤", "<=").replace("≥", ">=").replace("−", "-")
        source = re.sub(r"```(?:c(?:pp)?|C)?\s*|[◆◇]", "", source).strip()
        if len(source) > 3000 or source.count("@") != 1 or "smartai_count" in source:
            continue
        if (
            len(re.findall(r"\w+|\S", source)) > 360
            or max(source.count(c) for c in "({") > 32
        ):
            continue
        if not re.fullmatch(r"[\w\s=+\-*/<>(){},;!@]+", source, re.ASCII):
            continue
        try:
            block = (
                parser.parse(
                    "void f(void) {" + source.replace("@", "smartai_count: ") + "}"
                )
                .ext[0]
                .body
            )
            depends = bool(re.search(r"\bn\b", source))
            # Only claim concrete positive-integer samples when the source supplies
            # that domain. An additional restriction may exclude the boundary.
            if depends and (
                not re.search(r"正整数|positive integer", context, re.I)
                or re.search(
                    r"[<>≤≥=]|至少|大于|小于|偶数|奇数|素数|倍数|满足|整除|但|except|even|odd|prime|at least|greater than|less than",
                    context,
                    re.I,
                )
            ):
                continue
            inputs = [1, 2, 4, 5] if depends else [1]
            samples = {}
            for n in inputs:
                trace = _Trace(n)
                trace.visit(block)
                samples[str(n)] = trace.count
            results[label] = {"samples": samples} if depends else {"exact": trace.count}
            if depends:
                proven = _closed_count(block)
                if proven:
                    results[label]["formula"] = proven
        except (Unsupported, ParseError, RecursionError, ValueError):
            continue
    return results


def _value(node, n):
    if (
        isinstance(node, ast.Constant)
        and type(node.value) is int
        and abs(node.value) < 10**8
    ):
        return Fraction(node.value)
    if isinstance(node, ast.Name) and node.id == "n":
        return Fraction(n)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_value(node.operand, n)
    if isinstance(node, ast.BinOp):
        a, b = _value(node.left, n), _value(node.right, n)
        if isinstance(node.op, ast.Pow) and b.denominator == 1 and abs(b) <= 8:
            value = a ** int(b)
        else:
            ops = {
                ast.Add: operator.add,
                ast.Sub: operator.sub,
                ast.Mult: operator.mul,
                ast.Div: operator.truediv,
            }
            if type(node.op) not in ops:
                raise Unsupported()
            value = ops[type(node.op)](a, b)
        if max(value.numerator.bit_length(), value.denominator.bit_length()) > 128:
            raise Unsupported()
        return value
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and not node.keywords
    ):
        args = [_value(arg, n) for arg in node.args]
        if node.func.id in {"max", "min"} and 1 <= len(args) <= 4:
            return (max if node.func.id == "max" else min)(args)
        if (
            node.func.id == "isqrt"
            and len(args) == 1
            and args[0].denominator == 1
            and args[0] >= 0
        ):
            return Fraction(isqrt(int(args[0])))
    raise Unsupported()


def evaluate_expression(text, n):
    if not isinstance(text, str) or len(text) > 160:
        return None
    text = (
        text.strip().strip("$").replace("−", "-").replace("×", "*").replace("^", "**")
    )
    text = re.sub(r"(?<=[0-9n)])(?=\()", "*", text)
    try:
        expr = ast.parse(text.strip(), mode="eval")
        if sum(1 for _ in ast.walk(expr)) <= 48:
            return _value(expr.body, n)
    except (Unsupported, SyntaxError, ValueError, ArithmeticError, RecursionError):
        pass
    return None


def counterexample(formula, witness):
    samples = witness.get("samples", {"1": witness.get("exact")})
    for n, expected in samples.items():
        actual = evaluate_expression(formula, int(n))
        if actual is not None and actual != expected:
            return f"n={n}: the marked statement executes {expected} times, but the proposed expression gives {actual}."
    return None


def reference_error(text, witnesses):
    """Only challenge an identifiable final mathematical result of a numbered part."""
    if not witnesses or not text:
        return None
    parts = build_major_question_structure({"stem": text}, major_order=0).subparts
    if not set(witnesses).issubset({part.label for part in parts}):
        return None
    for part in parts:
        witness = witnesses.get(part.label)
        if not witness:
            continue
        # Do not treat intermediate constants, an equation condition, or a
        # count at an example input as the final general expression.
        match = re.search(
            r"(?:频度为|执行次数为|总次数为|final count\s*[:=]|frequency is)\s*[:：]?\s*\$?([^\n$。；;]+)\$?\s*[次。.]?\s*$",
            part.stem,
            re.I,
        )
        formula = match[1] if match else part.stem.strip().strip("。；;")
        issue = counterexample(formula, witness)
        if issue:
            return f"{part.label} {issue}"
    return None


def submitted_count_checks(text, witnesses):
    """A single active expression can refute a general count, or confirm a constant count."""
    if not witnesses:
        return {}
    parts = build_major_question_structure({"stem": text}, major_order=0).subparts
    results = {}
    for part in parts:
        witness = witnesses.get(part.label)
        if not witness:
            continue
        expr = re.sub(
            r"\[crossed[- ]out\s*:\s*[^\[\]]{1,180}\]", "", part.stem, flags=re.I
        )
        expr = expr.strip().strip("$。；;,， ")
        array = re.fullmatch(r"\\begin\{array\}\{[lcr]\}(.+)\\end\{array\}", expr, re.S)
        if array:
            expr = array[1].strip().strip("\\").strip()
        expr = expr.replace(r"\times", "*").replace(r"\cdot", "*")
        terms = [t.strip() for t in expr.split("=")]
        if any(not term for term in terms):
            continue
        if len(terms) > 1:
            # A false intermediate equality must not earn verified credit.
            if any("n" in term for term in terms):
                continue
            values = [evaluate_expression(term, 1) for term in terms]
            if None in values or len(set(values)) != 1:
                continue
        term = terms[-1]
        cleaned = re.sub(r"\s+|\\(?:left|right)", "", term)
        if re.fullmatch(
            r"(?:\\lfloor\\sqrt\{n\}\\rfloor|\[\\sqrt\{n\}\]|\[√n\])", cleaned
        ):
            cleaned = "isqrt(n)"
        if witness.get("formula") == cleaned:
            results[part.label] = {"proven_formula": cleaned}
            continue
        bad = counterexample(term, witness)
        if bad:
            results[part.label] = {"counterexample": bad}
        elif (
            "exact" in witness
            and "n" not in term
            and evaluate_expression(term, 1) == witness["exact"]
        ):
            results[part.label] = {"confirmed_exact_count": witness["exact"]}
    return results


def require_consistent_count_grades(checks, plan, draft, answer, problem):
    # Only an unambiguously result-only, single award may follow directly
    # from this evidence. A method, teacher rubric or partial-credit ledger
    # may independently entitle a student to points for their working.
    rubric = getattr(plan, "part_rubric", None)
    if (
        not checks
        or not rubric
        or rubric.teacher_rubric
        or re.search(
            r"证明|列出过程|写出步骤|说明理由|justify|show.*work", problem, re.I
        )
    ):
        return
    by_id = {d.part_id: d for d in getattr(draft, "part_decisions", ())}
    for part in rubric.parts:
        fact = checks.get(part.label)
        d = by_id.get(part.id)
        if not fact or not d or len(part.steps) != 1 or d.method == "alternative":
            continue
        if re.search(
            r"必须|仅当|同时|证明|过程完整|且.*(?:推导|解释)|must|justify",
            part.steps[0].criterion,
            re.I,
        ):
            continue
        if "counterexample" in fact and d.relevance == "complete":
            raise ValueError(
                f"{part.label} ({part.id}): {fact['counterexample']} The submitted final count cannot be complete."
            )
        if ("confirmed_exact_count" in fact or "proven_formula" in fact) and (
            d.relevance != "complete"
            or any(step.outcome != "earned" for step in d.steps)
        ):
            raise ValueError(
                f"{part.label} ({part.id}): both the source trace and the submitted expression give {fact.get('confirmed_exact_count',fact.get('proven_formula'))}. Award this result-only step."
            )
