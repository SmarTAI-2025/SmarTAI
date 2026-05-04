"""Unit tests for the rewritten ProgrammingSkill (sandbox 4-tier ladder).

Coverage:
  - Teacher test cases present       → cases_source="teacher", sandbox runs
  - No teacher cases, LLM generates  → cases_source="llm_generated", sandbox runs
  - Complexity keyword in stem/code  → cases_source="skipped_complex", no sandbox
  - Non-Python student code          → cases_source="skipped_lang", no sandbox
  - LLM marks all infeasible         → cases_source="skipped_complex"
  - LLM returns 20 cases             → only 8 actually run

Run with:
    /opt/anaconda3/envs/smartai/bin/python -m pytest backend/tests/test_programming_skill.py -v
"""
from __future__ import annotations

import os
os.environ["SMARTAI_HTTP_PROXY"] = ""
os.environ["SMARTAI_HTTPS_PROXY"] = ""

from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.models import ExpertResult, ProblemInfo, StudentAnswerInfo, TestCase
from backend.skills.programming import (
    ProgrammingSkill,
    _detect_language,
    _scan_complexity_keywords,
    _coerce_test_cases,
    MAX_LLM_GENERATED_CASES,
)


def _fake_provider():
    p = MagicMock()
    p.provider_id = "mock:test"
    p.ainvoke = AsyncMock()
    return p


def _make_problem(
    *,
    type_: str = "编程题",
    stem: str = "Read two ints, print their sum.",
    test_cases: list | None = None,
) -> ProblemInfo:
    return ProblemInfo(
        q_id="q1",
        number="1",
        type=type_,
        stem=stem,
        criterion="Correctness; readability.",
        test_cases=test_cases,
    )


def _make_answer(content: str) -> StudentAnswerInfo:
    return StudentAnswerInfo(q_id="q1", number="1", type="编程题", content=content)


PYTHON_ADD_CODE = """
a = int(input())
b = int(input())
print(a + b)
""".strip()


# ─── Pure-helper unit tests (no async, no LLM) ───────────────────────────────

def test_detect_language_python():
    assert _detect_language(PYTHON_ADD_CODE) == "python"


def test_detect_language_cpp():
    code = '#include <iostream>\nint main() { std::cout << 1; }'
    assert _detect_language(code) == "cpp"


def test_detect_language_java():
    code = 'public class Sol { public static void main(String[] args) {} }'
    assert _detect_language(code) == "java"


def test_scan_complexity_keywords_hits_tkinter():
    hit = _scan_complexity_keywords("import tkinter\nroot = tkinter.Tk()")
    assert hit is not None
    assert hit[0] == "tkinter"


def test_scan_complexity_keywords_clean():
    assert _scan_complexity_keywords("def add(a, b): return a + b") is None


def test_coerce_test_cases_from_dicts():
    raw = [
        {"input": "1\n2", "expected_output": "3", "description": "1+2"},
        {"input": "5\n7", "expected_output": "12", "description": "5+7"},
    ]
    out = _coerce_test_cases(raw)
    assert len(out) == 2
    assert all(isinstance(tc, TestCase) for tc in out)
    assert out[0].input == "1\n2"


def test_coerce_test_cases_from_models():
    raw = [TestCase(input="x", expected_output="y", description="z")]
    out = _coerce_test_cases(raw)
    assert len(out) == 1
    assert out[0].description == "z"


# ─── grade() — teacher cases ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_prog_with_teacher_test_cases(monkeypatch):
    problem = _make_problem(test_cases=[
        TestCase(input="1\n2", expected_output="3", description="1+2", source="teacher"),
        TestCase(input="5\n7", expected_output="12", description="5+7", source="teacher"),
    ])
    answer = _make_answer(PYTHON_ADD_CODE)

    # Mock run_sandbox to return 2/2 pass
    async def fake_sandbox(code, test_cases, *, language, per_case_timeout):
        from backend.tools.code_interpreter import ExecutionReport, TestResult
        results = [
            TestResult(test=tc, passed=True, actual_output=tc.expected_output, error="", duration_ms=10)
            for tc in test_cases
        ]
        return ExecutionReport(
            passed_count=len(results), total_count=len(results),
            pass_rate=1.0, results=results, summary="all pass",
        )

    monkeypatch.setattr("backend.skills.programming.run_sandbox", fake_sandbox)

    fake_output = MagicMock(
        score=10.0, max_score=10.0, confidence=0.95,
        comment="Clean code.", steps=[], logs="",
    )
    fake_raw = MagicMock(content="{}", duration_ms=50)

    async def fake_call(*a, **kw):
        return fake_output, fake_raw

    monkeypatch.setattr("backend.skills.programming.structured_llm_call", fake_call)

    skill = ProgrammingSkill(provider=_fake_provider())
    result = await skill.grade(problem, answer, student_id="s1")
    assert isinstance(result, ExpertResult)
    assert "教师测试用例" in result.comment
    assert "通过 2/2" in result.comment


# ─── grade() — LLM-generated cases ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_prog_no_cases_generates_via_llm(monkeypatch):
    problem = _make_problem(test_cases=None)
    answer = _make_answer(PYTHON_ADD_CODE)

    # LLM generates 3 cases all feasible
    async def fake_gen(provider, problem, function_name=None):
        return [
            TestCase(input="1\n1", expected_output="2", description="1+1",
                     source="llm_generated", sandbox_feasible=True),
            TestCase(input="0\n0", expected_output="0", description="zero",
                     source="llm_generated", sandbox_feasible=True),
            TestCase(input="100\n200", expected_output="300", description="big",
                     source="llm_generated", sandbox_feasible=True),
        ], None

    monkeypatch.setattr("backend.skills.programming._generate_test_cases", fake_gen)

    async def fake_sandbox(code, test_cases, *, language, per_case_timeout):
        from backend.tools.code_interpreter import ExecutionReport, TestResult
        results = [
            TestResult(test=tc, passed=True, actual_output=tc.expected_output, error="", duration_ms=10)
            for tc in test_cases
        ]
        return ExecutionReport(
            passed_count=len(results), total_count=len(results),
            pass_rate=1.0, results=results, summary="all pass",
        )

    monkeypatch.setattr("backend.skills.programming.run_sandbox", fake_sandbox)

    fake_output = MagicMock(
        score=9.0, max_score=10.0, confidence=0.9,
        comment="Looks good.", steps=[], logs="",
    )
    fake_raw = MagicMock(content="{}", duration_ms=50)

    async def fake_call(*a, **kw):
        return fake_output, fake_raw

    monkeypatch.setattr("backend.skills.programming.structured_llm_call", fake_call)

    skill = ProgrammingSkill(provider=_fake_provider())
    result = await skill.grade(problem, answer, student_id="s1")
    assert "AI 生成测试用例" in result.comment


# ─── grade() — keyword skips sandbox ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_prog_complexity_keyword_skips_sandbox(monkeypatch):
    problem = _make_problem(stem="Build a tkinter GUI window.", test_cases=None)
    code_with_tk = "import tkinter\nroot = tkinter.Tk()\nroot.mainloop()"
    answer = _make_answer(code_with_tk)

    # If sandbox runs, fail the test loud.
    sandbox_called = []

    async def fake_sandbox(*a, **kw):
        sandbox_called.append(1)
        raise AssertionError("sandbox should not run for tkinter")

    monkeypatch.setattr("backend.skills.programming.run_sandbox", fake_sandbox)

    fake_output = MagicMock(
        score=5.0, max_score=10.0, confidence=0.7,
        comment="Code reviewed manually.", steps=[], logs="",
    )
    fake_raw = MagicMock(content="{}", duration_ms=50)

    async def fake_call(*a, **kw):
        return fake_output, fake_raw

    monkeypatch.setattr("backend.skills.programming.structured_llm_call", fake_call)

    skill = ProgrammingSkill(provider=_fake_provider())
    result = await skill.grade(problem, answer, student_id="s1")
    assert sandbox_called == []
    assert "复杂度超出" in result.comment
    assert "tkinter" in result.comment or "GUI" in result.comment


# ─── grade() — non-Python ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_prog_non_python_skipped(monkeypatch):
    problem = _make_problem(test_cases=None)
    cpp = "#include <iostream>\nint main() { std::cout << 1; return 0; }"
    answer = _make_answer(cpp)

    # Likewise — sandbox must not run.
    async def fake_sandbox(*a, **kw):
        raise AssertionError("sandbox should not run for C++")

    monkeypatch.setattr("backend.skills.programming.run_sandbox", fake_sandbox)

    fake_output = MagicMock(
        score=6.0, max_score=10.0, confidence=0.7,
        comment="Reviewed C++.", steps=[], logs="",
    )
    fake_raw = MagicMock(content="{}", duration_ms=50)

    async def fake_call(*a, **kw):
        return fake_output, fake_raw

    monkeypatch.setattr("backend.skills.programming.structured_llm_call", fake_call)

    skill = ProgrammingSkill(provider=_fake_provider())
    result = await skill.grade(problem, answer, student_id="s1")
    assert "Python" in result.comment
    assert "代码审查" in result.comment


# ─── grade() — LLM marks every generated case infeasible ────────────────────

@pytest.mark.asyncio
async def test_prog_llm_marks_all_infeasible(monkeypatch):
    problem = _make_problem(test_cases=None)
    answer = _make_answer(PYTHON_ADD_CODE)

    async def fake_gen(provider, problem, function_name=None):
        return [
            TestCase(input="x", expected_output="y", description="needs network",
                     source="llm_generated", sandbox_feasible=False),
            TestCase(input="x", expected_output="y", description="needs GUI",
                     source="llm_generated", sandbox_feasible=False),
        ], None

    monkeypatch.setattr("backend.skills.programming._generate_test_cases", fake_gen)

    async def fake_sandbox(*a, **kw):
        raise AssertionError("sandbox should not run when all cases infeasible")

    monkeypatch.setattr("backend.skills.programming.run_sandbox", fake_sandbox)

    fake_output = MagicMock(
        score=5.0, max_score=10.0, confidence=0.6,
        comment="Code review only.", steps=[], logs="",
    )
    fake_raw = MagicMock(content="{}", duration_ms=50)

    async def fake_call(*a, **kw):
        return fake_output, fake_raw

    monkeypatch.setattr("backend.skills.programming.structured_llm_call", fake_call)

    skill = ProgrammingSkill(provider=_fake_provider())
    result = await skill.grade(problem, answer, student_id="s1")
    assert "复杂度超出" in result.comment


# ─── grade() — LLM returns >8 cases ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_prog_caps_at_max_llm_generated_cases(monkeypatch):
    problem = _make_problem(test_cases=None)
    answer = _make_answer(PYTHON_ADD_CODE)

    async def fake_gen(provider, problem, function_name=None):
        return [
            TestCase(input=f"{i}\n{i}", expected_output=str(2 * i),
                     description=f"case {i}", source="llm_generated",
                     sandbox_feasible=True)
            for i in range(20)
        ], None

    monkeypatch.setattr("backend.skills.programming._generate_test_cases", fake_gen)

    captured_cases = []

    async def fake_sandbox(code, test_cases, *, language, per_case_timeout):
        captured_cases.extend(test_cases)
        from backend.tools.code_interpreter import ExecutionReport, TestResult
        results = [
            TestResult(test=tc, passed=True, actual_output=tc.expected_output, error="", duration_ms=1)
            for tc in test_cases
        ]
        return ExecutionReport(
            passed_count=len(results), total_count=len(results),
            pass_rate=1.0, results=results, summary="all pass",
        )

    monkeypatch.setattr("backend.skills.programming.run_sandbox", fake_sandbox)

    fake_output = MagicMock(
        score=10.0, max_score=10.0, confidence=1.0,
        comment="Great.", steps=[], logs="",
    )
    fake_raw = MagicMock(content="{}", duration_ms=50)

    async def fake_call(*a, **kw):
        return fake_output, fake_raw

    monkeypatch.setattr("backend.skills.programming.structured_llm_call", fake_call)

    skill = ProgrammingSkill(provider=_fake_provider())
    await skill.grade(problem, answer, student_id="s1")
    assert len(captured_cases) == MAX_LLM_GENERATED_CASES


# ─── grade() — empty student code ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_prog_no_code():
    problem = _make_problem(test_cases=None)
    answer = _make_answer("")

    skill = ProgrammingSkill(provider=_fake_provider())
    result = await skill.grade(problem, answer, student_id="s1")
    assert result.score == 0.0
    assert "未提交" in result.comment or "No code" in result.comment
