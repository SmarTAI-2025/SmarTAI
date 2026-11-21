"""Source-bound admission for the given-program, given-stdin reasoning exercise."""

from decimal import Decimal
import hashlib
import re
import unicodedata
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
_ONES = dict(zip("zero one two three four five six seven eight nine".split(), range(10)))
_TEENS = dict(zip("ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split(), range(10, 20)))
_TENS = dict(zip("twenty thirty forty fifty sixty seventy eighty ninety".split(), range(20, 100, 10)))
_NONZERO_ONES = "|".join(list(_ONES)[1:])
_UNDER_HUNDRED = rf"(?:{'|'.join(_TENS)})(?:[ -](?:{_NONZERO_ONES}))?|{'|'.join(_TEENS)}|{_NONZERO_ONES}|zero"
ENGLISH_CARDINAL = rf"(?:(?:{_NONZERO_ONES}) hundred(?: (?:and )?(?:{_UNDER_HUNDRED}))?|{_UNDER_HUNDRED})"
_ENGLISH_CARDINAL = re.compile(ENGLISH_CARDINAL, re.I)
_OPTION = re.compile(r"^[ \t]*[(（]?([A-Za-zＡ-Ｚａ-ｚ]|[0-9０-９]+|[一二三四五六七八九十]+)"
                     r"[.．、)）:：][ \t]*(.*?)[ \t]*$", re.M)
_BLANK = re.compile(r"(?<!\\)_{2,}|(?:\\_){2,}|\\underline\{\s*(?:\\hspace\*?\{[^{}]+\}|_+)?\s*\}|□+")
_BLANK_COUNT = re.compile(r"(?<![第0-9])([1-9][0-9]*|[一二两三四五六七八九十]+)\s*(?:(?:个)?空(?!格|白|间)|blanks?\b)"
                          r"|\b(one|two|three|four|five|six|seven|eight|nine|ten)\s+blanks?\b", re.I)
_MULTIPLE_CHOICE = re.compile(r"多选|多项选择|不定项|可选多项|\b(?:multiple[- ]select|select all|choose all|"
                              r"more than one (?:answer|option))\b", re.I)
_PRINTED_ITEMS = re.compile(r"\b(?:which|select all|choose all)\b[^.?\n]{0,80}\b(?:values|numbers|lines)\b"
                            r"[^.?\n]{0,80}\b(?:print(?:s|ed)?|output|appear(?:s)?)\b"
                            r"|\b(?:select|choose) all (?:printed|output) (?:values|numbers|lines)\b"
                            r"|(?:选出|选择|哪些)[^。？\n]{0,36}(?:输出|打印)[^。？\n]{0,24}(?:数值|数字|行)", re.I)


def choice_number(text):
    from backend.services.math_rubric_plan import _number_value

    text = unicodedata.normalize("NFKC", text).strip().casefold().replace("−", "-")
    sign = re.match(r"^(?:[+负-]|minus\s+|negative\s+)", text)
    digits = text[sign.end():] if sign else text
    if _ENGLISH_CARDINAL.fullmatch(digits):
        words = re.split(r"[ -]+", digits)
        hundreds = 100 * _ONES[words.pop(0)] if len(words) > 1 and words[1] == "hundred" else 0
        vocabulary = _ONES | _TEENS | _TENS
        value = Decimal(hundreds + sum(vocabulary.get(word, 0) for word in words))
    else:
        value = _number_value(digits)
    return value.copy_negate() if sign and sign[0] != "+" else value


def choice_key(label):
    try:
        value = choice_number(label)
        fixed = format(value, "f")
        return fixed.rstrip("0").rstrip(".") if "." in fixed else fixed
    except ValueError:
        return unicodedata.normalize("NFKC", label).strip().upper()


def multiple_choice(stem):
    return bool(_MULTIPLE_CHOICE.search(_OPTION.sub("", _FENCE.sub("", stem))))


def choice_output_items(stem, stdout):
    prose = _OPTION.sub("", _FENCE.sub("", stem))
    match = _PRINTED_ITEMS.search(prose)
    if match:
        return stdout.strip().splitlines() if re.search(r"\blines\b|行", match[0], re.I) else stdout.split()
    return [stdout]


def blank_count(stem, input_quote):
    from backend.services.math_rubric_plan import _number_value

    prose = _FENCE.sub("", stem).replace(input_quote, "", 1)
    markers = len(_BLANK.findall(re.sub(r"`[^`\n]*`", "", prose)))
    declared = {int(_number_value(match[1] or match[2])) for match in _BLANK_COUNT.finditer(prose)}
    if markers:
        declared.add(markers)
    if len(declared) > 1:
        raise ValueError("The stated number of blanks conflicts with the blank markers.")
    return next(iter(declared), 1)


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
        if (len(options) < 2 or len(options) != len(option_rows) or source.choices != options
                or len({choice_key(label) for label in options}) != len(options)):
            return unavailable
        if not all(line in document for line in prose.splitlines() if _OPTION.fullmatch(line)):
            return unavailable
    elif source.choices:
        return unavailable
    return CodeOutputQuestion(stem_sha256=unavailable.stem_sha256, source=source)


def rebind_edited_code_output(previous, stem):
    """Bind the program and explicit input from the teacher's edited question."""
    unavailable = CodeOutputQuestion(stem_sha256=stem_digest(stem))
    source = CodeOutputQuestion.model_validate(previous).source
    blocks = list(_FENCE.finditer(stem))
    if source is None or len(blocks) != 1:
        return unavailable
    block, = blocks
    prose = stem[:block.start()] + stem[block.end():]
    language = _LANGUAGES.get(block["language"].strip().lower(), source.language)
    lines = prose.splitlines()
    if source.input_quote in prose and "\n" in source.input_quote:
        quote, stdin = source.input_quote, source.stdin
    else:
        inputs = []
        for line in lines:
            explicit = _INPUT.fullmatch(line)
            values = [explicit["payload"]] if explicit else _INLINE_INPUT.findall(line)
            inputs.extend((line, _option_text(value)) for value in values)
        if len(inputs) != 1:
            return unavailable
        quote, stdin = inputs[0]
    choices = {key: _option_text(value) for key, value in _OPTION.findall(prose)}
    if bool(choices) != (source.form == "choice"):
        return unavailable
    candidate = source.model_copy(update={"code": block["code"], "language": language, "stdin": stdin,
                                          "input_quote": quote, "choices": choices})
    return bind_code_output(candidate.model_dump(), stem=stem, document=stem)


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


def independent_output_steps(rules, count, *, allow_output_marker=False):
    if rules is None or len(rules.parts) != 1 or rules.nonblank_floor.resolve(rules.total_pts):
        raise ValueError("Use one funded point step per blank in source order.")
    part, = rules.parts
    if (len(part.steps) != count or part.deductions or part.alternative.source_quote or part.alternative.field_sources
            or any(step.required_conditions or (step.code_output and not allow_output_marker) or step.test_suite
                   for step in part.steps)):
        raise ValueError(f"Use exactly {count} independent point steps, one per blank in source order.")
    return part


_BLANK_POSITION = re.compile(r"第\s*([1-9][0-9]*|[一二两三四五六七八九十]+)\s*(?:个)?空"
                             r"|\bblank\s*#?([1-9][0-9]*)\b", re.I)


def ordered_blank_steps(rules, count):
    from backend.services.math_rubric_plan import _number_value

    part = independent_output_steps(rules, count)
    for index, step in enumerate(part.steps, 1):
        positions = {int(_number_value(match[1] or match[2])) for text in (step.criterion, step.source_quote)
                     for match in _BLANK_POSITION.finditer(text)}
        if positions - {index}:
            raise ValueError(f"Scoring step {index} must refer only to blank {index}, in source order.")
    return part


def selection_awards(text, maximum):
    """Read the complete fixed selection policy from the authoritative rubric."""
    from backend.services.binary_scoring import whole_question_policy
    from backend.services.math_rubric_plan import _NUMBER, _number_value

    if (re.match(r"\s*(?:全(?:部)?选对|全选正确)", text)
            and whole_question_policy(text, maximum)):
        return 0, 0
    award = rf"满分|full (?:credit|marks)|no credit|(?:{_NUMBER})\s*(?:分|points?|marks?)"
    clauses = {
        "full": r"全(?:部)?选(?:择)?(?:正确|对)|全对|all correct options",
        "wrong": r"(?:任一)?错选|选错|任何错误选项|any wrong option",
        "subset": r"少选(?:(?:且|但)?(?:无错选|未错选|没有错误选项))?|部分选对|partial selection",
    }
    scores = {}
    for clause in re.split(r"[；;。\n]|[,，](?=\s*(?:全|错|选错|少选|部分|all |any |partial ))", text, flags=re.I):
        if not clause.strip():
            continue
        for role, subject in clauses.items():
            match = re.fullmatch(rf"\s*(?:{subject})\s*(?:(?:得|给|计|earns?|gets?)\s*|[:：=]\s*)?"
                                 rf"({award})\s*\.?\s*", clause, re.I)
            if match and role not in scores:
                amount = match[1].casefold()
                if amount in {"满分", "full credit", "full marks"}:
                    scores[role] = Decimal(str(maximum))
                elif amount == "no credit":
                    scores[role] = 0
                else:
                    points = re.match(_NUMBER, amount, re.I)
                    try:
                        scores[role] = _number_value(points[0])
                    except ValueError as exc:
                        raise ValueError("Set full, wrong-selection and partial-selection awards explicitly.") from exc
                break
        else:
            raise ValueError("Set full, wrong-selection and partial-selection awards explicitly.")
    maximum = Decimal(str(maximum))
    if scores.get("full") != maximum or not all(0 <= scores.get(role, -1) <= maximum for role in ("wrong", "subset")):
        raise ValueError("Set full, wrong-selection and partial-selection awards explicitly.")
    return float(scores["wrong"]), float(scores["subset"])


def code_output_rubric_context(problem):
    data = code_output_data(problem)
    source = CodeOutputQuestion.model_validate(data).source if data else None
    if source is None:
        return {}
    stem = problem["stem"] if isinstance(problem, dict) else problem.stem
    context = {"code_output_form": source.form}
    if source.form == "blank":
        try:
            context["code_output_blank_count"] = blank_count(stem, source.input_quote)
        except ValueError:
            pass  # The server reports the conflicting source when validating a proposal.
    if source.form == "choice" and multiple_choice(stem):
        context["code_output_multi_select"] = True
    return context


RUBRIC_INSTRUCTION = (
    "For code_output_form=blank, fund exactly one independent part_rubric step per blank, "
    "in source order, using code_output_blank_count. Name each step by blank position and keep each explicit teacher award. "
    "Output whitespace or line breaks never create more blanks. For code_output_form=choice, "
    "fund only the option selection, without quoting or predicting the output. When code_output_multi_select "
    "is true, state the full-set, wrong-selection, and nonempty proper-subset awards in the single funded step. "
    "Preserve teacher rules. Without a teacher rule, use 'All correct options: full credit; "
    "Any wrong option: 0 points; Partial selection: 0 points' (or '全选正确：满分；错选：0分；少选：0分'). "
    "For code_output_form=explanation, use separate ordinary point steps for explanation and output. "
    "Preserve the teacher's explicit allocations. Mark only the independent final stdout step with code_output=true. "
    "Do not award the output again in any explanation step. Omit code_output for all other questions and steps."
)
