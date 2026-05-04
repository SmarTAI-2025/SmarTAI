"""
ProgrammingSkill: grades programming questions (编程题).

Strategy (4-tier sandbox-cases ladder):

    1. teacher          → ProblemInfo.test_cases (uploaded via
                          POST /tasks/{id}/upload_test_cases) is non-empty.
                          Run sandbox; pass/fail tally is the score anchor.
    2. llm_generated    → No teacher cases AND no complexity-keyword hits
                          AND student code is Python.
                          LLM generates ≤ 8 cases each carrying a
                          `sandbox_feasible: bool` self-assessment. Run sandbox
                          on the feasible subset.
    3. skipped_complex  → Either (a) the problem stem / student code mentions
                          GUI / network / multiprocessing / huge-input keywords,
                          or (b) every LLM-generated case marked itself
                          infeasible. We do NOT run sandbox; the LLM grades on
                          code review against the rubric and the comment carries
                          a metadata footer explaining why.
    4. skipped_lang     → Student wrote in a non-Python language (best-effort
                          detection). Sandbox supports only Python; LLM grades
                          purely on code review.

The sandbox itself is gated by a global asyncio.Semaphore (limit=8, configured
in backend/main.py lifespan) so this skill cannot fork-bomb the host even when
many students are graded concurrently.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional, List, Tuple, TYPE_CHECKING

from pydantic import BaseModel, Field

from backend.skills.base import GradingSkill, build_system_prompt, register_skill
from backend.models import ExpertResult, ProblemInfo, StudentAnswerInfo, StepScore, TestCase
from backend.llm.providers import BaseProvider
from backend.tools.structured_llm import structured_llm_call
from backend.tools.code_interpreter import run_sandbox, ExecutionReport

if TYPE_CHECKING:
    from backend.progress.tracker import ProgressReporter

logger = logging.getLogger(__name__)


# ─── LLM output schemas ──────────────────────────────────────────────────────

class ProgrammingGradingOutput(BaseModel):
    score: float = Field(ge=0)
    max_score: float = Field(default=10.0)
    confidence: float = Field(ge=0, le=1)
    comment: str
    steps: List[dict] = Field(default_factory=list)
    logs: str = Field(default="")


class TestCaseList(BaseModel):
    """LLM-generated test cases for a programming problem."""
    cases: List[TestCase] = Field(default_factory=list, max_length=20)


# ─── Prompt template loading ─────────────────────────────────────────────────

def _load_template() -> str:
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "prompts", "programming.txt")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return _DEFAULT_TEMPLATE


_DEFAULT_TEMPLATE = """You are a programming teacher grading student code.

Problem:
{problem}

Student Code:
```
{code}
```

Sandbox / Test Cases Result:
{branch_info}

Grading Rubric:
{rubric}

Return JSON: score, max_score, confidence, comment, steps, logs.
"""


# ─── Limits ──────────────────────────────────────────────────────────────────

# How many LLM-generated cases we run at most. Even if the LLM returns more,
# we slice down so the sandbox semaphore (8) is not blocked for too long.
MAX_LLM_GENERATED_CASES = 8

# Per-test sandbox timeout.
SANDBOX_PER_CASE_TIMEOUT_S = 10.0

# Keywords whose presence in the problem stem or student code marks the problem
# as "too complex for sandbox". When matched we skip sandbox and tell the LLM
# (and the teacher) we did so.
#
# Each keyword is tagged with a short rationale used in the comment metadata
# so the teacher can see WHY we skipped — "tkinter (GUI)" beats just "tkinter".
COMPLEXITY_KEYWORDS: List[Tuple[str, str]] = [
    ("tkinter", "GUI"),
    ("PyQt", "GUI"),
    ("pygame", "GUI"),
    ("matplotlib.pyplot", "图形输出"),
    ("matplotlib", "图形输出"),
    ("socket", "网络"),
    ("requests.", "网络"),
    ("urllib.", "网络"),
    ("urllib2", "网络"),
    ("multiprocessing", "多进程"),
    ("threading", "多线程"),
    ("asyncio", "异步"),
    ("os.fork", "fork"),
    ("subprocess", "子进程"),
    ("os.system", "系统调用"),
    ("tensorflow", "大型 ML 框架"),
    ("torch", "大型 ML 框架"),
    ("pandas.read_csv", "大文件 IO"),
    # NOTE: do NOT blacklist `input(` — the sandbox supports stdin via
    # asyncio.subprocess.communicate, and the vast majority of teacher
    # programming problems use input() to read test data. Adding "input(" here
    # was an early mistake that disabled sandbox for all standard stdin/stdout
    # problems.
]


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _extract_code(answer_content: str) -> str:
    """Extract code from the answer, stripping markdown fences if present."""
    # Match ```python ... ``` or ``` ... ```
    match = re.search(r"```(?:\w+)?\s*\n?(.*?)\n?\s*```", answer_content, re.DOTALL)
    if match:
        return match.group(1).strip()
    return answer_content.strip()


def _detect_language(code: str) -> str:
    """Best-effort language detection. Returns python | cpp | java | unknown.

    Used purely for "should we sandbox this?" gating. False-negatives drop us
    into LLM-only review which is still safe.
    """
    if not code:
        return "unknown"
    sig_python = ("def ", "import ", "print(", "elif ", "lambda ")
    sig_cpp = ("#include", "int main(", "std::", "cout <<")
    sig_java = ("public class ", "System.out.", "public static void main")
    if any(s in code for s in sig_cpp):
        return "cpp"
    if any(s in code for s in sig_java):
        return "java"
    if any(s in code for s in sig_python):
        return "python"
    return "unknown"


_TOP_LEVEL_INPUT_RE = re.compile(r"^[^#\n]*\binput\s*\(", re.MULTILINE)
_TOP_LEVEL_PRINT_RE = re.compile(r"^[^#\n]*\bprint\s*\(", re.MULTILINE)
_TOP_LEVEL_DEF_RE = re.compile(r"^def\s+([A-Za-z_]\w*)\s*\(", re.MULTILINE)


def _detect_function_only(code: str) -> Optional[str]:
    """Detect "student wrote a function definition with no top-level I/O".

    Returns the first top-level function name if so (LeetCode-style), else None.
    Falls back to None when neither input() / print() are present but no def
    either — that's just empty / non-functional code.
    """
    if not code:
        return None
    if _TOP_LEVEL_INPUT_RE.search(code) or _TOP_LEVEL_PRINT_RE.search(code):
        return None
    m = _TOP_LEVEL_DEF_RE.search(code)
    return m.group(1) if m else None


def _scan_complexity_keywords(text: str) -> Optional[Tuple[str, str]]:
    """Return (keyword, rationale) of the first match, or None."""
    if not text:
        return None
    for kw, rationale in COMPLEXITY_KEYWORDS:
        if kw in text:
            return kw, rationale
    return None


async def _generate_test_cases(
    provider: BaseProvider, problem: ProblemInfo, function_name: Optional[str] = None
) -> Tuple[List[TestCase], Optional[str]]:
    """Ask the LLM to generate up to 8 test cases.

    When `function_name` is provided, instructs the LLM to produce LeetCode-
    style function-call cases (function_args + expected_return) instead of
    stdin/stdout — the sandbox will wrap the student's function with a small
    harness. Otherwise stdin/stdout cases as before.

    Returns ``(cases, error_kind)`` where error_kind is:
      - None on success (cases may still be empty if LLM intentionally
        returned 0 cases)
      - "llm_failed_transient" — provider call timed out / 5xx / 429
      - "llm_returned_empty"   — LLM returned a parsed-but-empty list
    Caller uses error_kind to decide whether to surface a network/quota
    error or a "problem too complex" message in the metadata footer.
    """
    if function_name:
        system_prompt = (
            "You are an expert at writing concise unit tests for programming problems "
            "where the student is asked to implement a *function*. The student's code "
            f"defines a top-level function called `{function_name}`. Return at most 8 "
            "test cases. Each case has:\n"
            "  - function_name:    EXACTLY the string above\n"
            "  - function_args:    JSON array of positional arguments to pass\n"
            "  - expected_return:  Python repr() of the expected return value, "
            "                      e.g. '5', '[1, 1, 2]', '\"hello\"'\n"
            "  - description:      ≤ 60 chars label\n"
            "  - source:           ALWAYS \"llm_generated\"\n"
            "  - sandbox_feasible: true unless arguments are huge / non-JSON-able\n\n"
            "Do NOT use input/expected_output fields — leave them empty.\n"
            "Return JSON {\"cases\": [...]}"
        )
        user_prompt = (
            f"Problem (type={problem.type}):\n{problem.stem}\n\n"
            f"Rubric:\n{problem.criterion}\n\n"
            f"Student function name: {function_name}\n"
            "Generate the function-call test cases now."
        )
    else:
        system_prompt = (
            "You are an expert at writing concise stdin/stdout test cases for "
            "programming problems. Return at most 8 test cases. Each case has:\n"
            "  - input:            what the program reads from stdin (use \\n)\n"
            "  - expected_output:  what the program prints to stdout\n"
            "  - description:      ≤ 60 chars label\n"
            "  - source:           ALWAYS \"llm_generated\"\n"
            "  - sandbox_feasible: false ONLY if the test would require GUI, "
            "                      network, big files, or special environment. "
            "                      Otherwise true.\n\n"
            "If the problem is so complex that no simple stdin/stdout test makes "
            "sense (image processing, GUI, web scraping, etc.), return cases all "
            "marked sandbox_feasible=false.\n\n"
            "Return JSON {\"cases\": [...]}"
        )
        user_prompt = (
            f"Problem (type={problem.type}):\n{problem.stem}\n\n"
            f"Rubric:\n{problem.criterion}\n\n"
            "Generate the test cases now."
        )
    try:
        result, _raw = await structured_llm_call(
            provider,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            output_model=TestCaseList,
        )
    except Exception as e:
        logger.warning(f"_generate_test_cases LLM call failed: {e}")
        return [], "llm_failed_transient"

    if not result.cases:
        return [], "llm_returned_empty"

    # Force source="llm_generated" regardless of LLM output.
    cases = [
        TestCase(
            input=tc.input,
            expected_output=tc.expected_output,
            description=tc.description,
            source="llm_generated",
            sandbox_feasible=tc.sandbox_feasible,
            function_name=tc.function_name,
            function_args=tc.function_args,
            expected_return=tc.expected_return,
        )
        for tc in result.cases
    ]
    return cases, None


def _coerce_test_cases(raw: object) -> List[TestCase]:
    """Convert problem.test_cases (which may be list[dict] after JSON round-trip).

    Tasks store the test cases as model_dump()ed dicts inside problem_data,
    so by the time this skill runs they may come back as either TestCase
    instances OR dicts. Normalize to TestCase.
    """
    if not raw:
        return []
    out: List[TestCase] = []
    for item in raw:
        if isinstance(item, TestCase):
            out.append(item)
        elif isinstance(item, dict):
            try:
                out.append(TestCase(**item))
            except Exception as e:
                logger.warning(f"Skipping invalid test case dict {item}: {e}")
    return out


def _format_branch_info(
    cases_source: str,
    skipped_reason: Optional[str],
    exec_report: Optional[ExecutionReport],
    cases_run: int,
    cases_total_seen: int,
) -> str:
    """Build the {branch_info} string injected into the LLM prompt.

    Mirrors the metadata footer shape so the LLM understands what context it
    has and grades accordingly.
    """
    if cases_source in ("teacher", "llm_generated", "llm_function_call") and exec_report is not None:
        if cases_source == "teacher":
            header = "SANDBOX_RAN_TEACHER_CASES"
            note = ""
        elif cases_source == "llm_function_call":
            header = "SANDBOX_RAN_LLM_FUNCTION_CALL_CASES"
            note = "\n  Mode: LeetCode-style — student function called with args, return value compared."
        else:
            header = "SANDBOX_RAN_LLM_CASES"
            note = "\n  Note: cases were AI-generated and may have bugs — weight rubric criteria heavily."
        info = (
            f"{header}\n"
            f"  Pass rate: {exec_report.passed_count}/{exec_report.total_count} "
            f"({exec_report.pass_rate:.0%})\n"
            f"  Cases run: {cases_run}"
            + (f" (out of {cases_total_seen} generated)" if cases_total_seen else "")
            + f"\n  Summary: {exec_report.summary}{note}"
        )
        for r in exec_report.results[:5]:
            label = r.test.description or r.test.function_name or (r.test.input[:50] if r.test.input else "(no input)")
            info += f"\n  - {label!r}: {'PASS' if r.passed else 'FAIL'}"
            if r.error:
                info += f" | err: {r.error[:100]}"
        if cases_source == "llm_generated" and exec_report.total_count >= 3 and exec_report.passed_count == 0:
            info += (
                "\n  ⚠ ZERO_PASS_RATE: with ≥3 AI-generated cases all failing, the cases "
                "themselves may be wrong. Cap your confidence ≤ 0.5 and start your "
                "comment with '测试用例可能不准确，建议人工复核'."
            )
        return info
    if cases_source == "function_only_no_io":
        return (
            "SANDBOX_SKIPPED_FUNCTION_ONLY\n"
            f"  Reason: {skipped_reason or 'student submitted only a function definition'}\n"
            "  No execution attempted — grade on code review + LLM-generated function tests."
        )
    if cases_source == "llm_call_failed":
        return (
            "SANDBOX_LLM_CALL_FAILED\n"
            f"  Reason: {skipped_reason or 'LLM call timed out / rate-limited'}\n"
            "  No execution attempted — grade purely on code review."
        )
    if cases_source == "skipped_complex":
        return (
            f"SANDBOX_SKIPPED_COMPLEX\n"
            f"  Reason: {skipped_reason or 'unknown'}\n"
            "  No execution attempted — grade purely on code review."
        )
    if cases_source == "skipped_lang":
        return (
            f"SANDBOX_SKIPPED_LANGUAGE\n"
            f"  Reason: {skipped_reason or 'non-Python language'}\n"
            "  Sandbox only supports Python — grade purely on code review."
        )
    if cases_source == "no_code":
        return "SANDBOX_NO_CODE\n  Student submitted nothing."
    return f"UNKNOWN_BRANCH ({cases_source})"


def _format_metadata_zh(
    cases_source: str,
    skipped_reason: Optional[str],
    exec_report: Optional[ExecutionReport],
    cases_run: int,
) -> str:
    """Build the human-readable metadata footer appended to the LLM comment."""
    if cases_source == "teacher" and exec_report is not None:
        return (
            f"\n\n（沙箱测评：✓ 教师测试用例 {cases_run} 个，"
            f"通过 {exec_report.passed_count}/{exec_report.total_count}）"
        )
    if cases_source == "llm_generated" and exec_report is not None:
        zero_warn = ""
        if exec_report.total_count >= 3 and exec_report.passed_count == 0:
            zero_warn = " ⚠ 全部未通过，AI 生成的测试用例可能不准确，建议人工复核"
        return (
            f"\n\n（沙箱测评：✓ AI 生成测试用例 {cases_run} 个，"
            f"通过 {exec_report.passed_count}/{exec_report.total_count}{zero_warn}）"
        )
    if cases_source == "llm_function_call" and exec_report is not None:
        return (
            f"\n\n（沙箱测评：✓ LeetCode 风格函数调用测试 {cases_run} 个，"
            f"通过 {exec_report.passed_count}/{exec_report.total_count}）"
        )
    if cases_source == "function_only_no_io":
        return (
            "\n\n（沙箱测评：✗ 学生只提交了函数定义但 AI 未生成函数调用测试 — "
            "本评分基于 AI 代码审查）"
        )
    if cases_source == "llm_call_failed":
        return (
            "\n\n（沙箱测评：✗ AI 生成测试用例时调用失败（网络/限流），"
            "本评分基于 AI 代码审查；建议稍后重新批改本题）"
        )
    if cases_source == "skipped_complex":
        return (
            f"\n\n（沙箱测评：✗ 跳过 — 题目复杂度超出沙箱能力（{skipped_reason or '未知'}）；"
            f"本评分基于 AI 代码审查）"
        )
    if cases_source == "skipped_lang":
        return (
            f"\n\n（沙箱测评：✗ 跳过 — 当前仅支持 Python，{skipped_reason or '检测到非 Python 代码'}；"
            f"本评分基于 AI 代码审查）"
        )
    return ""


# ─── Skill ───────────────────────────────────────────────────────────────────

@register_skill("编程题")
class ProgrammingSkill(GradingSkill):
    name = "ProgrammingSkill"
    problem_type = "编程题"

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
            f"ProgrammingSkill.grade start: q_id={problem.q_id}, "
            f"has_test_cases={problem.test_cases is not None}, "
            f"provider={self.provider.provider_id}"
        )

        active_unit = None
        step_ctx = None
        if self.reporter:
            step_ctx = self.reporter.step(student_id, problem.q_id, self.name, self.provider.provider_id)
            active_unit = await step_ctx.__aenter__()

        try:
            # ─── Step 1: Extract code ────────────────────────────────────────
            if self.reporter and active_unit:
                await self.reporter.substep(active_unit, "extract_code")
            code = _extract_code(answer.content or "")
            if not code:
                return ExpertResult(
                    provider=self.provider.provider_id,
                    score=0.0,
                    max_score=10.0,
                    confidence=1.0,
                    comment="No code provided by student.\n\n（沙箱测评：✗ 学生未提交代码）",
                    steps=[],
                    logs="",
                )

            student_lang = _detect_language(code)
            function_only = _detect_function_only(code) if student_lang == "python" else None

            # ─── Step 2: Decide cases source ────────────────────────────────
            test_cases: List[TestCase] = _coerce_test_cases(problem.test_cases)
            cases_source: str
            #   "teacher" | "llm_generated" | "llm_function_call"
            # | "skipped_complex" | "skipped_lang" | "function_only_no_io"
            # | "llm_call_failed"
            skipped_reason: Optional[str] = None
            cases_total_seen = 0  # distinct from cases_run when LLM-generated > MAX

            if test_cases:
                # Teacher uploaded cases → use them as-is. Filter out any
                # sandbox_feasible=False cases (rare on teacher uploads but
                # honor the field if present).
                feasible = [tc for tc in test_cases if tc.sandbox_feasible]
                if feasible:
                    test_cases = feasible
                    cases_source = "teacher"
                else:
                    cases_source = "skipped_complex"
                    skipped_reason = "teacher cases all marked infeasible"
                    test_cases = []
            else:
                # No teacher cases → keyword scan first
                hit = _scan_complexity_keywords(problem.stem + "\n" + code)
                if hit:
                    cases_source = "skipped_complex"
                    skipped_reason = f"keyword:{hit[0]} ({hit[1]})"
                    test_cases = []
                else:
                    # Generate via LLM. If student wrote a bare function (no
                    # top-level I/O), prompt the LLM for LeetCode-style cases
                    # so the harness can call student's function directly.
                    if self.reporter and active_unit:
                        await self.reporter.substep(active_unit, "generate_test_cases")
                    generated, gen_error = await _generate_test_cases(
                        self.provider, problem, function_name=function_only,
                    )
                    cases_total_seen = len(generated)
                    if gen_error == "llm_failed_transient":
                        cases_source = "llm_call_failed"
                        skipped_reason = "transient LLM error during test-case generation"
                        test_cases = []
                    elif not generated:
                        if function_only:
                            cases_source = "function_only_no_io"
                            skipped_reason = (
                                f"student function {function_only}() has no top-level I/O "
                                "and AI returned no function-call cases"
                            )
                        else:
                            cases_source = "skipped_complex"
                            skipped_reason = "LLM did not return any test cases"
                        test_cases = []
                    else:
                        capped = generated[:MAX_LLM_GENERATED_CASES]
                        feasible = [tc for tc in capped if tc.sandbox_feasible]
                        if not feasible:
                            cases_source = "skipped_complex"
                            skipped_reason = "all LLM-generated cases marked infeasible"
                            test_cases = []
                        else:
                            test_cases = feasible
                            # If we asked for function-call cases AND the LLM
                            # actually used function_name, label as such.
                            if function_only and any(tc.function_name for tc in feasible):
                                cases_source = "llm_function_call"
                                # filter to only function-call cases (drop any
                                # stdin/stdout cases LLM mixed in — they would
                                # fail since student has no print/input)
                                test_cases = [tc for tc in feasible if tc.function_name]
                            elif function_only:
                                # Student wrote function-only but LLM ignored
                                # the hint and returned stdin/stdout cases.
                                # Running them would 0-pass. Fall back to
                                # code-review-only.
                                cases_source = "function_only_no_io"
                                skipped_reason = (
                                    f"student function {function_only}() has no top-level I/O; "
                                    "AI failed to generate function-call cases"
                                )
                                test_cases = []
                            else:
                                cases_source = "llm_generated"

            # Language gate (overrides everything except no_code)
            if student_lang != "python":
                cases_source = "skipped_lang"
                skipped_reason = f"detected language: {student_lang}"
                test_cases = []

            # ─── Step 3: Run sandbox if applicable ──────────────────────────
            exec_report: Optional[ExecutionReport] = None
            if cases_source in ("teacher", "llm_generated", "llm_function_call") and test_cases:
                if self.reporter and active_unit:
                    await self.reporter.substep(active_unit, "run_sandbox")
                exec_report = await run_sandbox(
                    code,
                    test_cases,
                    language="python",
                    per_case_timeout=SANDBOX_PER_CASE_TIMEOUT_S,
                )

            # ─── Step 4: Build prompt ───────────────────────────────────────
            if self.reporter and active_unit:
                await self.reporter.substep(active_unit, "build_prompt")
            branch_info = _format_branch_info(
                cases_source, skipped_reason, exec_report,
                cases_run=len(test_cases),
                cases_total_seen=cases_total_seen,
            )
            prompt = self._template
            prompt = prompt.replace("{problem}", problem.stem)
            prompt = prompt.replace("{code}", code)
            prompt = prompt.replace("{branch_info}", branch_info)
            # Backwards compat with legacy template that used {execution_results}
            prompt = prompt.replace("{execution_results}", branch_info)
            prompt = prompt.replace("{rubric}", problem.criterion)

            # ─── Step 5: LLM grading ────────────────────────────────────────
            if self.reporter and active_unit:
                await self.reporter.substep(active_unit, "llm_grade")

            system_prompt = build_system_prompt(
                "You are a programming teacher grading student code. "
                "Walk through the 4-step reasoning workflow (Functionality → Edge cases → "
                "Code quality → Efficiency) and produce a structured per-dimension score. "
                "Respect the sandbox branch rules in the user prompt.",
                self.language,
            )
            result, raw = await structured_llm_call(
                self.provider,
                system_prompt=system_prompt,
                user_prompt=prompt,
                output_model=ProgrammingGradingOutput,
            )

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
                cases_source, skipped_reason, exec_report, len(test_cases)
            )
            final_comment = (result.comment or "") + metadata_footer

            # Cap confidence when AI-generated cases all failed — this is the
            # signal that LLM-as-judge had a bad map of expected behavior, so
            # human review is needed even if the LLM itself sounded confident.
            confidence = max(0.0, min(result.confidence, 1.0))
            if (
                cases_source == "llm_generated"
                and exec_report is not None
                and exec_report.total_count >= 3
                and exec_report.passed_count == 0
            ):
                confidence = min(confidence, 0.5)

            return ExpertResult(
                provider=self.provider.provider_id,
                score=max(0.0, min(result.score, result.max_score)),
                max_score=result.max_score,
                confidence=confidence,
                comment=final_comment,
                steps=step_scores,
                logs=result.logs,
                raw_output=raw.content,
                duration_ms=raw.duration_ms,
            )

        except Exception as e:
            logger.error(f"ProgrammingSkill failed: {e}", exc_info=True)
            return self._blank_result(problem.q_id, 10.0, f"Programming grading failed: {e}")
        finally:
            if step_ctx:
                await step_ctx.__aexit__(None, None, None)
