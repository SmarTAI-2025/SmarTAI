"""One question-level execution, reused for every answer and grading retry."""

from contextlib import contextmanager
from contextvars import ContextVar
import re

from backend.config import settings
from backend.domain.code_output import CodeOutputQuestion, bind_code_output, code_output_data, stem_digest
from backend.models import Correction, StepScore, TestCase
from backend.services.programming_runs import QUESTION_OUTPUT, _validate_report, digest, runtime_identity
from backend.tools.code_interpreter import HostCodeExecutionDisabled, run_sandbox
from backend.tools.oci_grading_runner import IsolatedRunnerUnavailable

UNAVAILABLE = "code_output_unavailable"
_OUTPUT_LINE = re.compile(r"^[ \t]*(?:output|stdout|输出)[：:][ \t]?(.*)$", re.I | re.M)
_FENCED_ANSWER = re.compile(r"\A```[^\r\n]*\r?\n(.*?)^```[ \t]*(?=\r?$)", re.M | re.S)
_result_step = ContextVar("code_output_result_step", default=None)


class CodeOutputUnavailable(ValueError):
    pass


def _question_key(state, problem, descriptor):
    question = state.checkpoints.questions.get(problem.q_id) if state.checkpoints else None
    return digest({
        "kind": QUESTION_OUTPUT, "question": question.id if question else problem.q_id,
        "descriptor": descriptor.model_dump(mode="json"), "runtime": runtime_identity(),
    })


async def observed_output(state, problem, reporter):
    descriptor = CodeOutputQuestion.model_validate(code_output_data(problem))
    source = descriptor.source
    if (source is None or descriptor.stem_sha256 != stem_digest(problem.stem)
            or settings.program_runner_mode != "oci"):
        raise CodeOutputUnavailable()
    if bind_code_output(source, stem=problem.stem, document=problem.stem) != descriptor:
        raise CodeOutputUnavailable()
    key = _question_key(state, problem, descriptor)
    async with state.lock(key):
        saved = state.read(key, QUESTION_OUTPUT)
        if saved is not None:
            return source, saved["stdout"]
        if key in state.failures:
            raise CodeOutputUnavailable()
        if reporter:
            from backend.services.math_grading_context import report_math_progress

            await report_math_progress(reporter._emit_message, f"{problem.q_id}: verify_code_output")
        case = TestCase(input=source.stdin, expected_output="")
        try:
            report = await run_sandbox(source.code, [case], language=source.language)
            _validate_report(report, [case])
            row = report.results[0]
            # The comparison probe gives wrong_answer for normal nonempty
            # stdout. Only the successful process's stdout is evidence.
            if (row.isolation_mode != "oci_container" or row.stdout_truncated or row.stderr_truncated
                    or row.exit_reason not in {"success", "wrong_answer"}):
                raise CodeOutputUnavailable()
        except (IsolatedRunnerUnavailable, HostCodeExecutionDisabled, ValueError, TimeoutError):
            state.failures[key] = UNAVAILABLE
            raise CodeOutputUnavailable() from None
        state.store(QUESTION_OUTPUT, key, {
            "q_id": problem.q_id, "stdout": row.stdout,
            "execution_id": row.execution_id, "request_digest": row.request_digest,
            "isolation_mode": row.isolation_mode, "duration_ms": row.duration_ms,
        })
        return source, row.stdout


def submitted_output(text, source):
    text = text.strip()
    if source.form == "choice":
        match = re.fullmatch(r"(?:(?:answer|option|答案|选项)[：:]\s*)?(?:[（(]\s*)?([A-Z])(?:\s*[）)])?[.、]?",
                             text, re.I)
        return source.choices.get(match[1].upper()) if match else None
    fenced = _FENCED_ANSWER.match(text)
    if fenced and (source.form == "explanation" or not text[fenced.end():].strip()):
        return fenced[1]
    if source.form == "explanation":
        matches = _OUTPUT_LINE.findall(text)
        if len(matches) > 1 or (not matches and "\n" in text):
            raise CodeOutputUnavailable()
        if matches:
            text = matches[0].rstrip("\r")
    return text[1:-1] if re.fullmatch(r"`[^`]*`", text) else text


def output_feedback(correct, language):
    if language == "zh":
        return "作答与程序实际输出一致。" if correct else "作答与程序实际输出不一致。"
    return "Your answer matches the program output." if correct else "Your answer does not match the program output."


def unavailable_output(problem, language):
    return Correction(q_id=problem.q_id, type=problem.type, max_score=problem.max_score,
                      score=0, confidence=None, steps=[], synthesis_method=UNAVAILABLE,
                      requires_human_review=True, review_reasons=[UNAVAILABLE],
                      comment=("无法核实程序输出或对应得分，请核对题面、作答和评分标准后人工评分。"
                               if language == "zh" else
                               "The program output or its score could not be verified. Review the question, answer, and rubric to grade it."))


def output_only_correction(problem, correct, language):
    comment = output_feedback(correct, language)
    score = problem.max_score if correct else 0
    return Correction(q_id=problem.q_id, type=problem.type, score=score, max_score=problem.max_score,
                      confidence=1, comment=comment, steps=[StepScore(step_no=1, score=score,
                      is_correct=correct, desc=comment)], synthesis_method="code_output_verified")


@contextmanager
def output_step(plan, answer, *, correct, language):
    """Bind one independent result step without letting a model allocate points."""
    rules = plan.part_rubric
    if rules is None or len(rules.parts) != 1 or rules.nonblank_floor.resolve(rules.total_pts):
        raise CodeOutputUnavailable()
    part, = rules.parts
    steps = [step for step in part.steps if step.code_output]
    if (len(part.steps) < 2 or len(steps) != 1 or part.deductions
            or steps[0].required_conditions or steps[0].follow_through_pts
            or part.alternative.source_quote or part.alternative.field_sources):
        raise CodeOutputUnavailable()
    from backend.services.math_source_evidence import source_catalog

    # Quote the actual answer, never the expected output or a generated value.
    catalog = source_catalog(answer.content, "", "")
    output_lines = [line for line, (value, _, _) in catalog.items() if _OUTPUT_LINE.match(value)]
    output_block = _FENCED_ANSWER.match(answer.content.strip())
    if output_block:
        payload = output_block[1].splitlines()
        output_lines = [key for key, (line, _, _) in catalog.items() if line in payload]
    evidence = output_lines[0] if output_lines else next(iter(catalog), "")
    token = _result_step.set((plan.plan_id, part.id, steps[0].id, correct, evidence,
                              output_feedback(correct, language)))
    try:
        yield
    finally:
        _result_step.reset(token)


def apply_output_step(assessment, plan):
    """Save the server verdict in the canonical ledger used for history replay."""
    result = _result_step.get()
    if result is None or result[0] != plan.plan_id:
        return
    from backend.skills.part_scoring import StepDecision

    _, part_id, step_id, correct, evidence, feedback = result
    if len(assessment.part_decisions) != 1 or assessment.part_decisions[0].part_id != part_id:
        return  # The shared ledger validator will reject the malformed assessment.
    decision, = assessment.part_decisions
    score_step = StepDecision(id=step_id, outcome="earned" if correct else "missed",
                              evidence=evidence, reason=feedback)
    part, = plan.part_rubric.parts
    if decision.method == "alternative":
        raise ValueError("Judge the explanation as tracing the given program, using the ordinary point steps.")
    if decision.relevance in {"incorrect", "unrelated", "unanswered", "uncertain"}:
        if not correct:
            # Keep the process explanation and the authoritative result feedback
            # for a zero-credit answer.
            assessment.part_decisions = [decision.model_copy(update={
                "steps": (*[step for step in decision.steps if step.id != step_id], score_step),
            })]
            return
        prior = {step.id: step for step in decision.steps if step.outcome == "missed"}
        remaining = [prior.get(step.id) or StepDecision(id=step.id, outcome="missed", evidence="",
                     reason=decision.progress) for step in part.steps if step.id != step_id]
        assessment.part_decisions = [decision.model_copy(update={
            "relevance": "partial", "method": "reference", "student_quote": evidence, "errors": (),
            "steps": (*remaining, score_step),
        })]
        return
    final_steps = (*[step for step in decision.steps if step.id != step_id], score_step)
    full = (len(final_steps) == len(part.steps)
            and all(step.outcome == "earned" for step in final_steps)
            and all(all(checks) for checks in decision.condition_results.values()))
    assessment.part_decisions = [decision.model_copy(update={
        "steps": final_steps, "relevance": "complete" if full else "partial",
    })]


def output_step_instruction(plan):
    active = _result_step.get()
    if active is None or active[0] != plan.plan_id:
        return ""
    return (
        f"\nThe server grades output step {active[2]} by actual execution. "
        f"{active[-1]} Grade the student's explanation independently using the other steps. "
        "Trace the given program and use the ordinary point steps (method=reference or minor_variant). "
        "Leave the output step out of your ledger and do not discuss output correctness in your comment."
    )
