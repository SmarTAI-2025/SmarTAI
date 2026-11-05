"""Question-scoped test preparation and submission-scoped execution for programming.

The leased grading run owns these records. Only a successful preparation or a
validated execution can be restored. Failed-only retry explicitly inherits
them. A new full batch starts with an empty manifest.
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import math
import re
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from typing import Literal

from backend.config import settings
from backend.db import grading_repository
from backend.models import ProblemInfo, StudentAnswerInfo, TestCase, is_programming_question_type
from pydantic import ValidationError
from backend.services.grading_checkpoints import CheckpointWriteError, current_checkpoints
from backend.services.programming_reference_inputs import ReferenceInputCase, expectations_from_reference
from backend.services.programming_submission_mapping import C_FAMILY_FLAG, NATIVE_LANGUAGES, PYTHON_FLAG, has_markup_fence
from backend.services.programming_test_contract import has_function_only_contract, native_standard_supported
from backend.services.python_source_encoding import PythonSourceEncodingError, python_execution_source
from backend.tools.code_interpreter import ExecutionReport, TestResult, HostCodeExecutionDisabled
from backend.tools.oci_grading_runner import IsolatedRunnerUnavailable, NativeBuildUnavailable

VERSION = "programming_run_v1"
PREPARED = grading_repository.PROGRAMMING_CASES_PREPARED
AUXILIARY = grading_repository.PROGRAMMING_AUXILIARY_CHECK
STARTED = grading_repository.PROGRAMMING_PREPARATION_STARTED
PARTIAL = grading_repository.PROGRAMMING_EXECUTION_PARTIAL
COMPLETED = grading_repository.PROGRAMMING_EXECUTION_COMPLETED
_current = ContextVar("programming_run", default=None)


def digest(data):
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def source_hash(code):
    return hashlib.sha256(code.encode()).hexdigest()


@dataclass
class ProgrammingEvidence:
    status: Literal["executed", "unavailable", "unsupported", "no_submission"]
    code: str
    cases: list[TestCase]
    report: ExecutionReport | None = None
    reason: str = ""
    evidence_id: str = ""
    case_ids: tuple[str, ...] = ()
    source_language: str | None = None


def _saved(run_id, manifest, owner, *, auxiliary_events=None):
    # Failed-only manifests carry small source references. Keep large output
    # and every case observation exactly once, in the originating run.
    records = {}
    references = manifest.get("programming_artifacts") or {}
    kinds = (PREPARED, PARTIAL, COMPLETED, AUXILIARY)
    if auxiliary_events is not None:
        kinds = (AUXILIARY,)
        references = {key: ref for key, ref in references.items() if ref.get("kind") == AUXILIARY}
    source_runs = {ref["origin_run"] for ref in references.values()}
    for source in source_runs:
        for event in grading_repository.list_events(source, actor_id=owner, messages=kinds):
            row = event["payload"]
            ref = references.get(row.get("key"), {})
            if (row.get("owner") == owner and row.get("version") == VERSION
                    and ref.get("origin_run") == source and ref.get("digest") == row.get("digest")
                    and ref.get("kind") == row.get("kind")):
                records[row["key"]] = row
    if references.keys() - records.keys():
        raise CheckpointWriteError("programming_retry_evidence_missing")
    current = (auxiliary_events if auxiliary_events is not None else
               grading_repository.list_events(run_id, actor_id=owner, messages=(*kinds, STARTED)))
    for event in current:
        row = event["payload"]
        if row.get("version") == VERSION and row.get("owner") == owner:
            records[row["key"]] = row
    return records


def auxiliary_for_handoff(run, manifest, events):
    """Read completed or uncertain checks under the existing stop transaction."""
    current = [{"payload": event.payload} for event in events if event.message == AUXILIARY]
    records = _saved(run.id, manifest, run.teacher_id, auxiliary_events=current)
    for key, record in records.items():
        if (record.get("kind") != AUXILIARY or record.get("assignment") != run.assignment_id
                or record.get("key") != key or record.get("digest") != digest(record.get("value"))):
            raise CheckpointWriteError("programming_checkpoint_identity_invalid")
    return {key: record["value"] for key, record in records.items()}


def artifacts_for_retry(run_id, manifest, owner):
    # An interrupted preparation has unknown submission status and is never
    # inherited as a success. Only an explicit retry may start it again.
    return {k: {field: row[field] for field in ("origin_run", "kind", "digest")}
            for k, row in _saved(run_id, manifest, owner).items() if row.get("kind") in {PREPARED, PARTIAL, COMPLETED, AUXILIARY}}


def runtime_identity():
    return {"mode": settings.program_runner_mode, "image": settings.program_runner_image,
            "runtime": settings.program_runner_runtime, "version": VERSION}


class ProgrammingRun:
    def __init__(self, provider=None):
        self.provider = provider
        self.checkpoints = current_checkpoints()
        self.records = {}
        self.owner = None
        self.assignment = None
        self.run_id = uuid.uuid4().hex
        if self.checkpoints:
            cp = self.checkpoints
            run = grading_repository.get_run(cp.run_id)
            self.owner, self.assignment, self.run_id = run.teacher_id, run.assignment_id, run.id
            self.records = _saved(run.id, cp.manifest, self.owner)
        self.locks = {}
        # A shared unsuccessful attempt stops this batch's fan-out. It is never
        # persisted as a valid set of tests or a completed student execution.
        self.failures = {}

    def lock(self, key):
        return self.locks.setdefault(key, asyncio.Lock())

    def store(self, kind, key, value):
        row = {"kind": kind, "version": VERSION, "owner": self.owner, "assignment": self.assignment,
               "origin_run": self.run_id, "key": key, "value": value, "digest": digest(value)}
        if self.checkpoints:
            try:
                grading_repository.record_event(self.run_id, worker_id=self.checkpoints.worker_id,
                    level="info", message=kind, payload=row)
            except Exception as exc:
                raise CheckpointWriteError("programming_checkpoint_failed") from exc
        self.records[key] = row

    def read(self, key, kind):
        row = self.records.get(key)
        if row is None or row.get("kind") != kind:
            return None
        if (row.get("version") != VERSION or row.get("owner") != self.owner
                or row.get("assignment") != self.assignment or row.get("key") != key
                or row.get("digest") != digest(row.get("value"))):
            raise CheckpointWriteError("programming_checkpoint_identity_invalid")
        return deepcopy(row["value"])


def current_programming_run():
    return _current.get()


@contextmanager
def programming_run(registry, grading_setup):
    existing = _current.get()
    if existing is not None:
        yield existing
        return
    provider = None
    if grading_setup and callable(getattr(registry, "get", None)):
        provider = registry.get(grading_setup.primary_provider_id)
    elif callable(getattr(registry, "pick_default", None)):
        provider = registry.pick_default()
    state = ProgrammingRun(provider)
    token = _current.set(state)
    try:
        yield state
    finally:
        _current.reset(token)


def _source(content):
    # Remove only a single enclosing markup fence. A source file, including
    # leading indentation or comments containing backticks, stays byte-for-byte.
    fenced = re.fullmatch(r"\s*```(python|py|c\+\+|cpp|c|java)?[^\S\n]*\r?\n(.*?)^[ \t]*```\s*", content,
                          flags=re.DOTALL | re.MULTILINE | re.IGNORECASE)
    language, source = fenced.groups() if fenced else (None, content)
    if has_markup_fence(source, python=(language or "python").lower() in {"python", "py"}):
        raise ValueError("programming_ambiguous_code_blocks")
    return source


def _validate_report(report, cases):
    if (not isinstance(report, ExecutionReport) or len(report.results) != len(cases)
            or report.total_count != len(cases) or report.passed_count != sum(r.passed for r in report.results)
            or not math.isclose(report.pass_rate, report.passed_count / len(cases))):
        raise ValueError("programming_execution_contract_invalid")
    ids = set()
    for index, (case, result) in enumerate(zip(cases, report.results, strict=True)):
        if (result.test != case or result.executed is not True or type(result.passed) is not bool
                or (result.case_index is not None and result.case_index != index)
                or not math.isfinite(result.duration_ms) or result.duration_ms < 0):
            raise ValueError("programming_execution_contract_invalid")
        if settings.runtime_environment == "production" and result.isolation_mode != "oci_container":
            raise ValueError("programming_execution_contract_invalid")
        if result.isolation_mode == "oci_container":
            eid = getattr(result, "execution_id", None)
            if not eid or eid in ids or not re.fullmatch(r"[0-9a-f]{64}", getattr(result, "request_digest", "") or ""):
                raise ValueError("programming_execution_contract_invalid")
            ids.add(eid)


def _reference(problem):
    source = (problem._math_rubric_source or {}).get("presentation") or {}
    provenance = source.get("ai_completion_provenance") or {}
    # A generated solution is supplementary text for the scorer. Its output
    # cannot override a teacher's examples or certify another model's tests.
    if problem.solution_code and "solution_code" not in provenance:
        return _source(problem.solution_code)
    reference = (problem.reference_answer or "").strip()
    if "reference_answer" not in provenance and reference.lower().startswith(("```python\n", "```py\n", "```cpp\n", "```c++\n", "```c\n")):
        return _source(reference)
    return None


def _reference_language(problem, source):
    # The teacher's reference can be written in a different language from the
    # student answer. Prefer its explicit fence to any language in the stem.
    first = (problem.solution_code or problem.reference_answer or "").lstrip().splitlines()[0].strip().lower()
    fenced = {"```c": "c", "```cpp": "cpp", "```c++": "cpp", "```python": "python", "```py": "python"}
    if first in fenced:
        return fenced[first]
    if not has_native_main(source):
        return "python"
    try:
        ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        if re.search(r"c\+\+", problem.stem, re.I):
            return "cpp"
        if re.search(r"(?<![A-Za-z])C(?:11|17|语言|单文件|\b)", problem.stem, re.I):
            return "c"
        raise ValueError("programming_case_configuration_invalid") from None
    return "python"


def _reference_execution_identity(problem):
    runtime = runtime_identity()
    try:
        source = _reference(problem)
        if source and not has_function_only_contract(problem.stem, problem.test_cases):
            # Prior validations may have run a rewritten numeric stdin payload.
            runtime["reference_stdin_binding_version"] = 2
        copied = python_execution_source(source) if source else source
        if copied != source:
            runtime["execution_source_sha256"] = source_hash(copied)
    except ValueError:
        # Keep unverified material out of a prior successful preparation.
        # The regular preparation path reports the specific conflict.
        runtime["reference_execution_unavailable"] = True
    return runtime


def _question_key(state, problem):
    cp = state.checkpoints
    question = cp.questions.get(problem.q_id) if cp else None
    return digest({"engine": VERSION, "owner": state.owner, "assignment": state.assignment,
        "question": question.id if question else problem.q_id, "q_id": problem.q_id,
        "version": question.version if question else None, "stem": problem.stem,
        "rubric": problem.criterion, "max_score": problem.max_score, "reference": problem.reference_answer,
        "solution": problem.solution_code, "generated_reference_fields": sorted(
            ((problem._math_rubric_source or {}).get("presentation") or {}).get("ai_completion_provenance") or {}),
        "cases": [case.model_dump(mode="json") for case in problem.test_cases or ()],
        "config": cp.manifest.get("programming_preparation_config", cp.manifest.get("provider_configuration_fingerprint")) if cp else None,
        "runtime": _reference_execution_identity(problem)})


async def _notify(reporter, step):
    if reporter:
        from backend.services.math_grading_context import report_math_progress
        await report_math_progress(reporter._emit_message, step)


async def _prepare(state, problem, provider, generate, execute, reporter):
    from backend.services.programming_test_contract import ProgrammingCaseError, adapt_programming_cases, bind_stdin_cases, required_function_name
    from backend.skills.programming import _scan_complexity_keywords
    key = _question_key(state, problem)
    async with state.lock(key):
        if key in state.failures:
            return key, [], state.failures[key]
        saved = state.read(key, PREPARED)
        if saved:
            if (saved.get("q_id") != problem.q_id or saved.get("suite_hash") != digest(saved["cases"])
                    or not 1 <= len(saved["cases"]) <= 50):
                raise CheckpointWriteError("programming_suite_identity_invalid")
            return key, [TestCase.model_validate(row) for row in saved["cases"]], ""
        if state.read(key, STARTED):
            return key, [], "programming_preparation_interrupted"
        cases = problem.test_cases
        if not cases:
            if _scan_complexity_keywords(problem.stem):
                return key, [], "programming_mode_unsupported"
            state.store(STARTED, key, {"q_id": problem.q_id})
            await _notify(reporter, "prepare_programming_test_cases")
            from backend.progress.grading_activity import observe_auxiliary_request
            primary = state.provider or provider
            async with observe_auxiliary_request(reporter, primary):
                cases, error = await generate(primary, problem, function_name=required_function_name(problem.stem))
            if error or not cases:
                state.failures[key] = error or "programming_no_cases"
                return key, [], state.failures[key]
        try:
            derive_expectations = not problem.test_cases and any(isinstance(case, ReferenceInputCase) for case in cases)
            reference = _reference(problem)
            if derive_expectations:
                if not reference or not all(isinstance(case, ReferenceInputCase) for case in cases):
                    raise ValueError("programming_cases_unavailable")
                cases = [case.comparison_probe() for case in cases]
            cases = adapt_programming_cases(problem.stem, cases)
            if not cases or len(cases) > 50 or any(not c.sandbox_feasible for c in cases):
                raise ValueError("programming_cases_unavailable")
            data = [c.model_dump(mode="json") for c in cases]
            if len(json.dumps(data, ensure_ascii=False, allow_nan=False).encode()) > 256 * 1024:
                raise ValueError("programming_cases_unavailable")
            if reference:
                from backend.services.programming_test_contract import bind_programming_cases
                language = _reference_language(problem, reference)
                if language != "python" and not native_standard_supported(problem.stem + "\n" + problem.criterion, language, reference=True):
                    raise ValueError("native_standard_unsupported")
                executable = python_execution_source(reference) if language == "python" else reference
                ref_cases = (bind_programming_cases(problem.stem, cases, executable) if language == "python"
                             else bind_stdin_cases(problem.stem, cases))
                await _notify(reporter, "validate_programming_reference")
                report = await execute(executable, ref_cases, language=language, per_case_timeout=10.0)
                _validate_report(report, ref_cases)
                if derive_expectations:
                    cases = adapt_programming_cases(problem.stem, expectations_from_reference(cases, ref_cases, report))
                    data = [case.model_dump(mode="json") for case in cases]
                elif report.passed_count != len(cases):
                    raise ValueError("programming_reference_conflict")
        except ProgrammingCaseError as exc:
            state.failures[key] = ("programming_conflicting_expectations" if exc.issue == "conflicting_expectations"
                                   else "programming_case_configuration_invalid")
            return key, [], state.failures[key]
        except ValueError as exc:
            state.failures[key] = str(exc) if str(exc) in {
                "programming_conflicting_expectations", "programming_reference_conflict",
                "programming_cases_unavailable", "programming_execution_contract_invalid", "native_standard_unsupported",
            } else "programming_case_configuration_invalid"
            return key, [], state.failures[key]
        except (IsolatedRunnerUnavailable, HostCodeExecutionDisabled, TimeoutError) as exc:
            state.failures[key] = exc.reason if isinstance(exc, NativeBuildUnavailable) else "programming_execution_unavailable"
            return key, [], state.failures[key]
        state.store(PREPARED, key, {"q_id": problem.q_id, "cases": data, "suite_hash": digest(data),
                                    "reference_checked": bool(reference), "reference_derived_expectations": derive_expectations})
        return key, cases, ""


def has_native_main(code):
    # Ignore comments, literals and prototypes when identifying an entry.
    tokens = re.sub(r"""//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'""", " ", code, flags=re.S)
    # A call to main inside a helper, or a prototype followed by a helper
    # definition, does not establish an entry. The isolated linker is final.
    depth = 0
    candidate = False
    for match in re.finditer(r"[{};]|\bmain\s*\(", tokens):
        token = match[0]
        if token == "{":
            if depth == 0 and candidate:
                return True
            depth += 1
        elif token == "}":
            depth = max(0, depth - 1)
        elif depth == 0:
            candidate = token != ";"
    return False


async def prepare_pending(state, problems, students, selection, reporter):
    """Prepare before expert fan-out, only for questions with work to grade."""
    if state.provider is None:
        return
    from backend.skills.programming import _generate_test_cases, _detect_language, run_sandbox
    pending = set()
    for student in students.values():
        sid = student.get("stu_id")
        for answer in student.get("stu_ans", ()):
            qid = answer.get("q_id")
            if not sid or qid not in problems or (selection is not None and qid not in selection.get(sid, ())):
                continue
            content = answer.get("content") or ""
            flags = answer.get("flag") or ()
            if not content.strip() or "recognition_manual_pending" in flags or C_FAMILY_FLAG in flags:
                continue
            if any(f in NATIVE_LANGUAGES for f in flags):
                question = problems[qid]
                material = (str(question.get("stem") or "") + "\n" + str(question.get("criterion") or "")
                            if isinstance(question, dict) else question.stem + "\n" + question.criterion)
                eligible = has_native_main(content) and all(
                    native_standard_supported(material, NATIVE_LANGUAGES[f]) for f in flags if f in NATIVE_LANGUAGES
                )
            else:
                eligible = PYTHON_FLAG in flags or _detect_language(content) not in {"cpp", "java"}
            if eligible:
                pending.add(qid)
    for qid, raw in problems.items():
        if qid not in pending or not is_programming_question_type(raw.get("type") if isinstance(raw, dict) else raw.type):
            continue
        try:
            problem = ProblemInfo.model_validate(raw)
        except ValidationError:
            continue  # The grading adapter already projects invalid question inputs as unscored.
        if isinstance(raw, dict):
            problem._math_rubric_source = raw.get("_math_rubric_source") or {}
        await _prepare(state, problem, state.provider, _generate_test_cases, run_sandbox, reporter)


def _report_from_rows(rows):
    total = len(rows)
    passed = sum(r.passed for r in rows)
    return ExecutionReport(passed_count=passed, total_count=total, pass_rate=passed / total,
                           results=rows, summary=f"Passed {passed}/{total} tests")


def _persisted_row(row):
    return {k: v for k, v in asdict(row).items() if k != "test"}


async def _execute_remaining(state, key, identity, code, cases, execute):
    """Resume only verified cases of this exact submission. Incomplete suites never vote."""
    rows = {}
    partial = state.read(key, PARTIAL)
    if partial:
        if partial.get("identity") != identity:
            raise CheckpointWriteError("programming_partial_identity_invalid")
        for item in partial["rows"]:
            index = item["case_index"]
            if type(index) is not int or not 0 <= index < len(cases) or index in rows:
                raise CheckpointWriteError("programming_partial_identity_invalid")
            rows[index] = TestResult(test=cases[index], **item)
    missing = [i for i in range(len(cases)) if i not in rows]
    if not missing:
        # Cancellation can arrive after the final compared case but before the
        # aggregate is returned. Revalidate every row without dispatching an empty suite.
        report = _report_from_rows([rows[i] for i in range(len(cases))])
        _validate_report(report, cases)
        return report
    subset = [cases[i] for i in missing]

    def restore_index(row, local):
        original = missing[local]
        return replace(row, case_index=original, case_id=getattr(cases[original], "case_id", None) or f"case-{original + 1}")

    async def completed(row):
        local = row.case_index
        if type(local) is not int or not 0 <= local < len(subset) or missing[local] in rows:
            raise ValueError("programming_execution_contract_invalid")
        if row.case_id != (getattr(subset[local], "case_id", None) or f"case-{local + 1}"):
            raise ValueError("programming_execution_contract_invalid")
        # Validate the callback before saving any partial evidence. Full reports
        # also verify unique execution IDs across cases upon assembly.
        local_row = replace(row, case_index=0)
        _validate_report(_report_from_rows([local_row]), [subset[local]])
        rows[missing[local]] = restore_index(row, local)

    options = {"language": identity.get("language", "python"), "per_case_timeout": 10.0}
    if getattr(execute, "supports_case_checkpoints", False) is True:
        options["on_case_complete"] = completed
    try:
        result = await execute(code, subset, **options)
        _validate_report(result, subset)
    except (IsolatedRunnerUnavailable, HostCodeExecutionDisabled, TimeoutError, asyncio.CancelledError):
        if len(cases) - len(missing) < len(rows):
            state.store(PARTIAL, key, {"identity": identity,
                                      "rows": [_persisted_row(rows[i]) for i in sorted(rows)]})
        raise
    if not partial:
        return result
    for local, row in enumerate(result.results):
        rows[missing[local]] = restore_index(row, local)
    report = _report_from_rows([rows[i] for i in range(len(cases))])
    _validate_report(report, cases)
    return report


async def evidence_for_answer(*, problem: ProblemInfo, answer: StudentAnswerInfo, student_id: str,
                              task_id, provider, reporter, generate, execute) -> ProgrammingEvidence:
    from backend.services.programming_test_contract import bind_programming_cases, bind_stdin_cases
    from backend.skills.programming import _detect_language
    state = _current.get() or ProgrammingRun(provider)
    try:
        code = _source(answer.content)
    except ValueError:
        return ProgrammingEvidence("unavailable", answer.content, [], reason="programming_mapping_invalid")
    if "recognition_manual_pending" in answer.flag:
        return ProgrammingEvidence("unavailable", code, [], reason="programming_mapping_invalid")
    if len(code.encode()) > 64 * 1024:
        return ProgrammingEvidence("unavailable", code, [], reason="programming_source_too_large")
    if not code.strip():
        return ProgrammingEvidence("no_submission", code, [])
    native = [NATIVE_LANGUAGES[flag] for flag in answer.flag if flag in NATIVE_LANGUAGES]
    if native and (len(native) != 1 or PYTHON_FLAG in answer.flag or C_FAMILY_FLAG in answer.flag):
        return ProgrammingEvidence("unavailable", code, [], reason="programming_mapping_invalid")
    if native and not native_standard_supported(problem.stem + "\n" + problem.criterion, native[0]):
        return ProgrammingEvidence("unavailable", code, [], reason="native_standard_unsupported")
    if native and not has_native_main(code):
        return ProgrammingEvidence("unavailable", code, [], reason="native_main_required")
    if C_FAMILY_FLAG in answer.flag:
        return ProgrammingEvidence("unsupported", code, [], reason="programming_language_unsupported")
    if not native and PYTHON_FLAG not in answer.flag and _detect_language(code) in {"cpp", "java"}:
        try:
            ast.parse(code)  # A Python string or comment may contain a C/Java example.
        except (ValueError, SyntaxError, RecursionError):
            return ProgrammingEvidence("unsupported", code, [], reason="programming_language_unsupported")
    try:
        executable = code if native else python_execution_source(code)
    except PythonSourceEncodingError:
        return ProgrammingEvidence("unavailable", code, [], reason="programming_source_encoding_unavailable")
    plan_key, cases, reason = await _prepare(state, problem, provider, generate, execute, reporter)
    if reason:
        return ProgrammingEvidence("unsupported" if reason == "programming_mode_unsupported" else "unavailable",
                                   code, [], reason=reason)
    case_ids = [digest([plan_key, index, c.model_dump(mode="json")]) for index, c in enumerate(cases)]
    try:
        hints = {f.removeprefix("programming_source_entry:") for f in answer.flag
                 if f.startswith("programming_source_entry:")}
        if len(hints) > 1:
            raise ValueError("programming_mapping_invalid")
        if native:
            if hints or any(case.io_mode != "stdin" for case in cases):
                return ProgrammingEvidence("unavailable", code, [], reason="native_main_required")
            cases = bind_stdin_cases(problem.stem, cases)
        elif hints:
            cases = bind_programming_cases(problem.stem, cases, executable, entry_hint=hints.pop())
        else:
            cases = bind_programming_cases(problem.stem, cases, executable, validated_reference=_reference(problem))
    except ValueError:
        return ProgrammingEvidence("unavailable", code, [], reason="programming_mapping_invalid")
    cp = state.checkpoints
    identity = {"plan": plan_key, "assignment": state.assignment or task_id, "student": student_id,
                "revision": cp.revisions[student_id] if cp else None, "source_sha256": source_hash(code),
                "q_id": problem.q_id, "cases_sha256": digest([c.model_dump(mode="json") for c in cases]),
                "runtime": runtime_identity()}
    if native:
        identity["language"] = native[0]
    if executable != code:
        identity["execution_source_sha256"] = source_hash(executable)
    key = digest(identity)
    async with state.lock(key):
        if key in state.failures:
            return ProgrammingEvidence("unavailable", code, cases, reason=state.failures[key])
        saved = state.read(key, COMPLETED)
        if saved:
            if saved.get("identity") != identity or len(saved["rows"]) != len(cases):
                raise CheckpointWriteError("programming_evidence_identity_invalid")
            rows = [TestResult(test=c, **r) for c, r in zip(cases, saved["rows"], strict=True)]
            report = ExecutionReport(results=rows, **saved["summary"])
        else:
            await _notify(reporter, "run_programming_test_cases")
            try:
                report = await _execute_remaining(state, key, identity, executable, cases, execute)
            except (IsolatedRunnerUnavailable, HostCodeExecutionDisabled, TimeoutError, ValueError) as exc:
                if isinstance(exc, NativeBuildUnavailable):
                    state.failures[key] = exc.reason
                else:
                    state.failures[key] = "native_runner_unavailable" if native else "programming_execution_unavailable"
                return ProgrammingEvidence("unavailable", code, cases, reason=state.failures[key])
            saved = {"identity": identity,
                "rows": [_persisted_row(row) for row in report.results],
                "summary": {k: getattr(report, k) for k in ("passed_count", "total_count", "pass_rate", "summary")},
                "case_ids": case_ids}
            saved["evidence_id"] = digest([state.run_id, identity, saved["rows"]])
            state.store(COMPLETED, key, saved)
        _validate_report(report, cases)
        if saved["case_ids"] != case_ids:
            raise CheckpointWriteError("programming_case_identity_invalid")
        return ProgrammingEvidence("executed", code, cases, report, evidence_id=saved["evidence_id"],
                                   case_ids=tuple(case_ids), source_language=identity.get("language", "python"))
