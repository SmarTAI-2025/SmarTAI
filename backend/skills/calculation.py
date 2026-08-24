"""
CalculationSkill: grades calculation-type questions (计算题).

Strategy (4-tier fallback ladder):

    1. Reference present  → use teacher's reference_answer directly.
    2. Reference absent   → ask LLM to generate a SymPy program, statically
                            check it, run it through the versioned runner, and
                            repair the tool script at most once when the script
                            itself is invalid. Use stdout as the reference.
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
import hashlib
import json
import logging
import os
import re
import uuid
from typing import Awaitable, Callable, Literal, Optional, List, Tuple, TYPE_CHECKING

from pydantic import BaseModel, Field, ValidationError

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
from backend.tools.grading_runner import (
    RunnerExitReason,
    RunnerLimits,
    RunnerRequest,
    RunnerResourceStatus,
    RunnerResult,
    run_grading_request,
)

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


MAX_SYMPY_REPAIRS = 1
SYMPY_RUN_TIMEOUT_SECONDS = 10.0


class SympyAttemptTrace(BaseModel):
    attempt_number: int = Field(ge=1)
    phase: Literal["generated", "repaired"]
    code_sha256: str
    runner_result: RunnerResult


class SympyLoopResult(BaseModel):
    execution_id: str
    status: Literal[
        "succeeded",
        "generation_failed",
        "repair_generation_failed",
        "repair_exhausted",
        "timeout",
        "output_limit",
        "execution_failed",
    ]
    stop_reason: str
    reference_value: Optional[str] = None
    is_indefinite_integral: bool = False
    integral_variable: Optional[str] = None
    repair_count: int = Field(default=0, ge=0)
    attempts: List[SympyAttemptTrace] = Field(default_factory=list)


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

    Returns None on parse failure; the loop records ``generation_failed`` and
    falls back to LLM_ONLY scoring.
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


async def _repair_sympy_program(
    provider: BaseProvider,
    problem: ProblemInfo,
    *,
    previous_code: str,
    safe_error: str,
    repair_number: int,
) -> Optional[str]:
    """Repair generated tool code without seeing or changing student content."""

    system_prompt = (
        "You repair a short SymPy reference-calculation program. Fix only the "
        "tool script. Never change the mathematics problem, rubric, expected "
        "answer, or any student content. Only `import sympy` and "
        "`from sympy import ...` are allowed. The corrected program must print "
        "exactly one final answer and must not use input, files, network, "
        "processes, dynamic execution, or reflection. Return JSON "
        "{\"code\": \"<corrected program>\"}."
    )
    user_prompt = (
        f"Problem (type={problem.type}):\n{problem.stem}\n\n"
        f"Rubric (immutable):\n{problem.criterion}\n\n"
        f"Repair number: {repair_number}\n"
        f"Sanitized runner feedback:\n{safe_error[:1000]}\n\n"
        f"Previous generated script:\n{previous_code}\n\n"
        "Return the corrected SymPy program."
    )
    try:
        result, _raw = await structured_llm_call(
            provider,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            output_model=SympyProgramOutput,
        )
        return result.code
    except Exception as exc:
        logger.warning(
            "_repair_sympy_program failed; exception_type=%s",
            type(exc).__name__,
        )
        return None


def _code_sha256(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _invalid_request_result(execution_id: str) -> RunnerResult:
    return RunnerResult(
        execution_id=execution_id,
        executed=False,
        exit_reason=RunnerExitReason.STATIC_REJECTED,
        stderr="runner request validation failed",
        resource_status=RunnerResourceStatus.NOT_EXECUTED,
    )


async def _run_sympy_loop(
    provider: BaseProvider,
    problem: ProblemInfo,
    *,
    max_repairs: int = MAX_SYMPY_REPAIRS,
    timeout: float = SYMPY_RUN_TIMEOUT_SECONDS,
    execution_id: Optional[str] = None,
    runner: Optional[Callable[[RunnerRequest], Awaitable[RunnerResult]]] = None,
    phase_callback: Optional[Callable[[str], Awaitable[None]]] = None,
) -> SympyLoopResult:
    """Generate, check and execute SymPy code with a finite repair budget."""

    root_execution_id = execution_id or f"sympy-{uuid.uuid4().hex}"
    max_repairs = max(0, min(int(max_repairs), MAX_SYMPY_REPAIRS))
    execute = runner or run_grading_request

    async def emit(phase: str) -> None:
        if phase_callback is not None:
            await phase_callback(phase)

    await emit("generate_sympy")
    code = await _generate_sympy_program(provider, problem)
    if not code or not code.strip():
        return SympyLoopResult(
            execution_id=root_execution_id,
            status="generation_failed",
            stop_reason="generation_failed",
        )

    attempts: List[SympyAttemptTrace] = []
    repair_count = 0
    phase: Literal["generated", "repaired"] = "generated"
    repairable_reasons = {
        RunnerExitReason.STATIC_REJECTED,
        RunnerExitReason.SYNTAX_ERROR,
        RunnerExitReason.RUNTIME_ERROR,
    }

    while True:
        attempt_number = len(attempts) + 1
        attempt_execution_id = f"{root_execution_id}:a{attempt_number}"
        parsed_integral = _parse_integrate_call(code)
        is_indefinite_integral = bool(parsed_integral and parsed_integral[0])
        integral_variable = parsed_integral[1] if parsed_integral else None
        executable_code = _sanitize_sympy_output_code(code)
        await emit("check_sympy" if attempt_number == 1 else "check_repaired_sympy")
        try:
            request = RunnerRequest(
                execution_id=attempt_execution_id,
                task_type="sympy",
                language="python",
                code=executable_code,
                limits=RunnerLimits(timeout_seconds=timeout),
            )
        except ValidationError:
            runner_result = _invalid_request_result(attempt_execution_id)
        else:
            await emit("run_sympy")
            runner_result = await execute(request)

        attempts.append(SympyAttemptTrace(
            attempt_number=attempt_number,
            phase=phase,
            code_sha256=_code_sha256(executable_code),
            runner_result=runner_result,
        ))

        output = runner_result.stdout.strip()
        invalid_output = (
            runner_result.exit_reason == RunnerExitReason.SUCCESS and not output
        )
        if runner_result.exit_reason == RunnerExitReason.SUCCESS and output:
            return SympyLoopResult(
                execution_id=root_execution_id,
                status="succeeded",
                stop_reason="completed",
                reference_value=output,
                is_indefinite_integral=is_indefinite_integral,
                integral_variable=integral_variable,
                repair_count=repair_count,
                attempts=attempts,
            )

        can_repair = (
            invalid_output or runner_result.exit_reason in repairable_reasons
        )
        if can_repair and repair_count < max_repairs:
            repair_count += 1
            await emit("repair_sympy")
            feedback = (
                "program completed without printing a non-empty final answer"
                if invalid_output
                else runner_result.stderr or runner_result.exit_reason.value
            )
            repaired = await _repair_sympy_program(
                provider,
                problem,
                previous_code=code,
                safe_error=feedback,
                repair_number=repair_count,
            )
            if not repaired or not repaired.strip():
                return SympyLoopResult(
                    execution_id=root_execution_id,
                    status="repair_generation_failed",
                    stop_reason="repair_generation_failed",
                    repair_count=repair_count,
                    attempts=attempts,
                )
            code = repaired
            phase = "repaired"
            continue

        if can_repair:
            status = "repair_exhausted"
            stop_reason = "repair_exhausted"
        elif runner_result.exit_reason == RunnerExitReason.TIMEOUT:
            status = "timeout"
            stop_reason = "timeout"
        elif runner_result.exit_reason == RunnerExitReason.OUTPUT_LIMIT:
            status = "output_limit"
            stop_reason = "output_limit"
        else:
            status = "execution_failed"
            stop_reason = runner_result.exit_reason.value
        return SympyLoopResult(
            execution_id=root_execution_id,
            status=status,
            stop_reason=stop_reason,
            repair_count=repair_count,
            attempts=attempts,
        )


async def _run_sympy_in_sandbox(code: str, *, timeout: float = 10.0) -> Optional[str]:
    """Compatibility helper for callers that only need a reference value."""

    request = RunnerRequest(
        execution_id=f"sympy-compat-{uuid.uuid4().hex}",
        task_type="sympy",
        code=code,
        limits=RunnerLimits(timeout_seconds=timeout),
    )
    result = await run_grading_request(request)
    if result.exit_reason == RunnerExitReason.SUCCESS and result.stdout.strip():
        return result.stdout.strip()
    return None


def _build_sympy_audit_log(
    loop_result: Optional[SympyLoopResult],
    *,
    verification_status: str,
    ref_origin: str,
) -> str:
    payload: dict = {
        "schema_version": 1,
        "tool": "sympy",
        "reference_origin": ref_origin,
        "verification_status": verification_status,
        "max_repairs": MAX_SYMPY_REPAIRS,
    }
    if loop_result is not None:
        payload.update({
            "execution_id": loop_result.execution_id,
            "loop_status": loop_result.status,
            "stop_reason": loop_result.stop_reason,
            "repair_count": loop_result.repair_count,
            "attempts": [
                {
                    "attempt_number": attempt.attempt_number,
                    "phase": attempt.phase,
                    "code_sha256": attempt.code_sha256,
                    "executed": attempt.runner_result.executed,
                    "exit_reason": attempt.runner_result.exit_reason.value,
                    "duration_ms": round(attempt.runner_result.duration_ms, 3),
                    "resource_status": attempt.runner_result.resource_status.value,
                    "stdout_truncated": attempt.runner_result.stdout_truncated,
                    "stderr_truncated": attempt.runner_result.stderr_truncated,
                    "isolation_mode": attempt.runner_result.isolation_mode,
                }
                for attempt in loop_result.attempts
            ],
        })
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

# ─── Sanitiser: rewrite pretty()/pprint()/srepr()/latex() into print(str(...)) ─

#: Formatters that *return* a multi-line / non-sympifiable string.
#: They are rewritten only when their value is the program's direct output;
#: changing the returned representation elsewhere can change program meaning.
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
    and ``pretty(r, use_unicode=False)``).  Invalid/ambiguous calls and
    settings with executable expressions are left unchanged so sanitization
    cannot turn a failing program into a successful but incorrect reference.
    """
    expr_keywords = [kw for kw in node.keywords if kw.arg == "expr"]
    settings = [kw for kw in node.keywords if kw.arg != "expr"]
    if any(kw.arg is None or not isinstance(kw.value, ast.Constant) for kw in settings):
        return None
    if node.args:
        if len(node.args) != 1 or expr_keywords:
            return None
        return node.args[0]
    if len(expr_keywords) == 1:
        return expr_keywords[0].value
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
      so they are replaced only as a standalone output or the sole direct
      argument of ``print``.  Assignments and later computations are untouched.
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

    binding_counts: dict[str, int] = {}

    def record_binding(name: str) -> None:
        binding_counts[name] = binding_counts.get(name, 0) + 1

    for item in ast.walk(tree):
        if isinstance(item, ast.Name) and isinstance(item.ctx, (ast.Store, ast.Del)):
            record_binding(item.id)
        elif isinstance(item, ast.arg):
            record_binding(item.arg)
        elif isinstance(item, ast.alias) and item.name != "*":
            record_binding(item.asname or item.name.split(".")[0])
        elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            record_binding(item.name)
        elif isinstance(item, ast.ExceptHandler) and item.name:
            record_binding(item.name)

    # Rewriting injects calls to these builtins.  If either name is rebound
    # anywhere, leave the program unchanged rather than invoke user code.
    if binding_counts.get("print") or binding_counts.get("str"):
        return code
    ambiguous_bindings = {
        name for name, count in binding_counts.items() if count > 1
    }

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
            func = node.func
            shadows = self._current_shadows()
            if isinstance(func, ast.Name) and (
                func.id in shadows or func.id in ambiguous_bindings
            ):
                return None
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and (
                    func.value.id in shadows
                    or func.value.id in ambiguous_bindings
                )
            ):
                return None
            return table.classify_call(node)

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
            # Only rewrite a formatter when it is the sole direct value sent
            # to builtin print.  Arbitrary nested positions can use the
            # formatter string later and therefore must preserve semantics.
            if (
                isinstance(node.func, ast.Name)
                and node.func.id == "print"
                and len(node.args) == 1
                and not node.keywords
                and isinstance(node.args[0], ast.Call)
                and self._classify(node.args[0]) is not None
            ):
                arg = _format_call_argument(node.args[0])
                if arg is not None:
                    arg = self.visit(arg)
                    self.rewrite_count += 1
                    replacement = ast.Call(
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
                    return ast.copy_location(replacement, node)
            return self.generic_visit(node)

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
    sympy_status: str,
    has_reference: bool,
    ref_origin: str,
    *,
    loop_stop_reason: Optional[str] = None,
    repair_count: int = 0,
) -> str:
    """Build the metadata footer appended to the LLM comment.

    `ref_origin` is one of: "teacher", "ai_computed", "n/a" — reflects whether
    the reference came from teacher upload or from the LLM-generated sympy run.
    Visible to the teacher so they understand the provenance.
    """
    if sympy_status == "matched":
        origin_zh = "标答" if ref_origin == "teacher" else "AI 计算结果"
        repair_note = f"；计算脚本自动修正 {repair_count} 次" if repair_count else ""
        return f"\n\n（SymPy 验证：✓ 与{origin_zh}一致{repair_note}）"
    if sympy_status == "mismatched":
        origin_zh = "标答" if ref_origin == "teacher" else "AI 计算结果"
        return (
            f"\n\n（SymPy 验证：✗ 答案与{origin_zh}不一致；"
            "系统未修改学生答案，本评分基于过程分判断）"
        )
    if sympy_status == "sympy_failed":
        if has_reference:
            return "\n\n（SymPy 验证：未启用 — 学生答案表达式无法解析；本评分基于 AI 推理）"
        if loop_stop_reason == "timeout":
            return "\n\n（SymPy 验证：未完成 — 计算脚本执行超时；结果未经验证，需人工复核）"
        if loop_stop_reason == "output_limit":
            return "\n\n（SymPy 验证：未完成 — 计算脚本输出超限；结果未经验证，需人工复核）"
        if loop_stop_reason in {"repair_exhausted", "repair_generation_failed"}:
            return "\n\n（SymPy 验证：未完成 — 计算脚本修正后仍失败；结果未经验证，需人工复核）"
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
            sympy_loop: Optional[SympyLoopResult] = None
            #  ↑ legal values:
            #    matched | mismatched | sympy_failed | no_reference | unsuitable

            is_integral = False  # True when ref is an *indefinite* integral from LLM sympy
            integral_var: Optional[str] = None  # integration variable, if known
            if reference and reference.strip():
                ref_value = reference.strip()
                ref_origin = "teacher"
            else:
                async def report_sympy_phase(phase: str) -> None:
                    if self.reporter and active_unit:
                        await self.reporter.substep(active_unit, phase)

                sympy_loop = await _run_sympy_loop(
                    self.provider,
                    problem,
                    phase_callback=report_sympy_phase,
                )
                if sympy_loop.status == "succeeded":
                    ref_value = sympy_loop.reference_value
                    ref_origin = "ai_computed"
                    is_integral = sympy_loop.is_indefinite_integral
                    integral_var = sympy_loop.integral_variable
                elif sympy_loop.status == "generation_failed":
                    sympy_status = "no_reference"
                else:
                    sympy_status = "sympy_failed"

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
                    if sympy_loop and sympy_loop.stop_reason == "timeout":
                        verification_status = (
                            "The SymPy tool script timed out. No computed result was "
                            "verified; grade conservatively and request human review."
                        )
                    elif sympy_loop and sympy_loop.stop_reason == "output_limit":
                        verification_status = (
                            "The SymPy tool script exceeded its output limit. No "
                            "computed result was verified; request human review."
                        )
                    elif sympy_loop and sympy_loop.stop_reason in {
                        "repair_exhausted",
                        "repair_generation_failed",
                    }:
                        verification_status = (
                            "The SymPy tool script still failed after its single allowed "
                            "repair. No computed result was verified."
                        )
                    else:
                        verification_status = (
                            "sympy could not verify (program/expression parse failure)."
                        )
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
                sympy_status,
                has_reference=(ref_value is not None),
                ref_origin=ref_origin,
                loop_stop_reason=(sympy_loop.stop_reason if sympy_loop else None),
                repair_count=(sympy_loop.repair_count if sympy_loop else 0),
            )
            final_comment = (result.comment or "") + metadata_footer
            audit_log = _build_sympy_audit_log(
                sympy_loop,
                verification_status=sympy_status,
                ref_origin=ref_origin,
            )

            return normalize_expert_result(ExpertResult(
                provider=self.provider.provider_id,
                score=score,
                max_score=result.max_score,
                confidence=max(0.0, min(result.confidence, 1.0)),
                comment=final_comment,
                steps=step_scores,
                logs=audit_log,
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
