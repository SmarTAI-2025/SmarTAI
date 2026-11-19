"""Small, source-free question diagnostics shared by live and saved progress."""
from copy import deepcopy
import re
import json
from backend.domain.confidence_policy import question_issue_blocks_progress
from backend.domain.question_types import normalize_question_type


def required_programming_tests_missing(problem):
    """A funded test-suite award cannot be graded without its test evidence."""
    if normalize_question_type(problem.get("type")) != "编程题" or problem.get("test_cases"):
        return False
    draft = problem.get("rubric_draft")
    plan = draft.get("plan") if isinstance(draft, dict) else None
    rules = plan.get("part_rubric") if isinstance(plan, dict) else None
    parts = rules.get("parts") if isinstance(rules, dict) else None
    return any(isinstance(step, dict) and step.get("test_suite") is True
               for part in parts if isinstance(part, dict) and isinstance(part.get("steps"), list)
               for step in part["steps"]
               ) if isinstance(parts, list) else False


def required_programming_tests_issue(q_id):
    return {"issue_id": f"required-tests:{q_id}", "q_id": q_id, "field": "programming_tests",
            "code": "generation_failed", "status": "open", "severity": "blocking",
            "source_tokens": [], "details": {"reason": "required_programming_tests_missing"}}


def current_programming_test_issues(issues, problem, q_id):
    """Rebuild the derived test dependency without touching source issues."""
    current = [issue for issue in issues
               if (issue.get("details") or {}).get("reason") != "required_programming_tests_missing"]
    if required_programming_tests_missing(problem) and not any(
        issue.get("field") == "programming_tests" and issue.get("status") == "open"
        and issue.get("severity") == "blocking" for issue in current
    ):
        current = [issue for issue in current
                   if not (issue.get("field") == "programming_tests" and issue.get("code") == "generation_failed")]
        current.append(required_programming_tests_issue(q_id))
    return current


def normalize_formula_issue(issue):
    """A transcription disagreement is not evidence of missing source content."""
    if (issue.get("details") or {}).get("reason") != "conflicting_printed_formula":
        return issue
    details = {**issue.get("details", {}), "confidence_scope": "question"}
    coverage = details.get("coverage") or {}
    if details.get("error_code") or any(coverage.get(key) for key in ("failed_pages", "unprocessed_pages", "missing_targets")):
        return {**issue, "code": "recognition_partial", "severity": "blocking"}
    details.pop("incomplete", None)
    return {**issue, "code": "recognition_needs_review", "severity": "warning", "details": details}


def effective_question_issues(presentation, question_type=None):
    from backend.services.math_grading_context import uses_math_grading
    non_math = bool(normalize_question_type(question_type)) and not uses_math_grading(question_type)
    issues = []
    for issue in presentation.get("preparation_issues", []):
        issue = normalize_formula_issue(issue)
        if (non_math and issue.get("field") == "rubric"
                and (issue.get("details") or {}).get("reason") == "invalid_structured_rubric"):
            # Keep the old diagnostic for provenance, but do not require a
            # compiler that the corrected question type will never use.
            issue = {**issue, "status": "resolved"}
        coverage = (issue.get("details") or {}).get("coverage") or {}
        # Old source scores/labels were broadcast to every question. They are
        # not per-question assessments; keep actual failure evidence visible,
        # including incomplete question fragments from saved-text recovery.
        if (issue.get("code") in {"recognition_partial", "recognition_needs_review"}
                and not (issue.get("code") == "recognition_partial" and issue.get("severity") == "blocking")
                and (issue.get("details") or {}).get("confidence_scope") != "question"
                and not (issue.get("details") or {}).get("error_code")
                and not (issue.get("details") or {}).get("incomplete")
                and not any(coverage.get(key) for key in ("failed_pages", "unprocessed_pages", "missing_targets"))):
            continue
        if (issue.get("field") != "answer" and issue.get("code") in {"recognition_partial", "recognition_needs_review"} and issue.get("status") != "resolved"
                and ((issue.get("details") or {}).get("error_code")
                     or (issue.get("details") or {}).get("incomplete")
                     or any(coverage.get(key) for key in ("failed_pages", "unprocessed_pages", "missing_targets")))):
            issue = {**issue, "severity": "blocking", "status": "open"}
        if issue.get("code") == "manual_completion_required":
            issue = {**issue, "details": {**(issue.get("details") or {}), "required_fields": [
                field for field in (issue.get("details") or {}).get("required_fields", []) if field in {"number", "type", "stem", "criterion"}]}}
        if issue.get("field") == "answer" or (issue.get("code") == "generation_failed" and issue.get("field") == "programming_tests"
                and (issue.get("details") or {}).get("reason") != "required_programming_tests_missing") or (
            issue.get("code") in {"low_confidence", "recognition_needs_review"}
            and not (issue.get("details") or {}).get("error_code")
            and not any(coverage.get(key) for key in ("failed_pages", "unprocessed_pages", "missing_targets"))
        ):
            issue = {**issue, "severity": "warning"}
        issues.append(issue)
    return issues


def preparation_review(packages):
    result = []
    for q_id, row in packages.items():
        issues = []
        for issue in effective_question_issues(row, row.get("type")):
            if issue.get("status") != "open":
                continue
            details = issue.get("details") or {}
            safe = {key: deepcopy(details[key]) for key in (
                "reason", "incomplete", "confidence", "confidence_scope", "confidence_reasons", "coverage", "error_code",
                "required_fields", "review_display_reason", "provider_error_code", "provider_http_status", "alternative_caps",
            ) if key in details}
            draft = details.get("rubric_review_draft") or {}
            if draft.get("invalid_fields"):
                # Paths, never draft values or raw model responses.
                safe["invalid_fields"] = list(draft["invalid_fields"])[:24]
            issues.append({"issue_id": issue.get("issue_id", ""), "q_id": q_id,
                "field": issue.get("field"), "code": issue.get("code"),
                "severity": "blocking" if question_issue_blocks_progress(issue, row) else "warning", "status": "open", "details": safe})
        if issues:
            result.append({"q_id": q_id, "number": str(row.get("number") or ""), "issues": issues})
    return result


def bounded_preparation_review(rows):
    """Leave room for per-question counters in the 64 KiB progress envelope."""
    result, size = [], 0
    for original in rows:
        row = {**original, "issues": [{**i, "severity": "blocking"} for i in original["issues"] if question_issue_blocks_progress(i, original)]}
        if not row["issues"]:
            continue
        cost = len(json.dumps(row, ensure_ascii=False).encode("utf-8"))
        if size + cost > 8000 or len(result) >= 20:
            break
        result.append(row)
        size += cost
    return result


def public_preparation_review(value):
    """Allowlist persisted diagnostics as well as new worker snapshots."""
    result = []
    codes = {"recognition_partial", "recognition_needs_review", "low_confidence", "generation_failed",
             "manual_completion_required", "parse_anomaly", "source_conflict", "ambiguous_question_match",
             "rubric_step_reference_conflict", "invalid_test_case", "reference_solution_failed_case",
             "math_rubric_advisory", "ai_source_conflict", "unmapped_source_content", "max_score_not_found",
             "default_max_score_requires_review", "material_recognition_review_required", "material_parser_provider_required"}
    fields = {"stem", "answer", "rubric", "source", "programming_tests", "max_score"}
    reasons = {"conflicting_printed_formula", "invalid_structured_rubric", "test_case_output_conflict", "alternative_cap_review",
               "required_programming_tests_missing", "programming_test_award_unset", "teacher_rule_interpretation"}
    for row in value[:500] if isinstance(value, list) else []:
        if not isinstance(row, dict) or not isinstance(row.get("q_id"), str):
            continue
        items = []
        for issue in row.get("issues", [])[:24] if isinstance(row.get("issues"), list) else []:
            if (not isinstance(issue, dict) or issue.get("code") not in codes
                    or issue.get("field") not in fields or issue.get("status") != "open"):
                continue
            details = issue.get("details") if isinstance(issue.get("details"), dict) else {}
            safe = {}
            from backend.models import ProviderFailure
            from backend.services.background_errors import SAFE_BACKGROUND_ERROR_CODES
            if details.get("error_code") in SAFE_BACKGROUND_ERROR_CODES:
                safe["error_code"] = details["error_code"]
            if details.get("provider_error_code"):
                try:
                    diagnostic = ProviderFailure(code=details["provider_error_code"], http_status=details.get("provider_http_status"))
                    safe.update(provider_error_code=diagnostic.code, provider_http_status=diagnostic.http_status)
                except ValueError:
                    pass
            if details.get("reason") in reasons:
                safe["reason"] = details["reason"]
            if details.get("reason") == "alternative_cap_review" and isinstance(details.get("alternative_caps"), list):
                safe["alternative_caps"] = [{"label": str(cap.get("label", ""))[:80],
                    "maximum": cap["maximum"], "total": cap["total"]}
                    for cap in details["alternative_caps"][:100] if isinstance(cap, dict)
                    and type(cap.get("maximum")) in (int, float) and type(cap.get("total")) in (int, float)
                    and 0 <= cap["maximum"] < cap["total"] <= 10000]
            if details.get("incomplete") is True:
                safe["incomplete"] = True
            if details.get("confidence_scope") == "question":
                safe["confidence_scope"] = "question"
            if isinstance(details.get("confidence_reasons"), list):
                known = {"quality_uncalibrated", "coverage_not_semantic_accuracy", "low_confidence_content",
                         "coverage_incomplete", "target_content_unverified"}
                safe["confidence_reasons"] = [reason for reason in details["confidence_reasons"][:24]
                                              if isinstance(reason, str) and reason in known]
            if details.get("review_display_reason") in {"missing_type", "missing_number", "empty_stem", "missing_rubric",
                                                        "missing_answer", "missing_tests", "rubric_conflict"}:
                safe["review_display_reason"] = details["review_display_reason"]
            if isinstance(details.get("required_fields"), list):
                safe["required_fields"] = [field for field in details["required_fields"]
                    if field in {"number", "type", "stem", "criterion", "reference_answer"}]
            if type(details.get("confidence")) in (int, float) and 0 <= details["confidence"] <= 1:
                safe["confidence"] = details["confidence"]
            paths = details.get("invalid_fields")
            if isinstance(paths, list):
                safe["invalid_fields"] = [p for p in paths[:500] if isinstance(p, str)
                    and re.fullmatch(r"part_rubric(?:\.[a-z_]+|\[\d{1,3}\])+", p)]
            coverage = details.get("coverage")
            if isinstance(coverage, dict):
                safe["coverage"] = {key: [n for n in coverage.get(key, [])[:500]
                    if type(n) is int and 0 < n <= 10000] for key in ("failed_pages", "unprocessed_pages")
                    if isinstance(coverage.get(key), list)}
                for key in ("missing_targets", "unverified_targets"):
                    if isinstance(coverage.get(key), list):
                        safe["coverage"][key] = [n[:80] for n in coverage[key][:500] if isinstance(n, str)]
            effective = effective_question_issues({"preparation_issues": [{**issue, "details": safe}]})
            if not effective:
                continue
            issue = effective[0]
            safe = issue.get("details") or {}
            items.append({"issue_id": str(issue.get("issue_id", ""))[:120], "q_id": row["q_id"][:120],
                "field": issue["field"], "code": issue["code"], "status": "open",
                "severity": "blocking" if question_issue_blocks_progress(issue, row) else "warning", "details": safe})
        if items:
            result.append({"q_id": row["q_id"][:120], "number": str(row.get("number") or "")[:120], "issues": items})
    return result
