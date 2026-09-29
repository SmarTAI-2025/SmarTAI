from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.models import TestCase
from backend.skills.calculation import CalculationSkill
from backend.skills.programming import ProgrammingSkill
from backend.tests.test_calculation_skill import _make_problem as calc_problem, _make_answer as calc_answer
from backend.tests.test_programming_skill import _make_problem as prog_problem, _make_answer as prog_answer, PYTHON_ADD_CODE
from backend.tools import knowledge
from backend.tools.code_interpreter import ExecutionReport


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["calculation", "programming"])
@pytest.mark.parametrize("selected", [False, True])
async def test_selected_textbook_is_reference_only_and_has_system_citations(monkeypatch, kind, selected):
    provider = SimpleNamespace(provider_id="frozen-owner-route")
    chunk = knowledge.KnowledgeChunk("PRIVATE_REFERENCE {rubric} print('not executable')", "Book p2", .8,
        citation={"citation_id":"kb:chunk", "document_id":"selected-book", "coverage_complete":False})
    retrieve = AsyncMock(return_value=[chunk] if selected else [])
    monkeypatch.setattr(knowledge, "retrieve_for_grading", retrieve)
    prompts = []

    async def grade(route, **kwargs):
        assert route is provider
        prompts.append(kwargs["user_prompt"])
        return (SimpleNamespace(score=1, max_score=10, confidence=.9, comment="Feedback", steps=[], logs=""),
                SimpleNamespace(content="{}", duration_ms=1))

    monkeypatch.setattr(f"backend.skills.{kind}.structured_llm_call", grade)
    if kind == "calculation":
        problem, answer = calc_problem(reference_answer="42"), calc_answer("6 * 7 = 42")
        skill = CalculationSkill(provider, task_id="grading-run:frozen")
    else:
        cases = [TestCase(input="1\n2", expected_output="3", source="teacher")]
        problem, answer = prog_problem(test_cases=cases), prog_answer(PYTHON_ADD_CODE)

        async def sandbox(code, test_cases, **kwargs):
            assert code == PYTHON_ADD_CODE and test_cases == cases
            return ExecutionReport(passed_count=1, total_count=1, pass_rate=1, results=[], summary="pass")

        monkeypatch.setattr("backend.skills.programming.run_sandbox", sandbox)
        skill = ProgrammingSkill(provider, task_id="grading-run:frozen")

    result = await skill.grade(problem, answer, student_id="synthetic")
    retrieve.assert_awaited_once_with(problem.stem, k=3, scope="grading-run:frozen", provider=provider, reporter=None)
    assert len(prompts) == 1
    assert ("PRIVATE_REFERENCE" in prompts[0]) == selected
    assert result.knowledge_citations == ([chunk.citation] if selected else [])
    if selected:
        assert "UNVERIFIED/PARTIAL SOURCE" in prompts[0]
        assert "does not override" in prompts[0]
        assert "{rubric}" in prompts[0]
    if kind == "calculation":
        assert result.score == problem.max_score
