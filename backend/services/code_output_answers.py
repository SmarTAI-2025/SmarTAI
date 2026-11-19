"""Read student selections and stdout without treating unrelated prose as output."""

import re
import unicodedata

from backend.domain.code_output import ENGLISH_CARDINAL, choice_key, choice_number
from backend.services.math_rubric_plan import _NUMBER
from backend.tools.code_interpreter import compare_stdout

OUTPUT_LINE = re.compile(r"^[ \t]*(?:output|stdout|输出)[：:][ \t]?(.*)$", re.I | re.M)
FENCED_ANSWER = re.compile(r"\A```[^\r\n]*\r?\n(.*?)^```[ \t]*(?=\r?$)", re.M | re.S)
_EXPLANATION = re.compile(r"^[ \t]*(?:explanation|reasoning|trace|解释|原因|过程|推理)[：:]", re.I | re.M)
_PROSE = re.compile(r"(?:[A-Za-z]+\s+[^\n]+[.!?]|[\u3400-\u9fff]{4,}[^\n]*[。！？])$")
_SELECTION = re.compile(
    r"^(?:(?:my |the (?:correct )?|final )?answer|options?|(?:i )?(?:choose|select))"
    r"(?:\s*(?:[:=]|\bis\b)\s*|\s+)"
    r"|^(?:(?:正确)?(?:答案|选项)\s*(?:是|为)?|(?:我)?选(?:择)?)\s*[:=]?\s*",
    re.I,
)
_OPTION_NUMBER = rf"(?:(?:minus |negative |负)?{ENGLISH_CARDINAL}|(?:负)?{_NUMBER})"
_LABEL = re.compile(rf"^\(?([A-Za-z]|{_OPTION_NUMBER})\)?(?:(?![\w'’])|(?=[和及或]))", re.I)
_CONNECTOR = re.compile(r"^\s*(?:[,，;；、/&+]|\band\b|和|及)\s*|^\s+", re.I)
_NUMBER_PREFIX = re.compile(rf"^({_OPTION_NUMBER})(?!\w|\.\d)", re.I)


class CodeOutputUnavailable(ValueError):
    pass


def unquote(value):
    value = value.strip()
    return value[1:-1] if re.fullmatch(r"`[^`\n]*`", value) else value


def same_choice_value(actual, option):
    if compare_stdout(actual, option):
        return True
    try:
        return choice_number(actual) == choice_number(option)
    except ValueError:
        return False


def _stated_value(text, options):
    """Read a quoted, numeric, or known option value following an option label."""
    if quoted := re.match(r"^`([^`]+)`", text):
        return quoted[1], quoted.end()
    known = [value for value in set(options.values()) if value and (
        text == value or (text.startswith(value) and text[len(value):len(value) + 1] in " \t。.!?,，;；"))]
    if known:
        value = max(known, key=len)
        return value, len(value)
    number = _NUMBER_PREFIX.match(text)
    return (number[1], number.end()) if number else (None, 0)


def _line_choices(line, options):
    line = re.sub(r"^[-*•]\s+", "", line.strip(" \t*✓√☑"))
    explicit = _SELECTION.match(line)
    line = unquote(line[explicit.end():] if explicit else line)
    # A contiguous letter list is valid only when the entire line is a list.
    if re.fullmatch(r"[A-Za-z]{2,}", line) and set(line.upper()) <= options.keys():
        return set(line.upper())
    selected = set()
    remaining = line
    while (label := _LABEL.match(remaining)) and (key := choice_key(label[1])) in options:
        tail = remaining[label.end():]
        listed = tail.startswith((".", ":", "、", "="))
        payload = tail[1:].lstrip() if listed else tail.lstrip()
        next_label = _LABEL.match(payload)
        next_key = choice_key(next_label[1]) if next_label else None
        value, length = _stated_value(payload, options) if listed or next_key not in options else (None, 0)
        if (key == "A" and label[0] in {"a", "A"} and not (explicit or listed or value)
                and re.match(r"\s+[A-Za-z]{2,}", tail) and not _CONNECTOR.match(tail)[0].strip()):
            break  # An English article introducing an annotation is not a choice.
        selected.add(key)
        if value is not None:
            if not same_choice_value(value, options[key]):
                raise CodeOutputUnavailable()
            tail = payload[length:]
        elif listed:
            if next_key not in options:
                break
            tail = " " + payload
        alternative = re.match(r"^\s*(?:\bor\b|或(?:者)?)\s*", tail, re.I)
        if alternative and (other := _LABEL.match(tail[alternative.end():])) and choice_key(other[1]) in options:
            raise CodeOutputUnavailable()
        connector = _CONNECTOR.match(tail)
        remaining = tail[connector.end():] if connector else ""
        explicit = bool(connector and connector[0].strip())
        following = _LABEL.match(remaining)
        if not following or choice_key(following[1]) not in options:
            # A separate listed choice after an annotation is still a choice.
            remaining = next((clause.lstrip() for clause in re.split(r"[,，;；]", tail)[1:]
                              if (marked := _LABEL.match(clause.lstrip())) and choice_key(marked[1]) in options), "")
            explicit = bool(remaining)
    if selected:
        return selected
    value = unquote(line.rstrip("。"))
    by_value = {key for key, option in options.items() if same_choice_value(value, option)}
    if len(by_value) > 1:
        raise CodeOutputUnavailable()
    return by_value


def submitted_choices(text, choices):
    options = {choice_key(key): value for key, value in choices.items()}
    text = unicodedata.normalize("NFKC", text)
    marked = set()
    # Commas separate listed selections. Preserve the comma when it is part of a known literal.
    for line in text.splitlines():
        selected = _line_choices(line, options)
        if not selected:
            for clause in re.split(r"[,，;；]", line)[1:]:
                selected |= _line_choices(clause, options)
        if not selected:
            selected = {choice_key(match[1]) for match in re.finditer(rf"\(([A-Za-z]|{_NUMBER})\)", line, re.I)}
        marked.update(selected & options.keys())
    if not marked:
        raise CodeOutputUnavailable()
    return marked


def submitted_stdout(text):
    text = text.strip()
    fenced = FENCED_ANSWER.match(text)
    return fenced[1] if fenced and not text[fenced.end():].strip() else unquote(text)


def explanation_stdout(text, observed):
    """Return the complete result block and its first original student line."""
    text = text.replace("\r\n", "\n")
    leading = text.lstrip()
    label = OUTPUT_LINE.search(text)
    if FENCED_ANSWER.match(leading) or compare_stdout(leading, observed):
        label = None
    start = label.start(1) if label else len(text) - len(leading)
    payload = text[start:]
    content_start = start + len(payload) - len(payload.lstrip())
    payload = payload.lstrip()
    if fenced := FENCED_ANSWER.match(payload):
        if OUTPUT_LINE.search(payload, fenced.end()):
            raise CodeOutputUnavailable()
        value = fenced[1]
        if value.strip():
            content_start += fenced.start(1) + len(value) - len(value.lstrip())
        return value, f"student:{text.count(chr(10), 0, content_start) + 1}"
    if compare_stdout(unquote(payload), observed) and payload.strip():
        return unquote(payload), f"student:{text.count(chr(10), 0, content_start) + 1}"
    if label and OUTPUT_LINE.search(payload):
        raise CodeOutputUnavailable()
    if not label:
        if "\n" in payload.rstrip() or _PROSE.fullmatch(payload.rstrip()):
            raise CodeOutputUnavailable()
        value = unquote(payload)
    else:
        end = _EXPLANATION.search(payload)
        value = payload[:end.start()] if end else payload
        if not end:
            lines = value.rstrip().splitlines()
            expected_lines = observed.strip().splitlines()
            # A sentence after a numeric result is prose when it starts a
            # wholly prose suffix. Extra numeric lines are still output.
            if expected_lines and all(_numeric_line(line) for line in expected_lines if line.strip()):
                prose_at = next((index for index, line in enumerate(lines) if _PROSE.fullmatch(line.strip())), None)
                if prose_at is not None:
                    if (not prose_at or not all(_numeric_line(line) for line in lines[:prose_at] if line.strip())
                            or not all(_PROSE.fullmatch(line.strip()) for line in lines[prose_at:] if line.strip())):
                        raise CodeOutputUnavailable()
                    lines = lines[:prose_at]
                if len(lines) > 1 and not all(_numeric_line(line) for line in lines if line.strip()):
                    raise CodeOutputUnavailable()
            elif len(lines) > 1 and not compare_stdout(value, observed) and any(
                    _PROSE.fullmatch(line.strip()) or len(line.split()) > 1 for line in lines):
                raise CodeOutputUnavailable()
            value = "\n".join(lines)
        value = "\n".join(unquote(line) if line.strip().startswith("`") else line for line in value.strip().splitlines())
    if not value.strip():
        raise CodeOutputUnavailable()
    return value, f"student:{text.count(chr(10), 0, content_start) + 1}"


def _numeric_line(line):
    return bool(re.fullmatch(r"[\s0-9.,+eE−-]+", unquote(line)))
