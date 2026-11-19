"""One question-level execution, reused for every answer and grading retry."""

from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal
import re

from backend.config import settings
from backend.domain.code_output import (
    CodeOutputQuestion, bind_code_output, blank_count, choice_key, choice_output_items, code_output_data,
    independent_output_steps, multiple_choice, ordered_blank_steps, selection_awards, stem_digest,
)
from backend.models import Correction, StepScore, TestCase
from backend.services.code_output_answers import (
    CodeOutputUnavailable, submitted_choices, submitted_stdout,
)
from backend.services.programming_runs import QUESTION_OUTPUT, _validate_report, digest, runtime_identity
from backend.tools.code_interpreter import HostCodeExecutionDisabled, compare_stdout, run_sandbox
from backend.tools.oci_grading_runner import IsolatedRunnerUnavailable

UNAVAILABLE = "code_output_unavailable"
_result_step = ContextVar("code_output_result_step", default=None)


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


def _output_correction(problem, awards, language, *, part=None):
    # Keep the same precision and item IDs as the frozen score rows.
    steps = [
        StepScore(step_no=index, part_id=part.id if part else None,
                  item_id=part.steps[index - 1].id if part else None, score=score,
                  is_correct=correct, desc=description or output_feedback(correct, language))
        for index, (score, correct, description) in enumerate(awards, 1)
    ]
    total = float(sum(Decimal(str(step.score)) for step in steps))
    complete = all(step.is_correct for step in steps)
    if len(steps) == 1:
        comment = steps[0].desc
    elif 0 < total < problem.max_score:
        comment = "按评分标准给予部分分。" if language == "zh" else "Partial credit was awarded under the rubric."
    else:
        comment = output_feedback(complete, language)
    return Correction(q_id=problem.q_id, type=problem.type, score=total, max_score=problem.max_score,
                      confidence=1, comment=comment, steps=steps, synthesis_method="code_output_verified")


def _frozen_rules(problem):
    from backend.services.math_grading_context import get_math_plan
    from backend.services.rubric_drafts import saved_draft

    plan = get_math_plan(problem)
    if plan is None and (draft := saved_draft(problem)) is not None:
        plan = draft.plan
    return plan.part_rubric if plan else None


def _require_unambiguous_legacy_rule(problem):
    from backend.services.binary_scoring import whole_question_policy
    from backend.services.math_rubric_plan import _scoring_amounts

    if _scoring_amounts(problem.criterion) and not whole_question_policy(problem.criterion, problem.max_score):
        raise CodeOutputUnavailable()


def grade_blanks(problem, source, stdout, text, language):
    try:
        count = blank_count(problem.stem, source.input_quote)
        rules = _frozen_rules(problem)
        part = ordered_blank_steps(rules, count) if rules is not None or count != 1 else None
        if part is None:
            _require_unambiguous_legacy_rule(problem)
    except ValueError as exc:
        raise CodeOutputUnavailable() from exc
    if count == 1:
        correct = bool(text.strip()) and compare_stdout(stdout, submitted_stdout(text))
        return _output_correction(problem, [(problem.max_score if correct else 0, correct, "")], language, part=part)

    expected = stdout.split()
    provided = submitted_stdout(text).split() if text.strip() else [""] * count
    if len(expected) != count or len(provided) != count:
        raise CodeOutputUnavailable()
    awards = []
    for index, (value, target, step) in enumerate(zip(provided, expected, part.steps), 1):
        correct = compare_stdout(target, submitted_stdout(value))
        description = (f"第 {index} 空。" if language == "zh" else f"Blank {index}. ")
        awards.append((step.total_pts if correct else 0, correct, description + output_feedback(correct, language)))
    return _output_correction(problem, awards, language, part=part)


def grade_choices(problem, source, stdout, text, language):
    multiple = multiple_choice(problem.stem)
    outputs = choice_output_items(problem.stem, stdout) if multiple else [stdout]
    # Option identity comes from printed text. Numeric aliases apply only
    # when interpreting the student's written selection.
    expected = {choice_key(key) for key, value in source.choices.items()
                if any(compare_stdout(output, value) for output in outputs)}
    if not expected or (not multiple and len(expected) != 1):
        raise CodeOutputUnavailable()
    selected = submitted_choices(text, source.choices) if text.strip() else set()
    rules = _frozen_rules(problem)
    try:
        part = independent_output_steps(rules, 1, allow_output_marker=True) if rules else None
    except ValueError as exc:
        raise CodeOutputUnavailable() from exc
    if multiple:
        criterion = (rules.teacher_rubric or part.steps[0].criterion) if part else problem.criterion
        try:
            wrong_points, partial_points = selection_awards(criterion, problem.max_score)
        except ValueError as exc:
            raise CodeOutputUnavailable() from exc
    elif part is None:
        _require_unambiguous_legacy_rule(problem)
    correct = selected == expected
    award = problem.max_score if correct else 0
    description = ""
    if multiple and selected and not correct:
        subset = selected < expected
        award = partial_points if subset else wrong_points
        description = (
            ("少选" if subset else "含错选") + f"，按评分标准得 {award:g} 分。"
            if language == "zh" else
            ("Some correct options were omitted." if subset else "An incorrect option was selected.") + f" Rubric award: {award:g}."
        )
    return _output_correction(problem, [(award, award == problem.max_score, description)], language, part=part)


@contextmanager
def output_step(plan, *, correct, evidence, language):
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
