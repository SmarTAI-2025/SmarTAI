"""Real PDF source/artifact round trips; only reading and model replies are stubbed."""
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fitz
import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers

from backend.agents import incremental_question_preparation as inc
from backend.api import task_preparation as api
from backend.db import workflow_repository
from backend.models import QuestionScorePolicy, TestCase
from backend.progress.tracker import ProgressReporter
from backend.services import task_facade
from backend.services.task_execution import stop_task_run
from backend.tests.test_incremental_question_preparation import persisted, row
from backend.tests.test_question_preparation_recovery import _RecoveryRegistry, _claim
from backend.tests.test_task_background_workflows import _seed_task


async def pdf_pipeline(monkeypatch, role, pages, order):
    owner, task = _seed_task()
    registry = _RecoveryRegistry()
    tokens = []
    document = fitz.open()
    for _ in pages:
        document.new_page()
    data = document.tobytes()
    document.close()
    for source_role in (["problem"] if role == "problem" else ["problem", role]):
        is_pdf = source_role == role
        source = await api.preflight_problem_source(task_id=task,
            file=UploadFile(io.BytesIO(data), filename=role + ".pdf",
                headers=Headers({"content-type": "application/pdf"})) if is_pdf else None,
            inline_text=None if is_pdf else "Question 1: Explain the result.",
            library_material_id=None, stored_file_id=None, structure_mode="organized", role=source_role,
            extraction_hint="", save_to_library=False, recognition_provider_id="test-provider", defer_recognition=True,
            current=SimpleNamespace(id=owner), registry=registry)
        assert isinstance(source, dict), getattr(source, "body", source)
        tokens.append(source["source_token"])
    queued = await api._start_question_preparation(task_id=task,
        request=api.StartQuestionPreparationRequest(source_tokens=tokens, expected_workflow_revision=0,
            score_policy=QuestionScorePolicy(mode="uniform", uniform_max_score=10),
            recognition_provider_id="test-provider"), current=SimpleNamespace(id=owner),
        registry=registry, background_tasks=None, allow_prepared_source_reuse=False)
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: registry)
    operation = _claim(owner, queued["job_id"], "cross-page")
    inputs = api._rehydrate_question_preparation_inputs(operation)

    async def read(source, *, on_pages):
        if source[0].role != role:
            return source
        for number in order:
            await on_pages([dict(page_number=number, text=pages[number])])
        return source[0], "\n\n".join(pages.values()), {**source[2], "recognition": {
            "coverage": {"total_pages": len(pages), "requested_pages": list(pages)}}}

    pipeline = inc.IncrementalQuestionPreparation(operation=operation, sources=inputs["sources"],
        provider=inputs["route"].provider, provider_id=inputs["recognition_provider_id"],
        reporter=ProgressReporter(operation.operation_id), score_policy=inputs["score_policy"], read_source=read)
    return owner, task, operation, pipeline


@pytest.mark.asyncio
@pytest.mark.parametrize("role,field,heading", [(role, field, heading)
    for role, field in [("problem", "stem"), ("rubric", "criterion"),
        ("reference_answer", "reference_answer"), ("programming_tests", "test_cases")]
    for heading in ["none", "repeated", "steps"] if role != "problem" or heading != "steps"])
@pytest.mark.parametrize("reverse", [False, True])
async def test_two_page_single_item_keeps_both_parts_in_final_and_manual_view(monkeypatch, role, field, heading, reverse):
    parts = (["Input: 3", "Expected output: 6"] if role == "programming_tests"
             else ["列出方程2分", "推导结论8分"])
    pages = {number: ("Question 1\n" if heading == "repeated" else f"{number}. " if heading == "steps" else "") + part
             for number, part in enumerate(parts, 1)}
    owner, task, operation, pipeline = await pdf_pipeline(monkeypatch, role, pages, (2, 1) if reverse else (1, 2))
    calls = []

    async def extract(text, *args, **kwargs):
        if role == "problem":
            calls.append(text)
        return inc._rows([row(1, stem=text, criterion="" if role == "rubric" else "Teacher criterion.",
            reference_answer="" if role == "reference_answer" else "Teacher answer.")])

    async def material(*args, **kwargs):
        text = kwargs["user_prompt"].split("[Teacher material]\n", 1)[1].split("\n[End material]", 1)[0]
        calls.append(text)
        cases = [TestCase(input="3", expected_output="6")] if all(part in text for part in parts) else []
        return inc._MaterialItems(items=[inc._MaterialItem(number="1", text_value=text, test_cases=cases)]), None

    monkeypatch.setattr(inc, "_extract_problems_call", extract)
    monkeypatch.setattr(inc, "structured_llm_call", material)
    forbidden = AsyncMock(side_effect=AssertionError("Supplied material must not be regenerated"))
    monkeypatch.setattr(inc, "generate_missing_question_materials", forbidden)
    result = await pipeline.run()
    _, saved = persisted(owner, operation)
    assert len(result) == len(saved) == 1
    if field == "test_cases":
        assert saved["q1"][field][0]["input"] == "3"
        assert saved["q1"][field][0]["expected_output"] == "6"
        assert saved["q1"][field][0]["source"] == "teacher"
    else:
        assert all(part in saved["q1"][field] for part in parts)
    assert len(calls) == 1 and all(part in calls[0] for part in parts)
    assert not any(issue["code"] in {"source_conflict", "recognition_partial"}
        for issue in saved["q1"]["preparation_issues"])
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    stop_task_run(task_id=task, owner_id=owner, job_id=operation.operation_id,
        expected_revision=workflow.workflow_revision)
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    monkeypatch.setattr(inc, "structured_llm_call", forbidden)
    monkeypatch.setattr(inc, "_extract_problems_call", forbidden)
    response = await api.manually_complete_question_preparation(task, operation.operation_id,
        api.ManualQuestionPreparationRequest(expected_workflow_revision=workflow.workflow_revision),
        current=SimpleNamespace(id=owner))
    assert response["status"] == "ready_for_review"
    published = task_facade.get_task(task_id=task, owner_id=owner)["problem_data"]
    assert published["q1"][field] == saved["q1"][field]
    forbidden.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("role,field", [("rubric", "criterion"), ("reference_answer", "reference_answer")])
async def test_same_source_literal_parts_are_combined_even_if_model_returns_separate_items(monkeypatch, role, field):
    parts = ["列出方程2分", "推导结论8分"]
    owner, _, operation, pipeline = await pdf_pipeline(monkeypatch, role, dict(enumerate(parts, 1)), (2, 1))
    monkeypatch.setattr(inc, "_extract_problems_call", AsyncMock(return_value=inc._rows([
        row(1, criterion="" if role == "rubric" else "Supplied rubric.",
            reference_answer="" if role == "reference_answer" else "Supplied answer.")])))
    # Reverse model item order and repeat the first item: source order and
    # literal span identity, rather than response order, govern the merge.
    monkeypatch.setattr(inc, "structured_llm_call", AsyncMock(return_value=(inc._MaterialItems(items=[
        inc._MaterialItem(number="1", text_value=part) for part in [parts[1], parts[0], parts[0]]]), None)))
    await pipeline.run()
    _, saved = persisted(owner, operation)
    assert saved["q1"][field] == "\n\n".join(parts)
    assert not any(issue["code"] == "source_conflict" for issue in saved["q1"]["preparation_issues"])


@pytest.mark.parametrize("evidence,expected", [
    ("Question 1\nInput: 3\nQuestion 1\nExpected output: 6", True),
    ("Question 1\nInput: 3\nQuestion 2\nExpected output: 6", False),
    ("1. Input: 3\n2. Expected output: 6", True),
    ("Input: 3\nOutput: 4\nInput: 5\nOutput: 6", False),
])
def test_test_case_continuation_keeps_complete_pair_and_question_boundary_guards(evidence, expected):
    from backend.agents.ingest_agent import _saved_test_case_supported
    assert _saved_test_case_supported({"input": "3", "expected_output": "6"}, "", evidence,
        question_number="1") is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("role,field", [("rubric", "criterion"), ("reference_answer", "reference_answer")])
async def test_missing_middle_page_retains_both_material_parts_without_claiming_completeness(monkeypatch, role, field):
    from backend.domain.errors import RecognitionError, ValidationError
    parts = ["Question 1\n列出方程2分", "Question 1\n推导结论8分"]
    owner, _, operation, pipeline = await pdf_pipeline(monkeypatch, role,
        {1: parts[0], 2: "Unread page", 3: parts[1]}, (1, 3))
    original_read = pipeline.read_source
    async def read(source, **kwargs):
        draft, text, payload = await original_read(source, **kwargs)
        if draft.role == role:
            return draft, "\n\n".join(parts), {**payload, "source_error": "recognition_response_invalid",
                "recognition": {"coverage": {"total_pages": 3, "requested_pages": [1, 2, 3],
                    "processed_pages": [1, 3], "failed_pages": [2]}}}
        return draft, text, payload
    pipeline.read_source = read
    monkeypatch.setattr(inc, "_extract_problems_call", AsyncMock(return_value=inc._rows([
        row(1, criterion="" if role == "rubric" else "Supplied rubric.",
            reference_answer="" if role == "reference_answer" else "Supplied answer.")])))
    async def material(*args, **kwargs):
        text = kwargs["user_prompt"].split("[Teacher material]\n", 1)[1].split("\n[End material]", 1)[0]
        return inc._MaterialItems(items=[inc._MaterialItem(number="1", text_value=text)]), None
    monkeypatch.setattr(inc, "structured_llm_call", material)
    with pytest.raises((RecognitionError, ValidationError)) as failure:
        await pipeline.run()
    assert failure.value.code == "recognition_response_invalid"
    _, saved = persisted(owner, operation)
    assert all(part in saved["q1"][field] for part in parts)
    assert any(issue["code"] == "recognition_partial" for issue in saved["q1"]["preparation_issues"])
    assert not any(issue["code"] == "source_conflict" for issue in saved["q1"]["preparation_issues"])


@pytest.mark.asyncio
async def test_repeated_literal_in_another_question_cannot_reorder_teacher_steps(monkeypatch):
    from backend.tests.test_incremental_question_materials import material_setup
    shared='Check the boundary: 2 points.'
    specific='Derive the general case: 8 points.'
    text='Question 1\n'+shared+'\nQuestion 2\n'+specific+'\n'+shared
    owner,_,operation,pipeline=await material_setup(monkeypatch,[('rubric',text)])
    monkeypatch.setattr(inc,'_extract_problems_call',AsyncMock(return_value=inc._rows([
        row(i,reference_answer='Supplied answer.') for i in (1,2)])))
    monkeypatch.setattr(inc,'structured_llm_call',AsyncMock(return_value=(inc._MaterialItems(items=[
        inc._MaterialItem(number='1',text_value=shared),
        inc._MaterialItem(number='2',text_value=specific),
        inc._MaterialItem(number='2',text_value=shared)]),None)))
    await pipeline.run()
    _,saved=persisted(owner,operation)
    assert saved['q2']['criterion']==specific+'\n\n'+shared
