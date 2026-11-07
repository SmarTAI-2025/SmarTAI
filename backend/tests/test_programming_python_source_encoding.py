"""The executable copy must have the same decoded program as the uploaded bytes."""
import ast
import codecs
import hashlib

import pytest

from backend.models import TestCase as Case
from backend.services.python_source_encoding import (
    PythonSourceEncodingError, decode_python_submission, python_execution_source,
)
from backend.tools.code_interpreter import run_sandbox


@pytest.mark.parametrize(("encoding","label","literal"), [
    ("gbk","GBK","中文"), ("gb18030","gb18030","中文"),
    ("latin-1","latin-1","café"), ("cp1252","windows-1252","€"),
    ("shift_jis","shift_jis","文字"), ("utf-8-sig","utf-8","中文"),
])
@pytest.mark.parametrize("eol", ["\n","\r\n","\r"])
def test_declared_source_preserves_body_and_equivalent_execution_copy(encoding,label,literal,eol):
    source=eol.join([f"#! /usr/bin/python  # {literal}",f"# -*- coding: {label} -*-  # {literal}",
                     f"def solve(): return {literal!r}",""])
    original=source.encode(encoding)
    checksum=hashlib.sha256(original).digest()
    decoded=decode_python_submission(original)
    assert decoded==source
    copied=python_execution_source(decoded)
    assert copied==source.replace("coding: "+label, "coding: utf-8")
    # Compare compiler structures (including source coordinates), not only
    # pass counts. The teacher/student record still has the original bytes.
    original_tree=compile(original,"<source>","exec",ast.PyCF_ONLY_AST)
    copied_tree=compile(copied.encode("utf-8"),"<source>","exec",ast.PyCF_ONLY_AST)
    assert ast.dump(copied_tree,include_attributes=True)==ast.dump(original_tree,include_attributes=True)
    assert hashlib.sha256(original).digest()==checksum


def test_normalization_is_limited_to_real_first_two_line_cookie():
    code="x = 1\n# coding: latin-1\nprint('# coding: gbk')\n"
    assert decode_python_submission(code.encode())==python_execution_source(code)==code
    assert python_execution_source("# coding: utf-8-unix\nprint(1)")=="# coding: utf-8-unix\nprint(1)"


@pytest.mark.parametrize("body", [
    codecs.BOM_UTF8+b"# coding: latin-1\npass\n",
    b"# coding: invalid-encoding\npass\n", b"# coding: unicode_escape\npass\n",
    b"# coding: bz2_codec\npass\n", b"# coding: latin-1\n\x00binary",
    b"plain non-utf8 \xff", codecs.BOM_UTF8+codecs.BOM_UTF8+b"pass\n",
])
def test_encoding_failures_report_reviewable_input(body):
    with pytest.raises(PythonSourceEncodingError):
        decode_python_submission(body)


@pytest.mark.asyncio
async def test_unrepresentable_editor_source_never_executes_a_scored_case(monkeypatch):
    async def unexpected(*args,**kwargs):
        pytest.fail("Encoding conflict must stop before executing a student case")
    monkeypatch.setattr("backend.tools.code_interpreter._run_case",unexpected)
    with pytest.raises(PythonSourceEncodingError):
        await run_sandbox("# coding: latin-1\nprint('中文')",[Case(input="",expected_output="中文")])


@pytest.mark.asyncio
@pytest.mark.parametrize(("name","code","encoding","case"), [
    ("function-gbk","# coding: gbk\r\nTAG = '中文'\r\ndef solve(x): return TAG + x\r\n","gbk",
     Case(function_name="solve",function_args=["值"],expected_return="'中文值'")),
    ("stdin-latin1","# coding: latin-1 # café\nword='café'\nprint(word + input())\n","latin-1",
     Case(input="!\n",expected_output="café!")),
    ("bom-function","def solve(): return '中文'\n","utf-8-sig",
     Case(function_name="solve",function_args=[],expected_return="'中文'")),
])
async def test_authored_program_reaches_real_function_or_stdin_process(name,code,encoding,case):
    decoded=decode_python_submission(code.encode(encoding))
    assert decoded==code
    report=await run_sandbox(decoded,[case])
    assert report.passed_count==report.total_count==1, (name,report.results)


@pytest.mark.asyncio
async def test_run_keeps_original_unicode_source_and_binds_only_affected_execution_keys():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from backend.services import programming_runs as runs
    from backend.tests.test_programming_run_evidence import grade, question, submission

    source="# coding: gbk\ndef add(a,b): return '中文'\n"
    original_bytes=source.encode("gbk")
    decoded=decode_python_submission(original_bytes)
    copied=python_execution_source(decoded)
    tc=Case(io_mode="function",function_name="add",function_args=[2,3],expected_return="'中文'")
    problem=question([tc],solution_code=decoded)
    with runs.programming_run(SimpleNamespace(),None) as state:
        evidence=await grade(problem,"student",AsyncMock(),run_sandbox,submission(decoded))
        executed=next(item["value"] for item in state.records.values() if item["kind"]==runs.COMPLETED)
    assert evidence.status=="executed" and evidence.code==source and evidence.report.passed_count==1
    assert executed["identity"]["source_sha256"]==runs.source_hash(source)
    assert executed["identity"]["execution_source_sha256"]==runs.source_hash(copied)
    assert runs._reference_execution_identity(problem)["execution_source_sha256"]==runs.source_hash(copied)
    assert runs._reference_execution_identity(question([tc],solution_code="def add(a,b): return a+b"))==runs.runtime_identity()


@pytest.mark.asyncio
async def test_unverified_encoding_never_becomes_a_student_vote():
    from unittest.mock import AsyncMock
    from backend.tests.test_programming_run_evidence import grade,question,case,submission
    execute=AsyncMock(side_effect=AssertionError("unverifiable encoding must not run"))
    evidence=await grade(question([case()]),"student",AsyncMock(),execute,submission("# coding: unicode_escape\ndef add(a,b): return a+b"))
    assert evidence.status=="unavailable" and not evidence.report
    execute.assert_not_awaited()


def test_source_with_student_syntax_error_is_still_receivable():
    body="# coding: latin-1 # café\ndef broken(:".encode("latin-1")
    assert decode_python_submission(body)=="# coding: latin-1 # café\ndef broken(:"


@pytest.mark.parametrize(("stem", "cases"), [
    ("Implement def add(a,b).", []),
    ("Write a function to add two integers.", [Case(input="2,3", expected_output="5")]),
    ("Any function name is allowed.", []),
    ("Add two integers.", [Case(io_mode="function", input="2,3", expected_return="5")]),
    ("Add two integers.", [Case(function_args=[2, 3], expected_return="5")]),
])
def test_function_reference_keeps_its_pre_stdin_change_identity(stem, cases):
    from backend.services import programming_runs as runs
    from backend.tests.test_programming_run_evidence import question

    problem = question(cases, solution_code="def add(a,b): return a+b").model_copy(update={"stem": stem})
    assert runs._reference_execution_identity(problem) == runs.runtime_identity()


@pytest.mark.parametrize("stem", [
    "Count the bytes in this integer record from stdin.",
    "Count the bytes in the supplied record.",
    "Either write a function or read from stdin.",
])
def test_stdin_or_ambiguous_reference_disallows_old_cached_validation(stem):
    from backend.services import programming_runs as runs
    from backend.tests.test_programming_run_evidence import question

    case = Case(input="-2,0,+3\r\n", expected_output="9")
    problem = question([case], solution_code="import sys\nprint(len(sys.stdin.buffer.read()))").model_copy(update={"stem": stem})
    assert runs._reference_execution_identity(problem) == {**runs.runtime_identity(), "reference_stdin_binding_version": 2}


@pytest.mark.asyncio
@pytest.mark.parametrize("stdin", [False, True], ids=["reuse-function", "revalidate-stdin"])
async def test_retry_of_pre_binding_change_reference_validation(monkeypatch, stdin):
    from unittest.mock import AsyncMock
    from backend.services import programming_runs as runs
    from backend.tests.test_programming_run_evidence import question

    if stdin:
        case = Case(input="-2,0,+3\r\n", expected_output="9")
        problem = question([case], solution_code="import sys\nprint(len(sys.stdin.buffer.read()))")
        problem.stem = "Count the bytes in this integer record from stdin."
    else:
        case = Case(io_mode="function", function_args=[2, 3], expected_return="5")
        problem = question([case], solution_code="def add(a,b): return a+b")
        problem.stem = "Return the sum."
    state = runs.ProgrammingRun()
    with monkeypatch.context() as prior:
        prior.setattr(runs, "_reference_execution_identity", lambda _: runs.runtime_identity())
        old_key = runs._question_key(state, problem)
    data = [case.model_dump(mode="json")]
    state.store(runs.PREPARED, old_key, {"q_id": problem.q_id, "cases": data,
                                       "suite_hash": runs.digest(data), "reference_checked": True})
    execute = AsyncMock(wraps=run_sandbox)
    key, cases, reason = await runs._prepare(state, problem, None, None, execute, None)
    assert not reason and len(cases) == 1
    if stdin:
        assert key != old_key and execute.await_count == 1
        assert execute.await_args.args[1][0].input == case.input
    else:
        assert key == old_key
        execute.assert_not_awaited()
