"""
CalculationSkill: grades calculation-type questions (计算题).

Strategy (4-tier fallback ladder):

    1. Reference present  → use teacher's reference_answer directly.
    2. Reference absent   → ask LLM to generate a sympy program, run it in the
                            sandbox, use stdout as the reference value.
    3. Reference resolved → SymPy.verify_equivalent / verify_value compares
                            student's final expression against the reference.
                              matched     → award full marks (LLM only writes a
                                            short comment)
                              mismatched  → LLM does *process-credit only*
                                            (final answer is wrong)
                              sympy_failed/unsuitable
                                          → LLM_ONLY fallback (current behaviour)
    4. Every comment carries a metadata footer telling the teacher exactly
       which path was taken. ("（SymPy 验证：✓ ...）" / "✗ ..." / "未启用 ...")

LLM inference is therefore restricted to:
  - writing a short message when sympy says ✓
  - writing process-credit feedback when sympy says ✗
  - writing the entire grade when sympy is unavailable / unsuitable

It NEVER does the arithmetic itself when sympy can do it.
"""
from __future__ import annotations

import ast
import logging
import os
import re
from typing import Optional, List, TYPE_CHECKING

from pydantic import BaseModel, Field

from backend.skills.base import (
    GradingSkill,
    authoritative_score_instruction,
    build_system_prompt,
    normalize_expert_result,
    register_skill,
)
from backend.models import ExpertResult, ProblemInfo, StudentAnswerInfo, StepScore, TaskGradingSetup
from backend.llm.providers import BaseProvider
from backend.tools.structured_llm import structured_llm_call
from backend.tools import numerical
from backend.tools.code_interpreter import run_python_subprocess

if TYPE_CHECKING:
    from backend.progress.tracker import ProgressReporter

logger = logging.getLogger(__name__)


# ─── Output schemas ──────────────────────────────────────────────────────────

class CalcGradingOutput(BaseModel):
    score: float = Field(ge=0, allow_inf_nan=False)
    max_score: float = Field(default=10.0, gt=0, allow_inf_nan=False)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    comment: str
    steps: List[dict] = Field(default_factory=list)


class SympyProgramOutput(BaseModel):
    """LLM-generated sympy code that, when executed, prints the reference value to stdout."""
    code: str = Field(
        description="A self-contained Python program. Only `import sympy` and "
                    "`from sympy import ...` are allowed. The program must "
                    "print() the final answer (and ONLY the final answer) to "
                    "stdout. No file I/O, no network, no input(). At most a "
                    "few seconds of computation."
    )


# ─── Prompt template loading ─────────────────────────────────────────────────

def _load_template() -> str:
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "prompts", "calc.txt")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return _DEFAULT_TEMPLATE


_DEFAULT_TEMPLATE = """You are a mathematics teacher grading a calculation problem.

Problem:
{problem}

Student Answer:
{answer}

Correct Answer (reference):
{correct_answer}

Verification Result: {verification_status}
Branch: {branch}

Grading Rubric:
{rubric}

Return JSON with: score, max_score, confidence, comment, steps.
"""


# ─── Helpers ─────────────────────────────────────────────────────────────────

# Regex grabs candidates for "the student's final answer" — we look at the very
# last `=` followed by an expression on the same logical line.
_FINAL_EQ_RE = re.compile(r"=\s*([^\s=][^\n=]*?)\s*\.?\s*$", re.MULTILINE)


def _extract_final_expression(text: str) -> Optional[str]:
    """Best-effort extraction of the student's final expression for sympy verify.

    Heuristic order:
      1. Last `= <expr>` on its own line.
      2. Last non-empty line, with leading "answer:" / "答案:" stripped.
      3. None — caller falls back to LLM_ONLY.

    The returned string is then sympified by numerical.verify_equivalent /
    verify_value; if those return None, sympy_status becomes "sympy_failed"
    and the comment metadata reflects that.
    """
    if not text:
        return None

    # Strategy 1: trailing `= expr`
    matches = _FINAL_EQ_RE.findall(text)
    if matches:
        candidate = matches[-1].strip()
        if candidate:
            return candidate

    # Strategy 2: last non-empty line
    for line in reversed([ln.strip() for ln in text.splitlines() if ln.strip()]):
        # Strip common Chinese / English answer prefixes
        for prefix in ("答案：", "答案:", "Answer:", "answer:", "结果：", "结果:"):
            if line.startswith(prefix):
                line = line[len(prefix):].strip()
                break
        # Skip lines that are obviously prose (no digits, no operators)
        if any(c.isdigit() for c in line) or any(op in line for op in "+-*/^√()"):
            return line
        # Pure-word line ("我不会") — give up
        return None
    return None


def _parse_integrate_call(
    code: str,
) -> Optional[Tuple[bool, Optional[str]]]:
    """Inspect LLM-generated sympy code for an ``integrate(...)`` call.

    Returns ``(is_indefinite, var)`` where:
      - ``is_indefinite`` is True for a *single-variable* indefinite integral
        ``integrate(f, x)`` (2nd arg is a bare ``Name``).  ``var`` is that
        variable's name.
      - ``is_indefinite`` is False for a *definite* integral
        ``integrate(f, (x, a, b))`` (2nd arg is a ``Tuple``).  ``var`` is None —
        definite integrals must use strict comparison, not derivative compare.
      - Returns ``None`` when no ``integrate(...)`` call is found or the form is
        ambiguous (e.g. multi-variate ``integrate(f, x, y)``, keyword args,
        starred args, or unparseable code).

    Only the *first* ``integrate`` call in the program is considered; a
    well-formed reference program uses integrate once to print the answer.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            fname = func.attr
        elif isinstance(func, ast.Name):
            fname = func.id
        else:
            continue
        if fname != "integrate":
            continue
        # Skip calls with keyword args / star args — form is ambiguous.
        if node.keywords or any(isinstance(a, ast.Starred) for a in node.args):
            return None
        args = node.args
        # integrate(f) or integrate(f, x, y, ...) — not a standard single-var
        # indefinite/definite form we recognise.
        if len(args) != 2:
            return None
        second = args[1]
        if isinstance(second, ast.Name):
            return (True, second.id)            # indefinite: integrate(f, x)
        if isinstance(second, ast.Tuple):
            return (False, None)                # definite: integrate(f, (x, a, b))
        # Any other shape (e.g. integrate(f, x**2)) — ambiguous.
        return None
    return None


async def _generate_sympy_program(
    provider: BaseProvider, problem: ProblemInfo
) -> Optional[str]:
    """Ask the LLM to write a sympy program that prints the reference value.

    Returns None on parse failure; caller marks sympy_status="sympy_failed"
    and falls back to LLM_ONLY scoring.
    """
    system_prompt = (
        "You are an expert at translating mathematics problems into sympy code. "
        "Output a self-contained Python program that, when run, prints ONLY the "
        "final correct answer (no explanation, no labels) to stdout. Use sympy "
        "for symbolic work; you may import only `sympy` and `from sympy import ...`. "
        "Do NOT use input(), file I/O, or network. Keep the program short — it "
        "must finish in under 10 seconds.\n\n"
        "OUTPUT RULES (critical):\n"
        "  - Use plain `print(expr)` to output the answer — a single-line repr.\n"
        "  - Do NOT use `pretty`, `pprint`, `srepr`, `latex`, or `sympy.printing` — "
        "    they produce multi-line / non-sympifiable output that breaks grading.\n"
        "  - Output the RAW result of `integrate(...)` / `solve(...)` / etc.\n"
        "  - Do NOT manually add a constant of integration (+ C) — `integrate` "
        "    already omits it by design; the verifier handles +C.\n"
        "  - Do NOT simplify, expand, or rewrite the result.\n\n"
        "Return JSON {\"code\": \"<the program>\"}."
    )
    user_prompt = (
        f"Problem (type={problem.type}):\n{problem.stem}\n\n"
        f"Rubric:\n{problem.criterion}\n\n"
        "Write the sympy program now."
    )
    try:
        result, _raw = await structured_llm_call(
            provider,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            output_model=SympyProgramOutput,
        )
        return result.code
    except Exception as e:
        logger.warning(
            "_generate_sympy_program failed; exception_type=%s",
            type(e).__name__,
        )
        return None


async def _run_sympy_in_sandbox(code: str, *, timeout: float = 10.0) -> Optional[str]:
    """Execute the LLM-generated sympy program; return stdout on success.

    Uses backend.tools.code_interpreter.run_python_subprocess, which is gated
    by the global sandbox semaphore (limit=8) so this never fork-bombs even
    when many students are graded concurrently.
    """
    try:
        result = await run_python_subprocess(code, "", timeout=timeout)
    except Exception as e:
        logger.warning(
            "_run_sympy_in_sandbox failed; exception_type=%s",
            type(e).__name__,
        )
        return None
    if result.passed and result.actual_output:
        return result.actual_output.strip()
    if result.error:
        logger.info(f"sympy program failed: {result.error[:200]}")
    return None


# ─── Sanitiser: rewrite pretty()/pprint()/srepr()/latex() into print(str(...)) ─

#: Formatters that *return* a multi-line / non-sympifiable string.
#: Safe to replace with ``str(<arg>)`` in any position (the return value is
#: preserved, only the representation changes to single-line ``str()``).
_RETURN_FUNCS = {"pretty", "srepr", "latex"}

#: Formatters that *print to stdout themselves* and return ``None``.
#: Only safe to rewrite when they appear as a standalone expression — in a
#: nested position (assignment RHS, outer-call argument, return value) the
#: rewrite would either lose stdout or silently change the return value, so we
#: leave them untouched and let the sandbox fail safely (→ LLM_ONLY).
_SELF_PRINT_FUNCS = {"pprint", "pretty_print"}

#: All formatter names we know about (union of the two sets above).
_FORMAT_FUNCS = _RETURN_FUNCS | _SELF_PRINT_FUNCS


class _SymPySymbolTable:
    """Track which names/aliases refer to SymPy, so we only rewrite SymPy calls.

    Built from a single AST pass over the module body.  It records:

    - ``sympy_modules``: names bound to the ``sympy`` module via
      ``import sympy as <alias>`` or ``import sympy``.
    - ``sympy_names``: names bound to specific SymPy functions via
      ``from sympy import <name>`` (only when <name> ∈ _FORMAT_FUNCS).
    - ``star_import``: whether ``from sympy import *`` was seen.
    - ``aliases``: ``<alias> -> <format_func_name>`` for assignments like
      ``fmt = sp.pretty`` or ``fmt = pretty``.
    - ``shadowed``: names locally redefined (``def latex`` / ``latex = ...``
      / function parameters) that must NOT be treated as SymPy.

    A bare-name call ``pretty(r)`` is confirmed SymPy only when ``pretty`` ∈
    ``sympy_names`` *or* (``star_import`` is True and ``pretty`` is not
    ``shadowed``).  This is the D-1b heuristic: ``from sympy import *`` is a
    common LLM pattern, and SymPy does export these names, so an unshadowed
    bare call is assumed to come from SymPy.
    """

    def __init__(self) -> None:
        self.sympy_modules: set[str] = set()
        self.sympy_names: dict[str, str] = {}
        self.star_import: bool = False
        self.aliases: dict[str, str] = {}
        self.shadowed: set[str] = set()

    def process_import(self, node: ast.stmt) -> None:
        """Record ``import sympy`` / ``import sympy as sp`` / ``from sympy import ...``."""
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                self.sympy_modules.discard(bound)
                if alias.name == "sympy":
                    self.sympy_modules.add(bound)
                    # a bare ``import sympy`` does NOT create ``pretty`` —
                    # only ``sympy.pretty`` is available, handled via Attribute.
                    if alias.asname:
                        self.sympy_modules.add(alias.asname)
            return
        if isinstance(node, ast.ImportFrom):
            if node.module != "sympy":
                return
            for alias in node.names:
                if alias.name == "*":
                    self.star_import = True
                    continue
                if alias.name in _FORMAT_FUNCS:
                    bound = alias.asname or alias.name
                    self.aliases.pop(bound, None)
                    self.shadowed.discard(bound)
                    self.sympy_names[bound] = alias.name
            return

    def process_assign(self, node: ast.Assign) -> None:
        """Track ``fmt = sp.pretty`` / ``fmt = pretty`` (alias) and ``latex = 1`` (shadow).

        Returns early once the assignment is classified.  Three cases:

        1. ``fmt = sp.pretty`` — value is a confirmed SymPy attribute that IS a
           format func → record the alias.
        2. ``fmt = pretty`` — value is a bare name that resolves (directly or
           transitively via an existing alias) to a format func → record the
           alias (resolved to the ultimate format-func name).
        3. Anything else — the target no longer refers to a format func.  Any
           previous alias for the target is cleared, and if the target name is
           itself a format-func name it is marked as shadowed.
        """
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            return
        target_name = node.targets[0].id
        value = node.value

        # Determine whether this assignment creates / updates an alias.
        new_alias: str | None = None
        if isinstance(value, ast.Attribute) and value.attr in _FORMAT_FUNCS:
            if self._is_sympy_attr(value):
                new_alias = value.attr
        elif isinstance(value, ast.Name):
            resolved = self._resolve_name(value.id)
            if resolved is not None:
                new_alias = resolved

        self.sympy_modules.discard(target_name)
        self.sympy_names.pop(target_name, None)
        if new_alias is not None:
            self.aliases[target_name] = new_alias
            self.shadowed.discard(target_name)
        else:
            # Assignment does not create an alias — clear any previous one.
            self.aliases.pop(target_name, None)
            # If the target name is itself a format-func name, it is now
            # shadowed (``latex = 5`` makes the SymPy ``latex`` unreachable).
            self.shadowed.add(target_name)

    def process_func_def(self, node: ast.FunctionDef) -> None:
        """``def latex(...):`` shadows the SymPy function."""
        self.sympy_modules.discard(node.name)
        self.sympy_names.pop(node.name, None)
        self.aliases.pop(node.name, None)
        self.shadowed.add(node.name)

    def process_args(self, args: ast.arguments) -> None:
        """Function parameters named like a format func shadow it inside the body."""
        for arg in args.args + args.posonlyargs + args.kwonlyargs:
            if arg.arg in _FORMAT_FUNCS:
                self.shadowed.add(arg.arg)

    def _is_sympy_attr(self, attr: ast.Attribute) -> bool:
        """``sp.pretty`` where ``sp`` is a confirmed SymPy module."""
        if not isinstance(attr.value, ast.Name):
            return False
        return attr.value.id in self.sympy_modules

    def _resolve_name(self, name: str) -> str | None:
        """Resolve a bare name to a format-func name, or ``None`` if not SymPy.

        Handles direct imports (``from sympy import latex``), aliases
        (``fmt = sp.pretty`` → resolves transitively), and the D-1b star-import
        heuristic.  Returns ``None`` if the name is shadowed or not confirmed.
        """
        if name in self.shadowed:
            return None
        if name in self.aliases:
            return self.aliases[name]
        if name in self.sympy_names:
            return self.sympy_names[name]
        # D-1b: ``from sympy import *`` heuristic — SymPy exports these names,
        # and the name is not locally shadowed, so assume it comes from SymPy.
        if self.star_import and name in _FORMAT_FUNCS:
            return name
        return None

    def _is_confirmed_name(self, name: str) -> bool:
        """Bare name known to be a SymPy format func (for alias source checks)."""
        return self._resolve_name(name) is not None

    def classify_call(self, node: ast.Call) -> str | None:
        """Return ``"return"`` / ``"self_print"`` / ``None`` for a confirmed call.

        ``"return"``  — ``_RETURN_FUNCS`` member (pretty/srepr/latex), returns a string.
        ``"self_print"`` — ``_SELF_PRINT_FUNCS`` member (pprint/pretty_print), prints itself.
        ``None`` — not a confirmed SymPy format call (leave untouched).
        """
        func = node.func
        if isinstance(func, ast.Attribute):
            if func.attr not in _FORMAT_FUNCS:
                return None
            if not self._is_sympy_attr(func):
                return None
            return self._category(func.attr)
        if isinstance(func, ast.Name):
            name = func.id
            # Shadow check first — a shadowed name is never a format call,
            # even if it was previously aliased.
            if name in self.shadowed:
                return None
            resolved = self._resolve_name(name)
            if resolved is not None:
                return self._category(resolved)
            return None
        return None

    @staticmethod
    def _category(func_name: str) -> str:
        if func_name in _RETURN_FUNCS:
            return "return"
        return "self_print"


def _format_call_argument(node: ast.Call) -> ast.expr | None:
    """Extract the *expression* argument from a format call.

    Handles positional (``pretty(expr)``), keyword-only (``pretty(expr=r)``),
    and ``pretty(r, use_unicode=False)``).  Returns ``None`` if no usable
    argument is found (the caller will treat this as a non-match).
    """
    if node.args:
        return node.args[0]
    # keyword-only call: ``sp.pretty(expr=value, use_unicode=False)``
    for kw in node.keywords:
        if kw.arg == "expr":
            return kw.value
    if node.keywords:
        return node.keywords[0].value
    return None


def _sanitize_sympy_output_code(code: str) -> str:
    """Rewrite LLM-generated sympy code so stdout stays single-line & sympifiable.

    If the LLM ignored the prompt and used ``pretty(expr)``, ``pprint(expr)``,
    ``srepr(expr)``, or ``latex(expr)`` to print the answer, the sandbox stdout
    would be multi-line ASCII art (or LaTeX) that ``sympy.sympify`` cannot parse
    → SymPy verification silently fails and grading degrades to LLM_ONLY.

    This function parses the code with ``ast`` and structurally replaces
    **confirmed SymPy** format calls so stdout stays single-line:

    - ``_RETURN_FUNCS`` (``pretty`` / ``srepr`` / ``latex``) — return a string,
      so they are replaced by ``str(<arg>)`` in **any** position (the return
      value is preserved, only the representation changes).
    - ``_SELF_PRINT_FUNCS`` (``pprint`` / ``pretty_print``) — print to stdout
      themselves and return ``None``.  A **standalone** expression
      ``sp.pprint(expr)`` is replaced with ``print(str(<arg>))``.  In a
      **nested** position (assignment RHS, outer-call argument, return value)
      the call is left **untouched** — rewriting would either lose stdout or
      silently change the return value, so the sandbox fails safely (→
      LLM_ONLY) rather than risk corrupting the mathematical reference.

    Only calls whose source is confirmed to be SymPy (via ``import sympy`` /
    ``from sympy import …`` / ``from sympy import *`` heuristics / tracked
    aliases) are rewritten.  A locally-defined ``def latex(x): …`` or a
    shadowing assignment ``latex = …`` is detected and **never** touched, so
    non-SymPy code with the same function name is safe.

    Unparseable code is returned unchanged (the sandbox will report the error
    as before).

    Returns the (possibly rewritten) code string.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code

    table = _SymPySymbolTable()

    class _Transformer(ast.NodeTransformer):
        def __init__(self) -> None:
            self.rewrite_count = 0
            # Track shadowing introduced *inside* nested scopes (function
            # bodies) that the top-level pass 1 might have missed.  We keep a
            # simple scope stack; Python scoping means a local ``def latex``
            # or ``latex = ...`` inside a function shadows the global for that
            # body only.
            self._scope_shadows: list[set[str]] = []

        def _current_shadows(self) -> set[str]:
            return set().union(*self._scope_shadows) if self._scope_shadows else set()

        def visit_Module(self, node: ast.Module) -> ast.AST:  # noqa: N802
            for index, stmt in enumerate(node.body):
                if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                    table.process_import(stmt)
                node.body[index] = self.visit(stmt)
                if isinstance(stmt, ast.FunctionDef):
                    table.process_func_def(stmt)
                elif isinstance(stmt, ast.Assign):
                    table.process_assign(stmt)
            return node

        def _classify(self, node: ast.Call) -> str | None:
            cat = table.classify_call(node)
            if cat is None:
                return None
            # honour in-scope shadowing from nested function bodies
            func = node.func
            if isinstance(func, ast.Name) and func.id in self._current_shadows():
                return None
            return cat

        def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:  # noqa: N802
            local: set[str] = {node.name}
            for arg in node.args.args + node.args.posonlyargs + node.args.kwonlyargs:
                local.add(arg.arg)
            if node.args.vararg:
                local.add(node.args.vararg.arg)
            if node.args.kwarg:
                local.add(node.args.kwarg.arg)
            # detect ``latex = ...`` assignments inside the body
            for child in ast.walk(node):
                if isinstance(child, ast.Assign):
                    for tgt in child.targets:
                        if isinstance(tgt, ast.Name):
                            local.add(tgt.id)
            self._scope_shadows.append(local)
            result = self.generic_visit(node)
            self._scope_shadows.pop()
            return result

        def visit_Lambda(self, node: ast.Lambda) -> ast.AST:  # noqa: N802
            local = {
                arg.arg
                for arg in node.args.args + node.args.posonlyargs + node.args.kwonlyargs
            }
            self._scope_shadows.append(local)
            result = self.generic_visit(node)
            self._scope_shadows.pop()
            return result

        def visit_Expr(self, node: ast.Expr) -> ast.AST:  # noqa: N802
            # Standalone expression: ``sp.pprint(expr)`` or ``pretty(expr)``.
            # Both categories are safe here — we wrap in ``print(str(...))``
            # so stdout stays single-line and sympifiable.
            if isinstance(node.value, ast.Call):
                cat = self._classify(node.value)
                if cat is not None:
                    arg = _format_call_argument(node.value)
                    if arg is None:
                        return self.generic_visit(node)
                    arg = self.visit(arg)
                    self.rewrite_count += 1
                    replacement = ast.Expr(
                        value=ast.Call(
                            func=ast.Name(id="print", ctx=ast.Load()),
                            args=[
                                ast.Call(
                                    func=ast.Name(id="str", ctx=ast.Load()),
                                    args=[arg],
                                    keywords=[],
                                )
                            ],
                            keywords=[],
                        )
                    )
                    return ast.copy_location(replacement, node)
            return self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> ast.AST:  # noqa: N802
            cat = self._classify(node)
            if cat is None:
                return self.generic_visit(node)
            arg = _format_call_argument(node)
            if arg is None:
                return self.generic_visit(node)
            if cat == "self_print":
                # D-2b: pprint/pretty_print in a nested position would lose
                # stdout or change the return value if rewritten.  Leave it
                # untouched so the sandbox fails safely → LLM_ONLY.
                return self.generic_visit(node)
            # cat == "return": pretty/srepr/latex return a string → str(arg)
            arg = self.visit(arg)
            self.rewrite_count += 1
            replacement = ast.Call(
                func=ast.Name(id="str", ctx=ast.Load()),
                args=[arg],
                keywords=[],
            )
            return ast.copy_location(replacement, node)

    transformer = _Transformer()
    rewritten_tree = transformer.visit(tree)
    if not transformer.rewrite_count:
        return code  # nothing to fix
    ast.fix_missing_locations(rewritten_tree)
    rewritten = ast.unparse(rewritten_tree) + "\n"
    logger.info(
        "_sanitize_sympy_output_code rewrote %d format call(s)",
        transformer.rewrite_count,
    )
    return rewritten


def _format_metadata_zh(
    sympy_status: str, has_reference: bool, ref_origin: str
) -> str:
    """Build the metadata footer appended to the LLM comment.

    `ref_origin` is one of: "teacher", "ai_computed", "n/a" — reflects whether
    the reference came from teacher upload or from the LLM-generated sympy run.
    Visible to the teacher so they understand the provenance.
    """
    if sympy_status == "matched":
        origin_zh = "标答" if ref_origin == "teacher" else "AI 计算结果"
        return f"\n\n（SymPy 验证：✓ 与{origin_zh}一致）"
    if sympy_status == "mismatched":
        origin_zh = "标答" if ref_origin == "teacher" else "AI 计算结果"
        return f"\n\n（SymPy 验证：✗ 答案与{origin_zh}不一致；本评分基于过程分判断）"
    if sympy_status == "sympy_failed":
        if has_reference:
            return "\n\n（SymPy 验证：未启用 — 学生答案表达式无法解析；本评分基于 AI 推理）"
        return "\n\n（SymPy 验证：未启用 — sympy 程序执行失败；本评分基于 AI 推理）"
    if sympy_status == "no_reference":
        return "\n\n（SymPy 验证：未启用 — 标答缺失且 AI 未能生成 sympy 程序；本评分基于 AI 推理）"
    if sympy_status == "unsuitable":
        return "\n\n（SymPy 验证：未启用 — 题目不适合符号计算；本评分基于 AI 推理）"
    return ""


# ─── Skill ───────────────────────────────────────────────────────────────────

@register_skill("计算题")
class CalculationSkill(GradingSkill):
    name = "CalculationSkill"
    problem_type = "计算题"

    def __init__(
        self,
        provider: BaseProvider,
        *,
        reporter: Optional["ProgressReporter"] = None,
        language: str = "en",
        task_id: Optional[str] = None,
        grading_setup: Optional[TaskGradingSetup] = None,
    ):
        super().__init__(
            provider, reporter=reporter, language=language, task_id=task_id,
            grading_setup=grading_setup,
        )
        self._template = _load_template()

    async def grade(
        self,
        problem: ProblemInfo,
        answer: StudentAnswerInfo,
        *,
        student_id: str = "",
    ) -> ExpertResult:
        logger.info(
            f"CalculationSkill.grade start: q_id={problem.q_id}, "
            f"has_reference={problem.reference_answer is not None}, "
            f"provider={self.provider.provider_id}"
        )

        active_unit = None
        step_ctx = None
        if self.reporter:
            step_ctx = self.reporter.step(student_id, problem.q_id, self.name, self.provider.provider_id)
            active_unit = await step_ctx.__aenter__()

        try:
            student_text = answer.content or ""
            reference: Optional[str] = problem.reference_answer

            # ─── Step 1: Determine reference value ───────────────────────────
            ref_value: Optional[str] = None
            ref_origin: str = "n/a"  # "teacher" | "ai_computed" | "n/a"
            sympy_status: str = "unsuitable"
            #  ↑ legal values:
            #    matched | mismatched | sympy_failed | no_reference | unsuitable

            is_integral = False  # True when ref is an *indefinite* integral from LLM sympy
            integral_var: Optional[str] = None  # integration variable, if known
            if reference and reference.strip():
                ref_value = reference.strip()
                ref_origin = "teacher"
            else:
                # No teacher-supplied reference → LLM writes a sympy program
                if self.reporter and active_unit:
                    await self.reporter.substep(active_unit, "generate_sympy")
                sympy_code = await _generate_sympy_program(self.provider, problem)
                if sympy_code:
                    # Indefinite-integral detection: SymPy's ``integrate(f, x)``
                    # omits +C, so a correct student answer that includes +C
                    # would be marked mismatched by strict symbolic comparison.
                    # Only *indefinite* integrals get derivative-based verify;
                    # *definite* integrals ``integrate(f, (x, a, b))`` yield a
                    # scalar (possibly with free params) and must use strict
                    # comparison.  Parsing the call form (rather than a substring
                    # match on "integrate(") also distinguishes the two.
                    parsed = _parse_integrate_call(sympy_code)
                    if parsed is not None:
                        is_integral, integral_var = parsed
                    if self.reporter and active_unit:
                        await self.reporter.substep(active_unit, "run_sympy")
                    # Defensively rewrite pretty()/pprint()/srepr()/latex() calls
                    # into print(str(...)) so stdout stays single-line and
                    # sympifiable — even if the LLM ignored the prompt rules.
                    sympy_code = _sanitize_sympy_output_code(sympy_code)
                    stdout = await _run_sympy_in_sandbox(sympy_code, timeout=10.0)
                    if stdout:
                        ref_value = stdout
                        ref_origin = "ai_computed"
                    else:
                        sympy_status = "sympy_failed"
                else:
                    sympy_status = "no_reference"

            # ─── Step 2: Verify against reference (if we have one) ──────────
            if ref_value is not None:
                if self.reporter and active_unit:
                    await self.reporter.substep(active_unit, "sympy_verify")
                student_expr = _extract_final_expression(student_text)
                if student_expr:
                    if is_integral:
                        # Indefinite-integral answer (no teacher reference):
                        # compare derivatives so the +C constant vanishes.  Pass
                        # the parsed integration variable; the verifier also has
                        # a fallback to infer it from the reference's free symbols
                        # and a safety valve against stale substitution vars.
                        ok: Optional[bool] = await numerical.verify_derivative_equivalent(
                            student_expr, ref_value, var=integral_var
                        )
                    else:
                        ok = await numerical.verify_equivalent(student_expr, ref_value)
                        if ok is None:
                            # symbolic compare failed → try numeric closeness
                            ok = await numerical.verify_value(student_expr, ref_value, rel_tol=1e-6)
                    if ok is True:
                        sympy_status = "matched"
                    elif ok is False:
                        sympy_status = "mismatched"
                    else:
                        sympy_status = "sympy_failed"
                else:
                    sympy_status = "sympy_failed"  # could not extract expression

            # ─── Step 3: Pick LLM branch ─────────────────────────────────────
            if sympy_status == "matched":
                branch = "VERIFIED_CORRECT"
                verification_status = (
                    f"sympy confirms student answer matches the "
                    f"{'teacher reference' if ref_origin == 'teacher' else 'AI-computed reference'}."
                )
            elif sympy_status == "mismatched":
                branch = "VERIFIED_INCORRECT"
                verification_status = (
                    "sympy confirms student answer does NOT match the reference. "
                    "Final answer is wrong."
                )
            else:  # sympy_failed | no_reference | unsuitable
                branch = "LLM_ONLY"
                if sympy_status == "sympy_failed":
                    verification_status = "sympy could not verify (program/expression parse failure)."
                elif sympy_status == "no_reference":
                    verification_status = "No reference value available."
                else:
                    verification_status = "Problem not suitable for symbolic verification."

            # ─── Step 4: LLM grading ─────────────────────────────────────────
            if self.reporter and active_unit:
                await self.reporter.substep(active_unit, "llm_grade")

            prompt = self._template
            prompt = prompt.replace("{problem}", problem.stem)
            prompt = prompt.replace("{answer}", student_text or "(No answer provided)")
            prompt = prompt.replace("{correct_answer}", ref_value or "(not provided)")
            prompt = prompt.replace("{verification_status}", verification_status)
            prompt = prompt.replace("{branch}", branch)
            prompt = prompt.replace("{rubric}", problem.criterion)

            system_prompt = build_system_prompt(
                "You are a mathematics teacher grading a calculation problem. "
                "Walk through the 4-step reasoning workflow (Setup → Method → Derivation → Result) "
                "and produce a structured per-dimension score. Respect the verification branch "
                "rules in the user prompt."
                + authoritative_score_instruction(problem.max_score),
                self.language,
                self.grading_setup,
            )

            result, raw = await structured_llm_call(
                self.provider,
                system_prompt=system_prompt,
                user_prompt=prompt,
                output_model=CalcGradingOutput,
            )

            # Force full marks when sympy says matched — LLM might still
            # nitpick presentation; not worth it.
            score = result.score
            if sympy_status == "matched":
                score = result.max_score
            # Process-credit cap when sympy says mismatched: at most 70% of max.
            elif sympy_status == "mismatched":
                score = min(score, result.max_score * 0.7)
            score = max(0.0, min(score, result.max_score))

            step_scores = []
            for s in result.steps:
                if isinstance(s, dict):
                    step_scores.append(StepScore(
                        step_no=s.get("step_no", len(step_scores) + 1),
                        desc=s.get("desc", s.get("comment", "")),
                        is_correct=bool(s.get("is_correct", True)),
                        score=float(s.get("score", 0.0)),
                    ))

            metadata_footer = _format_metadata_zh(
                sympy_status, has_reference=(ref_value is not None), ref_origin=ref_origin
            )
            final_comment = (result.comment or "") + metadata_footer

            return normalize_expert_result(ExpertResult(
                provider=self.provider.provider_id,
                score=score,
                max_score=result.max_score,
                confidence=max(0.0, min(result.confidence, 1.0)),
                comment=final_comment,
                steps=step_scores,
                raw_output=raw.content,
                duration_ms=raw.duration_ms,
            ), problem.max_score)

        except Exception as e:
            logger.error(
                "CalculationSkill failed; exception_type=%s",
                type(e).__name__,
            )
            from backend.skills.base import classify_skill_error
            kind, friendly = classify_skill_error(e)
            return self._blank_result(problem.q_id, problem.max_score, friendly, error_kind=kind)
        finally:
            if step_ctx:
                await step_ctx.__aexit__(None, None, None)
