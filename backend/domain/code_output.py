"""Source-bound admission for the given-program, given-stdin reasoning exercise."""

import hashlib
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class CodeOutputSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    form: Literal["choice", "blank", "explanation"]
    language: str = Field(max_length=24)
    code: str = Field(min_length=1, max_length=32_768)
    stdin: str = Field(max_length=8192)
    input_quote: str = Field(min_length=1, max_length=8448)
    choices: dict[str, str] = Field(default_factory=dict, max_length=12)


class CodeOutputQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stem_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: CodeOutputSource | None = None


_FENCE = re.compile(r"^```(?P<language>[^\r\n]*)\r?\n(?P<code>.*?)^```[ \t]*(?=\r?$)", re.M | re.S)
_LANGUAGES = {"py": "python", "python": "python", "c": "c", "cpp": "cpp", "c++": "cpp"}
_INPUT = re.compile(r"^[ \t]*(?:sample input|given input|standard input|stdin|input|示例输入|样例输入|标准输入|输入示例|给定输入|输入)[ \t]*[:：](?:[ \t]|\r?\n)?(?P<payload>.+)\Z", re.I | re.S)
_INLINE_INPUT = re.compile(
    r"(?:\b(?:input (?:is|of)|stdin is)\b|(?:输入(?:是|为)))"
    r"[ \t]*(?P<payload>`[^`\r\n]+`|[+-]?\d+(?:\.\d+)?)(?=[ \t]*(?:[,，。;；?？]|$))",
    re.I,
)
_OPTION = re.compile(r"^[ \t]*[(（]?([A-Z])[.、)）:：][ \t]*(.*?)[ \t]*$", re.M)


def stem_digest(stem):
    return hashlib.sha256(stem.encode("utf-8")).hexdigest()


def _option_text(value):
    return value[1:-1] if re.fullmatch(r"`[^`]*`", value) else value


def bind_code_output(candidate, *, stem, document, incomplete=False):
    """Accept original bytes or retain an unavailable descriptor for manual grading."""
    unavailable = CodeOutputQuestion(stem_sha256=stem_digest(stem))
    if incomplete:
        return unavailable
    try:
        source = CodeOutputSource.model_validate(candidate)
    except ValidationError:
        return unavailable
    if source.language not in {"python", "c", "cpp"}:
        return unavailable
    blocks = list(_FENCE.finditer(stem))
    if len(blocks) != 1:
        return unavailable
    block, = blocks
    declared = block["language"].strip().lower()
    if (block["code"] != source.code or not source.code.strip()
            or (declared and _LANGUAGES.get(declared) != source.language)
            or not re.search(r"^" + re.escape(source.code), document, re.M)):
        return unavailable
    for original in _FENCE.finditer(document):
        if source.code in original["code"] and (
            original["code"] != source.code
            or (original["language"].strip() and _LANGUAGES.get(original["language"].strip().lower()) != source.language)
        ):
            return unavailable
    prose = stem[:block.start()] + stem[block.end():]
    quote = source.input_quote
    original_input = re.compile(r"^" + re.escape(quote) + r"(?=\r?$)", re.M)
    payload = _INPUT.fullmatch(quote)
    inputs = [payload["payload"]] if payload else _INLINE_INPUT.findall(quote)
    if (len(inputs) != 1 or _option_text(inputs[0]) != source.stdin
            or not original_input.search(prose) or not original_input.search(document)):
        return unavailable
    if source.form == "choice":
        option_rows = _OPTION.findall(prose)
        options = {label: _option_text(value) for label, value in option_rows}
        if len(options) < 2 or len(options) != len(option_rows) or source.choices != options:
            return unavailable
        if not all(line in document for line in prose.splitlines() if _OPTION.fullmatch(line)):
            return unavailable
    elif source.choices:
        return unavailable
    return CodeOutputQuestion(stem_sha256=unavailable.stem_sha256, source=source)


def code_output_data(problem):
    """Read the server-owned descriptor from preparation or persisted provenance."""
    if isinstance(problem, dict):
        if problem.get("type") != "推理题":
            return None
        direct = problem.get("code_output")
        provenance = problem.get("_math_rubric_source") or problem.get("source") or {}
    else:
        if problem.type != "推理题":
            return None
        direct = getattr(problem, "code_output", None)
        provenance = getattr(problem, "_math_rubric_source", None) or getattr(problem, "source", None) or {}
    data = direct or (provenance.get("presentation") or {}).get("code_output")
    return data.model_dump(mode="json") if isinstance(data, CodeOutputQuestion) else data


def code_output_form(problem):
    data = code_output_data(problem)
    if not data:
        return None
    parsed = CodeOutputQuestion.model_validate(data)
    return parsed.source.form if parsed.source else None


RUBRIC_INSTRUCTION = (
    "For a question with code_output_form=choice or blank, fund only the correct program output, "
    "without quoting or predicting the output. For code_output_form=explanation, use separate ordinary "
    "point steps for explanation and output. Preserve the teacher's explicit allocations. Mark only "
    "the independent final stdout step with code_output=true. Do not award the output again in any "
    "explanation step. Omit code_output for all other questions and steps."
)
