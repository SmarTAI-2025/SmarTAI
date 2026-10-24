"""Request-size and durable partial-result contracts for page preparation."""
import asyncio
from types import SimpleNamespace

import pytest

from backend.agents import incremental_question_preparation as inc
from backend.domain.errors import RecognitionError, ValidationError
from backend.progress.tracker import ProgressReporter
from backend.services.question_source_batches import read_question_pdf_batches
from backend.tests.test_incremental_question_preparation import setup, row, generated, persisted
from backend.tests.test_question_source_batches import fake_result
from backend.tests.test_recognition_agent import request


async def page_pipeline(monkeypatch, pages):
    owner, task, operation, pipeline = await setup(monkeypatch)
    async def read(source, *, on_pages):
        for number, text in pages.items():
            await on_pages([dict(page_number=number, text=text)])
        return source[0], "\n\n".join(pages.values()), source[2]
    pipeline.read_source = read
    return owner, task, operation, pipeline


@pytest.mark.asyncio
@pytest.mark.parametrize("continuation", [False, True])
async def test_two_pages_four_questions_request_count_and_all_fields_saved(monkeypatch, continuation):
    pages = {1: "1. First question.\n2. Second question.",
             2: ("A continuation of question two.\n" if continuation else "") +
                "3. Third question.\n4. Fourth question."}
    owner, _, operation, pipeline = await page_pipeline(monkeypatch, pages)
    extracted, generated_ids = [], []
    async def extract(text, *args, **kwargs):
        extracted.append(text)
        numbers = [number for number in range(1, 5) if f"{number}. " in text]
        return inc._rows([row(number) for number in numbers])
    async def generate(data, targets, *args, **kwargs):
        generated_ids.append(data["q1"]["number"])
        return [generated(target["target"], "Complete answer." if target["target"] == "reference_answer"
                          else "Full credit for a correct explanation.") for target in targets]
    monkeypatch.setattr(inc, "_extract_problems_call", extract)
    monkeypatch.setattr(inc, "generate_missing_question_materials", generate)
    result = await pipeline.run()
    assert len(extracted) == (1 if continuation else 2)
    assert sorted(generated_ids) == ["1", "2", "3", "4"]
    assert len(result) == 4
    _, saved = persisted(owner, operation)
    assert all(item["stem"] and item["reference_answer"] and item["criterion"] for item in saved.values())
    progress = await pipeline.reporter.snapshot()
    assert progress.stage_metrics["solution_total_questions"] == 4
    assert progress.stage_metrics["solution_completed_questions"] == 4
    assert not progress.active_question_ids


@pytest.mark.asyncio
async def test_natural_language_score_allocation_is_one_request_for_all_questions(monkeypatch):
    _, _, _, pipeline = await page_pipeline(monkeypatch, {1: "1. First.\n2. Second.", 2: "3. Third.\n4. Fourth."})
    pipeline.policy = pipeline.policy.model_copy(update={"mode": "per_question", "per_question_text": "Every exercise has ten points."})
    calls = []
    async def extract(text, *args, **kwargs):
        return inc._rows([row(number, criterion="Teacher rubric.", reference_answer="Teacher answer.")
            for number in range(1, 5) if f"{number}. " in text])
    async def resolve(data, *args, **kwargs):
        calls.append(list(data))
        return {key: SimpleNamespace(max_score=10, source="per_question_text", review_status="needs_review") for key in data}
    monkeypatch.setattr(inc, "_extract_problems_call", extract)
    monkeypatch.setattr(inc, "resolve_question_score_policy", resolve)
    assert len(await pipeline.run()) == 4
    assert calls == [["q1", "q2", "q3", "q4"]]
    assert len([key for key in pipeline.refs if key.startswith("score_plan:")]) == 1
    pipeline.units.clear()
    await pipeline.restore()
    restored = pipeline.project()
    assert len(restored) == 4
    assert all(item["max_score"] == 10 and item["max_score_source"] == "per_question_text"
               for item in restored.values())
    assert calls == [["q1", "q2", "q3", "q4"]]


@pytest.mark.asyncio
async def test_score_plan_waits_for_complete_source_and_rejects_a_different_question_set(monkeypatch):
    _, _, operation, pipeline = await setup(monkeypatch)
    pipeline.policy = pipeline.policy.model_copy(update={"mode": "per_question",
        "per_question_text": "The last question is worth twenty points; all earlier questions are worth ten."})
    retry, calls = False, []
    async def read(source, *, on_pages):
        pages = {number: f"Question {number}: Explain item {number}." for number in range(1, 4 if retry else 3)}
        for number, text in pages.items():
            await on_pages([dict(page_number=number, text=text)])
        payload = {**source[2], "source_error": None if retry else "ocr_incomplete", "recognition": {
            "coverage": {"total_pages": 3, "requested_pages": [1, 2, 3], "failed_pages": [] if retry else [3]}}}
        return source[0], "\n\n".join(pages.values()), payload
    async def extract(text, *args, **kwargs):
        return inc._rows([row(number, criterion="Teacher rubric.", reference_answer="Teacher answer.")
            for number in range(1, 4) if f"Question {number}:" in text])
    async def resolve(data, *args, **kwargs):
        calls.append([item["number"] for item in data.values()])
        return {key: SimpleNamespace(max_score=20 if key == list(data)[-1] else 10,
            source="per_question_text", review_status="needs_review") for key in data}
    pipeline.read_source = read
    monkeypatch.setattr(inc, "_extract_problems_call", extract)
    monkeypatch.setattr(inc, "resolve_question_score_policy", resolve)
    with pytest.raises((RecognitionError, ValidationError)):
        await pipeline.run()
    assert calls == []
    # A retained plan for a different source partition must not leak into the
    # complete set after recovery, even if the first question is unchanged.
    await pipeline.finish(pipeline._score_plan_key([row(1)]), inc._rows([dict(number="1", max_score=20,
        max_score_source="per_question_text", max_score_review_status="needs_review")]))
    retry = True
    resumed = inc.IncrementalQuestionPreparation(operation=operation, sources=pipeline.sources,
        provider=pipeline.provider, provider_id=pipeline.provider_id, reporter=ProgressReporter(operation.operation_id),
        score_policy=pipeline.policy, read_source=read)
    result = await resumed.run()
    assert [(item["number"], item["max_score"]) for item in result.values()] == [("1", 10), ("2", 10), ("3", 20)]
    assert calls == [["1", "2", "3"]]


@pytest.mark.asyncio
async def test_score_plan_stays_small_when_the_question_view_exceeds_artifact_limit(monkeypatch):
    from backend.services import question_preparation_artifacts as artifacts
    _, _, _, pipeline = await setup(monkeypatch)
    pipeline.policy = pipeline.policy.model_copy(update={"mode": "per_question", "per_question_text": "Every exercise has five points."})
    monkeypatch.setattr(artifacts, "MAX_QUESTION_PREPARATION_ARTIFACT_BYTES", 16_000)
    monkeypatch.setattr(inc, "_chunk_problem_text", lambda *_: ["1", "2"])
    async def extract(text, *args, **kwargs):
        return inc._rows([row(text, stem="A" * 4_500, criterion="Teacher rubric.", reference_answer="Teacher answer.")])
    calls = []
    async def resolve(data, *args, **kwargs):
        calls.append(list(data))
        return {key: SimpleNamespace(max_score=5, source="per_question_text", review_status="needs_review") for key in data}
    monkeypatch.setattr(inc, "_extract_problems_call", extract)
    monkeypatch.setattr(inc, "resolve_question_score_policy", resolve)
    await pipeline.run()
    pipeline.units.clear()
    await pipeline.restore()
    assert all(item["max_score"] == 5 and item["max_score_source"] == "per_question_text"
               for item in pipeline.project().values())
    assert calls == [["q1", "q2"]]


@pytest.mark.asyncio
async def test_large_page_splits_between_complete_questions_without_marking_them_partial(monkeypatch):
    text = "Question 1: " + "A" * 12_100 + "\nQuestion 2: " + "B" * 12_100
    _, _, _, pipeline = await page_pipeline(monkeypatch, {1: text})
    calls = []
    async def extract(chunk, *args, **kwargs):
        calls.append(chunk)
        return inc._rows([row(number, stem=("A" if number == 1 else "B") * 12_100,
            criterion="Teacher rubric.", reference_answer="Teacher answer.")
            for number in (1, 2) if f"Question {number}:" in chunk])
    monkeypatch.setattr(inc, "_extract_problems_call", extract)
    result = await pipeline.run()
    assert len(calls) == len(result) == 2
    assert all(not any(issue["code"] == "recognition_partial" for issue in item["preparation_issues"])
               for item in result.values())


@pytest.mark.asyncio
async def test_page_with_no_questions_finishes_without_retry_or_generation(monkeypatch):
    _, _, _, pipeline = await page_pipeline(monkeypatch, {
        1: "This is explanatory material, with no exercise.", 2: "1. A real exercise."})
    calls = []
    async def extract(text, *args, **kwargs):
        calls.append(text)
        return inc._rows([row(1, criterion="Teacher rubric.", reference_answer="Teacher answer.")]) if "1. " in text else {}
    monkeypatch.setattr(inc, "_extract_problems_call", extract)
    async def no_generation(*args, **kwargs):
        raise AssertionError("An empty page or supplied material must not trigger generation")
    monkeypatch.setattr(inc, "generate_missing_question_materials", no_generation)
    assert len(await pipeline.run()) == 1
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_completed_page_is_saved_while_next_page_response_is_pending(monkeypatch):
    owner, _, operation, pipeline = await page_pipeline(monkeypatch, {
        1: "1. First question.\n2. Second question.", 2: "3. Third question.\n4. Fourth question."})
    blocked, saved = asyncio.Event(), asyncio.Event()
    async def extract(text, *args, **kwargs):
        if "3. " in text:
            await blocked.wait()
        return inc._rows([row(number) for number in ([3, 4] if "3. " in text else [1, 2])])
    async def generate(*args, **kwargs):
        saved.set()
        await blocked.wait()
        return []
    monkeypatch.setattr(inc, "_extract_problems_call", extract)
    monkeypatch.setattr(inc, "generate_missing_question_materials", generate)
    runner = asyncio.create_task(pipeline.run())
    await asyncio.wait_for(saved.wait(), 5)
    _, rows = persisted(owner, operation)
    assert [item["number"] for item in rows.values()] == ["1", "2"]
    runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runner
    _, rows = persisted(owner, operation)
    assert [item["number"] for item in rows.values()] == ["1", "2"]


@pytest.mark.asyncio
async def test_selected_question_missing_still_fails_after_page_batching(monkeypatch):
    _, _, _, pipeline = await page_pipeline(monkeypatch, {1: "1. First.\n2. Second."})
    pipeline.wanted = ["2"]
    async def extract(*args, **kwargs):
        return inc._rows([row(1)])
    monkeypatch.setattr(inc, "_extract_problems_call", extract)
    with pytest.raises(ValidationError, match="incomplete"):
        await pipeline.run()


@pytest.mark.asyncio
async def test_out_of_order_pages_wait_for_shared_source_context(monkeypatch):
    _, _, _, pipeline = await setup(monkeypatch)
    calls = []
    async def extract(text, *args, source_context, **kwargs):
        calls.append((text, source_context))
        return inc._rows([row(number, criterion="Rubric.", reference_answer="Answer.")
            for number in (1, 2, 3) if f"Question {number}" in text])
    monkeypatch.setattr(inc, "_extract_problems_call", extract)
    async def read(source, *, on_pages):
        for number in (2, 3):
            await on_pages([dict(page_number=number, text=f"Question {number}\nCompute x + {number}.")])
        await asyncio.sleep(0)
        assert not calls
        await on_pages([dict(page_number=1, text="For all questions, let x = 5.\nQuestion 1\nCompute x + 1.")])
        return source
    pipeline.read_source = read
    assert len(await pipeline.run()) == 3
    assert len(calls) == 3
    assert all("x = 5" in context for _, context in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_patches", [0, 4])
async def test_parallel_page_reads_share_original_window_recheck_allowance(saved_patches):
    active = peak = 0
    patches, reads = [], []
    async def saved_page_extras(*args, **kwargs):
        return {"patch": saved_patches, "empty_recovery": 0}
    async def run(scoped, *_args, repair_admission, **_kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        assert scoped.policy.max_patches == 4  # Cache identity must not depend on completion order.
        reads.extend(scoped.pages)
        await asyncio.sleep(0)
        if await repair_admission("patch"):
            patches.extend(scoped.pages)
        active -= 1
        return fake_result(scoped.pages)
    async def on_pages(pages):
        pass
    _, summary = await read_question_pdf_batches(service=SimpleNamespace(run=run, saved_page_extras=saved_page_extras),
        request=request(b"pdf"), content=b"pdf", engine=SimpleNamespace(), total_pages=12,
        reporter=ProgressReporter("window-budget"), on_pages=on_pages)
    assert peak == 12 and sorted(reads) == list(range(1, 13))
    assert len(patches) == 4 - saved_patches
    assert summary["coverage"]["processed_pages"] == list(range(1, 13))


@pytest.mark.asyncio
async def test_stop_during_retry_keeps_saved_scores_in_manual_review(monkeypatch):
    from unittest.mock import AsyncMock
    from backend.api import task_preparation as api
    from backend.db import workflow_repository
    from backend.models import QuestionScorePolicy
    from backend.services import task_facade
    from backend.services.task_execution import stop_task_run
    from backend.tests.test_question_preparation_recovery import _claim, _RecoveryRegistry

    start = api._start_question_preparation

    async def per_question_start(**kwargs):
        kwargs["request"] = kwargs["request"].model_copy(update={"score_policy": QuestionScorePolicy(
            mode="per_question", per_question_text="Every exercise has five points.")})
        return await start(**kwargs)

    monkeypatch.setattr(api, "_start_question_preparation", per_question_start)
    owner, task, operation, pipeline = await page_pipeline(monkeypatch, {1: "Question 1: Complete problem."})
    extract = AsyncMock(return_value=inc._rows([row(1, criterion="Teacher rubric.", reference_answer="Teacher answer.")]))
    resolve = AsyncMock(side_effect=lambda data, *a, **kw: {
        key: SimpleNamespace(max_score=5, source="per_question_text", review_status="needs_review") for key in data})
    monkeypatch.setattr(inc, "_extract_problems_call", extract)
    monkeypatch.setattr(inc, "resolve_question_score_policy", resolve)
    assert (await pipeline.run())["q1"]["max_score"] == 5
    # A later failure must not undo an already durable whole-question score plan.
    task_facade._fail_operation(task, owner, operation.operation_id, operation.attempt,
        "provider_response_invalid", expected_lease_token=operation.lease_token)
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    retried = await api.retry_question_preparation(task_id=task, job_id=operation.operation_id,
        request=api.RetryQuestionPreparationRequest(expected_workflow_revision=workflow.workflow_revision),
        background_tasks=None, current=SimpleNamespace(id=owner), registry=_RecoveryRegistry())
    current = _claim(owner, retried["job_id"], "score-stop-retry")
    inputs = api._rehydrate_question_preparation_inputs(current)
    reading = asyncio.Event()

    async def delayed(source, **kwargs):
        reading.set()
        await asyncio.Event().wait()

    resumed = inc.IncrementalQuestionPreparation(operation=current, sources=inputs["sources"],
        provider=inputs["route"].provider, provider_id=inputs["recognition_provider_id"],
        reporter=ProgressReporter(current.operation_id), score_policy=inputs["score_policy"], read_source=delayed)
    runner = asyncio.create_task(resumed.run())
    await asyncio.wait_for(reading.wait(), 3)
    runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runner
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    stop_task_run(task_id=task, owner_id=owner, job_id=current.operation_id, expected_revision=workflow.workflow_revision)
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    response = await api.manually_complete_question_preparation(task, current.operation_id,
        api.ManualQuestionPreparationRequest(expected_workflow_revision=workflow.workflow_revision),
        current=SimpleNamespace(id=owner))
    assert response["status"] == "ready_for_review"
    actual = task_facade.get_task(task_id=task, owner_id=owner)["problem_data"]["q1"]
    assert actual["max_score"] == 5 and actual["max_score_source"] == "per_question_text"
    extract.assert_awaited_once()
    resolve.assert_awaited_once()
