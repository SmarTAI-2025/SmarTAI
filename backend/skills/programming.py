"""Rubric-based programming scoring over shared, question-scoped execution evidence."""
from __future__ import annotations

import json
import logging
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from backend.llm.providers import BaseProvider
from backend.models import ExpertResult, GradingConfidence, ProblemInfo, StepScore, StudentAnswerInfo, TestCase
from backend.services.grading_scores import round_ai_score
from backend.services.programming_reference_inputs import NewTestInput
from backend.services.programming_test_contract import has_single_required_argument, required_function_name
from backend.skills.base import (
    GradingSkill, build_system_prompt, classify_skill_error,
    format_deterministic_feedback, normalize_expert_result, normalize_score_scale, register_skill,
)
from backend.skills.programming_evidence import describe_run, mark_teacher_case_score_review, unavailable, ungraded_message
from backend.tools import knowledge as kb_tool
from backend.tools.code_interpreter import run_sandbox
from backend.tools.structured_llm import (
    PermanentLLMError, StructuredOutputBoundsError, StructuredOutputInvalidError,
    TransientLLMError, structured_llm_call,
)

logger = logging.getLogger(__name__)
_TEMPLATE = (Path(__file__).resolve().parents[1] / "prompts" / "programming.txt").read_text(encoding="utf-8")


def _sum_step_awards(validated):
    return float(sum((Decimal(str(step.score)) for step in validated.get("steps", ())), Decimal(0)))


class ProgrammingAward(StepScore):
    # Optional evidence is validated separately so a malformed citation cannot discard a valid score.
    deduction_basis: object = None
    max_points: float | None = Field(default=None, gt=0, allow_inf_nan=False)


class ProgrammingGradingOutput(BaseModel):
    steps: list[ProgrammingAward]
    # Keep explicitly supplied legacy totals subject to the consistency check.
    score: float = Field(default_factory=_sum_step_awards, ge=0, allow_inf_nan=False, validate_default=True)
    max_score: float = Field(default=10.0, gt=0, allow_inf_nan=False)
    confidence: GradingConfidence = Field(default=0.0, ge=0, le=1, allow_inf_nan=False)
    comment: str = ""
    logs: str | None = ""
    boundary_case: object = None  # Optional diagnostic; malformed proposals never invalidate a score.


MAX_LLM_GENERATED_CASES = 8


class TestCaseList(BaseModel):
    cases: list[TestCase] = Field(default_factory=list, max_length=MAX_LLM_GENERATED_CASES)
    inputs: list[NewTestInput] | None = Field(default=None, max_length=MAX_LLM_GENERATED_CASES)
    single_arguments: list[object] | None = Field(default=None, max_length=MAX_LLM_GENERATED_CASES)

    @model_validator(mode="after")
    def one_collection(self):
        if len(self.model_fields_set & {"cases", "inputs", "single_arguments"}) != 1:
            raise ValueError("Supply one collection of tests.")
        return self

    @model_validator(mode="before")
    @classmethod
    def accept_case_list(cls, value):
        value = {"cases": value} if isinstance(value, list) else value
        if isinstance(value, dict):
            for field in ("inputs", "single_arguments"):
                draft = value.get(field)
                if isinstance(draft, list) and MAX_LLM_GENERATED_CASES < len(draft) <= 2 * MAX_LLM_GENERATED_CASES:
                    # A modest overrun in an *unfrozen input proposal* is a format
                    # mistake eligible for one repair. Preserve the hard bound for
                    # excessive responses; never truncate or execute an oversized suite.
                    raise ValueError("Propose no more than eight legal inputs.")
        return value


# Hints in the frozen question for workloads outside the configured sandbox.
#
# Each keyword is tagged with a short rationale used in the comment metadata
# so the teacher can see WHY we skipped — "tkinter (GUI)" beats just "tkinter".
COMPLEXITY_KEYWORDS: list[tuple[str, str]] = [
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


def _detect_language(code: str) -> str:
    """Legacy language hints used by programming run preparation."""
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


def _scan_complexity_keywords(text: str) -> tuple[str, str] | None:
    """Return (keyword, rationale) of the first match, or None."""
    if not text:
        return None
    for kw, rationale in COMPLEXITY_KEYWORDS:
        if kw in text:
            return kw, rationale
    return None


def _format_error(message):
    error = StructuredOutputInvalidError(message)
    error.repair_context = message  # Fixed contract description, never raw student or provider text.
    return error


def _validate_awards(result, maximum):
    if "score" not in result.model_fields_set or any(step.max_points is not None for step in result.steps):
        if "max_score" not in result.model_fields_set or result.max_score != maximum:
            raise _format_error("Use the question's stated maximum for the rubric.")
        for step in result.steps:
            if (step.max_points is None or step.score > step.max_points
                    or step.is_correct != (step.score == step.max_points)):
                raise _format_error("Distinguish available points from earned points. Mark full credit correct; partial or zero credit is not full credit.")
        if sum(Decimal(str(step.max_points)) for step in result.steps) != Decimal(str(maximum)):
            raise _format_error("The available points across distinct rubric criteria must add up to the question maximum.")
    elif "comment" not in result.model_fields_set:
        raise _format_error("Include a comment or explain each award with its available points.")
    total = Decimal(str(result.score))
    steps = sum((Decimal(str(step.score)) for step in result.steps), Decimal(0))
    difference = abs(steps - total)
    if len({step.step_no for step in result.steps}) != len(result.steps):
        raise _format_error("Use each rubric step number only once.")
    normalized_total, normalized_steps = normalize_score_scale(
        score=result.score, reported_max_score=result.max_score, authoritative_max_score=maximum, steps=result.steps,
    )
    normalized_gap = abs(sum(step.score for step in normalized_steps) - normalized_total)
    if (not result.steps or normalized_gap > 0.010000000001 or difference > Decimal("0.01")
            or difference * Decimal(str(maximum)) / Decimal(str(result.max_score)) > Decimal("0.01")):
        raise _format_error("Return rubric awards whose scores sum to the total within 0.01 points.")


def _error_kind(exc):
    from backend.services.background_errors import classify_background_error
    from backend.skills.math_grading import _critical_failure
    if _critical_failure(exc):
        raise exc
    if isinstance(exc, (StructuredOutputInvalidError, StructuredOutputBoundsError)):
        return "parse_failed"
    if str(exc) in {"provider_content_blocked", "provider_recitation_blocked"}:
        return str(exc)
    code = classify_background_error(exc, "workflow_failed")
    kinds = {
        "provider_zero_quota": "zero_quota", "provider_daily_quota_exceeded": "daily_quota_exhausted",
        "provider_rate_limited": "quota_exhausted", "provider_response_invalid": "parse_failed",
        "provider_timeout": "transient_llm", "provider_upstream_unavailable": "transient_llm",
        "provider_unreachable": "transient_llm",
    }
    if code != "workflow_failed":
        return kinds.get(code, code)
    if isinstance(exc, PermanentLLMError):
        return "provider_request_rejected"
    if isinstance(exc, TransientLLMError) and exc.__cause__ is None:
        return "transient_llm"
    # The retry wrapper treats unknown errors as transient. Only propagate
    # that classification when the actual exception carries transport evidence.
    return classify_skill_error(exc)[0]


def _teacher_material(problem, *, for_generation=False):
    material = {
        "problem": problem.stem, "rubric": problem.criterion, "max_score": problem.max_score,
        "reference_answer": problem.reference_answer, "solution_code": problem.solution_code,
    }
    presentation = (problem._math_rubric_source or {}).get("presentation") or {}
    generated = presentation.get("ai_completion_provenance") or {}
    if for_generation:
        # A generated solution cannot serve as an independent answer oracle.
        for field in ("reference_answer", "solution_code"):
            if field in generated:
                material.pop(field)
    derived = sorted(set(material).intersection(generated))
    if derived:
        material["ai_generated_fields"] = derived
    return material


async def _generate_test_cases(
    provider: BaseProvider, problem: ProblemInfo, function_name: str | None = None,
) -> tuple[list[TestCase], str | None]:
    """Generate only from the frozen question. The shared caller owns locking and storage."""
    entry = function_name or required_function_name(problem.stem)
    system = (
        "Create at most eight concise programming test cases from the frozen teacher material. "
        "Cover representative inputs, boundaries and likely mistakes within the stated input domain. "
        "Preserve teacher expectations, use the independent reference to determine expected values, "
        "and never invent a new requirement. Return a JSON object with one key, cases, containing "
        "the list of tests. If none can be derived, return {\"cases\":[]}. Use quoted JSON strings "
        "for expected_output or expected_return. Supply only data, never harness code. "
        "Use stdin/input and expected_output for programs; explicitly set expected_output even when empty. "
        "For function problems use io_mode=function, function_args (positional JSON array), "
        "function_kwargs for keyword arguments when needed, and expected_return as Python repr text. "
        "In function_args each slot is one positional argument. One list argument [2, 5] is "
        "encoded as [[2, 5]], one empty list as [[]], and two scalar arguments as [2, 5]. "
        "Keep fixed-shape tests within the problem domain. "
        "Use the specified function_name when the teacher requires one. When no name is specified, "
        "leave the name unset; do not impose an identifier. Mark infeasible tests sandbox_feasible=false."
    )
    from backend.services.programming_runs import _reference
    reference_available = bool(_reference(problem))
    if reference_available:
        interface = ('Use {"args":[...]} for positional function arguments, plus optional kwargs. '
                     if entry else 'For a function use {"args":[...]}; for a complete program use {"input":"literal stdin"}. ')
        envelope = '{"inputs":[...]}'
        if has_single_required_argument(problem.stem):
            envelope = '{"single_arguments":[...]}'
            interface = ('Each element is the actual value of the single parameter, in its real JSON type. '
                         'A list value is a JSON array, an integer is a JSON number, and text is a JSON string. '
                         'Do not wrap these values in objects, strings encoding arrays, or an extra argument vector. ')
        system = (
            'Choose up to eight distinct, legal test inputs from the frozen question and teacher rubric. '
            'Include ordinary and boundary cases that can expose plausible errors. '
            'Return exactly one JSON object ' + envelope + '. ' + interface +
            'Preserve spaces and line endings inside literal input. Do not predict or supply output: '
            'the grader runs the independently provided reference to determine the expected values. '
            'Never use student code, modify the problem domain, or generate a script.'
        )
    prompt = json.dumps({**_teacher_material(problem, for_generation=True), "required_function_name": entry}, ensure_ascii=False)

    original_inputs = []
    invalid_positions = []

    def checked_new_inputs(drafts, adapted, input_rows):
        from backend.services import programming_runs as runs
        from backend.services.programming_input_domain import input_domain
        from backend.services.programming_reference_inputs import nearby_legal_integer_inputs

        state = runs.current_programming_run()
        if state is None or problem.test_cases:
            return drafts
        domains = [input_domain(problem, case) for case in adapted]
        if original_inputs:
            if len(drafts) != len(invalid_positions) or not all(domain == "allowed" for domain in domains):
                raise _format_error("Supply the requested number of verified legal replacements.")
            revised = original_inputs.copy()
            for index, replacement in zip(invalid_positions, drafts, strict=True):
                revised[index] = replacement
            if len({runs.digest(c.model_dump(mode="json")) for c in revised}) != len(revised):
                raise _format_error("Keep replacement inputs distinct from all retained cases.")
            return revised

        if "outside" not in domains or "unknown" in domains:
            return drafts
        # Only replace provably illegal, freshly proposed inputs, before any
        # student runs. The original, including difficult legal cases, is retained.
        record = [c.model_dump(mode="json", exclude_unset=True) for c in drafts]
        if len(json.dumps(record, ensure_ascii=False).encode()) > 16_384:
            return drafts
        original_inputs.extend(drafts)
        invalid_positions.extend(i for i, domain in enumerate(domains) if domain == "outside")
        key = runs.digest(["generated_input_repair_v1", runs._question_key(state, problem)])
        state.store(runs.AUXILIARY, key, {
            "q_id": problem.q_id, "original_inputs": record, "domains": domains, "status": "proposed",
        })
        neighbors = nearby_legal_integer_inputs(problem, drafts, adapted, domains)
        if neighbors is not None:
            return neighbors
        retained = json.dumps([row.model_dump(exclude_unset=True) for row, domain in
                               zip(input_rows, domains, strict=True) if domain == "allowed"])
        exclusions = (" Already retained (JSON input data): " + retained) if len(retained) <= 1000 else ""
        raise _format_error(f"Return only {len(invalid_positions)} distinct replacement inputs in the "
                            "original input-only JSON format. Use legal inputs from the task domain. "
                            "The grader retains the other inputs and computes the expected outputs." + exclusions)

    def validate(generated):
        from backend.services.programming_test_contract import ProgrammingCaseError, adapt_programming_cases
        input_rows = generated.inputs
        if generated.single_arguments is not None:
            if not has_single_required_argument(problem.stem):
                raise _format_error("This question requires a full argument vector or a literal stdin input.")
            input_rows = [NewTestInput(args=[value]) for value in generated.single_arguments]
        if input_rows is not None:
            if not reference_available:
                raise _format_error("Supply explicit expectations when there is no independent reference program.")
            drafts = [item.draft() for item in input_rows]
            try:
                adapted = adapt_programming_cases(problem.stem, [draft.comparison_probe() for draft in drafts])
            except ProgrammingCaseError as exc:
                raise _format_error("Use exactly one complete value per function argument. For a single list parameter, put the list inside args.") from exc
            generated.cases = checked_new_inputs(drafts, adapted, input_rows)
            return
        if original_inputs:
            raise _format_error("An input-only repair cannot assign expected outputs.")
        if entry and any(c.function_name and c.function_name != entry for c in generated.cases):
            raise _format_error("Use the frozen required_function_name for every function test.")
        try:
            # Feed structural mistakes back to the existing single format repair
            # before the shared class preparation is frozen. Never alter an expectation.
            generated.cases = adapt_programming_cases(problem.stem, [
                case.model_copy(update={"source": "llm_generated"}) for case in generated.cases
            ])
        except ProgrammingCaseError as exc:
            raise _format_error(
                "The test inputs, expected values, or argument shapes do not match the question. "
                "Supply one function_args slot per parameter, with a list inside that slot for a list input. "
                "Use a textual expected_return for functions or an explicit expected_output for stdin."
            ) from exc

    try:
        result, _ = await structured_llm_call(
            provider, system_prompt=system, user_prompt=prompt,
            output_model=TestCaseList, validate_output=validate,
        )
    except Exception as exc:
        logger.warning("Programming test generation failed; exception_type=%s", type(exc).__name__)
        kind = _error_kind(exc)
        if original_inputs and kind == "parse_failed":
            return original_inputs, None  # Preparation retains and explains the unresolved domain conflict.
        return [], kind
    if not result.cases:
        return [], "programming_no_cases"
    return [case.model_copy(update={"source": "llm_generated"}) for case in result.cases], None


@register_skill("编程题")
class ProgrammingSkill(GradingSkill):
    name = "ProgrammingSkill"
    problem_type = "编程题"

    async def grade(self, problem: ProblemInfo, answer: StudentAnswerInfo, *, student_id: str = "") -> ExpertResult:
        from contextlib import nullcontext
        from backend.services.programming_runs import evidence_for_answer

        context = (self.reporter.step(student_id, problem.q_id, self.name, self.provider.provider_id)
                   if self.reporter else nullcontext())
        evidence = None
        async with context as unit:
            try:
                if self.reporter and unit:
                    await self.reporter.substep(unit, "prepare_execution_evidence")
                run = await evidence_for_answer(
                    problem=problem, answer=answer, student_id=student_id, task_id=self.task_id,
                    provider=self.provider, reporter=self.reporter, generate=_generate_test_cases, execute=run_sandbox,
                )
                evidence, examples = describe_run(run, self.language, problem)
                if evidence.score_status == "unavailable":
                    result = self._blank_result(problem.q_id, problem.max_score,
                                                evidence.safe_summary, error_kind=evidence.primary_reason)
                    result.programming_evidence = evidence
                    return result
                if run.status == "no_submission":
                    return ExpertResult(
                        provider=self.provider.provider_id, score=0, max_score=problem.max_score, confidence=1,
                        comment=format_deterministic_feedback(
                            self.grading_setup, zh_message="学生未提交代码，本题记 0 分。",
                            en_message="No student code was provided, so this item receives 0 points.",
                            zh_detail="未运行沙箱或模型批改。", en_detail="No sandbox or model grading was run.",
                            zh_suggestion="请补交代码后重新批改。", en_suggestion="Submit the missing code, then grade again.",
                            legacy_message=evidence.safe_summary,
                        ), programming_evidence=evidence,
                    )

                if self.reporter and unit:
                    await self.reporter.substep(unit, "retrieve_knowledge")
                chunks = await kb_tool.retrieve_for_grading(
                    problem.stem, k=3, scope=self.task_id, provider=self.provider, reporter=self.reporter,
                )
                data = {**_teacher_material(problem), "submitted_source": run.code,
                        "execution": evidence.model_dump(exclude={"evidence_id"}), "case_examples": examples}
                if chunks:
                    data["textbook_reference"] = kb_tool.context_text(chunks)
                if self.reporter and unit:
                    await self.reporter.substep(unit, "llm_grade")
                result, raw = await structured_llm_call(
                    self.provider, system_prompt=build_system_prompt(
                        _TEMPLATE + f"\nThe authoritative maximum for this question is {problem.max_score:g}. "
                        "Set max_score to that maximum. All step awards must fit within it.",
                        self.language, self.grading_setup,
                    ),
                    user_prompt=json.dumps(data, ensure_ascii=False),
                    output_model=ProgrammingGradingOutput,
                    validate_output=lambda output: _validate_awards(output, problem.max_score),
                )
                confidence = result.confidence
                if (not evidence.teacher_tests.total and evidence.generated_tests.total >= 3
                        and evidence.generated_tests.passed == 0):
                    confidence = min(confidence, 0.5)
                # A second, freely generated summary can contradict a validated
                # award and the executed cases. Present the same rubric judgments
                # in the summary and detail views when the provider supplied them.
                comment = result.comment
                if isinstance(result, ProgrammingGradingOutput) and all(step.max_points is not None for step in result.steps):
                    deductions = [step.desc for step in result.steps if not step.is_correct]
                    if deductions:
                        comment = "\n".join(deductions)
                    else:
                        # This summary is derived from validated awards, so it
                        # must apply feedback preferences after replacing the
                        # model's independently generated overall comment.
                        detail = "\n".join(
                            f"{step.desc} ({step.score:g}/{step.max_points:g})"
                            for step in result.steps
                        )
                        comment = format_deterministic_feedback(
                            self.grading_setup, complete=True,
                            zh_message="按本题评分标准，各项均获满分。",
                            en_message="All rubric criteria received full credit.",
                            zh_detail=detail, en_detail=detail,
                            legacy_message=("按本题评分标准，各项均获满分。" if self.language == "zh"
                                            else "All rubric criteria received full credit."),
                        )
                from backend.services.programming_boundary_checks import boundary_feedback, verified_boundary
                boundary = await verified_boundary(problem, run, evidence, getattr(result, "boundary_case", None), run_sandbox, self.reporter)
                if boundary:
                    evidence.boundary_note = boundary_feedback(boundary, self.language)
                    evidence.safe_summary += "\n" + evidence.boundary_note
                vote = normalize_expert_result(ExpertResult(
                    provider=self.provider.provider_id, score=result.score, max_score=result.max_score,
                    confidence=confidence, comment=f"{comment}\n\n{evidence.safe_summary}",
                    steps=result.steps, raw_output=raw.content, duration_ms=raw.duration_ms,
                    programming_evidence=evidence, knowledge_citations=kb_tool.citations(chunks),
                ), problem.max_score)
                from backend.services.programming_partial_review import record_deduction_basis
                record_deduction_basis(vote, result.steps, run, problem)
                mark_teacher_case_score_review(vote, self.language)
                if evidence.execution_status == "unsupported" and round_ai_score(vote.score, vote.max_score) == 0:
                    # Without a run, code review alone cannot establish a publishable student zero.
                    vote.error_kind = "programming_unverified_zero"
                    vote.confidence = 0
                    vote.steps = []
                    evidence.score_status = "unavailable"
                    evidence.primary_reason = vote.error_kind
                    evidence.safe_summary = ("未运行，无法确认学生零分，请人工复核。" if self.language == "zh"
                                             else "Ungraded. No execution supports a zero score; teacher review required.")
                    vote.programming_evidence = evidence
                    vote.comment = evidence.safe_summary
                return vote
            except Exception as exc:
                logger.warning("Programming grading failed; exception_type=%s", type(exc).__name__)
                kind = _error_kind(exc)
                if evidence is None:
                    evidence = unavailable(kind, "", self.language)
                else:
                    evidence = evidence.model_copy(update={
                        "score_status": "unavailable", "primary_reason": kind, "requires_human_review": True,
                    })
                    evidence.safe_summary = f"{ungraded_message(kind, self.language)} {evidence.safe_summary}"
                result = self._blank_result(problem.q_id, problem.max_score, evidence.safe_summary, error_kind=kind)
                result.programming_evidence = evidence
                return result
