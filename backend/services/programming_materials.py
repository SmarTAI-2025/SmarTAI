"""Resolve new model-proposed inputs with the existing isolated reference runner."""
from __future__ import annotations

import ast
from dataclasses import dataclass

from backend.models import ProblemInfo
from backend.services.programming_reference_inputs import ReferenceInputCase, expectations_from_reference
from backend.services.programming_test_contract import (
    _declared_modes, adapt_programming_cases, bind_programming_cases, bind_stdin_cases, case_input_key, has_function_only_contract,
    native_standard_supported, required_function_name,
)
from backend.tools.structured_llm import llm_attempt_budget

GENERATED_TEST_COUNT = 10


class ReferenceExecutionError(ValueError):
    """Generated reference code is repairable; teacher code remains unchanged."""


def has_teacher_reference_code(source):
    """Use the same supplied-code authority rules as execution, without an LLM."""
    from backend.services.programming_runs import _reference
    problem = ProblemInfo(q_id="q1", number="", stem=source.get("stem") or "", criterion="",
                          solution_code=source.get("solution_code"), reference_answer=source.get("reference_answer"))
    problem._math_rubric_source = {"presentation": source}
    try:
        return bool(_reference(problem, teacher_only=True))
    except ValueError:
        return False


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


def material_problem(source):
    problem = ProblemInfo.model_validate({"number": "", "criterion": "", **source})
    problem._math_rubric_source = {"presentation": source}
    return problem


@dataclass
class ReferenceRunner:
    problem: ProblemInfo
    executable: str
    language: str
    body: list

    @classmethod
    def from_problem(cls, problem):
        from backend.services.programming_runs import _reference, _reference_language, has_native_main
        from backend.services.python_source_encoding import python_execution_source

        code = _reference(problem, include_generated=True)
        if not code:
            raise ReferenceExecutionError("Generate a runnable reference implementation before generating test outputs.")
        language = _reference_language(problem, code)
        if language != "python":
            if not native_standard_supported(problem.stem + "\n" + problem.criterion, language, reference=True):
                raise ReferenceExecutionError("The reference language or function interface is not supported by the runner.")
            if not has_native_main(code):
                raise ReferenceExecutionError("C/C++ reference execution requires a complete program with main.")
            return cls(problem, code, language, [])
        code = python_execution_source(code)
        try:
            tree = ast.parse(code)
            compile(tree, "<reference>", "exec")
            body = tree.body
        except (ValueError, SyntaxError) as exc:
            raise ReferenceExecutionError("The reference has invalid Python syntax; repair the generated implementation.") from exc
        return cls(problem, code, language, body)

    def check_declared_entry(self):
        if not has_function_only_contract(self.problem.stem, self.problem.test_cases):
            _, stdin_mode = _declared_modes(self.problem.stem, required_function_name(self.problem.stem))
            if stdin_mode:
                self.check_stdin_entry()
            return
        if self.language != "python":
            raise ReferenceExecutionError("C/C++ function execution requires a supported harness.")
        name = required_function_name(self.problem.stem)
        # A module may legally export an alias or a callable constructed by
        # top-level code. Reject only a demonstrably absent declared entry.
        if (all(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Pass)) for node in self.body)
                and not any(isinstance(node, ast.FunctionDef) and (not name or node.name == name) for node in self.body)):
            raise ReferenceExecutionError("The reference must implement the question's declared function.")

    def check_stdin_entry(self):
        definitions = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        if (any(isinstance(node, definitions) for node in self.body)
                and not any(isinstance(call, ast.Call) for node in self.body if not isinstance(node, definitions)
                            for call in ast.walk(node))):
            raise ReferenceExecutionError("The reference only defines functions but the question requires stdin/stdout.")

    async def run(self, cases, *, execute=None):
        from backend.services.programming_runs import _validate_report
        from backend.tools.code_interpreter import run_sandbox

        if self.language == "python":
            bound = bind_programming_cases(self.problem.stem, cases, self.executable)
            if all(c.io_mode == "stdin" for c in bound):
                self.check_stdin_entry()
        else:
            if any(c.io_mode == "function" for c in cases):
                raise ReferenceExecutionError("C/C++ function execution requires a supported harness.")
            bound = bind_stdin_cases(self.problem.stem, cases)
        report = await (execute or run_sandbox)(self.executable, bound, language=self.language, per_case_timeout=5.0)
        _validate_report(report, bound)
        return bound, report


async def resolve_generated_outputs(source, cases, *, execute=None, provider=None, reporter=None,
                                    review_evidence=None):
    from backend.services.programming_input_domain import input_domain
    from backend.services.programming_runs import input_review_fingerprint

    problem = material_problem(source)
    runner = ReferenceRunner.from_problem(problem)
    teacher_cases = [case for case in problem.test_cases or [] if case.source == "teacher"]
    if teacher_cases:
        checks, report = await runner.run(teacher_cases, execute=execute)
        if report.passed_count != len(checks):
            raise ReferenceExecutionError("The reference implementation contradicts supplied teacher tests; preserve their outputs and repair the generated reference.")
    probes = adapt_programming_cases(problem.stem, input_probes(cases))
    if len(probes) < GENERATED_TEST_COUNT:
        raise ValueError("Supply at least ten distinct legal test inputs.")
    identities = {case_input_key(problem.stem, c, reference=runner.executable) for c in probes}
    if len(identities) != len(probes):
        raise ValueError("Supply distinct test inputs; repeated inputs must not change their scoring weight.")
    if any(not c.sandbox_feasible for c in probes):
        raise ValueError("Use only runnable test inputs verifiably inside the question's stated domain.")
    domains = [input_domain(problem, c) for c in probes]
    feedback = {}
    if provider is not None and "unknown" in domains and "outside" not in domains:
        from backend.services.programming_input_review import review_input_domains
        # Input review is required before executing otherwise valid proposals.
        # A successful last generation attempt must still be able to reach it.
        with llm_attempt_budget(independent=True):
            domains = await review_input_domains(problem, probes, domains, provider, reporter=reporter, feedback=feedback)
    if any(domain != "allowed" for domain in domains):
        rejected = "; ".join(f"{i}: {domain}" + (f" — {feedback[i]}" if i in feedback else "")
                             for i, domain in enumerate(domains, 1) if domain != "allowed")
        raise ValueError("Test inputs outside or not supported by the question's domain (" + rejected + "). Repair only these inputs; preserve the question's constraints.")
    reference_cases, report = await runner.run(probes, execute=execute)
    try:
        resolved = expectations_from_reference(probes, reference_cases, report)
    except ValueError as exc:
        raise ReferenceExecutionError("The reference failed to produce complete, usable outputs for the proposed inputs.") from exc
    if all(case.io_mode == "stdin" for case in reference_cases) and not any(row.stdout for row in report.results):
        raise ReferenceExecutionError("The reference produced no stdout for the entire suite. Check its entry/input/output wiring and include an observable-output input; do not invent outputs.")
    resolved = adapt_programming_cases(problem.stem, resolved)
    if review_evidence is not None:
        review_evidence["input_review_fingerprint"] = input_review_fingerprint(problem, resolved)
    return resolved
