"""Associate a single uploaded source file with the assignment's programming questions.

Read declarations and explicit labels as evidence only. The complete decoded file
is the answer even when the student's implementation violates the teacher's API.
"""
from __future__ import annotations

import ast
import io
import re
import tokenize
from collections import Counter, defaultdict
from pathlib import PurePath

from backend.models import is_programming_question_type
from backend.services.upload_contract import PROGRAMMING_SOURCE_SUFFIXES
from backend.services.programming_test_contract import required_function_names
from backend.services.question_matching import question_number_candidates

NATIVE_SOURCE_MAPPING_VERSION = 4
PYTHON_FLAG = "programming_source_language:python"
C_FAMILY_FLAG = "programming_source_language:c_family"
NATIVE_LANGUAGES = {"programming_source_language:c": "c", "programming_source_language:cpp": "cpp"}
SOURCE_LANGUAGE_FLAGS = frozenset({PYTHON_FLAG, C_FAMILY_FLAG, *NATIVE_LANGUAGES})


def is_source_file(filename):
    return PurePath(filename).suffix.lower() in PROGRAMMING_SOURCE_SUFFIXES


def may_contain_source(filename, problems):
    if is_source_file(filename):
        return bool(problems)
    return (PurePath(filename).suffix.lower() in {".txt", ".md", ".markdown"} and bool(problems)
            and all(is_programming_question_type(p.get("type")) for p in problems.values()))


def _standalone_python(text):
    # Unlabelled text needs positive evidence of a *whole* module. Bound parser
    # complexity before AST construction. Ambiguous prose stays on document review.
    if len(text) > 500_000 or not re.search(r"(?m)^def [^\W\d]\w*\s*\(|\b(?:input\s*\(|sys\.stdin\b)", text):
        return False
    statement_tokens = 0
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            statement_tokens += 1
            if token.type == tokenize.NEWLINE:
                statement_tokens = 0
            if statement_tokens > 256:
                return False
        module = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, tokenize.TokenError):
        return False
    # A module may start with one docstring. Other bare strings, names or
    # formulas can be written answers and still require document review.
    statements = module.body[1:] if ast.get_docstring(module, clean=False) is not None else module.body
    if any(isinstance(node, ast.Expr) and not isinstance(node.value, ast.Call) for node in statements):
        return False
    if any(isinstance(node, ast.FunctionDef) for node in module.body):
        return True
    for node in ast.walk(module):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "input":
            return True
        if isinstance(node, ast.Attribute) and node.attr == "stdin" and isinstance(node.value, ast.Name):
            if node.value.id == "sys":
                return True
    return False


def has_markup_fence(text, *, python=True):
    """Ignore fence-shaped lines quoted inside Python literals."""
    if not re.search(r"(?m)^[^\S\r\n]*```", text):
        return False
    if not python:
        return True
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type in {tokenize.OP, tokenize.ERRORTOKEN} and token.string == "`":
                column = token.start[1]
                if not token.line[:column].strip() and token.line.startswith("```", column):
                    return True
    except (SyntaxError, tokenize.TokenError):
        return True
    return False


def native_source(filename, text, problems):
    if not may_contain_source(filename, problems) or not text:
        return None
    suffix = PurePath(filename).suffix.lower()
    if is_source_file(filename):
        return suffix, text
    fence = re.fullmatch(r"[ \t]*```(python|py|c|cpp|c\+\+)[ \t]*\r?\n(.*?)^```[ \t]*\s*",
                         text, re.I | re.S | re.M)
    if fence and fence[2].strip():
        python = fence[1].lower() in {"python", "py"}
        if not has_markup_fence(fence[2], python=python):
            return (".py" if python else ".cpp"), fence[2]
    if suffix == ".txt" and _standalone_python(text):
        return ".py", text
    return None


_QUESTION_COMMENT = re.compile(
    r"#\s*(?:第\s*|(?:question|problem|q)\s*)(\d+(?:\.\d+)*)(?:\s*题)?(?=[\s:：.、()（）-]|$)", re.I)


def _python_declarations(text):
    """Return top-level declarations, with any immediately preceding question label.

    Tokenizing (without importing or compiling) excludes nested functions,
    apparent headings inside strings and indented comments in a helper's body.
    A syntax error does not prevent retaining the literal submission.
    """
    functions = []
    label = declaration_line = None
    decorator = False
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            line, column = token.start
            if token.type == tokenize.COMMENT:
                heading = _QUESTION_COMMENT.match(token.string) if column == 0 else None
                if heading:
                    label = heading[1]
                continue
            if token.type == tokenize.NEWLINE:
                decorator = False
                continue
            if token.type in {tokenize.NL, tokenize.INDENT, tokenize.DEDENT}:
                continue
            if token.string == "@" and column == 0:
                decorator = True
            if decorator:
                continue
            if token.string == "def" and column == 0:
                declaration_line = line
            else:
                if line == declaration_line and token.type == tokenize.NAME:
                    functions.append((token.string, label))
                declaration_line = label = None
    except (tokenize.TokenError, SyntaxError):
        pass
    return functions


def _teacher_entries(problem):
    names = required_function_names(problem.get("stem") or "")
    for case in problem.get("test_cases") or ():
        if isinstance(case, dict) and case.get("source", "teacher") == "teacher":
            name = case.get("function_name")
            if isinstance(name, str) and name:
                names.add(name)
    return names


def source_answers(filename, text, problems, target_ids):
    """Return bounded answer rows or None for the ordinary document parser."""
    native = native_source(filename, text, problems)
    if native is None:
        return None
    suffix, text = native
    programming = {qid: p for qid, p in problems.items() if is_programming_question_type(p.get("type"))}
    copies = len(programming.keys() & target_ids)
    if len(target_ids) > 1000 or len(text) > 500_000 or copies * len(text) + 1024 * len(target_ids) > 1_000_000:
        raise ValueError("source_text_too_large")

    python = suffix == ".py"
    definitions = _python_declarations(text) if python else []
    actual = Counter(name for name, _ in definitions)
    contracts = {qid: _teacher_entries(p) for qid, p in programming.items()}
    owners = defaultdict(set)
    for qid, names in contracts.items():
        for name in names:
            owners[name].add(qid)
    labelled = defaultdict(list)
    conflicts = set()
    for name, number in definitions:
        if number is None:
            continue
        candidates = question_number_candidates(number, problems)
        if len(candidates) != 1:
            if not candidates and len(programming) == 1:
                conflicts.update(programming)
            conflicts.update(candidates)
            conflicts.update(owners[name])
            continue
        qid = candidates[0]
        labelled[qid].append(name)
        if owners[name] and owners[name] != {qid}:
            conflicts.update({qid, *owners[name]})

    answers = []
    for qid, problem in problems.items():
        if qid not in target_ids:
            continue
        content, flags = "", []
        if qid not in programming:
            # A code-only upload supplies no transcription of a non-code question.
            flags = ["recognition_manual_pending"]
        else:
            content = text
            names = contracts[qid]
            entry = next(iter(names)) if len(names) == 1 else None
            labels = labelled[qid]
            unique_question = len(programming) == 1
            ambiguous = qid in conflicts or len(names) > 1 or len(labels) > 1
            if entry:
                ambiguous |= (not python or len(owners[entry]) != 1 or actual[entry] > 1
                              or (bool(labels) and labels != [entry] and actual[entry] > 0)
                              or (not unique_question and not labels and not actual[entry]))
            elif labels:
                # Per-answer binding, never the first function of the whole module.
                ambiguous |= actual[labels[0]] != 1
                if len(labels[0]) > 200:
                    ambiguous = True
                if not ambiguous:
                    flags.append("programming_source_entry:" + labels[0])
            else:
                # Only a sole programming question can own an unlabelled script.
                # Multiple functions without a teacher entry need review unless the
                # task explicitly asks for a stdin program (it may have helpers).
                stdin = re.search(r"\bstdin\b|standard input|标准输入", problem.get("stem") or "", re.I)
                ambiguous |= not unique_question or (len(definitions) > 1 and not stdin)
            if ambiguous:
                flags = ["recognition_manual_pending"]
            if not python:
                flags.append(("programming_source_language:c" if suffix == ".c" else "programming_source_language:cpp")
                             if is_source_file(filename) else C_FAMILY_FLAG)
            elif PurePath(filename).suffix.lower() == ".py":
                flags.append(PYTHON_FLAG)
        answers.append(dict(q_id=qid, number=str(problem.get("number") or ""),
                            type=problem.get("type") or "", content=content, flag=flags))
    return answers
