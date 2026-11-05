"""Prepare teacher-visible examples, then bind a copy to one Python submission."""
from __future__ import annotations

import ast
import inspect
import json
import keyword
import re
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation

from backend.models import TestCase


class ProgrammingCaseError(ValueError):
    """The supplied materials do not define one executable test contract."""

    def __init__(self, message, *, issue="configuration", case_number=None):
        super().__init__(message)
        self.issue = issue
        self.case_number = case_number


# Only declaration/requirement phrases establish a name. A call appearing in
# an example, or an arbitrary helper in a submission, is not an entry contract.
_DECLARATION = re.compile(
    r"(?:\bdef\s+|(?:\b(?:implement|define|write)\s+(?:(?:a|the)\s+)?"
    r"(?:(?:Python\s+)?function\s+)?|(?:实现|编写|定义)\s*(?:一个)?\s*(?:函数)?\s*"
    r"|(?:\bfunction|函数)\s*|(?:\b(?:function )?signature|函数签名|入口函数)\s*[:：]\s*)"
    r"[ \t\x60]*)(?P<name>[A-Za-z_]\w*)\s*\(",
    re.I,
)
_NAMED = re.compile(
    r"(?:\bfunction\s+(?:named|called)|函数名(?:称)?(?:必须)?(?:为|是)|名为(?=[^。；\n]*的函数)"
    r"|(?:实现|编写|定义)\s*(?:一个)?\s*函数\s*|(?:实现|编写|定义|\bimplement|\bdefine)\s+(?=\x60))"
    r"\s*[\x60'\"]?([A-Za-z_][A-Za-z_0-9]*)", re.I,
)
_FREE_NAME = re.compile(
    r"\bany (?:valid )?(?:function )?name\b|\b(?:function )?name (?:is )?(?:arbitrary|optional|not (?:specified|required))\b"
    r"|(?:函数名|命名)(?:称)?(?:可以)?(?:任意|自定|不限|不作要求)|不(?:指定|限定|规定)函数名", re.I,
)
_FUNCTION = re.compile(r"\b(?:implement|define|write)\s+(?:(?:a|the|one)\s+)?(?:python\s+)?function\b|(?:实现|编写|定义)\s*(?:一个)?\s*函数", re.I)
_STDIN = re.compile(r"\bstdin\b|\bstandard input\b|标准输入|\bwrite (?:a |the )?(?:python )?program\b|编写(?:一个)?程序|(?:\bread\b.{0,100}\b(?:print|stdout|output)\b)|(?:读入|读取|输入).{0,60}(?:打印|输出)", re.I)
_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_NUMBERS = re.compile(rf"{_NUMBER}(?:[ \t]*[,，、][ \t]*{_NUMBER})+")
_EXPECTED_PAIR = re.compile(r"\s*(?:输入|input)\s*[:：]?\s*(.+?)\s*[,，;；]\s*(?:预期(?:结果|输出)?|期望(?:结果|输出)?|expected(?: output)?)\s*[:：]?\s*(.*?)\s*", re.I | re.S)

# Unlabelled arrows are also used in prose, code and diagrams. Only a whole
# row of literal values qualifies; quoted contents are retained verbatim.
_QUOTED = r"""(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')"""
_SCALAR = rf"(?:{_NUMBER}|{_QUOTED}|True|False|None|true|false|null)"
_ARROW_PAIR = re.compile(
    rf"[ \t]*({_NUMBERS.pattern}|{_SCALAR})[ \t]*(?:→|->)[ \t]*({_SCALAR})[ \t]*"
)


def literal_programming_case_pair(line: str) -> tuple[str, str] | None:
    pair = _EXPECTED_PAIR.fullmatch(line) or _ARROW_PAIR.fullmatch(line)
    return (pair[1], pair[2]) if pair else None


def literal_programming_case_groups(text: str) -> list[tuple[str, list[TestCase]]] | None:
    """Read an entire literal example sheet, or leave the whole sheet to the importer."""
    from backend.services.source_question_units import question_headings

    headings = question_headings(text)
    if headings and text[:headings[0].start()].strip():
        return None
    parts = [("", text)] if not headings else [
        (h["number"], h["title"] + "\n" + text[h.end():headings[i + 1].start() if i + 1 < len(headings) else len(text)])
        for i, h in enumerate(headings)
    ]
    groups = []
    for number, part in parts:
        cases = []
        for line in part.splitlines():
            if not line.strip():
                continue
            pair = literal_programming_case_pair(line)
            if pair is None:
                return None
            cases.append(TestCase(input=pair[0], expected_output=pair[1], source="teacher"))
        if not cases:
            return None
        groups.append((number, cases))
    return groups


def _declarations(stem):
    return list(_DECLARATION.finditer(stem or ""))


def required_function_names(stem: str) -> set[str]:
    """All teacher entry candidates, excluding example definitions when an entry is named."""
    declarations = _declarations(stem)
    named = {name for name in _NAMED.findall(stem or "") if not keyword.iskeyword(name)}
    for match in declarations:
        if not match.group().lstrip().lower().startswith("def "):
            named.add(match["name"])
    if named or _FREE_NAME.search(stem or ""):
        return named
    return {match["name"] for match in declarations}


def required_function_name(stem: str) -> str | None:
    names = required_function_names(stem)
    return names.pop() if len(names) == 1 else None


def _signature(args: ast.arguments) -> inspect.Signature:
    parameter = inspect.Parameter
    params = []
    positional = [*args.posonlyargs, *args.args]
    defaults_at = len(positional) - len(args.defaults)
    for i, arg in enumerate(positional):
        kind = parameter.POSITIONAL_ONLY if i < len(args.posonlyargs) else parameter.POSITIONAL_OR_KEYWORD
        params.append(parameter(arg.arg, kind, default=None if i >= defaults_at else parameter.empty))
    if args.vararg:
        params.append(parameter(args.vararg.arg, parameter.VAR_POSITIONAL))
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        params.append(parameter(arg.arg, parameter.KEYWORD_ONLY, default=None if default is not None else parameter.empty))
    if args.kwarg:
        params.append(parameter(args.kwarg.arg, parameter.VAR_KEYWORD))
    return inspect.Signature(params)


def _teacher_signature(stem, name):
    signatures = set()
    for match in _declarations(stem):
        if match["name"] != name:
            continue
        # Parse the signature only. Defaults and annotations are never evaluated.
        start = match.start("name")
        for close in re.finditer(r"\)", stem[start:start + 4096]):
            try:
                node = ast.parse("def " + stem[start:start + close.end()] + ":\n pass").body[0]
                signatures.add(_signature(node.args))
            except (SyntaxError, ValueError):
                continue
            break
        else:
            raise ProgrammingCaseError("The required function signature is invalid.")
    if len(signatures) > 1:
        raise ProgrammingCaseError("The question contains conflicting signatures.")
    return next(iter(signatures), None)


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ProgrammingCaseError("Arguments must be finite JSON values.", issue="arguments") from exc


def _literal(node):
    # ast.literal_eval also accepts tuples, sets and bytes. The executor
    # transports JSON, so never silently turn a tuple into a list, etc.
    if isinstance(node, ast.Name) and node.id in {"true", "false", "null"}:
        return {"true": True, "false": False, "null": None}[node.id]
    if isinstance(node, ast.List):
        return [_literal(element) for element in node.elts]
    if isinstance(node, ast.Dict):
        result = {}
        for key, value in zip(node.keys, node.values):
            key = _literal(key)
            if not isinstance(key, str) or key in result:
                raise ProgrammingCaseError("Object keys must be unique strings.")
            result[key] = _literal(value)
        return result
    value = ast.literal_eval(node)
    if type(value) not in (type(None), str, bool, int, float):
        raise ProgrammingCaseError("Unsupported function argument.", issue="arguments")
    _json(value)
    return value


def _arguments(text, signature, *, vector=False):
    value = text.strip()
    if _NUMBERS.fullmatch(value):
        value = re.sub(r"[，、]", ",", value)
    elif re.fullmatch(rf"{_NUMBER}(?:\s+{_NUMBER})+", value):
        value = ", ".join(value.split())
    if value == "()":
        value = ""
    try:
        call = ast.parse("f(" + value + "\n)", mode="eval").body
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name) or call.func.id != "f":
            raise ValueError
        args = [_literal(arg) for arg in call.args]
        kwargs = {kw.arg: _literal(kw.value) for kw in call.keywords}
        if None in kwargs or len(kwargs) != len(call.keywords):
            raise ValueError
    except (TypeError, ValueError, SyntaxError, RecursionError) as exc:
        # An explicit JSON string or a single plain word is one scalar.
        if re.fullmatch(r"[\w-]+", value) and value.isidentifier() and not keyword.iskeyword(value):
            return [value], {}
        raise ProgrammingCaseError("Use literal function arguments or named keyword arguments.", issue="arguments") from exc

    if not kwargs and len(args) == 1:
        item = args[0]
        if vector and isinstance(item, dict) and set(item) == {"args", "kwargs"}:
            if not isinstance(item["args"], list) or not isinstance(item["kwargs"], dict):
                raise ProgrammingCaseError("Invalid args/kwargs object.", issue="arguments")
            return item["args"], item["kwargs"]
        if isinstance(item, list):
            if vector:
                return item, {}
            if signature is not None:
                # The natural input [1,2] is a single list for f(xs).
                # For f(a,b), an array is also a conventional argument vector.
                try:
                    signature.bind(item)
                except TypeError:
                    return item, {}
    return args, kwargs


def _check_call(args, kwargs, signature):
    if json.loads(_json([args, kwargs])) != [args, kwargs]:
        raise ProgrammingCaseError("Arguments must round-trip as JSON values.", issue="arguments")
    if any(not k.isidentifier() or keyword.iskeyword(k) for k in kwargs):
        raise ProgrammingCaseError("Invalid keyword argument name.", issue="arguments")
    if signature is not None:
        try:
            signature.bind(*args, **kwargs)
        except TypeError as exc:
            raise ProgrammingCaseError("The test arguments do not match the required signature.", issue="arguments") from exc


def _expectation(text, comparison, *, function):
    if comparison == "numeric":
        try:
            if not Decimal(text.strip()).is_finite():
                raise ValueError
        except (ValueError, InvalidOperation) as exc:
            raise ProgrammingCaseError("Numeric comparison needs a finite expected number.", issue="expected_result") from exc
    elif comparison == "json":
        try:
            _json(json.loads(text))
        except (ValueError, RecursionError) as exc:
            raise ProgrammingCaseError("JSON comparison needs a valid finite JSON expectation.", issue="expected_result") from exc
    elif function:
        if comparison in {"trimmed", "tokens"}:
            raise ProgrammingCaseError("This comparison requires standard output.")
        try:
            ast.literal_eval(text.strip())
        except (ValueError, SyntaxError, RecursionError):
            try:
                value = json.loads(text)
                _json(value)
            except (ValueError, RecursionError):
                if text.lstrip().startswith(("[", "{", "'", '"')):
                    raise ProgrammingCaseError("Invalid literal expected return.", issue="expected_result")
                value = text
            return repr(value)
    return text


def _adapt_case(case, name, signature, *, function_mode, stdin_mode, edited):
    structured = case.function_args is not None or case.function_kwargs is not None
    is_function = structured or case.io_mode == "function" or case.function_name is not None or case.expected_return is not None
    if case.function_name is not None and (not case.function_name.isidentifier()
            or keyword.iskeyword(case.function_name) or (name and name != case.function_name)):
        raise ProgrammingCaseError("The test specifies an invalid or conflicting function name.")
    if (case.io_mode == "stdin" and "io_mode" in case.model_fields_set and is_function
            and (case.input or case.expected_output)):
        raise ProgrammingCaseError("The test specifies both stdin and function fields.")
    if stdin_mode and is_function:
        raise ProgrammingCaseError("A stdin program cannot use function-call fields.")

    fields = case.model_fields_set
    text = case.input
    expected = case.expected_output if "expected_output" in fields else None
    pair = _EXPECTED_PAIR.fullmatch(text)
    if pair:
        if expected is not None and expected != pair[2]:
            raise ProgrammingCaseError("The example contains two different expectations.", issue="expected_result")
        text, expected = pair.groups()
    if not (function_mode or is_function):
        if expected is None:
            raise ProgrammingCaseError("The test needs an explicit expected output.", issue="expected_result")
        _expectation(expected, case.comparison, function=False)
        return case.model_copy(update={"expected_output": expected}) if pair else case

    args, kwargs = case.function_args or [], case.function_kwargs or {}
    if not structured and not text.strip() and (
        "input" not in fields or (case.function_name and signature is None)
    ):
        raise ProgrammingCaseError("The function test needs explicit arguments (use [] for none).", issue="arguments")
    # Structured-only historical rows have empty display placeholders. Fill
    # those once for the existing editor. On save, its submitted fields win.
    if not structured or text or (edited and "input" in fields):
        vector = structured
        parsed_args, parsed_kwargs = _arguments(text, signature, vector=vector)
        if structured and _json([args, kwargs]) != _json([parsed_args, parsed_kwargs]):
            natural = _arguments(text, signature, vector=False)
            if _json([args, kwargs]) == _json(list(natural)):
                parsed_args, parsed_kwargs = natural
            elif edited and len(args) == 1 and isinstance(args[0], list) and text.strip().startswith("["):
                # Keep the natural [1,2] spelling of a single-list case.
                if len(parsed_args) != 1 or not isinstance(parsed_args[0], list):
                    parsed_args, parsed_kwargs = natural
            elif not edited:
                raise ProgrammingCaseError("The displayed input conflicts with the stored arguments.", issue="arguments")
        args, kwargs = parsed_args, parsed_kwargs
    _check_call(args, kwargs, signature)
    if structured and not edited and case.expected_return is None and not text and not expected:
        raise ProgrammingCaseError("The structured test needs an explicit expected return.", issue="expected_result")
    displayed = expected
    if not edited and case.expected_return is not None and not expected:
        # Historical structured cases serialized empty text placeholders.
        displayed = case.expected_return
    if displayed is None:
        displayed = case.expected_return
    if displayed is None:
        raise ProgrammingCaseError("The function test needs an explicit expected return.", issue="expected_result")
    result = _expectation(displayed, case.comparison, function=True)
    if not edited and case.expected_return is not None and result != _expectation(
        case.expected_return, case.comparison, function=True
    ):
        raise ProgrammingCaseError("The displayed expectation conflicts with the stored return.", issue="expected_result")
    updates = dict(io_mode="function", expected_return=result)
    if not structured and signature is None and text.strip().startswith("["):
        # A plain list can mean f(xs) or f(a,b). With no required signature
        # keep that original input until binding; never use a student's
        # signature to alter a teacher-mandated entry.
        if name or case.function_name:
            raise ProgrammingCaseError("Specify the argument vector for this named function.", issue="arguments")
    else:
        updates["function_args"] = args
    if name or case.function_name:
        updates["function_name"] = name or case.function_name
    if kwargs or case.function_kwargs is not None:
        updates["function_kwargs"] = kwargs
    if not case.input and structured and not edited and (args or kwargs or "input" not in fields):
        updates["input"] = _json({"args": args, "kwargs": kwargs} if kwargs else args)
    if not case.expected_output and displayed and not (expected == "" and result == repr("")):
        updates["expected_output"] = displayed
    return case.model_copy(update=updates)


def adapt_programming_cases(stem: str, cases, *, edited=False) -> list[TestCase]:
    """Validate and preserve a complete collection without consulting student code."""
    if not isinstance(cases, Sequence) or isinstance(cases, (str, bytes)) or len(cases) > 50:
        raise ProgrammingCaseError("Expected a list of at most 50 test cases.", issue="case_limit")
    if not cases:
        return []
    names = required_function_names(stem)
    if len(names) > 1:
        raise ProgrammingCaseError("The question does not identify one function entry point.")
    name = next(iter(names), None)
    signature = _teacher_signature(stem, name)
    function_mode = bool(name or _FUNCTION.search(stem) or _FREE_NAME.search(stem))
    alternatives = re.search(r"\beither\b|\bor (?:use|read|write)\b|或者|或(?:者)?使用|二选一", stem, re.I)
    stdin_mode = bool(_STDIN.search(stem)) and (not function_mode or bool(alternatives))
    if function_mode and stdin_mode:
        raise ProgrammingCaseError("The question specifies two possible execution interfaces.")
    result, outputs = [], {}
    for i, raw in enumerate(cases, 1):
        try:
            if isinstance(raw, TestCase):
                raw = {key: getattr(raw, key) for key in raw.model_fields_set}
            if isinstance(raw, dict) and raw.keys() - TestCase.model_fields.keys():
                raise ProgrammingCaseError("Unknown test case fields.")
            case = TestCase.model_validate(raw, strict=True).model_copy(deep=True)
            case = _adapt_case(case, name, signature, function_mode=function_mode,
                               stdin_mode=stdin_mode, edited=edited)
            identity = (["function", case.function_name, case.function_args if case.function_args is not None else case.input, case.function_kwargs or {}]
                        if case.io_mode == "function" else ["stdin", case.input])
            if case.io_mode == "function" and signature is not None:
                identity = ["function", signature.bind(*case.function_args, **(case.function_kwargs or {})).arguments]
            key = _json(identity)
            expected = case.expected_return if case.io_mode == "function" else case.expected_output
            if key in outputs and outputs[key] != expected:
                raise ProgrammingCaseError("The same input has conflicting expected results.", issue="conflicting_expectations")
            outputs[key] = expected
        except (ValueError, RecursionError) as exc:
            message = str(exc) if isinstance(exc, ProgrammingCaseError) else "Invalid case fields."
            raise ProgrammingCaseError(
                f"Test case {i}: {message}", case_number=i,
                issue=exc.issue if isinstance(exc, ProgrammingCaseError) else "configuration",
            ) from exc
        result.append(case)
    if len({c.io_mode for c in result}) > 1 or len({c.function_name for c in result if c.function_name}) > 1:
        raise ProgrammingCaseError("The tests specify conflicting execution interfaces.")
    return result


def _stdin_cases(stem, cases):
    # Existing stdin payloads (including spaces, line endings and CSV) are
    # literal. Only translate a natural numeric enumeration for a stated
    # numeric, whitespace-based input protocol.
    converted = []
    for case in cases:
        pair = _EXPECTED_PAIR.fullmatch(case.input)
        value = pair[1] if pair and pair[2] == case.expected_output else case.input
        if _NUMBERS.fullmatch(value.strip()) and re.search(r"integer|number|整数|数字|数值|两个数", stem, re.I):
            if not re.search(r"csv|comma|逗号|顿号", stem, re.I):
                delimiter = "\n" if re.search(r"separate lines?|each (?:on )?a line|one per line|two lines|每行|两行|分行", stem, re.I) else " "
                value = delimiter.join(re.split(r"\s*[,，、]\s*", value.strip()))
        converted.append(case.model_copy(update={"input": value}))
    return converted


def _script_io(body):
    # Ignore I/O inside function/class definitions until a module-level
    # statement actually runs. A main guard is an executable program path.
    for statement in body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for node in ast.walk(statement):
            if isinstance(node, ast.Name) and node.id == "__name__":
                return True
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"print", "input"}:
                return True
            if isinstance(node, ast.Attribute) and node.attr in {"stdin", "stdout"} and isinstance(node.value, ast.Name) and node.value.id == "sys":
                return True
    return False


def bind_programming_cases(stem: str, cases, code: str, *, entry_hint: str | None = None) -> list[TestCase]:
    """Resolve unspecified interfaces per submission, never fix an explicit student error."""
    prepared = adapt_programming_cases(stem, cases)
    if not prepared or required_function_name(stem):
        return prepared
    function_mode = any(c.io_mode == "function" for c in prepared) or bool(_FUNCTION.search(stem))
    if not function_mode and _STDIN.search(stem):
        return _stdin_cases(stem, prepared)
    # A named teacher test is itself an explicit entry requirement.
    teacher_names = {c.function_name for c in prepared if c.source == "teacher" and c.function_name}
    if teacher_names and not _FREE_NAME.search(stem):
        name = next(iter(teacher_names))
        return [c.model_copy(update={"function_name": name}) for c in prepared]
    try:
        body = ast.parse(code).body
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise ProgrammingCaseError("Cannot identify the interface of this submission.") from exc
    candidates = [node for node in body if isinstance(node, ast.FunctionDef)]
    script_io = _script_io(body)
    if not function_mode and script_io and not candidates:
        return _stdin_cases(stem, prepared)
    if entry_hint is not None and (function_mode or not script_io):
        candidates = [node for node in candidates if node.name == entry_hint]
        if len(candidates) != 1:
            raise ProgrammingCaseError("The mapped function must have one top-level definition.")
    if len({node.name for node in candidates}) != len(candidates):
        raise ProgrammingCaseError("A function name has multiple definitions.")
    matches = []
    for node in candidates:
        try:
            signature = _signature(node.args)
            bound = []
            for case in prepared:
                case = case.model_copy(update={"function_name": None})
                bound.append(_adapt_case(case, node.name, signature, function_mode=True,
                                         stdin_mode=False, edited=False))
        except (ValueError, RecursionError):
            continue
        matches.append(bound)
    if not function_mode and script_io:
        if matches:
            raise ProgrammingCaseError("Both stdin and function interfaces are possible.")
        return _stdin_cases(stem, prepared)
    if len(matches) != 1:
        raise ProgrammingCaseError("Cannot bind the tests to a unique compatible top-level function.")
    return matches[0]
