"""Translate shared programming run facts into scoring input and safe feedback."""
from __future__ import annotations

import json
import re
from collections import Counter

from backend.models import ProblemInfo, ProgrammingEvidence, ProgrammingTestCounts
from backend.services.programming_test_contract import required_function_name

# Only student outcomes may vote. An unknown runner status must fail closed.
_STUDENT_FAILURES = {
    "wrong_answer": ("结果与预期不符", "result differs from the expected value"),
    "compile_error": ("编译失败", "compilation failed"),
    "link_error": ("链接失败", "linking failed"),
    "syntax_error": ("Python 语法错误", "Python syntax error"),
    "runtime_error": ("程序运行出错", "program raised an error"),
    "timeout": ("程序运行超时", "program exceeded the time limit"),
    "output_limit": ("输出超过限制", "output exceeded the limit"),
    "memory_limit": ("内存使用超过限制", "memory limit exceeded"),
    "pid_limit": ("进程数超过限制", "process limit exceeded"),
    "target_function_missing": ("未找到题目要求的函数", "required function was missing"),
    "signature_mismatch": ("函数参数与题目要求不符", "function parameters did not match the requirement"),
}
# These fixed explanations may enter a teacher's saved result. Unknown codes
# and exception strings stay out of public text.
_UNGRADED_REASONS = {
    "native_standard_unsupported": ("本题要求的语言标准超出 C11 / C++17 支持范围，请按题目要求人工复核。", "The requested standard is outside C11 / C++17. Review the submission using the required standard."),
    "native_main_required": ("请教师复核函数片段，或提交含 main 的单文件标准输入程序。", "Review the function fragment or submit one stdin program with main."),
    "toolchain_unavailable": ("请管理员修复镜像中的编译器、头文件或链接器，再重试。", "Ask the administrator to repair the compiler, headers or linker in the image, then retry."),
    "native_runner_unavailable": ("请管理员检查隔离运行时和镜像，再重试。", "Ask the administrator to check the OCI runtime and image, then retry."),
    "programming_execution_unavailable": ("测试服务未能完成运行，请稍后重试失败项。", "The test service could not finish running the tests. Retry the failed items."),
    "programming_reference_conflict": ("参考程序未通过本题测试，请核对参考程序与预期结果。", "The reference program failed the tests. Check it against the expected results."),
    "programming_conflicting_expectations": ("同一输入有不同的预期结果，请核对本题测试样例。", "The same input has different expected results. Check the test cases."),
    "programming_case_configuration_invalid": ("测试样例与题目要求不匹配，请核对输入、预期结果和函数要求。", "Check the test inputs and expected results against the question and its function requirements."),
    "programming_cases_unavailable": ("这组样例无法运行，请核对样例内容、数量及大小。", "These tests could not be run. Check their content, number and size."),
    "programming_no_cases": ("未能生成可靠的测试样例，请补充输入和预期结果后重试。", "No reliable tests were generated. Add inputs and expected results, then retry."),
    "programming_preparation_interrupted": ("测试样例准备中断，请重试失败项。", "Test preparation was interrupted. Retry the failed items."),
    "programming_mapping_invalid": ("无法确定要测试的作答或函数，请核对本题与学生源码的对应关系。", "The submission or function to test is unclear. Check which code belongs to this question."),
    "programming_source_encoding_unavailable": ("无法按源码声明的字符编码运行，请核对原文件的编码。", "The source could not be run using its declared text encoding. Check the original file."),
    "programming_source_too_large": ("源码超过单文件执行上限，请人工复核。", "The source exceeds the single-file execution limit. Review it manually."),
    "mode_unsupported": ("测试服务无法运行本题要求的调用方式，请人工复核。", "The test service cannot run the required type of call. Review this item manually."),
    "contract_invalid": ("无法核实执行结果是否对应本题测试，请重试失败项。", "The results could not be verified against this question’s tests. Retry the failed items."),
    "parse_failed": ("模型返回的内容不完整或格式不符合要求，请重试失败项。", "The model returned an incomplete or invalid response. Retry the failed items."),
    "transient_llm": ("模型服务连接失败或响应超时，请稍后重试。", "The model connection failed or timed out. Try again later."),
    "zero_quota": ("模型当前无可用额度，请检查账户额度或更换模型。", "The model has no available quota. Check the account allowance or choose another model."),
    "daily_quota_exhausted": ("模型今日额度已用完，请更换模型或在额度恢复后重试。", "The model’s daily quota is exhausted. Choose another model or retry when the quota resets."),
    "quota_exhausted": ("模型服务限制了请求，请稍后重试并核对可用额度。", "The model service limited requests. Check the available quota and try again later."),
    "provider_auth_failed": ("模型服务未接受凭据，请核对模型设置。", "The model service rejected the credentials. Check the model settings."),
    "provider_permission_denied": ("当前账户无法调用此模型，请核对权限或更换模型。", "The account cannot use this model. Check its permissions or choose another model."),
    "provider_model_not_found": ("模型服务找不到所选模型，请核对模型设置。", "The selected model was not found. Check the model settings."),
    "provider_request_rejected": ("模型服务拒绝了请求，请核对模型设置后重试。", "The model service rejected the request. Check the model settings before retrying."),
    "provider_content_blocked": ("模型服务拦截了此次内容，请检查材料或人工复核。", "The model service blocked the content. Check the material or review it manually."),
    "provider_recitation_blocked": ("模型服务因内容重复限制拦截了回复，请人工复核。", "The model service blocked the response under its recitation policy. Review the item manually."),
    "provider_submit_uncertain": ("模型服务是否已接收请求尚未确认，请先核对调用记录。", "The model service may have received the request. Check the request history before retrying."),
}


def ungraded_message(reason, language):
    if reason == "programming_execution_contract_invalid":
        reason = "contract_invalid"
    messages = _UNGRADED_REASONS.get(reason, (
        "未能取得有效的批改结果，请查看任务进度或人工复核。",
        "A valid grading result was unavailable. Check task progress or review the item manually.",
    ))
    return "未评分。" + messages[0] if language == "zh" else "Ungraded. " + messages[1]


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
        if reason == "compile_error":
            location = re.fullmatch(r"(missing_semicolon|missing_header|unknown_identifier|source_error) line ([1-9]\d{0,4})",
                                    result.error or "")
            if location:
                detail.update(diagnostic=location[1], line=int(location[2]))
        elif reason == "link_error" and result.error in {"unresolved_symbol", "duplicate_symbol"}:
            detail["diagnostic"] = result.error
        if result.error:
            last_line = result.error.strip().splitlines()[-1:]
            match = re.match(r"([A-Za-z]{1,32}(?:Error|Exception))\b", last_line[0]) if last_line else None
            if match:
                detail["error_type"] = match[1]
    return detail


def unavailable(reason, evidence_id, language, *, mapping="unknown", execution="not_executed"):
    return ProgrammingEvidence(
        evidence_id=evidence_id, execution_status=execution, mapping_status=mapping,
        score_status="unavailable", primary_reason=reason,
        requires_human_review=True, safe_summary=ungraded_message(reason, language),
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
        detail["case_number"] = index + 1
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
        hints = {
            "compile_error": ("编译失败，请检查语法、类型和头文件。", "Compilation failed. Check syntax, types and headers."),
            "link_error": ("链接失败，请在同一文件中定义引用的函数。", "Linking failed. Define the referenced symbols in this file."),
            "wrong_answer": ("输出不符，请对照该用例的预期值。", "Output differs. Compare it with the expected value for this case."),
            "runtime_error": ("程序异常退出，请检查内存访问和退出状态。", "The program exited abnormally. Check memory accesses and exit status."),
            "timeout": ("运行超时，请检查循环终止条件和复杂度。", "Execution timed out. Check loop termination and complexity."),
        }
        for detail in representatives:
            if detail.get("line"):
                action = {
                    "missing_semicolon": ("检查此处及上一行末尾的分号。", "Check for a missing semicolon here or on the preceding line."),
                    "missing_header": ("检查头文件名及拼写。", "Check the included header name."),
                    "unknown_identifier": ("检查变量或函数的声明和拼写。", "Check the name and declaration."),
                    "source_error": ("检查该行附近的语法和类型。", "Check syntax and types near this line."),
                }[detail["diagnostic"]]
                hints["compile_error"] = (f"编译器报错位置为第 {detail['line']} 行。{action[0]}",
                                          f"Compiler error at line {detail['line']}. {action[1]}")
            if detail.get("diagnostic") == "duplicate_symbol":
                hints["link_error"] = ("链接时发现重复定义，请为每个符号保留一份定义。",
                                       "Linking found duplicate definitions. Keep one definition per symbol.")
        fragments.extend(hints[k][0 if language == "zh" else 1] for k in failure_counts if k in hints)
        failures = ("、" if language == "zh" else ", ").join(
            f"样例 {d['case_number']}（{_STUDENT_FAILURES[d['outcome']][0]}）" if language == "zh"
            else f"case {d['case_number']} ({_STUDENT_FAILURES[d['outcome']][1]})"
            for d in representatives[:_MAX_CASE_EXAMPLES]
        )
        fragments.append(("失败示例：" if language == "zh" else "Failed cases: ") + failures)
    ai_only = not teachers.total
    if ai_only:
        fragments.append("仅有 AI 生成用例，建议教师复核" if language == "zh"
                         else "Only AI-generated expectations were tested. Teacher review recommended.")
    # An entry/argument error can verify the interface deduction but gives no
    # observed result for the algorithm. Preserve the teacher's process credit.
    interface_only = not passed and bool(teachers.total) and set(failure_counts) <= {"target_function_missing", "signature_mismatch"}
    if interface_only:
        fragments.append("所有样例都未能调用指定函数，请核对实现思路应得的分数" if language == "zh"
                         else "No test could call the required function. Review any credit for the implementation.")
    first = next((d for d in representatives if d["source"] == "teacher"),
                 representatives[0] if representatives else None)
    primary = first["outcome"] if first else "none"
    if primary == "wrong_answer" and first["source"] == "teacher":
        primary = "teacher_case_failed"
    return ProgrammingEvidence(
        evidence_id=run.evidence_id, mapping_status="valid", execution_status="executed",
        score_status="provisional" if ai_only or interface_only else "final", isolation_mode=modes.pop(),
        teacher_tests=teachers, generated_tests=generated, failure_counts=dict(failure_counts),
        primary_reason=primary, requires_human_review=ai_only or interface_only, safe_summary=" · ".join(fragments),
    ), (representatives + repeated + passed)[:_MAX_CASE_EXAMPLES]
