"""Cross-page answer regressions assert final content, not just saved fragments."""
import asyncio
from types import SimpleNamespace

import pytest

from backend.db import assignment_repository, workflow_repository
from backend.services import task_facade
from backend.services.submission_manual_review import continue_manually
from backend.services.task_execution import stop_task_run
from backend.tests.test_recognition_agent import pdf
from backend.tests.test_submission_incremental import _registry, _response, _claim, _saved_signal, _questions
from backend.tests.test_task_background_workflows import _seed_task

FIRST = "List the equation."
SECOND = "Derive the conclusion."


def _numbered_task():
    owner, task = _seed_task()
    assignment_repository.add_question(task, teacher_id=owner, q_id="q1", order_index=0,
        number="1", type="short", stem="A question", criterion="Award 10 points", max_score=10)
    return owner, task


@pytest.mark.asyncio
@pytest.mark.parametrize("layout", ["unnumbered", "repeated_heading", "numbered_steps", "heading_then_steps"])
@pytest.mark.parametrize("reverse", [False, True])
async def test_contiguous_answer_pages_reach_the_final_answer(monkeypatch, layout, reverse):
    owner, task = _numbered_task()
    page_texts = {
        "unnumbered": [FIRST, SECOND],
        "repeated_heading": ["Question 1: " + FIRST, "Question 1: " + SECOND],
        "numbered_steps": ["1. " + FIRST, "2. " + SECOND],
        "heading_then_steps": ["Question 1: " + FIRST, "1. " + SECOND],
    }[layout]
    seen = []

    async def invoke(messages):
        text = messages[-1].content.split("**[Student Submission Content]**:", 1)[1]
        seen.append(text)
        return _response(content="\n".join(part for part in (FIRST, SECOND) if part in text))

    registry = _registry(monkeypatch, invoke)

    async def read(**kwargs):
        for number in ([2, 1] if reverse else [1, 2]):
            await kwargs["on_pages"]([dict(page_number=number, text=page_texts[number - 1])])
        return SimpleNamespace(text="\n\n".join(page_texts), recognition=None)

    monkeypatch.setattr(task_facade, "read_question_source", read)
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename="S1.pdf", content=pdf(page_texts), content_type="application/pdf", registry=registry)
    await task_facade.run_durable_submission_recognition(_claim(owner, queued["job_id"]))
    after = task_facade.get_task(task_id=task, owner_id=owner)
    calls = registry.provider.ainvoke.await_count
    if after["status"] == "error":
        await continue_manually(task_id=task, owner_id=owner, job_id=queued["job_id"],
            expected_revision=after["workflow_revision"])
        after = task_facade.get_task(task_id=task, owner_id=owner)
    answer = after["student_data"]["S1"]["stu_ans"][0]
    assert answer["content"] == FIRST + "\n" + SECOND
    assert len(seen) == 1
    assert not any(flag in answer["flag"] for flag in ("recognition_failed", "recognition_manual_pending"))
    assert registry.provider.ainvoke.await_count == calls


@pytest.mark.asyncio
async def test_missing_middle_page_keeps_both_available_answer_parts_in_manual_review(monkeypatch):
    owner, task = _numbered_task()
    pages = {1: "Question 1: " + FIRST, 3: "Question 1: " + SECOND}

    async def invoke(messages):
        text = messages[-1].content.split("**[Student Submission Content]**:", 1)[1]
        return _response(content="\n".join(part for part in (FIRST, SECOND) if part in text))

    registry = _registry(monkeypatch, invoke)

    async def read(**kwargs):
        for number, text in pages.items():
            await kwargs["on_pages"]([dict(page_number=number, text=text)])
        return SimpleNamespace(text="\n\n".join(pages.values()), recognition={
            "error_code": "recognition_response_invalid", "coverage": {
                "requested_pages": [1, 2, 3], "processed_pages": [1, 3], "failed_pages": [2]}})

    monkeypatch.setattr(task_facade, "read_question_source", read)
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename="S1.pdf", content=pdf([pages[1], "Unread page", pages[3]]),
        content_type="application/pdf", registry=registry)
    await task_facade.run_durable_submission_recognition(_claim(owner, queued["job_id"]))
    before = task_facade.get_task(task_id=task, owner_id=owner)
    calls = registry.provider.ainvoke.await_count
    await continue_manually(task_id=task, owner_id=owner, job_id=queued["job_id"],
        expected_revision=before["workflow_revision"])
    answer = task_facade.get_task(task_id=task, owner_id=owner)["student_data"]["S1"]["stu_ans"][0]
    assert FIRST in answer["content"] and SECOND in answer["content"]
    assert "recognition_manual_pending" in answer["flag"]
    assert registry.provider.ainvoke.await_count == calls


@pytest.mark.asyncio
async def test_pause_after_cross_page_answer_preserves_all_parts_without_manual_ai(monkeypatch):
    owner, task = _numbered_task()
    assignment_repository.add_question(task, teacher_id=owner, q_id="q2", order_index=1,
        number="2", type="short", stem="Another question", criterion="Award 10 points", max_score=10)
    saved = _saved_signal(monkeypatch)
    pages = {1: "Question 1: " + FIRST, 2: "Question 1: " + SECOND, 3: "Question 2: Not yet parsed."}

    async def invoke(messages):
        ids = [item["q_id"] for item in _questions(messages)]
        assert ids == ["q1"]
        text = messages[-1].content.split("**[Student Submission Content]**:", 1)[1]
        return _response(content="\n".join(part for part in (FIRST, SECOND) if part in text))

    registry = _registry(monkeypatch, invoke)

    async def read(**kwargs):
        for number, text in pages.items():
            await kwargs["on_pages"]([dict(page_number=number, text=text)])
        await asyncio.wait_for(saved.wait(), 3)
        raise asyncio.CancelledError()

    monkeypatch.setattr(task_facade, "read_question_source", read)
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename="S1.pdf", content=pdf(list(pages.values())), content_type="application/pdf", registry=registry)
    with pytest.raises(asyncio.CancelledError):
        await task_facade.run_durable_submission_recognition(_claim(owner, queued["job_id"]))
    stop_task_run(task_id=task, owner_id=owner, job_id=queued["job_id"],
        expected_revision=workflow_repository.get_workflow(task, owner_id=owner).workflow_revision)
    before = task_facade.get_task(task_id=task, owner_id=owner)
    calls = registry.provider.ainvoke.await_count
    await continue_manually(task_id=task, owner_id=owner, job_id=queued["job_id"],
        expected_revision=before["workflow_revision"])
    answers = {answer["q_id"]: answer for answer in task_facade.get_task(task_id=task, owner_id=owner)
        ["student_data"]["S1"]["stu_ans"]}
    assert answers["q1"]["content"] == FIRST + "\n" + SECOND
    assert answers["q2"]["content"] == ""
    assert registry.provider.ainvoke.await_count == calls
