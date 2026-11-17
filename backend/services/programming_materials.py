"""Resolve new model-proposed inputs with the existing isolated reference runner."""
from __future__ import annotations

import ast
import json

from backend.models import ProblemInfo
from backend.services.programming_reference_inputs import ReferenceInputCase, expectations_from_reference
from backend.services.programming_test_contract import (
    adapt_programming_cases, bind_programming_cases, bind_stdin_cases, native_standard_supported,
)

GENERATED_TEST_COUNT = 10


class ReferenceExecutionError(ValueError):
    """Generated reference code is repairable; teacher code remains unchanged."""


def input_probes(cases):
    """Discard model-predicted answers before either execution or persistence."""
    probes = []
    for case in cases:
        data = case.model_dump(exclude_unset=True)
        data.update(source="llm_generated", expected_output="", expected_return=None)
        if case.function_args is not None or case.function_name:
            data["io_mode"] = "function"
        probes.append(ReferenceInputCase.model_validate(data).comparison_probe())
    return probes


async def resolve_generated_outputs(source, cases, *, execute=None):
    from backend.services.programming_runs import _reference, _reference_language, _source, _validate_report
    from backend.services.programming_input_domain import input_domain
    from backend.services.python_source_encoding import python_execution_source
    from backend.tools.code_interpreter import run_sandbox

    problem = ProblemInfo.model_validate({"number": "", "criterion": "", **source})
    problem._math_rubric_source = {"presentation": {
        "ai_completion_provenance": source.get("ai_completion_provenance") or {},
    }}
    reference = _reference(problem)
    if not reference and problem.solution_code:
        # A generated reference may derive NEW expectations, never replace a
        # teacher's existing expectations or become independent evidence.
        reference = _source(problem.solution_code)
        problem._math_rubric_source = {}
    if not reference:
        raise ValueError("Generate a runnable reference implementation before generating test outputs.")
    language = _reference_language(problem, reference)
    if language != "python" and not native_standard_supported(problem.stem + "\n" + problem.criterion, language, reference=True):
        raise ValueError("The reference language or function interface is not supported by the runner.")
    executable = python_execution_source(reference) if language == "python" else reference
    body = []
    if language == "python":
        try:
            body = ast.parse(executable).body
        except SyntaxError as exc:
            raise ReferenceExecutionError("The reference has invalid Python syntax; repair the generated implementation.") from exc

    def bind(rows):
        return (bind_programming_cases(problem.stem, rows, executable) if language == "python"
                else bind_stdin_cases(problem.stem, rows))

    execute = execute or run_sandbox
    teacher_cases = [case for case in problem.test_cases or [] if case.source == "teacher"]
    if teacher_cases:
        checks = bind(teacher_cases)
        report = await execute(executable, checks, language=language, per_case_timeout=5.0)
        _validate_report(report, checks)
        if report.passed_count != len(checks):
            raise ReferenceExecutionError("The reference implementation contradicts supplied teacher tests; preserve their outputs and repair the generated reference.")
    probes = adapt_programming_cases(problem.stem, input_probes(cases))
    if len(probes) < GENERATED_TEST_COUNT:
        raise ValueError("Supply at least ten distinct legal test inputs.")
    if any(not c.sandbox_feasible or input_domain(problem, c) == "outside" for c in probes):
        raise ValueError("Use only runnable test inputs inside the question's stated domain.")
    identities = {json.dumps([c.io_mode, c.function_args, c.function_kwargs or {}] if c.io_mode == "function"
                             else [c.io_mode, c.input], sort_keys=True, allow_nan=False) for c in probes}
    if len(identities) != len(probes):
        raise ValueError("Supply distinct test inputs; repeated inputs must not change their scoring weight.")
    reference_cases = bind(probes)
    if all(case.io_mode == "stdin" for case in reference_cases):
        if language == "python":
            definitions = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            if (any(isinstance(node, definitions) for node in body)
                    and not any(isinstance(call, ast.Call) for node in body if not isinstance(node, definitions)
                                for call in ast.walk(node))):
                raise ReferenceExecutionError("The reference only defines functions but the question requires stdin/stdout. Supply an explicit function contract or a complete input/output wrapper.")
        else:
            from backend.services.programming_runs import has_native_main
            if not has_native_main(executable):
                raise ReferenceExecutionError("C/C++ reference execution requires a complete program with main.")
    report = await execute(executable, reference_cases, language=language, per_case_timeout=5.0)
    _validate_report(report, reference_cases)
    try:
        resolved = expectations_from_reference(probes, reference_cases, report)
    except ValueError as exc:
        raise ReferenceExecutionError("The reference failed to produce complete, usable outputs for the proposed inputs.") from exc
    return adapt_programming_cases(problem.stem, resolved)
