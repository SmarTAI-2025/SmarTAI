"""Conservative answer boundaries and composition of independently saved units."""
from __future__ import annotations

from dataclasses import replace
from backend.services.source_question_units import QUESTION_HEADING


def merge_parse_repair(previous, current, question_ids, *, problems):
    """Repair invalid fields while keeping every earlier verified field intact."""
    if previous is None or previous.student is None:
        return current
    if current.student is None:
        return replace(current, student=previous.student, student_candidate=previous.student_candidate,
                       matched_answer_count=previous.matched_answer_count)

    def invalid_fields(answer, repair_ids):
        flags = answer.get("flag", [])
        fields = {field for flag in flags if flag.startswith("recognition_invalid_fields:")
                  for field in flag.split(":", 1)[1].split(",")}
        if "recognition_manual_pending" in flags and not fields and answer["q_id"] in repair_ids:
            return {"number", "type", "content", "flag", "recognition_confidence"}
        return fields

    def field_size(value):
        return len(value) if isinstance(value, str) else sum(map(len, value)) if isinstance(value, list) else 0

    student = dict(previous.student)
    remaining_budget = 1_000_000 - 1024 * len(question_ids) - len(student.get("stu_id", "")) - len(student.get("stu_name", ""))
    remaining_budget -= sum(field_size(answer.get(field)) for answer in student.get("stu_ans", [])
                            for field in ("q_id", "number", "type", "content", "flag"))
    old_invalid = set((student.get("recognition") or {}).get("identity_invalid_fields", []))
    new_invalid = set((current.student.get("recognition") or {}).get("identity_invalid_fields", []))
    for field in old_invalid - new_invalid:
        student[field] = current.student[field]
    identity_invalid = old_invalid & new_invalid
    if not identity_invalid and current.student.get("identity_status") == "matched":
        student["identity_status"] = "matched"
    answers = {answer["q_id"]: dict(answer) for answer in student.get("stu_ans", [])}
    old_repairs = set((student.get("recognition") or {}).get("repair_question_ids", []))
    new_repairs = set((current.student.get("recognition") or {}).get("repair_question_ids", []))
    repair_fields = {q_id: invalid_fields(answer, old_repairs) for q_id, answer in answers.items()}
    model_reviews = set((student.get("recognition") or {}).get("model_review_question_ids", [])) & set(answers)
    incoming_reviews = set((current.student.get("recognition") or {}).get("model_review_question_ids", []))

    def matching_fields(answer):
        return tuple(answer.get(field) for field in ("q_id", "number", "content"))

    for incoming in current.student.get("stu_ans", []):
        q_id = incoming["q_id"]
        previous_answer = answers.get(q_id)
        if previous_answer is None:
            answers[q_id] = dict(incoming)
            repair_fields[q_id] = invalid_fields(incoming, new_repairs)
            if q_id in incoming_reviews:
                model_reviews.add(q_id)
            continue
        previous_matching = matching_fields(previous_answer)
        bad_before, bad_after = repair_fields[q_id], invalid_fields(incoming, new_repairs)
        for field in bad_before - bad_after:
            value = incoming.get(field)
            delta = field_size(value) - field_size(previous_answer.get(field))
            if delta > remaining_budget:
                bad_after.add(field)
                continue
            remaining_budget -= delta
            previous_answer[field] = value
        # A type-only repair cannot clear review of unchanged matching data.
        # Adopt new review evidence only when it describes the retained fields;
        # discarded answer rewrites must not contaminate the saved answer.
        if matching_fields(previous_answer) == matching_fields(incoming):
            if q_id in incoming_reviews:
                model_reviews.add(q_id)
            elif matching_fields(previous_answer) != previous_matching:
                model_reviews.discard(q_id)
        remaining = bad_before & bad_after
        repair_fields[q_id] = remaining
        flags = [flag for flag in previous_answer.get("flag", [])
                 if flag != "recognition_manual_pending" and not flag.startswith("recognition_invalid_fields:")]
        if "content" in bad_before and "content" not in remaining:
            flags = [flag for flag in flags if flag != "recognition_incomplete"]
        if remaining:
            flags.extend(["recognition_manual_pending", "recognition_invalid_fields:" + ",".join(sorted(remaining))])
        elif q_id in model_reviews:
            flags.append("recognition_manual_pending")
        previous_answer["flag"] = flags
    student["stu_ans"] = [answer for q_id, answer in answers.items() if q_id in question_ids]
    pending = sorted(q_id for q_id in question_ids if q_id not in answers or repair_fields[q_id])
    student["recognition"] = {**student.get("recognition", {}),
        **current.student.get("recognition", {}), "repair_question_ids": pending,
        "missing_question_ids": [q_id for q_id in question_ids if q_id not in answers],
        "identity_invalid_fields": sorted(identity_invalid),
        "model_review_question_ids": sorted(model_reviews & set(question_ids))}
    # A repaired type does not validate the old answer content that we kept.
    # Matching review is independent of format retries: do not send more model
    # calls solely to clear this review marker.
    from backend.services.question_matching import mark_question_match_conflicts
    mark_question_match_conflicts(student, problems)
    uncertain = current.stable_error_code == "provider_submit_uncertain"
    identity_review = student.get("identity_status") != "matched"
    return replace(current, student=student, student_candidate=student.get("stu_id"),
        matched_answer_count=len(student["stu_ans"]),
        status="parse_failed" if pending or uncertain else "identity_conflict" if identity_review else "parsed",
        stable_error_code="provider_submit_uncertain" if uncertain else "submission_parse_invalid" if pending
            else "identity_needs_review" if identity_review else None,
        failure_phase="recognition" if uncertain else "structured_parse" if pending else "identity" if identity_review else None,
        retryable=bool(pending) and not uncertain)


def invalidate_answer_units(result, question_ids):
    """Retain text but revoke completeness when later source evidence conflicts."""
    invalid = set(question_ids)
    if result.student is None or not invalid:
        return result
    answers = result.student.get("stu_ans", [])
    if not any(answer.get("q_id") in invalid for answer in answers):
        return result
    student = {**result.student, "stu_ans": [dict(answer, flag=list(dict.fromkeys([
        *answer.get("flag", []), "recognition_failed", "recognition_manual_pending",
    ]))) if answer.get("q_id") in invalid else answer for answer in answers]}
    recognition = student.get("recognition")
    if recognition is not None:
        student["recognition"] = {**recognition, "complete_question_ids": [
            q_id for q_id in recognition.get("complete_question_ids", []) if q_id not in invalid]}
    uncertain = result.stable_error_code == "provider_submit_uncertain"
    return replace(result, student=student, status="parse_failed",
        stable_error_code="provider_submit_uncertain" if uncertain else "submission_parse_failed",
        failure_phase="recognition", retryable=not uncertain)


# Bare numbered lines may be steps of a proof. Only explicit question labels
# or Markdown headings may split a student's document into independent calls.
def split_submission_question_units(source, problems, target_ids=None):
    """Return complete, unambiguous heading-bounded units; otherwise one source.

    A document with missing pages cannot prove its boundary text complete. It
    stays one partial source, and its failure flags remain attached to answers.
    """
    text = source.text or ""
    targets = set(target_ids if target_ids is not None else problems)
    recognition = source.recognition or {}
    coverage = recognition.get("coverage") or {}
    if source.pre_error_code or recognition.get("error_code") or any(
        coverage.get(key) for key in ("failed_pages", "unprocessed_pages", "missing_targets")
    ):
        return [(source, targets)]
    matches = list(QUESTION_HEADING.finditer(text))
    from backend.services.question_matching import resolve_question_match
    mapped = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        q_id = resolve_question_match(match.group("number"), text[match.start():end], problems)
        mapped.append([q_id] if q_id else [])
    if (len(matches) < 2 or any(len(ids) != 1 for ids in mapped)
            or len({ids[0] for ids in mapped}) != len(mapped)):
        return [(source, targets)]
    preamble = text[:matches[0].start()].strip()
    units = []
    for index, (match, ids) in enumerate(zip(matches, mapped)):
        q_id = ids[0]
        if q_id not in targets:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.start():end].strip()
        if not body:
            return [(source, targets)]
        units.append((replace(source, text="\n\n".join(part for part in (preamble, body) if part)), {q_id}))
    return units or [(source, targets)]


def merge_source_answer_units(results, source, problems):
    """Combine independently saved question units without inventing absent answers."""
    students = [result for result in results if result.student is not None]
    if not students:
        return results[0]
    first = students[0]
    student = {**first.student}
    answers, model_reviews = {}, set()
    for result in students:
        unit_reviews = set((result.student.get("recognition") or {}).get("model_review_question_ids", []))
        for answer in result.student.get("stu_ans", []):
            q_id = answer["q_id"]
            answers[q_id] = answer
            model_reviews.discard(q_id)
            if q_id in unit_reviews:
                model_reviews.add(q_id)
    student["stu_ans"] = [answers[q_id] for q_id in problems if q_id in answers]
    student["recognition"] = {**student.get("recognition", {}),
        "model_review_question_ids": sorted(model_reviews & set(problems))}
    student.update(source_id=source.source_id, stored_file_id=source.stored_file_id,
                   source_filename=source.filename)
    failures = [result for result in results if result.status in {"parse_failed", "no_matching_answer"}]
    incomplete = bool(failures or set(problems) - set(answers))
    if incomplete:
        missing = [q_id for q_id in problems if q_id not in answers]
        student["recognition"] = {**student.get("recognition", {}), "missing_question_ids": missing}
    # One file is one identity. Inconsistent model identities across its units
    # require review, never separate student rows or silent relabelling.
    conflict = any(result.student_candidate != first.student_candidate for result in students)
    if conflict:
        student["identity_status"] = "needs_review"
    failure = next((result for result in failures if result.stable_error_code == "provider_submit_uncertain"),
                   failures[0] if failures else None)
    return replace(first, student=student, matched_answer_count=len(answers),
        status="parse_failed" if incomplete else "identity_conflict" if conflict else first.status,
        stable_error_code=(failure.stable_error_code if failure else "submission_parse_failed") if incomplete
            else "identity_needs_review" if conflict else first.stable_error_code,
        failure_phase="recognition" if incomplete else "identity" if conflict else first.failure_phase,
        retryable=any(result.retryable for result in failures) if failures else incomplete)
