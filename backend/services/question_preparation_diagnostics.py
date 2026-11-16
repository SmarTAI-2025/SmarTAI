"""Small, source-free question diagnostics shared by live and saved progress."""
from copy import deepcopy
import re
import json


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


def preparation_review(packages):
    result = []
    for q_id, row in packages.items():
        issues = []
        for original in row.get("preparation_issues", []):
            issue = normalize_formula_issue(original)
            if issue.get("status") != "open":
                continue
            details = issue.get("details") or {}
            safe = {key: deepcopy(details[key]) for key in (
                "reason", "incomplete", "confidence", "confidence_scope", "coverage", "error_code",
                "required_fields", "review_display_reason", "provider_error_code", "provider_http_status", "alternative_caps",
            ) if key in details}
            draft = details.get("rubric_review_draft") or {}
            if draft.get("invalid_fields"):
                # Paths, never draft values or raw model responses.
                safe["invalid_fields"] = list(draft["invalid_fields"])[:24]
            issues.append({"issue_id": issue.get("issue_id", ""), "q_id": q_id,
                "field": issue.get("field"), "code": issue.get("code"),
                "severity": issue.get("severity"), "status": "open", "details": safe})
        if issues:
            result.append({"q_id": q_id, "number": str(row.get("number") or ""), "issues": issues})
    return result


def bounded_preparation_review(rows):
    """Leave room for per-question counters in the 64 KiB progress envelope."""
    result, size = [], 0
    for original in rows:
        row = {**original, "issues": [i for i in original["issues"] if i.get("severity") == "blocking"]}
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
             "math_rubric_advisory", "ai_source_conflict", "unmapped_source_content", "max_score_not_found", "default_max_score_requires_review"}
    fields = {"stem", "answer", "rubric", "source", "programming_tests", "max_score"}
    reasons = {"conflicting_printed_formula", "invalid_structured_rubric", "test_case_output_conflict", "alternative_cap_review"}
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
            if details.get("review_display_reason") in {"missing_type", "missing_number", "empty_stem", "missing_rubric", "missing_answer", "rubric_conflict"}:
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
                if isinstance(coverage.get("missing_targets"), list):
                    safe["coverage"]["missing_targets"] = [n[:80] for n in coverage["missing_targets"][:500] if isinstance(n, str)]
            items.append({"issue_id": str(issue.get("issue_id", ""))[:120], "q_id": row["q_id"][:120],
                "field": issue["field"], "code": issue["code"], "status": "open",
                "severity": "blocking" if issue.get("severity") == "blocking" else "warning", "details": safe})
        if items:
            result.append({"q_id": row["q_id"][:120], "number": str(row.get("number") or "")[:120], "issues": items})
    return result
