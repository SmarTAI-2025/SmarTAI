"""Supplemental observations stay separate from the frozen, shared scoring suite."""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backend.models import TestCase
from backend.services import programming_runs as runs
from backend.services.programming_input_domain import input_domain
from backend.services.programming_test_contract import bind_programming_cases
from backend.services.python_source_encoding import python_execution_source
from backend.tools.code_interpreter import HostCodeExecutionDisabled
from backend.tools.oci_grading_runner import IsolatedRunnerUnavailable


class BoundaryInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    stdin: str | None = Field(default=None, max_length=400)
    args: list[Any] | None = Field(default=None, max_length=16)
    kwargs: dict[str, Any] | None = None
    expected: str = Field(max_length=200)


def _input(case):
    if case.function_name:
        return {"args": case.function_args or [], "kwargs": case.function_kwargs or {}}
    return case.input


def _proposed_case(proposal, cases):
    try:
        if not isinstance(proposal, dict) or len(json.dumps(proposal, allow_nan=False).encode()) > 2048:
            return None
        extra = BoundaryInput.model_validate(proposal)
    except (ValidationError, ValueError, TypeError, RecursionError):
        return None
    first = cases[0]
    if len({(c.function_name, c.comparison, c.abs_tol, c.rel_tol) for c in cases}) != 1:
        return None
    options = dict(comparison=first.comparison, abs_tol=first.abs_tol, rel_tol=first.rel_tol, source="llm_generated")
    if first.function_name:
        if extra.args is None or extra.stdin is not None:
            return None
        case = TestCase(**options, io_mode="function", function_name=first.function_name,
                        function_args=extra.args, function_kwargs=extra.kwargs, expected_return=extra.expected)
    else:
        if extra.stdin is None or extra.args is not None or extra.kwargs is not None:
            return None
        case = TestCase(**options, io_mode="stdin", input=extra.stdin, expected_output=extra.expected)
    if any(runs.digest(_input(old)) == runs.digest(_input(case)) for old in cases):
        return None
    return case


def _compact(value):
    # Escape controls and Markdown delimiters in user code output before it
    # enters a saved result, and retain only a small visible preview.
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    text = "".join(f"\\u{ord(c):04x}" if unicodedata.category(c) == "Cf" else c for c in text[:100])
    if len(text) > 96:
        text = text[:95] + "…"
    delimiter = "`" * (max((len(s) for s in re.findall(r"`+", text)), default=0) + 1)
    return f"{delimiter} {text} {delimiter}"


def _observation(case, result):
    if result.passed or not result.executed:
        return None
    mismatch = result.exit_reason == "wrong_answer" or (result.exit_reason in {"success", ""} and not result.error)
    if not mismatch and result.exit_reason != "runtime_error":
        return None
    given = _input(case)
    if case.function_name and not given["kwargs"]:
        given = given["args"]
    expected = case.expected_return if case.function_name else case.expected_output
    return {"input": _compact(given), "expected": _compact(expected),
            "actual": _compact(result.actual_output) if mismatch else None}


def boundary_feedback(observation, language):
    given, expected, actual = (observation[k] for k in ("input", "expected", "actual"))
    if language == "zh":
        observed = f"本次作答得到 {actual}" if actual is not None else "本次作答运行出错"
        return f"额外检查发现，输入 {given} 时，经标答核对的预期为 {expected}，{observed}。这一输入在教师用例之外，计分以教师标准为准。"
    observed = f"produced {actual}" if actual is not None else "encountered a runtime error"
    return f"For the additional input {given}, the submission {observed}. The reference confirms the expected value {expected}. This input is outside the teacher tests. Credit follows the teacher rubric."


async def _reference_matches(state, problem, ref_source, ref_case, language, execute, reporter):
    # Every student keeps an independent execution, but a class needs only one
    # reference run for the same question revision, input and proposed expectation.
    ref_key = runs.digest(["boundary_reference_v1", runs._question_key(state, problem), language, ref_case.model_dump(mode="json")])
    async with state.lock(ref_key):
        saved = state.read(ref_key, runs.AUXILIARY)
        if saved is not None:
            return saved.get("matched") is True
        state.store(runs.AUXILIARY, ref_key, {"status": "started"})
        await runs._notify(reporter, "validate_programming_reference")
        report = await execute(ref_source, [ref_case], language=language, per_case_timeout=5.0)
        runs._validate_report(report, [ref_case])
        matched = report.passed_count == 1
        state.store(runs.AUXILIARY, ref_key, {"status": "done", "matched": matched})
        return matched


async def verified_boundary(problem, run, public, proposal, execute, reporter):
    if (run.status != "executed" or not run.cases or not public.teacher_tests.total
            or public.teacher_tests.passed != public.teacher_tests.total):
        return None
    reference = runs._reference(problem)
    if not reference:
        return None
    # Preparation has already checked the independent teacher reference on all
    # frozen cases. Use that result before considering any new sandbox work.
    teacher_inputs = {runs.digest(_input(c)) for c in run.cases if c.source == "teacher"}
    for case, result in zip(run.cases, run.report.results, strict=True):
        if (case.source == "llm_generated" and runs.digest(_input(case)) not in teacher_inputs
                and input_domain(problem, case) == "allowed"):
            observed = _observation(case, result)
            if observed:
                return observed
    state = runs.current_programming_run()
    if state is None and proposal is None:
        return None
    state = state or runs.ProgrammingRun()
    key = runs.digest(["boundary_check_v2", run.evidence_id])
    async with state.lock(key):
        previous = state.read(key, runs.AUXILIARY)
        if previous is not None:
            # A pre-domain receipt contains only a display preview. Preserve the
            # attempt limit on resume and surface only independently checked inputs.
            return previous.get("observation") if previous.get("input_domain") == "allowed" else None
        case = _proposed_case(proposal, run.cases)
        if case is None or input_domain(problem, case) != "allowed":
            return None
        state.store(runs.AUXILIARY, key, {"status": "started"})
        observation = None
        try:
            language = runs._reference_language(problem, reference)
            ref_source = python_execution_source(reference) if language == "python" else reference
            # In a free-name function question the teacher and student may use
            # different names. Bind the reference's own contract independently.
            ref_case = case
            if case.function_name:
                if language != "python":
                    raise ValueError("unsupported_reference_interface")
                raw = case.model_dump(exclude_unset=True, exclude={"function_name"})
                ref_case, = bind_programming_cases(problem.stem, [raw], ref_source)
            if await _reference_matches(state, problem, ref_source, ref_case, language, execute, reporter):
                student_language = run.source_language or "python"
                source = python_execution_source(run.code) if student_language == "python" else run.code
                await runs._notify(reporter, "run_programming_test_cases")
                report = await execute(source, [case], language=student_language, per_case_timeout=5.0)
                runs._validate_report(report, [case])
                result, = report.results
                observation = _observation(case, result)
        except (IsolatedRunnerUnavailable, HostCodeExecutionDisabled, TimeoutError, ValueError):
            # An optional experiment cannot turn a valid official grade into a
            # student failure. Lease loss and cancellation still propagate.
            pass
        state.store(runs.AUXILIARY, key, {"status": "done", "input_domain": "allowed", "observation": observation})
        return observation
