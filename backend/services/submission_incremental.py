"""Conservative answer boundaries and composition of independently saved units."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from bisect import bisect_right
from collections import Counter
from dataclasses import replace

from backend.services.programming_submission_mapping import native_source
from backend.services.question_structure import _PAREN_LABEL_RE, _PURE_LABEL_RE, _is_subpart_marker
from backend.services.source_question_units import QUESTION_HEADING, question_headings, source_question_units


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
    openings = (student.get("recognition") or {}).get("source_opening_hashes", {})
    incoming_openings = (current.student.get("recognition") or {}).get("source_opening_hashes", {})
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
    if openings or incoming_openings:
        # Only carry evidence for the exact transcription retained by repair.
        student["recognition"]["source_opening_hashes"] = {
            q_id: digest for q_id, answer in answers.items()
            if q_id in question_ids and (q_id in openings or q_id in incoming_openings)
            and (digest := hashlib.sha256(answer["content"].encode()).hexdigest())
                in (openings.get(q_id), incoming_openings.get(q_id))
        }
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
    if len(matches) < 2 or native_source(source.filename, text, problems) is not None:
        return [(source, targets)]
    from backend.services.question_matching import resolve_question_match
    mapped = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        q_id = resolve_question_match(match.group("number"), text[match.start():end], problems)
        mapped.append([q_id] if q_id else [])
    if (any(len(ids) != 1 for ids in mapped)
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
        units.append((replace(source, whole_source=False, text="\n\n".join(part for part in (preamble, body) if part)), {q_id}))
    return units or [(source, targets)]


SOURCE_OPENING_QUOTE_LIMIT = 320
PREVIOUS_PAGE_FAILED = "recognition_boundary_previous_page_failed"
NEXT_PAGE_FAILED = "recognition_boundary_next_page_failed"
_PAGE_GAP_PREFIX = re.compile(
    r"(?:#{1,6}\s+)?(?:\*\*)?(?:(?:question|problem|q|题目?|第)\s*)?"
    r"(?P<number>\d+(?:\.\d+)*)\s*(?:题)?(?:\*\*)?\s*[.、):：-]?(?:\*\*)?"
    r"(?:\s*[（(](?:[a-z]|\d+|续[^()（）]*|continued)[）)])*\s*",
    re.IGNORECASE,
)
_SUPERSCRIPT_DIGITS = "⁰¹²³⁴⁵⁶⁷⁸⁹"
_EXPONENT = re.compile(rf"[{_SUPERSCRIPT_DIGITS}]+|\^([0-9])(?![0-9])")
_EXPONENT_DIGITS = str.maketrans(_SUPERSCRIPT_DIGITS, "0123456789")


def _page_gap_text(value):
    # Keep the exponent explicit. Compatibility normalization alone would
    # confuse x² with x2, and subscripts are different source evidence.
    return _EXPONENT.sub(lambda match: "^{" + (match[1] or match[0].translate(_EXPONENT_DIGITS)) + "}",
                         " ".join(value.split()))


def matches_source_opening(candidate: tuple[str, str | None], content: str, quote: str) -> bool:
    """Compare the opening text, allowing an optional matching subpart label."""
    source_prefix, part_label = candidate
    content = " ".join(content.split())
    quote = " ".join(quote.split())
    if part_label:
        label = re.escape(part_label[1:-1])
        matching_label = re.compile(rf"^[（(]{label}[）)]\s*", re.IGNORECASE)
        content = matching_label.sub("", content, count=1)
        quote = matching_label.sub("", quote, count=1)
    return bool(quote and source_prefix.startswith(quote) and content.startswith(quote))


def successful_submission_pages(pages, coverage):
    # Do not use an earlier attempt's candidate for a currently unread page.
    return {number: pages[number] for number in coverage.get("processed_pages", []) if number in pages}


def answer_boundaries_on_read_pages(read_pages, problems, coverage):
    """Find closed answers, pending openings, and headings at a readable page's start."""
    if not (read_pages and (coverage.get("failed_pages") or coverage.get("unprocessed_pages"))):
        return set(), {}, {}
    from backend.services.question_matching import question_number_aliases, resolve_question_match

    aliases = {alias: q_id for q_id, labels in question_number_aliases(problems).items() for alias in labels}
    units = source_question_units(read_pages, known_numbers=set(aliases), number_key=aliases.get,
                                  qualified=True, check_opening=True, source_end_page=coverage.get("total_pages"))
    closed, openings, leading_headings = set(), {}, {}
    failed_pages = set(coverage.get("failed_pages") or ())
    for unit in units:
        gap_page = unit.previous_gap_page
        follows_failure = gap_page is not None and gap_page in failed_pages
        if not (unit.complete or unit.closure == "unverified_opening" or follows_failure):
            continue
        q_id = resolve_question_match(unit.number, unit.text, problems)
        if not q_id:
            continue
        if follows_failure:
            leading_headings[gap_page + 1] = q_id
        if unit.complete:
            closed.add(q_id)
            continue
        if unit.closure != "unverified_opening":
            continue
        heading = question_headings(unit.text, qualified=True)[0]
        # A continuation annotation can delimit the preceding answer.
        # A standard subpart label still allows separate opening evidence.
        title = heading.group("title").strip()
        part = _PURE_LABEL_RE.fullmatch(title)
        if part:
            title = ""
        elif (
            (inline := _PAREN_LABEL_RE.match(title))
            and title[inline.end():].startswith((" ", "\t"))
            and _is_subpart_marker(title, inline)
        ):
            part = inline
            title = title[inline.end():].lstrip()
        annotation = re.match(r"\([^()]*\)|（[^（）]*）", title)
        if annotation and annotation.end() == len(title):
            continue
        opening_text = title + unit.text[heading.end("title"):]
        prefix = " ".join(opening_text.split())[:SOURCE_OPENING_QUOTE_LIMIT]
        if prefix:
            openings[q_id] = (prefix, part.group("label") if part else None)
    return closed, openings, leading_headings


def page_gap_review_flags(read_pages, failed_pages, leading_headings, answers, number_aliases=None):
    """Mark page edges supported by already-matched answers and saved OCR."""
    pages = sorted(read_pages)
    failed = set(failed_pages)
    aliases = number_aliases or {}
    text, offsets, ends, previous = [], [], [], None
    offset = 0
    for page in pages:
        if previous is not None:
            text.append(" " if page == previous + 1 else "\n")
            offset += 1
        offsets.append(offset)
        normalized = _page_gap_text(read_pages[page])
        text.append(normalized)
        offset += len(normalized)
        ends.append(offset)
        previous = page
    source = "".join(text)
    edges = {}

    def locate(content):
        if not content:
            return None
        start = source.find(content)
        if start < 0 or source.find(content, start + 1) >= 0:
            return None
        end = start + len(content)
        # Short answers must still match whole words, not pieces of another
        # answer, identifier or number.
        if ((start and content[0].isalnum() and source[start - 1].isalnum())
                or (end < len(source) and content[-1].isalnum() and source[end].isalnum())):
            return None
        return start, end

    locations = {}
    for answer in answers:
        lines = [_page_gap_text(line) for line in (answer.get("content") or "").splitlines() if line.strip()]
        if not lines:
            continue
        span = locate(" ".join(lines))
        if span:
            spans = (span,)
        elif len(lines) > 1:
            # The structured answer may join fragments on either side of a
            # failed page. Locate both ends without joining OCR across the gap.
            start_span, end_span = locate(lines[0]), locate(lines[-1])
            if not (start_span and end_span and start_span[1] <= end_span[0]):
                continue
            spans = (start_span, end_span)
        else:
            continue
        locations[answer["q_id"]] = spans

    occurrences = Counter(span for spans in locations.values() for span in spans)
    for q_id, spans in locations.items():
        if any(occurrences[span] != 1 for span in spans):
            continue
        for start, end in spans:
            first_index = bisect_right(offsets, start) - 1
            last_index = bisect_right(offsets, end - 1) - 1
            first, last = pages[first_index], pages[last_index]
            # Being the closest *matched* answer is insufficient: unlocated
            # work may still lie between a match and the missing page.
            if first - 1 in failed:
                prefix = source[offsets[first_index]:start].strip()
                label = _PAGE_GAP_PREFIX.fullmatch(prefix) if prefix and not prefix.isdigit() else None
                matching_label = label and unicodedata.normalize("NFKC", label["number"]) in aliases.get(q_id, ())
                if not prefix or matching_label:
                    edges.setdefault((first, PREVIOUS_PAGE_FAILED), set()).add(q_id)
            if last + 1 in failed and end == ends[last_index]:
                edges.setdefault((last, NEXT_PAGE_FAILED), set()).add(q_id)

    # An explicit heading at the very top can identify the first answer
    # even if its text was reformatted. A heading alone cannot prove a
    # tail: later OCR text might belong to a different answer.
    for page, q_id in leading_headings.items():
        edges.setdefault((page, PREVIOUS_PAGE_FAILED), set()).add(q_id)

    result = {}
    for (_, flag), ids in edges.items():
        if len(ids) == 1:
            q_id, = ids
            result.setdefault(q_id, []).append(flag)
    return {q_id: sorted(flags) for q_id, flags in result.items()}


def merge_source_answer_units(results, source, problems):
    """Combine independently saved question units without inventing absent answers."""
    students = [result for result in results if result.student is not None]
    if not students:
        return results[0]
    first = students[0]
    student = {**first.student}
    answers, model_reviews, openings = {}, set(), {}
    for result in students:
        recognition = result.student.get("recognition") or {}
        unit_reviews = set(recognition.get("model_review_question_ids", []))
        openings.update(recognition.get("source_opening_hashes", {}))
        for answer in result.student.get("stu_ans", []):
            q_id = answer["q_id"]
            answers[q_id] = answer
            model_reviews.discard(q_id)
            if q_id in unit_reviews:
                model_reviews.add(q_id)
    student["stu_ans"] = [answers[q_id] for q_id in problems if q_id in answers]
    student["recognition"] = {**student.get("recognition", {}),
        "model_review_question_ids": sorted(model_reviews & set(problems))}
    if openings:
        student["recognition"]["source_opening_hashes"] = openings
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
