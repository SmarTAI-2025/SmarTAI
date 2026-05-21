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

import logging
import os
import re
from typing import Optional, List, TYPE_CHECKING

from pydantic import BaseModel, Field

from backend.skills.base import GradingSkill, build_system_prompt, register_skill
from backend.models import ExpertResult, ProblemInfo, StudentAnswerInfo, StepScore
from backend.llm.providers import BaseProvider
from backend.tools.structured_llm import structured_llm_call
from backend.tools import numerical
from backend.tools.code_interpreter import run_python_subprocess

if TYPE_CHECKING:
    from backend.progress.tracker import ProgressReporter

logger = logging.getLogger(__name__)


# ─── Output schemas ──────────────────────────────────────────────────────────

class CalcGradingOutput(BaseModel):
    score: float = Field(ge=0)
    max_score: float = Field(default=10.0)
    confidence: float = Field(ge=0, le=1)
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
        logger.warning(f"_generate_sympy_program failed: {e}")
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
        logger.warning(f"_run_sympy_in_sandbox subprocess error: {e}")
        return None
    if result.passed and result.actual_output:
        return result.actual_output.strip()
    if result.error:
        logger.info(f"sympy program failed: {result.error[:200]}")
    return None


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
    ):
        super().__init__(provider, reporter=reporter, language=language, task_id=task_id)
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

            if reference and reference.strip():
                ref_value = reference.strip()
                ref_origin = "teacher"
            else:
                # No teacher-supplied reference → LLM writes a sympy program
                if self.reporter and active_unit:
                    await self.reporter.substep(active_unit, "generate_sympy")
                sympy_code = await _generate_sympy_program(self.provider, problem)
                if sympy_code:
                    if self.reporter and active_unit:
                        await self.reporter.substep(active_unit, "run_sympy")
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
                    ok: Optional[bool] = await numerical.verify_equivalent(student_expr, ref_value)
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
                "rules in the user prompt.",
                self.language,
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

            return ExpertResult(
                provider=self.provider.provider_id,
                score=score,
                max_score=result.max_score,
                confidence=max(0.0, min(result.confidence, 1.0)),
                comment=final_comment,
                steps=step_scores,
                raw_output=raw.content,
                duration_ms=raw.duration_ms,
            )

        except Exception as e:
            logger.error(f"CalculationSkill failed: {e}", exc_info=True)
            from backend.skills.base import classify_skill_error
            kind, friendly = classify_skill_error(e)
            return self._blank_result(problem.q_id, 10.0, friendly, error_kind=kind)
        finally:
            if step_ctx:
                await step_ctx.__aexit__(None, None, None)
