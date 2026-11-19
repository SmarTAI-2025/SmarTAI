"""Small owner-scoped review facts for failure pages; no answer or model text."""
import math
import re
from backend.domain.confidence_policy import confidence_needs_attention

_ANSWER_FLAGS = frozenset({
    "answer_assignment_conflict", "recognition_matching_review", "recognition_matching_advisory", "recognition_failed", "parse_failed",
    "no_matching_answer", "recognition_manual_pending", "recognition_paused", "recognition_incomplete",
    "recognition_partial", "recognition_needs_review", "recognition_metadata_review", "recognition_boundary_uncertain", "recognition_boundary_previous_page_failed",
    "recognition_boundary_next_page_failed",
    "ocr_only_structure_requires_teacher_review", "recognition_very_low_confidence", "very_low_confidence",
    "recognition_low_confidence", "low_confidence", "manual_blank_confirmed", "provider_submit_uncertain",
})


def public_answer_diagnostics(answers, limit=20, *, required_only=False):
    def required(row):
        return any(flag in {"answer_assignment_conflict", "recognition_matching_review", "recognition_failed",
                           "parse_failed", "no_matching_answer", "recognition_manual_pending",
                           "recognition_paused", "recognition_incomplete"}
                   or flag.startswith("recognition_invalid_fields:") for flag in row["flag"])
    rows = []
    for answer in answers or []:
        if not isinstance(answer, dict):
            continue
        flags = []
        for flag in answer.get("flag") or []:
            if not isinstance(flag, str):
                continue
            if flag in _ANSWER_FLAGS:
                flags.append(flag)
            elif re.fullmatch(r"recognition_(?:failed|unprocessed)_pages:\d+(?:,\d+)*", flag):
                flags.append(flag[:200])
            elif isinstance(flag, str) and flag.startswith("recognition_invalid_fields:"):
                fields = [field for field in flag.split(":", 1)[1].split(",")
                          if field in {"q_id", "number", "type", "content", "flag", "recognition_confidence"}]
                if fields:
                    flags.append("recognition_invalid_fields:" + ",".join(fields))
        value = answer.get("recognition_confidence")
        confidence = value if type(value) in {float, int} and math.isfinite(value) and 0 <= value <= 1 else None
        # These flags provide context for other issues, but are not displayed
        # as standalone review notes. Do not count them as affected responses.
        ignored = {"manual_blank_confirmed", "recognition_needs_review", "recognition_metadata_review",
                   "ocr_only_structure_requires_teacher_review", "recognition_very_low_confidence",
                   "very_low_confidence", "recognition_low_confidence", "low_confidence"}
        visible_flags = [flag for flag in flags if flag not in ignored
                         and not flag.startswith(("recognition_failed_pages:", "recognition_unprocessed_pages:"))]
        if answer.get("review_status") == "confirmed" and not required({"flag": flags}):
            continue
        if not visible_flags and not confidence_needs_attention(confidence):
            continue
        rows.append({"q_id": str(answer.get("q_id") or "")[:120],
                     "number": str(answer.get("number") or "")[:120], "flag": list(dict.fromkeys(flags)),
                     "recognition_confidence": confidence,
                     "has_content": answer.get("has_content") is True or bool(str(answer.get("content") or "").strip())})
    # Preserve severe issues even when advisory rows precede the bounded page.
    rows = [row for row in rows if required(row)] if required_only else sorted(rows, key=lambda row: not required(row))
    return rows[:limit] if limit is not None else rows


def public_grading_diagnostics(results, limit=20):
    """Keep the shape used by review notes, without feedback or raw responses."""
    rows = []
    for result in results:
        review = result.teacher_review or {}
        from backend.domain.grading_review import result_programming_review_reason
        programming_reason = result_programming_review_reason(result)
        experts = [{"provider": str(expert.get("provider") or "Model")[:120],
                    "provider_id": str(expert.get("provider_id") or "")[:120], "score": expert.get("score", 0),
                    "max_score": expert.get("max_score", result.ai_max_score),
                    "confidence": expert.get("confidence"), "comment": "", "steps": [],
                    "error_kind": expert.get("error_kind") if expert.get("error_kind") in {
                        "quota_exhausted", "transient_llm", "parse_failed", "general", "operation_cancelled"} else "general" if expert.get("error_kind") else None}
                   for expert in result.ai_expert_results[:8]]
        rows.append({"student_id": result.student_id, "q_id": result.q_id, "type": "",
                     "score": result.effective_score, "max_score": result.ai_max_score,
                     "confidence": result.ai_confidence, "comment": "", "steps": [],
                     "expert_results": experts, "synthesis_method": result.ai_synthesis_method,
                     "requires_human_review": result.requires_review,
                     "review_reasons": list(dict.fromkeys([*result.review_reasons, *([programming_reason] if programming_reason else [])])),
                     "initial_review_reasons": list(result.initial_review_reasons),
                     "review_status": "confirmed" if review.get("review_type") == "confirm" else "edited" if review else "pending"})
    rows.sort(key=lambda row: row["score"] is not None)
    return rows[:limit], {"affected": len(rows), "missing_scores": sum(row["score"] is None for row in rows)}
