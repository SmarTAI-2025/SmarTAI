"""Bounded mathematical evidence, never a scoring policy.

Expressions use a small arithmetic grammar, not Python or SymPy source code.
``antiderivative`` compares two primitives modulo an additive constant; it does
not integrate the reference. ``domain`` accepts real/complex or conjunctions
of scalar comparisons (e.g. ``x > 0 and x < pi``). Unsupported input is unknown.
Every supported result is conditional on the caller's supplied assumptions;
this tool cannot certify that those assumptions came from the original problem.
"""
from __future__ import annotations

import ast
import asyncio
import math
import os
from pathlib import Path
import re
import sys
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictBool

MAX_EXPRESSION_LENGTH = 2048
MAX_AST_NODES = 256
MAX_AST_DEPTH = 32
MAX_SYMBOLS = 8
MAX_POWER = 128
WALL_SECONDS = 5.0
CPU_SECONDS = 4
MAX_OUTPUT_BYTES = 32768
_CONSTANT_NAMES = {"pi", "E", "e", "I", "i"}
_INTEGRATION_CONSTANTS = {"C", "c", "C1", "C2", "c1", "c2"}
_ASSUMPTIONS = {
    "real", "complex", "positive", "negative", "nonnegative", "nonpositive",
    "nonzero", "integer", "rational", "finite",
}
_FUNCTIONS = {"sin", "cos", "tan", "exp", "log", "sqrt", "Abs", "abs",
              "sinh", "cosh", "conjugate", "re", "im",
              "Integral", "integrate", "Derivative", "diff", "Eq"}
_AssumptionName = Annotated[str, Field(min_length=1, max_length=16)]
_AssumptionFlags = Annotated[dict[_AssumptionName, StrictBool], Field(max_length=len(_ASSUMPTIONS))]


class MathVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)

    student_expression: str = Field(min_length=1, max_length=MAX_EXPRESSION_LENGTH)
    reference_expression: str = Field(min_length=1, max_length=MAX_EXPRESSION_LENGTH)
    comparison: Literal["equivalence", "numeric", "antiderivative"] = "equivalence"
    variable: str | None = Field(default=None, max_length=32)
    assumptions: dict[_AssumptionName, _AssumptionFlags] = Field(default_factory=dict, max_length=MAX_SYMBOLS)
    domain: str | None = Field(default=None, max_length=512)
    allow_approximation: bool = False
    absolute_tolerance: float | None = Field(default=None, ge=0)
    relative_tolerance: float | None = Field(default=None, ge=0)
    student_unit: str | None = Field(default=None, max_length=128)
    reference_unit: str | None = Field(default=None, max_length=128)


class MathVerificationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    status: Literal["supported", "refuted", "unknown"]
    method: str
    reason: str
    conditions: list[str] = Field(default_factory=list)
    counterexample: str | None = None
    details: dict[str, JsonValue] = Field(default_factory=dict)


def _unknown(reason: str, *, method: str = "unavailable") -> MathVerificationEvidence:
    return MathVerificationEvidence(status="unknown", method=method, reason=reason)


async def verify_math(request: MathVerificationRequest) -> MathVerificationEvidence:
    """Run a fixed worker with a hard wall deadline, killing it on cancellation.

    No shell, eval, sympify(string), generated program, or model is involved.
    The existing sandbox concurrency gate is shared; programming sandbox policy
    is unchanged. Worker environment excludes application credentials.
    """
    from backend.tools.sandbox_runtime import get_sandbox_semaphore

    try:
        # Revalidate even model_construct / mutated nested dictionaries.
        request = MathVerificationRequest.model_validate(request.model_dump())
    except Exception:
        return _unknown("Invalid mathematical verification request.", method="input")
    process = None
    try:
        async with asyncio.timeout(WALL_SECONDS):
            async with get_sandbox_semaphore():
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "-I", str(Path(__file__).resolve()), "--worker",
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    env={"LANG": "C.UTF-8", "PYTHONHASHSEED": "0"},
                    cwd=os.path.dirname(os.path.abspath(__file__)),
                )
                output, _ = await process.communicate(request.model_dump_json().encode())
                if process.returncode != 0 or len(output) > MAX_OUTPUT_BYTES:
                    return _unknown("Worker failed or exceeded its resource limit.", method="resource_limit")
                return MathVerificationEvidence.model_validate_json(output)
    except TimeoutError:
        return _unknown("Mathematical verification exceeded its time limit.", method="timeout")
    except asyncio.CancelledError:
        raise
    except Exception:
        return _unknown("Mathematical verification could not be completed.")
    finally:
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()


class _Unsupported(ValueError):
    pass


def _tree(source: str) -> ast.Expression:
    # ^ is mathematical exponentiation here, not Python's bitwise operator.
    source = source.strip().replace("^", "**")
    if not source or len(source) > MAX_EXPRESSION_LENGTH:
        raise _Unsupported("Expression length limit exceeded.")
    try:
        tree = ast.parse(source, mode="eval")
    except (SyntaxError, ValueError, RecursionError):
        raise _Unsupported("Expression is outside the supported arithmetic grammar.") from None
    if sum(1 for _ in ast.walk(tree)) > MAX_AST_NODES:
        raise _Unsupported("Expression complexity limit exceeded.")
    pending = [(tree, 0)]
    while pending:
        node, depth = pending.pop()
        if depth > MAX_AST_DEPTH:
            raise _Unsupported("Expression nesting limit exceeded.")
        pending.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
    tree.source = source
    return tree


def _valid_name(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,15}", name))


class _Parser:
    def __init__(self, sp, symbols, *, real: bool = False):
        self.sp, self.symbols, self.real = sp, symbols, real
        self.guards = []
        self.used_symbols = set()
        self.source = ""

    def parse(self, text):
        tree = _tree(text)
        self.source = tree.source
        return self.node(tree.body)

    def node(self, node):
        sp = self.sp
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            token = ast.get_source_segment(self.source, node)
            if token is None or not re.fullmatch(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", token):
                raise _Unsupported("Only decimal integer and rational literals are supported.")
            if len(token) > 100 or (isinstance(node.value, float) and not math.isfinite(node.value)):
                raise _Unsupported("Numeric literal limit exceeded.")
            # Exact decimal rational, avoiding binary float rounding in exact mode.
            value = sp.Rational(token)
            if abs(value.p).bit_length() > 4096 or value.q.bit_length() > 4096:
                raise _Unsupported("Numeric literal magnitude limit exceeded.")
            return value
        if isinstance(node, ast.Name):
            constants = {"pi": sp.pi, "e": sp.E, "E": sp.E, "i": sp.I, "I": sp.I}
            if node.id in constants:
                return constants[node.id]
            if node.id not in self.symbols:
                raise _Unsupported("Unsupported symbol.")
            self.used_symbols.add(self.symbols[node.id])
            return self.symbols[node.id]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = self.node(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp):
            left, right = self.node(node.left), self.node(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                self.guards.append(sp.Ne(right, 0))
                return left / right
            if isinstance(node.op, ast.Pow):
                if not right.is_Rational or abs(right) > MAX_POWER:
                    raise _Unsupported("Only bounded rational powers are supported.")
                if right == 0 and left.is_zero is not False:
                    raise _Unsupported("Zero to the zeroth power needs a convention not supplied by this request.")
                if left.is_Rational and max(abs(left.p).bit_length(), left.q.bit_length()) * max(1, abs(right)) > 4096:
                    raise _Unsupported("Numeric power magnitude limit exceeded.")
                if right < 0:
                    self.guards.append(sp.Ne(left, 0))
                if right.q != 1 and (self.real or (left.free_symbols and left.is_real is True)):
                    self.guards.append(sp.Ge(left, 0))
                return left ** right
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            name = node.func.id
            if name in {"Integral", "integrate", "Derivative", "diff"}:
                return self.calculus(node, name)
            if name not in _FUNCTIONS or len(node.args) != 1 or node.keywords:
                raise _Unsupported("Unsupported mathematical function or arguments.")
            arg = self.node(node.args[0])
            if name == "sqrt":
                if self.real or (arg.free_symbols and arg.is_real is True):
                    self.guards.append(sp.Ge(arg, 0))
                return sp.sqrt(arg)
            if name == "log":
                self.guards.append(sp.Gt(arg, 0) if self.real or (arg.free_symbols and arg.is_real is True) else sp.Ne(arg, 0))
            if name == "tan":
                self.guards.append(sp.Ne(sp.cos(arg), 0))
            return getattr(sp, "Abs" if name == "abs" else name)(arg)
        raise _Unsupported("Expression is outside the supported arithmetic grammar.")

    def calculus(self, node, name):
        sp = self.sp
        if node.keywords or len(node.args) != 2:
            raise _Unsupported("Calculus expressions need exactly two positional arguments.")
        if name in {"Derivative", "diff"}:
            var_node = node.args[1]
            if not isinstance(var_node, ast.Name) or var_node.id not in self.symbols:
                raise _Unsupported("Differentiation requires an explicit variable.")
            previous_symbols = set(self.used_symbols)
            guard_start = len(self.guards)
            expression = self.node(node.args[0])
            # Only polynomials and entire functions here. More complex
            # derivatives need branch/singularity analysis, not silent .doit().
            if not _entire_expression(sp, expression) or any(g is not sp.true for g in self.guards[guard_start:]):
                raise _Unsupported("Derivative expressions currently require polynomials or entire sin/cos/exp/sinh/cosh expressions.")
            result = sp.diff(expression, self.symbols[var_node.id])
            self.used_symbols = previous_symbols | result.free_symbols
            return result
        bounds = node.args[1]
        if not isinstance(bounds, ast.Tuple) or len(bounds.elts) != 3 or not isinstance(bounds.elts[0], ast.Name):
            raise _Unsupported("Definite integrals require (variable, lower, upper).")
        var_name = bounds.elts[0].id
        if var_name not in self.symbols:
            raise _Unsupported("Unsupported integration variable.")
        variable = self.symbols[var_name]
        if variable.is_real is False or any(
            getattr(variable, f"is_{flag}") is not None
            for flag in ("positive", "negative", "nonnegative", "nonpositive", "nonzero", "integer", "rational")
        ):
            raise _Unsupported("Bound integration variables must not carry restricted assumptions.")
        previous_symbols = set(self.used_symbols)
        guard_start = len(self.guards)
        integrand = self.node(node.args[0])
        lower, upper = (self.node(n) for n in bounds.elts[1:])
        if any(v.free_symbols or v.is_real is not True or not _finite(sp, v) for v in (lower, upper)):
            raise _Unsupported("Definite integral bounds must be finite real constants.")
        if (integrand.free_symbols - {variable} or not _entire_expression(sp, integrand)
                or any(g is not sp.true for g in self.guards[guard_start:])):
            raise _Unsupported("Definite integrals currently require single-variable entire integrands without parameters.")
        # Integrating only entire expressions on a finite real interval avoids
        # treating principal values or endpoint singularities as ordinary integrals.
        result = sp.integrate(integrand, (variable, lower, upper))
        if result.has(sp.Integral) or not _finite(sp, result):
            raise _Unsupported("The definite integral did not resolve to a finite constant.")
        self.used_symbols = previous_symbols | result.free_symbols
        return result


def _entire_expression(sp, expression):
    if any(p.exp.is_integer is not True or p.exp < 0 for p in expression.atoms(sp.Pow)):
        return False
    return all(f.func in {sp.sin, sp.cos, sp.exp, sp.sinh, sp.cosh}
               for f in expression.atoms(sp.Function))


def _make_context(sp, req):
    domain = (req.domain or "").strip()
    real = domain.lower() in {"real", "reals", "r"}
    complex_domain = domain.lower() in {"complex", "complexes", "c"}
    constraints_tree = None if not domain or real or complex_domain else _tree(domain)
    names = set()
    for text in (req.student_expression, req.reference_expression):
        names |= {n.id for n in ast.walk(_tree(text)) if isinstance(n, ast.Name)}
    domain_names = set()
    if constraints_tree is not None:
        domain_names = {n.id for n in ast.walk(constraints_tree) if isinstance(n, ast.Name)}
        names |= domain_names
    names -= _FUNCTIONS | _CONSTANT_NAMES
    domain_names -= _FUNCTIONS | _CONSTANT_NAMES
    if req.variable:
        names.add(req.variable)
    names |= set(req.assumptions)
    if len(names) > MAX_SYMBOLS or any(not _valid_name(n) for n in names):
        raise _Unsupported("Symbol count or name limit exceeded.")
    if names & {"nan", "NaN", "oo", "inf", "Infinity", "zoo"}:
        raise _Unsupported("Non-finite constants are not supported.")
    ordered = constraints_tree is not None and any(
        isinstance(n, (ast.Lt, ast.LtE, ast.Gt, ast.GtE)) for n in ast.walk(constraints_tree)
    )
    symbols = {}
    for name in sorted(names):
        flags = dict(req.assumptions.get(name, {}))
        if set(flags) - _ASSUMPTIONS:
            raise _Unsupported("Unsupported variable assumption.")
        if flags.get("finite") is False or flags.get("complex") is False:
            raise _Unsupported("Only finite real or complex variables are supported.")
        if name in _CONSTANT_NAMES:
            raise _Unsupported("Named mathematical constants cannot be rebound.")
        if real or (ordered and name in domain_names):
            if flags.get("real") is False:
                raise _Unsupported("Conflicting real-domain assumptions.")
            flags["real"] = True
        flags.setdefault("complex", True)
        symbols[name] = sp.Symbol(name, **flags)
    parser = _Parser(sp, symbols, real=real)
    constraints = []
    if constraints_tree:
        parser.source = constraints_tree.source

        def relation(node):
            if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
                return [c for item in node.values for c in relation(item)]
            if not isinstance(node, ast.Compare):
                raise _Unsupported("Domain must be scalar comparisons joined by 'and'.")
            values = [parser.node(n) for n in [node.left, *node.comparators]]
            operators = {ast.Eq: sp.Eq, ast.NotEq: sp.Ne, ast.Lt: sp.Lt,
                         ast.LtE: sp.Le, ast.Gt: sp.Gt, ast.GtE: sp.Ge}
            if any(type(op) not in operators for op in node.ops):
                raise _Unsupported("Unsupported domain comparison.")
            return [operators[type(op)](values[i], values[i + 1]) for i, op in enumerate(node.ops)]
        constraints = relation(constraints_tree.body)
        constraints.extend(parser.guards)
        if len(set().union(*(c.free_symbols for c in constraints))) > 1:
            raise _Unsupported("Explicit domain comparisons currently support one variable.")
    # Convert supported assumptions to predicates for domain reasoning.
    for symbol in symbols.values():
        for flag, predicate in (
            ("positive", sp.Gt), ("nonnegative", sp.Ge), ("negative", sp.Lt),
            ("nonpositive", sp.Le), ("nonzero", sp.Ne),
        ):
            if getattr(symbol, f"is_{flag}") is True:
                constraints.append(predicate(symbol, 0, evaluate=False))
    return symbols, constraints, real


def _truth(sp, proposition):
    if proposition is True or proposition is sp.true:
        return True
    if proposition is False or proposition is sp.false:
        return False
    return None


def _implied(sp, guard, constraints, symbols):
    truth = _truth(sp, guard)
    if truth is not None:
        return truth
    if guard in constraints:
        return True
    normalized = sp.simplify(guard)
    if any(normalized == sp.simplify(c) for c in constraints):
        return True
    # Only certified univariate real inequalities are handled. No heuristic
    # satisfiability or finite sampling is used to prove a domain implication.
    free = set().union(*(c.free_symbols for c in [*constraints, guard]))
    if len(free) == 1:
        symbol = next(iter(free))
        if symbol.is_real is True:
            try:
                impossible = sp.reduce_inequalities([*constraints, sp.Not(guard)], symbol)
                if impossible is sp.false:
                    return True
            except (NotImplementedError, ValueError, TypeError):
                pass
    return None


def _unit(sp, text):
    # Multiplicative SI subset; affine units (Celsius/Fahrenheit) deliberately
    # unsupported. Exponents/dimensions are evaluated by a separate tiny AST.
    units = {
        "m": (1, (1, 0, 0, 0, 0)), "cm": (sp.Rational(1, 100), (1, 0, 0, 0, 0)),
        "mm": (sp.Rational(1, 1000), (1, 0, 0, 0, 0)), "km": (1000, (1, 0, 0, 0, 0)),
        "s": (1, (0, 0, 1, 0, 0)), "ms": (sp.Rational(1, 1000), (0, 0, 1, 0, 0)),
        "min": (60, (0, 0, 1, 0, 0)), "h": (3600, (0, 0, 1, 0, 0)),
        "kg": (1, (0, 1, 0, 0, 0)), "g": (sp.Rational(1, 1000), (0, 1, 0, 0, 0)),
        "A": (1, (0, 0, 0, 1, 0)), "K": (1, (0, 0, 0, 0, 1)),
        "N": (1, (1, 1, -2, 0, 0)), "J": (1, (2, 1, -2, 0, 0)),
        "W": (1, (2, 1, -3, 0, 0)), "Pa": (1, (-1, 1, -2, 0, 0)),
        "Hz": (1, (0, 0, -1, 0, 0)),
    }
    zero = (0, 0, 0, 0, 0)

    def walk(node):
        if isinstance(node, ast.Name) and node.id in units:
            scale, dim = units[node.id]
            return sp.Rational(scale), dim
        if isinstance(node, ast.Constant) and type(node.value) is int and node.value == 1:
            return sp.S.One, zero
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mult, ast.Div)):
            a, da = walk(node.left)
            b, db = walk(node.right)
            sign = 1 if isinstance(node.op, ast.Mult) else -1
            return a * b ** sign, tuple(x + sign * y for x, y in zip(da, db))
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            exponent = node.right
            sign = 1
            if isinstance(exponent, ast.UnaryOp) and isinstance(exponent.op, ast.USub):
                exponent, sign = exponent.operand, -1
            if not isinstance(exponent, ast.Constant) or type(exponent.value) is not int or abs(exponent.value) > 12:
                raise _Unsupported("Unit exponent is unsupported.")
            power = sign * exponent.value
            scale, dim = walk(node.left)
            return scale ** power, tuple(x * power for x in dim)
        raise _Unsupported("Unit is outside the supported multiplicative SI subset.")
    return walk(_tree(text).body)


def _finite(sp, value):
    return not value.has(sp.zoo, sp.oo, -sp.oo, sp.nan) and value.is_finite is True


def _admissible(sp, assignment, symbols, constraints):
    for name, value in assignment.items():
        symbol = symbols[name]
        for flag in _ASSUMPTIONS:
            required = getattr(symbol, f"is_{flag}")
            if required is not None and getattr(value, f"is_{flag}") is not required:
                return False
    subs = {symbols[name]: value for name, value in assignment.items()}
    return all(_truth(sp, c.subs(subs)) is True for c in constraints)


def _counterexample(sp, left, right, left_guards, right_guards, symbols, constraints):
    from itertools import product

    # Exact rational/complex witnesses only, with all assumptions checked.
    values = [sp.S.Zero, sp.S.One, -sp.S.One, sp.Integer(2), sp.Integer(-2),
              sp.Rational(1, 2), sp.Integer(3), sp.I, -sp.I]
    active = sorted(set().union(
        left.free_symbols, right.free_symbols,
        *(g.free_symbols for g in [*left_guards, *right_guards, *constraints]),
    ), key=str)
    if len(active) > 3:
        return None
    for index, choices in enumerate(product(values, repeat=len(active))):
        if index >= 160:
            break
        assignment = {s.name: v for s, v in zip(active, choices)}
        if not _admissible(sp, assignment, symbols, constraints):
            continue
        substitutions = dict(zip(active, choices))
        lg = [_truth(sp, g.subs(substitutions)) for g in left_guards]
        rg = [_truth(sp, g.subs(substitutions)) for g in right_guards]
        lv, rv = left.subs(substitutions), right.subs(substitutions)
        left_defined = all(t is True for t in lg) and _finite(sp, lv)
        right_defined = all(t is True for t in rg) and _finite(sp, rv)
        left_undefined = False in lg or lv.has(sp.zoo, sp.oo, -sp.oo, sp.nan)
        right_undefined = False in rg or rv.has(sp.zoo, sp.oo, -sp.oo, sp.nan)
        # A domain mismatch is a witness within the requested domain where
        # precisely one expression is defined; report that explicitly.
        if (left_defined and right_undefined) or (right_defined and left_undefined):
            return assignment, "domain", lv if left_defined else None, rv if right_defined else None
        if left_defined and right_defined:
            difference = sp.simplify(lv - rv)
            if difference.is_zero is False:
                return assignment, "value", lv, rv
    return None


def _equation_sides(tree):
    node = tree.body
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq):
        return node.left, node.comparators[0]
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "Eq" and len(node.args) == 2 and not node.keywords):
        return node.args
    return None


def _verify_equations(sp, req, symbols, constraints, real, trees):
    """Exact finite solution sets for univariate polynomial/rational equations."""
    conditions = ["Compares equation solution sets on the supplied domain, not expression values.",
                  "The caller must check these conditions against the original problem."]
    if req.domain:
        conditions.append("Requested domain: " + req.domain)
    conditions.extend(f"{name}: {key}={value}" for name, flags in req.assumptions.items() for key, value in flags.items())
    details = {"comparison": "equivalence", "relation_kind": "equations"}

    def result(status, reason, witness=None):
        return MathVerificationEvidence(status=status, method="solution_sets", reason=reason,
                                        conditions=conditions, counterexample=witness, details=details)

    if req.comparison != "equivalence" or req.allow_approximation or req.student_unit or req.reference_unit:
        return result("unknown", "Equation solution sets require exact equivalence without quantity units.")
    parsers, expressions = [], []
    for tree in trees:
        sides = _equation_sides(tree)
        if not sides:
            return result("unknown", "Both sides must be explicitly written equations.")
        parser = _Parser(sp, symbols, real=real)
        parser.source = tree.source
        left, right = (parser.node(n) for n in sides)
        parsers.append(parser)
        expressions.append(left - right)
    active = set().union(*(p.used_symbols for p in parsers))
    if len(active) != 1 or any(c.free_symbols - active for c in constraints):
        return result("unknown", "Only univariate equations without additional free parameters are supported.")
    variable = next(iter(active))
    if sp.simplify(sp.And(*constraints)) is sp.false:
        return result("unknown", "The supplied domain is empty or inconsistent.")
    if variable.is_real is True:
        try:
            if sp.reduce_inequalities(constraints, variable) is sp.false:
                return result("unknown", "The supplied domain is empty or inconsistent.")
        except (NotImplementedError, ValueError, TypeError):
            pass
    if req.variable is not None and variable.name != req.variable:
        return result("unknown", "The requested variable does not match the equations.")
    solution_sets = []
    for expression, parser in zip(expressions, parsers):
        if not expression.is_rational_function(variable):
            return result("unknown", "Only polynomial or rational equations are supported.")
        numerator, _ = sp.fraction(sp.cancel(expression))
        polynomial = sp.Poly(numerator, variable)
        if polynomial.is_zero or polynomial.degree() > 4:
            return result("unknown", "Only finite solution sets of degree at most four are supported.")
        roots = sp.solveset(numerator, variable, domain=sp.S.Complexes)
        if not isinstance(roots, sp.FiniteSet) and roots is not sp.S.EmptySet:
            return result("unknown", "A complete finite root set could not be established.")
        accepted = []
        for root in roots:
            checks = []
            for flag in _ASSUMPTIONS:
                required = getattr(variable, f"is_{flag}")
                if required is not None:
                    actual = getattr(root, f"is_{flag}")
                    checks.append(None if actual is None else actual == required)
            checks.extend(_truth(sp, sp.simplify(c.subs(variable, root))) for c in [*constraints, *parser.guards])
            if False in checks:
                continue
            if None in checks:
                return result("unknown", "A candidate root's assumptions or original domain could not be checked.")
            accepted.append(root)
        solution_sets.append(sp.FiniteSet(*accepted))
    details["student_solutions"], details["reference_solutions"] = (
        [str(r) for r in sorted(roots, key=str)] for roots in solution_sets
    )
    details["student_domain_requirements"], details["reference_domain_requirements"] = (
        [str(g) for g in parser.guards] for parser in parsers
    )
    if solution_sets[0] == solution_sets[1]:
        return result("supported", "The complete finite solution sets agree under the supplied conditions.")
    for side in (0, 1):
        for root in solution_sets[side]:
            if all(sp.simplify(root - other).is_zero is False for other in solution_sets[1 - side]):
                details["witness_kind"] = "extra_solution" if side == 0 else "lost_solution"
                details["assignment"] = {variable.name: str(root)}
                return result("refuted", "A checked root belongs to only one equation's solution set.",
                              f"{variable.name}={root}")
    return result("unknown", "The candidate root sets could not be compared exactly.")


def _verify_sync(req):
    import sympy as sp

    symbols, constraints, real = _make_context(sp, req)
    trees = [_tree(text) for text in (req.student_expression, req.reference_expression)]
    if any(_equation_sides(tree) for tree in trees):
        return _verify_equations(sp, req, symbols, constraints, real, trees)
    parsers = [_Parser(sp, symbols, real=real), _Parser(sp, symbols, real=real)]
    left, right = [p.parse(t) for p, t in zip(
        parsers, (req.student_expression, req.reference_expression)
    )]
    guards = [p.guards for p in parsers]
    conditions = [
        "Evidence applies only to this requested relation and supplied conditions.",
        "The caller must check the conditions against the original problem.",
        "Unspecified variables are finite complex numbers; ordered domains constrain their variable to real numbers.",
    ]
    if req.domain:
        conditions.append("Requested domain: " + req.domain)
    conditions.extend(f"{name}: {key}={value}" for name, flags in req.assumptions.items() for key, value in flags.items())
    details = {
        "comparison": req.comparison,
        "student_domain_requirements": [str(g) for g in guards[0]],
        "reference_domain_requirements": [str(g) for g in guards[1]],
    }

    def result(status, method, reason, counterexample=None):
        return MathVerificationEvidence(
            status=status, method=method, reason=reason, conditions=conditions,
            details=details, counterexample=counterexample,
        )

    if any(_truth(sp, c) is False for c in constraints):
        return result("unknown", "domain", "The supplied domain is empty or inconsistent.")
    joint_domain = sp.simplify(sp.And(*constraints, *guards[0], *guards[1]))
    if joint_domain is sp.false:
        return result("unknown", "domain", "The expressions have no common defined values under the supplied conditions.")
    domain_free = set().union(*(c.free_symbols for c in constraints))
    if len(domain_free) == 1:
        symbol = next(iter(domain_free))
        if symbol.is_real:
            try:
                if sp.reduce_inequalities(constraints, symbol) is sp.false:
                    return result("unknown", "domain", "The supplied domain is empty or inconsistent.")
            except (NotImplementedError, ValueError, TypeError):
                pass
    if bool(req.student_unit) != bool(req.reference_unit):
        return result("unknown", "units", "A unit is missing on one side; its meaning cannot be inferred.")
    if req.student_unit and req.reference_unit:
        a, da = _unit(sp, req.student_unit)
        b, db = _unit(sp, req.reference_unit)
        details["student_unit"] = req.student_unit
        details["reference_unit"] = req.reference_unit
        if da != db:
            return result("refuted", "units", "The supplied units have different physical dimensions.")
        left = (a / b) * left
        conditions.append("Student magnitude converted to the reference unit; absolute tolerance is in that reference unit.")
    else:
        conditions.append("No physical units were asserted or checked.")

    free = left.free_symbols | right.free_symbols
    # Do not erase a syntactically present unbound variable by cancellation.
    if req.comparison == "numeric" and any(p.used_symbols for p in parsers):
        return result("unknown", "numeric", "Unbound free variables cannot be treated as numeric constants.")
    if req.comparison == "antiderivative" and not free:
        return result("unknown", "derivative", "An active integration variable is required to compare primitives.")
    if not free and not any(g.free_symbols for side in guards for g in side):
        if any(_truth(sp, g) is not True for side in guards for g in side) or not _finite(sp, left) or not _finite(sp, right):
            return result("unknown", "domain", "One or both constant expressions are undefined or non-finite.")
        if real and (left.is_real is not True or right.is_real is not True):
            return result("unknown", "domain", "The constant expressions are not both real on the requested real domain.")
        difference = sp.simplify(left - right)
        if difference == 0:
            return result("supported", "exact", "The finite constant expressions are exactly equal.")
        if req.allow_approximation:
            if req.absolute_tolerance is None and req.relative_tolerance is None:
                return result("unknown", "numeric", "Approximation needs an explicitly supplied tolerance.")
            absolute = sp.Rational(str(req.absolute_tolerance or 0))
            relative = sp.Rational(str(req.relative_tolerance or 0))
            error = sp.Abs(difference)
            threshold = sp.Max(absolute, relative * sp.Abs(right))
            details.update({
                "absolute_error": str(sp.N(error, 30)),
                "allowed_error": str(sp.N(threshold, 30)),
                "tolerance_rule": "abs(student-reference) <= max(atol, rtol*abs(reference))",
            })
            within = _truth(sp, error <= threshold)
            if within is None:
                return result("unknown", "numeric", "The tolerance boundary could not be certified.")
            return result("supported" if within else "refuted", "numeric",
                          "The constants meet the supplied tolerance." if within else "The constants exceed the supplied tolerance.")
        if difference.is_zero is False:
            return result("refuted", "exact", "The constants are not exactly equal; approximation was not permitted.")
        return result("unknown", "symbolic", "Exact equality could not be established.")

    if req.comparison == "antiderivative":
        candidates = {s.name for s in free} - _INTEGRATION_CONSTANTS
        variable = req.variable or (next(iter(candidates)) if len(candidates) == 1 else None)
        if variable not in {s.name for s in free} or variable in _INTEGRATION_CONSTANTS:
            return result("unknown", "derivative", "An unambiguous active integration variable is required.")
        symbol = symbols[variable]
        left_constants = sorted(s.name for s in left.free_symbols if s.name in _INTEGRATION_CONSTANTS)
        right_constants = sorted(s.name for s in right.free_symbols if s.name in _INTEGRATION_CONSTANTS)
        details["integration_constants"] = {"student": left_constants, "reference": right_constants}
        details["variable"] = variable
        conditions.append("Compares two primitives, not a primitive against an integrand.")
        for expression in (left, right):
            for constant in expression.free_symbols:
                if constant.name in _INTEGRATION_CONSTANTS:
                    coefficient = sp.diff(expression, constant)
                    if coefficient.free_symbols or coefficient.is_zero is not False or not _finite(sp, coefficient):
                        return result("unknown", "integration_constant",
                                      "Only unrestricted additive integration constants are supported.")
                    if constant.is_real is False or (constant.is_real is True and coefficient.is_real is not True):
                        return result("unknown", "integration_constant",
                                      "The additive term does not establish an unrestricted constant on the stated number domain.")
                    if any(getattr(constant, f"is_{flag}") is not None for flag in (
                        "positive", "negative", "nonnegative", "nonpositive", "nonzero", "integer", "rational",
                    )):
                        return result("unknown", "integration_constant",
                                      "Restricted integration constants do not establish an arbitrary constant family.")
        # Real differentiability of Abs/sqrt/log can require more than their
        # value domains. Limit this first version to polynomial/rational and
        # elementary analytic functions whose singularities are tracked.
        if left.has(sp.Abs, sp.conjugate, sp.re, sp.im) or right.has(sp.Abs, sp.conjugate, sp.re, sp.im):
            return result("unknown", "derivative", "Differentiability conditions are outside the supported scope.")
        for expr, side in ((left, guards[0]), (right, guards[1])):
            for power in expr.atoms(sp.Pow):
                if power.exp.is_Rational and power.exp.q != 1:
                    side.append(sp.Gt(power.base, 0) if power.base.is_real else sp.Ne(power.base, 0))
        # Unconstrained complex log and fractional powers have branch cuts.
        if symbol.is_real is not True and (left.has(sp.log) or right.has(sp.log) or any(
            p.exp.is_Rational and p.exp.q != 1 for e in (left, right) for p in e.atoms(sp.Pow)
        )):
            return result("unknown", "derivative", "Complex branch differentiability was not established.")
        left, right = sp.diff(left, symbol), sp.diff(right, symbol)
        details["derivative_domain_requirements"] = [[str(g) for g in side] for side in guards]
        if not left_constants and right_constants:
            details["derivatives_equal"] = sp.simplify(left - right) == 0
            return result("unknown", "integration_constant",
                          "The reference includes an arbitrary constant absent from the student expression; derivative equality alone does not verify the family.")
        conditions.append("Equality of derivatives is only modulo constants on each connected domain component.")

    if req.allow_approximation and sp.simplify(left - right) != 0:
        return result("unknown", "numeric", "Uniform approximate comparison of variable expressions is not supported.")
    witness = _counterexample(sp, left, right, *guards, symbols, constraints)
    if witness:
        assignment, kind, lv, rv = witness
        details.update({
            "witness_kind": kind,
            "assignment": {name: str(value) for name, value in assignment.items()},
            "student_value": str(lv) if lv is not None else "undefined",
            "reference_value": str(rv) if rv is not None else "undefined",
        })
        return result("refuted", "counterexample", "A checked assignment in the requested domain refutes the relation.",
                      ", ".join(f"{name}={value}" for name, value in assignment.items()))
    remaining = [
        {g for g in side if _implied(sp, g, constraints, symbols) is not True}
        for side in guards
    ]
    if any(sp.false in side for side in remaining):
        return result("unknown", "domain", "An expression is undefined under the supplied conditions.")
    if remaining[0] != remaining[1]:
        return result("unknown", "domain", "The expressions' domains could not be shown to agree.")
    for g in sorted(remaining[0], key=str):
        conditions.append("Both expressions require: " + str(g))
    difference = sp.simplify(left - right)
    if difference != 0 and constraints:
        difference = sp.refine(difference, sp.And(*constraints))
    if difference == 0:
        return result("supported", "derivative" if req.comparison == "antiderivative" else "symbolic",
                      "Symbolic reduction establishes this relation on the stated domain.")
    return result("unknown", "symbolic",
                  "No symbolic identity or valid counterexample was established; finite matching samples are not a proof.")


def _worker_main():
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS + 1))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
        if sys.platform.startswith("linux"):
            resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2)
        payload = sys.stdin.buffer.read(16385)
        if len(payload) > 16384:
            raise _Unsupported("Request byte limit exceeded.")
        req = MathVerificationRequest.model_validate_json(payload)
        evidence = _verify_sync(req)
    except _Unsupported as exc:
        evidence = _unknown(str(exc), method="unsupported")
    except Exception:
        evidence = _unknown("Mathematical verification could not be completed.")
    encoded = evidence.model_dump_json()
    if len(encoded.encode()) > MAX_OUTPUT_BYTES:
        encoded = _unknown("Evidence output limit exceeded.", method="resource_limit").model_dump_json()
    sys.stdout.write(encoded)


if __name__ == "__main__" and sys.argv[1:] == ["--worker"]:
    _worker_main()
