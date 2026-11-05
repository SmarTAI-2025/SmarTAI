"""Translate shared programming run facts into scoring input and safe feedback."""
from __future__ import annotations

import json
import re
from collections import Counter

from backend.models import ProblemInfo, ProgrammingEvidence, ProgrammingTestCounts
from backend.services.programming_test_contract import required_function_name

# Only student outcomes may vote. An unknown runner status must fail closed.
_STUDENT_FAILURES = {
    "wrong_answer", "compile_error", "syntax_error", "runtime_error", "timeout",
    "output_limit", "memory_limit", "pid_limit", "target_function_missing", "signature_mismatch",
}
_MAX_CASE_EXAMPLES = 6


def _failure_reason(result):
    reason = result.exit_reason
    if reason in _STUDENT_FAILURES:
        return reason
    # Earlier TestResults reported process success before comparing stdout.
    if reason in {"", "success"} and not result.error:
        return "wrong_answer"
    return None


def _short(value: str, limit=140):
    return value if len(value) <= limit else value[:limit - 1] + "…"


def _case_detail(case_id, case_label, case, result, reason):
    detail = {"case_id": case_id, "case_label": case_label, "source": case.source, "outcome": reason}
    if not result.passed:
        expected = case.expected_return if case.function_name else case.expected_output
        if case.function_name:
            arguments = {"args": case.function_args, "kwargs": getattr(case, "function_kwargs", None) or {}}
            detail["call_arguments"] = _short(json.dumps(arguments, ensure_ascii=False))
        else:
            detail["input"] = _short(case.input)
        detail["expected"] = _short(expected or "")
        detail["actual"] = _short(result.actual_output)
        # A traceback, temporary path or arbitrary exception message is never
        # needed to explain a runtime error. Only keep a Python exception type.
        if result.error:
            last_line = result.error.strip().splitlines()[-1:]
            match = re.match(r"([A-Za-z]{1,32}(?:Error|Exception))\b", last_line[0]) if last_line else None
            if match:
                detail["error_type"] = match[1]
    return detail


def unavailable(reason, evidence_id, language, *, mapping="unknown", execution="not_executed"):
    message = (f"未评分，未取得有效执行证据（{reason}）。请检查后重试或人工复核。"
               if language == "zh" else
               f"Ungraded. Execution evidence is unavailable ({reason}). Check the configuration or review this item.")
    return ProgrammingEvidence(
        evidence_id=evidence_id, execution_status=execution, mapping_status=mapping,
        score_status="unavailable", primary_reason=reason,
        requires_human_review=True, safe_summary=message,
    )


def describe_run(run, language, problem: ProblemInfo):
    """Return a public projection and bounded prompt examples from the same facts."""
    if run.status == "no_submission":
        summary = "未提交代码，未运行。" if language == "zh" else "No code submitted. No execution."
        return ProgrammingEvidence(
            evidence_id=run.evidence_id, score_status="final", execution_status="not_requested",
            primary_reason="no_submission", safe_summary=summary, requires_human_review=False,
        ), []
    if run.status == "unsupported" and run.reason in {
        "programming_language_unsupported", "language_unsupported", "programming_mode_unsupported",
    }:
        if run.reason.endswith("language_unsupported") and (
            required_function_name(problem.stem) or
            any(case.io_mode == "function" or case.function_name for case in problem.test_cases or ())
        ):
            # A confirmed function protocol on an unsupported language cannot
            # be satisfied by the current runner. Never turn it into a vote.
            return unavailable("mode_unsupported", run.evidence_id, language,
                               mapping="conflict", execution="unsupported"), []
        # Code review of a genuinely unsupported language/task is a scored
        # advisory. A bad mapping, input or runner configuration is unscored.
        summary = ("沙箱测评：未运行，仅依据源码和教师标准审查，建议复核。"
                   if language == "zh" else
                   "Sandbox skipped: outside the sandbox's supported capabilities. AI code review only; teacher review recommended.")
        return ProgrammingEvidence(
            evidence_id=run.evidence_id, score_status="provisional", execution_status="unsupported",
            primary_reason=run.reason, safe_summary=summary, requires_human_review=True,
        ), []
    if run.status != "executed":
        reason = run.reason or "contract_invalid"
        mapping = "unknown"
        if reason in {"runner_mode_not_supported", "mode_unsupported"}:
            return unavailable("mode_unsupported", run.evidence_id, language, mapping="conflict", execution="unsupported"), []
        if "ambiguous" in reason:
            mapping = "ambiguous"
        elif "mapping" in reason:
            mapping = "missing" if "missing" in reason else "conflict"
        return unavailable(reason, run.evidence_id, language, mapping=mapping), []

    report = run.report
    invalid = unavailable("contract_invalid", run.evidence_id, language)
    if (not run.cases or report is None or report.total_count != len(run.cases)
            or len(report.results) != len(run.cases)
            or report.passed_count != sum(row.passed for row in report.results)):
        return invalid, []

    counts = {"teacher": ProgrammingTestCounts(), "llm_generated": ProgrammingTestCounts()}
    failure_counts = Counter()
    # The first failure for each (reason, source) comes before repeated
    # failures and all successes, regardless of the case's original position.
    representatives, repeated, passed = [], [], []
    seen = set()
    ids = set()
    modes = set()
    # Persistent runs assign IDs to the frozen suite; older reports only have
    # positional labels within that suite.
    frozen_ids = getattr(run, "case_ids", ())
    if frozen_ids and (len(frozen_ids) != len(run.cases) or len(set(frozen_ids)) != len(frozen_ids)):
        return invalid, []
    for index, (case, row) in enumerate(zip(run.cases, report.results)):
        expected_id = getattr(case, "case_id", None) or f"case-{index + 1}"
        case_id = getattr(row, "case_id", "") or expected_id
        if (case != row.test or getattr(row, "executed", True) is not True
                or case_id != expected_id or case_id in ids
                or getattr(row, "case_index", None) not in (None, index)):
            return invalid, []
        ids.add(case_id)
        case_label = case_id
        if frozen_ids:
            case_id = frozen_ids[index]
        modes.add(row.isolation_mode)
        count = counts[case.source]
        count.total += 1
        if row.passed:
            if row.exit_reason not in ("", "success"):
                return invalid, []
            count.passed += 1
            passed.append(_case_detail(case_id, case_label, case, row, "passed"))
            continue
        reason = _failure_reason(row)
        if reason is None:
            return invalid, []
        failure_counts[reason] += 1
        key = (reason, case.source)
        detail = _case_detail(case_id, case_label, case, row, reason)
        (repeated if key in seen else representatives).append(detail)
        seen.add(key)

    if len(modes) != 1 or not modes <= {"oci_container", "host_subprocess"}:
        return invalid, []
    teachers, generated = counts["teacher"], counts["llm_generated"]
    fragments = []
    for stat, en, zh in [(teachers, "teacher-provided tests", "教师用例"),
                         (generated, "AI-generated tests", "AI 生成用例")]:
        if stat.total:
            fragments.append(f"{zh}通过 {stat.passed}/{stat.total}" if language == "zh"
                             else f"{en}: passed {stat.passed}/{stat.total}")
    if representatives:
        failures = ", ".join(f"{d['case_label']} ({d['outcome']})" for d in representatives[:_MAX_CASE_EXAMPLES])
        fragments.append(("失败示例：" if language == "zh" else "Failed cases: ") + failures)
    ai_only = not teachers.total
    if ai_only:
        fragments.append("仅有 AI 生成用例，建议教师复核" if language == "zh"
                         else "Only AI-generated expectations were tested. Teacher review recommended.")
    first = next((d for d in representatives if d["source"] == "teacher"),
                 representatives[0] if representatives else None)
    primary = first["outcome"] if first else "none"
    if primary == "wrong_answer" and first["source"] == "teacher":
        primary = "teacher_case_failed"
    return ProgrammingEvidence(
        evidence_id=run.evidence_id, mapping_status="valid", execution_status="executed",
        score_status="provisional" if ai_only else "final", isolation_mode=modes.pop(),
        teacher_tests=teachers, generated_tests=generated, failure_counts=dict(failure_counts),
        primary_reason=primary, requires_human_review=ai_only, safe_summary=" · ".join(fragments),
    ), (representatives + repeated + passed)[:_MAX_CASE_EXAMPLES]
