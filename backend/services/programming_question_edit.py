"""Prepare programming material changes before the version-checked question save."""
from __future__ import annotations

import ast
from copy import deepcopy
import re

from backend.domain.errors import DomainError, ValidationError
from backend.models import is_programming_question_type
from backend.services.background_errors import classify_background_error
from backend.services.programming_input_domain import input_domain
from backend.services.programming_input_review import review_input_domains
from backend.services.programming_materials import (
    GENERATED_TEST_COUNT, ReferenceExecutionError, ReferenceRunner, material_problem,
)
from backend.services.programming_reference_inputs import expectations_from_reference
from backend.services.programming_runs import (
    _source, answer_code_block, current_reference_texts, input_review_fingerprint,
    reference_material, reference_materials_digest, source_hash,
)
from backend.services.programming_test_contract import (
    ProgrammingCaseError, adapt_programming_cases, case_input_key, literal_programming_case_pair,
    required_function_name,
)
from backend.tools.code_interpreter import HostCodeExecutionDisabled, _values_match, compare_stdout
from backend.tools.oci_grading_runner import IsolatedRunnerUnavailable

_BINDING = "programming_reference_binding"


def _invalid(message, index=None, issue="configuration"):
    return ValidationError(message, code="invalid_programming_test_cases",
                           details={"case_number": index, "issue": issue})


def _expectation(case):
    return case.expected_return if case.io_mode == "function" else case.expected_output


def _same_expected(left, right):
    a, b = _expectation(left), _expectation(right)
    if a == b:
        return True
    if left.io_mode != right.io_mode or left.comparison != right.comparison:
        return False
    compare = _values_match if left.io_mode == "function" and left.comparison != "json" else compare_stdout
    return compare(a, b, left.comparison, 0, 0) and compare(b, a, left.comparison, 0, 0)


def _comparison(case):
    return case.comparison, case.abs_tol, case.rel_tol, case.function_name, case.sandbox_feasible


def _edited_case(stem, row, previous):
    if not isinstance(row, dict):
        raise ProgrammingCaseError("Expected a test case object.")
    if not any(row.get(k) is not None for k in ("input", "function_args", "function_kwargs")):
        raise ProgrammingCaseError("Supply an input, including an explicit empty input.", issue="arguments")
    if row.get("expected_output") is None and row.get("expected_return") is None:
        raise ProgrammingCaseError("Supply an explicit expected output or return.", issue="expected_result")
    # Old forms carry both visible text and the last parsed argument vector.
    # Structured API clients may change the vector instead. Resolve conflicts
    # only if the other representation matches its stored original.
    try:
        return adapt_programming_cases(stem, [row])[0]
    except ProgrammingCaseError as exc:
        if not any(row.get(k) is not None for k in ("function_args", "function_kwargs", "expected_return")):
            raise
        display = ("input", "expected_output")
        structured = ("function_args", "function_kwargs", "expected_return")
        same_structured = any(all(row.get(k) == getattr(old, k) for k in structured) for old in previous)
        same_display = any(all(row.get(k) == getattr(old, k) for k in display) for old in previous)
        if same_structured:
            return adapt_programming_cases(stem, [row], edited=True)[0]
        if same_display:
            return adapt_programming_cases(stem, [{k: v for k, v in row.items() if k not in display}])[0]
        raise exc


def _case_edits(before, candidate):
    stem = before["stem"]
    old = adapt_programming_cases(stem, before.get("test_cases") or [])
    rows = candidate.get("test_cases") or []
    if not isinstance(rows, list) or len(rows) > 50:
        raise _invalid("Supply at most 50 test cases.", issue="case_limit")
    origins = candidate.get("test_case_origins")
    if origins is not None:
        if not isinstance(origins, list) or len(origins) != len(rows):
            raise _invalid("Each submitted row must identify its original position or be marked new.")
        supplied = [i for i in origins if i is not None]
        if any(type(i) is not int or not 0 <= i < len(old) for i in supplied) or len(supplied) != len(set(supplied)):
            raise _invalid("Test case origins must be unique valid positions in the previous version.")
    cases = []
    for index, row in enumerate(rows, 1):
        try:
            if before["stem"] != candidate["stem"] and row in (before.get("test_cases") or []):
                cases.append(old[before["test_cases"].index(row)])
            else:
                prior = old
                if origins is not None:
                    j = origins[index - 1]
                    prior = [old[j]] if j is not None else []
                cases.append(_edited_case(candidate["stem"], row, prior))
        except ProgrammingCaseError as exc:
            raise _invalid(str(exc), index, exc.issue) from exc

    comparison_stem = stem if candidate["stem"] == stem else ""
    old_inputs = [case_input_key(comparison_stem, case) for case in old]
    inputs = [case_input_key(comparison_stem, case) for case in cases]
    remaining = set(range(len(old)))
    validate, authored = set(), set()
    for i, case in enumerate(cases):
        if origins is None:
            matches = [j for j in sorted(remaining) if old_inputs[j] == inputs[i] and _same_expected(old[j], case)]
            # Prefer the original title for duplicate pairs with different origins.
            j = next((j for j in matches if old[j].title == case.title), matches[0] if matches else None)
        else:
            j = origins[i]
        if j is None:
            authored.add(i)
            continue
        remaining.remove(j)
        same_input, same_expected = inputs[i] == old_inputs[j], _same_expected(case, old[j])
        if same_input != same_expected:
            raise _invalid("Change both the input and expected result of this test case.", i + 1, "paired_edit")
        if not same_input:
            authored.add(i)
            continue
        cases[i] = case.model_copy(update={"source": old[j].source})
        if _comparison(case) != _comparison(old[j]):
            validate.add(i)
    for i in sorted(authored):
        if origins is None and any(inputs[i] == old_inputs[j] or _same_expected(cases[i], old[j]) for j in remaining):
            raise _invalid("Change both the input and expected result of this test case.", i + 1, "paired_edit")
        cases[i] = cases[i].model_copy(update={"source": "teacher"})
    return cases, validate | authored, authored


def _same_implementation(left, right):
    if left == right:
        return True
    if not left or not right:
        return False
    # Python comments and redundant parentheses do not change the executable
    # syntax. Native preprocessor directives and literal whitespace matter.
    def python_tree(material):
        if material.lstrip().lower().startswith(("```c", "```java")):
            return None
        return ast.dump(ast.parse(_source(material)))
    try:
        tree = python_tree(left)
        return tree is not None and tree == python_tree(right)
    except (ValueError, SyntaxError, RecursionError):
        return False


def _binding(row, field, material, stale):
    supplement = (row.get("teacher_material_supplements") or {}).get("reference_answer") or {}
    supplemented_code = (field == "reference_answer" and supplement
                         and material != answer_code_block(supplement.get("original")))
    generated = field == "generated_code" or field in (row.get("ai_completion_provenance") or {}) or supplemented_code
    value = {
        "source_kind": "ai_generated" if generated else "teacher",
        "field": field, "sha256": source_hash(material) if material else None,
        "materials_sha256": reference_materials_digest(row["stem"], row.get("reference_answer")),
        "stale_fields": sorted(stale - {field}),
    }
    if field == "generated_code":
        value["generated_code"] = material
    return value


def _set_origin(row, field, *, generated):
    ai = dict(row.get("ai_completion_provenance") or {})
    material = dict(row.get("material_provenance") or {})
    if generated:
        ai[field] = {"source_kind": "ai_generated"}
        material.pop(field, None)
    else:
        ai.pop(field, None)
        material[field] = {**(material.get(field) or {}), "source_kind": "teacher_edited"}
    row["ai_completion_provenance"] = ai
    row["material_provenance"] = material


def _example_pair(name, line):
    line = re.sub(r"^(?:example|示例|样例)\s*[:：]\s*", "", line.strip(), flags=re.I)
    pair = literal_programming_case_pair(line)
    if pair is None and name:
        match = re.fullmatch(re.escape(name) + r"\((.*)\)\s*(?:→|->|=>|returns?\s+|返回)\s*(.+)", line)
        if match:
            pair = match.groups()
    return pair


def _explicit_examples(stem):
    """Read only complete example lines whose arguments/expectation are explicit."""
    name = required_function_name(stem)
    return adapt_programming_cases(stem, [
        {"input": pair[0], "expected_output": pair[1]} for line in stem.splitlines()
        if (pair := _example_pair(name, line))
    ])


async def _generate(row, targets, get_provider, reporter):
    from backend.agents.ingest_agent import generate_missing_question_materials

    provider = get_provider()
    if provider is None:
        raise ValidationError("Enable a model to complete the missing programming materials.", code="no_provider_configured")
    qid = row["q_id"]
    # This is a fresh generation context. Archived teacher code is never shown
    # as a candidate implementation. Newly generated sibling code can be used
    # by the existing generator to execute its proposed inputs.
    source = {**row, **current_reference_texts(row)}
    source.pop(_BINDING, None)
    field = (row.get(_BINDING) or {}).get("field")
    if field:
        source["solution_code"] = reference_material(material_problem(row), include_generated=True)
        provenance = dict(source.get("ai_completion_provenance") or {})
        if (row.get(_BINDING) or {}).get("source_kind") == "ai_generated" or field in provenance:
            provenance["solution_code"] = {"source_kind": "ai_generated"}
        else:
            provenance.pop("solution_code", None)
        source["ai_completion_provenance"] = provenance
    scoring = row.get("programming_scoring") or {}
    if scoring.get("test_points") is not None and "input_rules" in scoring:
        source["criterion"] = "\n".join(filter(None, (source.get("criterion"), scoring["input_rules"])))
    requested = [{"q_id": qid, "target_id": f"{qid}:{field}", "target": field} for field in targets]
    results = await generate_missing_question_materials(
        {qid: source}, requested, GENERATED_TEST_COUNT, provider, reporter,
        manage_progress_lifecycle=False,
    )
    result = {item.target: item for item in results}
    if set(result) != set(targets):
        raise ValidationError("Incomplete programming materials.", code="provider_response_invalid")
    return result


def _choose_reference(before, row, semantic, code_edit):
    old_binding = before.get(_BINDING) or {}
    stale = set(old_binding.get("stale_fields") or ())
    code = row.get("solution_code")
    answer = answer_code_block(row.get("reference_answer"))
    old_answer = answer_code_block(before.get("reference_answer"))
    if semantic:
        for field, value in (("solution_code", code), ("reference_answer", answer)):
            if value:
                stale.add(field)
    if (code_edit and not semantic and "solution_code" in stale and old_binding
            and _same_implementation(code, before.get("solution_code"))):
        return old_binding
    if code_edit and code and (not semantic or not _same_implementation(code, before.get("solution_code"))):
        if answer and not _same_implementation(code, answer):
            stale.add("reference_answer")
        return _binding(row, "solution_code", code, stale)
    if semantic and answer and not _same_implementation(answer, old_answer):
        # A newly accepted, independent implementation in the revised answer.
        if code and _same_implementation(code, answer) and "solution_code" not in (row.get("ai_completion_provenance") or {}):
            return _binding(row, "solution_code", code, stale)
        return _binding(row, "reference_answer", answer, stale)
    if not semantic:
        if answer and "reference_answer" not in stale and "reference_answer" not in (row.get("ai_completion_provenance") or {}):
            return _binding(row, "reference_answer", answer, stale)
        if code and "solution_code" not in stale:
            return _binding(row, "solution_code", code, stale)
    return _binding(row, None, None, stale)


def _store_generated_reference(row, code):
    stale = set(row[_BINDING]["stale_fields"])
    generated = row.get("ai_completion_provenance") or {}
    # Fresh generation received only the revised materials. If its result
    # corroborates a teacher original, keep that original as the authority.
    for field, material in (("solution_code", row.get("solution_code")),
                            ("reference_answer", answer_code_block(row.get("reference_answer")))):
        if field in stale and field not in generated and _same_implementation(material, code):
            row[_BINDING] = _binding(row, field, material, stale)
            return material
    field = "generated_code"
    material_source = row.get("material_provenance") or {}
    if not row.get("solution_code") or ("solution_code" in generated and "solution_code" not in material_source):
        field = "solution_code"
        row[field] = code
        _set_origin(row, field, generated=True)
    row[_BINDING] = _binding(row, field, code, stale)
    return code


async def _verify(runner, cases, indices):
    if not indices:
        return
    checks = [cases[i] for i in indices]
    if any(not c.sandbox_feasible for c in checks):
        raise _invalid("The changed test must be executable.")
    _, report = await runner.run(checks)
    for index, result in zip(indices, report.results, strict=True):
        if not result.passed or result.stdout_truncated or result.stderr_truncated:
            raise ValidationError("The reference did not match the expected result.",
                                  code="programming_reference_conflict", details={"case_number": index + 1})


async def _recheck_generated_inputs(row, cases, get_provider, reporter):
    positions, prepared, removed = [], [], set()
    for i, case in enumerate(cases):
        if case.source == "llm_generated":
            try:
                prepared.append(adapt_programming_cases(row["stem"], [case])[0])
                positions.append(i)
            except ProgrammingCaseError:
                removed.add(i)
    if prepared:
        scoring = row.get("programming_scoring") or {}
        problem = material_problem({**row, "test_cases": [],
                                    "criterion": scoring.get("input_rules", row.get("criterion", ""))})
        domains = [input_domain(problem, case) for case in prepared]
        if "unknown" in domains:
            domains = await review_input_domains(problem, prepared, domains, get_provider(), reporter=reporter)
        removed.update(i for i, domain in zip(positions, domains, strict=True) if domain != "allowed")
    return removed


async def _prepare(before, candidate, edited_fields, get_provider, reporter):
    fields = set(edited_fields)
    semantic = {f for f in candidate.get("programming_semantic_fields", fields) if f in fields
                and f in ("stem", "reference_answer") and candidate.get(f) != before.get(f)}
    code_edit = "solution_code" in fields and candidate.get("solution_code") != before.get("solution_code")
    tests_edit = "test_cases" in fields and candidate.get("test_cases") != before.get("test_cases")
    if not (semantic or code_edit or tests_edit):
        return {}
    row = deepcopy(candidate)
    if code_edit:
        _set_origin(row, "solution_code", generated=False)
    if tests_edit:
        cases, validate, authored = _case_edits(before, row)
    else:
        cases = adapt_programming_cases(before["stem"], before.get("test_cases") or [])
        validate, authored = set(), set()
    # Keep valid historical rows intact when a form reorders or relabels them.
    if authored:
        _set_origin(row, "test_cases", generated=False)
    points = (row.get("programming_scoring") or {}).get("test_points")
    only_tests = not semantic and not code_edit
    if only_tests and not validate:
        if not cases and points and before.get("test_cases"):
            raise _invalid("The test allocation requires at least one verified test case.")
        if tests_edit:
            row["test_cases"] = [
                {**raw, "source": c.source} if raw.get("source", "teacher") != c.source else raw
                for raw, c in zip(row.get("test_cases") or [], cases, strict=True)
            ]
        return _patch(candidate, row)

    old_problem = material_problem(before)
    old = reference_material(old_problem, include_generated=True)
    if only_tests:
        # Editing a sample cannot change its oracle or create upstream code.
        if not reference_material(material_problem(row), include_generated=True):
            raise ValidationError("A current reference is required to verify the test.", code="programming_cases_unavailable")
    else:
        row[_BINDING] = _choose_reference(before, row, semantic, code_edit)

    # Explicit examples newly supplied with this revision are independent of
    # outputs produced by the previous reference. Never use old generated tests
    # as evidence to promote a retired implementation.
    examples = _explicit_examples(row["stem"]) if semantic or code_edit else []
    if semantic and not row[_BINDING]["field"] and old and not code_edit:
        old_field = (before.get(_BINDING) or {}).get("field")
        if not old_field:
            old_field = "solution_code" if old == before.get("solution_code") else "reference_answer"
        independent = reference_material(old_problem, teacher_only=True)
        # Appended examples can establish compatibility with an otherwise
        # unchanged specification. A coincidentally matching value is never
        # enough to certify an implementation under a changed general rule.
        if independent and examples and semantic == {"stem"} and _example_only_change(before["stem"], row["stem"]):
            proposed = _binding(row, old_field, old, set(row[_BINDING]["stale_fields"]))
            reference = ReferenceRunner.from_problem(material_problem({**row, _BINDING: proposed}))
            _, report = await reference.run(examples)
            if all(r.passed and not r.stdout_truncated and not r.stderr_truncated for r in report.results):
                row[_BINDING] = proposed
                examples = []  # already verified this material and these exact rows

    missing_code = not reference_material(material_problem(row), include_generated=True)
    removed = await _recheck_generated_inputs(row, cases, get_provider, reporter) if semantic else set()
    missing_tests = bool(points and not cases) or bool(removed)
    generated = {}
    generation_ran_active_reference = True
    if missing_code or missing_tests:
        if only_tests:
            raise ValidationError("A current reference is required to verify the test.", code="programming_cases_unavailable")
        targets = (["solution_code"] if missing_code else []) + (["test_cases"] if missing_tests else [])
        teacher = [c for c in cases if c.source == "teacher"] + examples
        source = {**row, "test_cases": [c.model_dump(mode="json", exclude_unset=True) for c in teacher]}
        generated = await _generate(source, targets, get_provider, reporter)
        if missing_code:
            code = generated["solution_code"].text_value
            active = _store_generated_reference(row, code)
            generation_ran_active_reference = active == code

    runner = ReferenceRunner.from_problem(material_problem(row))
    if reporter and (validate or semantic or code_edit) and cases:
        await reporter._emit_message("Checking the affected programming examples against the current reference.")
    if not cases and not missing_tests:
        runner.check_declared_entry()
    active = reference_material(runner.problem, include_generated=True)
    behavior_changed = bool(semantic) or (code_edit and not _same_implementation(old, active))
    generated_checked = missing_tests and generation_ran_active_reference
    if examples and behavior_changed and not generated_checked:
        await _verify(runner, examples, list(range(len(examples))))
    teacher_checks = sorted(i for i, case in enumerate(cases) if i not in removed and (
        i in validate or behavior_changed and case.source == "teacher"))
    # The existing generator already executed all teacher rows as part of
    # producing the new inputs' expected outputs.
    if not generated_checked:
        await _verify(runner, cases, teacher_checks)
    if behavior_changed:
        indices = [i for i, c in enumerate(cases) if c.source == "llm_generated" and i not in removed]
        if indices:
            original = [cases[i] for i in indices]
            bound, report = await runner.run(original)
            for i, case in zip(indices, expectations_from_reference(original, bound, report), strict=True):
                cases[i] = case
    if missing_tests:
        count = len(removed) or GENERATED_TEST_COUNT
        cases = [case for i, case in enumerate(cases) if i not in removed]
        known = {case_input_key(row["stem"], c, reference=runner.executable) for c in cases}
        new_cases = generated["test_cases"].test_cases
        if not generation_ran_active_reference:
            bound, report = await runner.run(new_cases)
            new_cases = expectations_from_reference(new_cases, bound, report)
        for new in new_cases:
            key = case_input_key(row["stem"], new, reference=runner.executable)
            if key not in known:
                cases.append(new)
                known.add(key)
                count -= 1
                if count == 0:
                    break
        if count:
            raise ValidationError("Insufficient distinct verified inputs.", code="programming_cases_unavailable")
        if all(c.source == "llm_generated" for c in cases):
            _set_origin(row, "test_cases", generated=True)
    if cases and (tests_edit or behavior_changed or missing_tests):
        saved = [c.model_dump(mode="json", exclude_unset=True) for c in adapt_programming_cases(row["stem"], cases)]
        if not tests_edit:
            kept = [c for i, c in enumerate(before.get("test_cases") or []) if i not in removed]
            for i, original in enumerate(kept):
                if cases[i].source == "teacher":
                    saved[i] = original
        row["test_cases"] = saved
        if (semantic or missing_tests) and all(c.source == "llm_generated" for c in cases):
            ai = dict(row.get("ai_completion_provenance") or {})
            ai["test_cases"] = {**(ai.get("test_cases") or {}), "source_kind": "ai_generated",
                                "input_review_fingerprint": input_review_fingerprint(runner.problem, cases)}
            row["ai_completion_provenance"] = ai
    return _patch(candidate, row)


def _example_only_change(old, new):
    def without_examples(text):
        name = required_function_name(text)
        return [line.strip() for line in text.splitlines() if line.strip() and not _example_pair(name, line)]
    return without_examples(old) == without_examples(new)


def _patch(candidate, row):
    return {k: row[k] for k in (_BINDING, "solution_code", "test_cases", "material_provenance",
                                "ai_completion_provenance") if k in row and row[k] != candidate.get(k)}


async def prepare_programming_edit(*, before, candidate, edited_fields, get_provider, reporter=None) -> dict:
    """Return only the server-derived patch. All execution precedes atomic publication."""
    if not is_programming_question_type(candidate.get("type")):
        return {}
    try:
        return await _prepare(before, candidate, edited_fields, get_provider, reporter)
    except DomainError:
        raise
    except ProgrammingCaseError as exc:
        raise _invalid(str(exc), exc.case_number, exc.issue) from exc
    except (HostCodeExecutionDisabled, IsolatedRunnerUnavailable, TimeoutError) as exc:
        raise ValidationError("The reference runner is unavailable.", code="programming_execution_unavailable") from exc
    except ReferenceExecutionError as exc:
        raise ValidationError(str(exc), code="programming_reference_conflict") from exc
    except Exception as exc:
        code = classify_background_error(exc, "programming_case_configuration_invalid")
        raise ValidationError("The programming materials could not be verified.", code=code) from exc
