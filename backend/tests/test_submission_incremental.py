import asyncio
import io
import json
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.api import tasks
from backend.db import assignment_repository, file_repository, workflow_repository
from backend.domain.errors import InvalidTransition, ValidationError
from backend.services import task_facade
from backend.services.submission_manual_review import continue_manually
from backend.services.task_execution import stop_task_run
from backend.services.workflow_worker import LeasedOperation
from backend.tests.test_task_background_workflows import _BackgroundTasks, _Registry, _seed_task


def _registry(monkeypatch, invoke):
    registry = _Registry()
    registry.provider = SimpleNamespace(provider_id="test-provider", supports_vision=False,
                                        ainvoke=AsyncMock(side_effect=invoke))
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: registry)
    return registry


def _questions(messages):
    return json.loads(messages[-1].content.split("**[Question Data (JSON)]**:\n", 1)[1]
                      .split("\n\n**[Student Submission Content]**:", 1)[0])


def _response(q_id="q1", content="Saved answer"):
    return SimpleNamespace(content=json.dumps(dict(stu_id="S1", stu_name="Student One",
        stu_ans=[dict(q_id=q_id, content=content, number=q_id[1:], type="short", flag=[])])))


def _claim(owner, job):
    row = workflow_repository.claim_operation(job, owner_id=owner, worker_id="test", lease_seconds=60)
    return LeasedOperation(row, worker_id="test", lease_seconds=60)


def _saved_signal(monkeypatch):
    saved, loop = asyncio.Event(), asyncio.get_running_loop()
    original = workflow_repository.save_operation_checkpoint
    def save(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs.get("stage") == "parser_saved":
            loop.call_soon_threadsafe(saved.set)
        return result
    monkeypatch.setattr(workflow_repository, "save_operation_checkpoint", save)
    return saved


@pytest.mark.asyncio
@pytest.mark.parametrize("pause", [False, True])
async def test_two_page_submission_batches_questions_and_manual_never_calls_ai(monkeypatch, pause):
    from backend.tests.test_recognition_agent import pdf
    owner, task = _seed_task()
    for number in range(1, 5):
        assignment_repository.add_question(task, teacher_id=owner, q_id=f"q{number}", order_index=number - 1,
            number=str(number), type="short", stem=f"Question {number}", criterion="Award 10 points", max_score=10)
    saved, calls = _saved_signal(monkeypatch), []
    async def invoke(messages):
        ids = [item["q_id"] for item in _questions(messages)]
        calls.append(ids)
        if pause and ids == ["q3", "q4"]:
            await asyncio.wait_for(saved.wait(), 3)
            raise asyncio.CancelledError()
        return SimpleNamespace(content=json.dumps(dict(stu_id="S1", stu_name="Student One",
            stu_ans=[dict(q_id=q_id, number=q_id[1:], type="short", content="Saved " + q_id, flag=[]) for q_id in ids])))
    registry = _registry(monkeypatch, invoke)
    pages = [dict(page_number=1, text="Question 1: First answer\nQuestion 2: Second answer"),
             dict(page_number=2, text="Question 3: Third answer\nQuestion 4: Fourth answer")]
    async def read(**kwargs):
        await kwargs["on_pages"](pages)
        return SimpleNamespace(text="\n\n".join(page["text"] for page in pages), recognition=None)
    monkeypatch.setattr(task_facade, "read_question_source", read)
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner, filename="S1.pdf",
        content=pdf([page["text"] for page in pages]), content_type="application/pdf", registry=registry)
    operation = _claim(owner, queued["job_id"])
    if pause:
        with pytest.raises(asyncio.CancelledError):
            await task_facade.run_durable_submission_recognition(operation)
        stop_task_run(task_id=task, owner_id=owner, job_id=operation.operation_id,
            expected_revision=workflow_repository.get_workflow(task, owner_id=owner).workflow_revision)
        before = task_facade.get_task(task_id=task, owner_id=owner)
        await continue_manually(task_id=task, owner_id=owner, job_id=operation.operation_id,
            expected_revision=before["workflow_revision"])
    else:
        await task_facade.run_durable_submission_recognition(operation)
    after = task_facade.get_task(task_id=task, owner_id=owner)
    assert sorted(calls) == [["q1", "q2"], ["q3", "q4"]]
    answers = {answer["q_id"]: answer for answer in after["student_data"]["S1"]["stu_ans"]}
    assert all(answers[key]["content"] == "Saved " + key for key in ("q1", "q2"))
    assert all(bool(answers[key]["content"]) is not pause for key in ("q3", "q4"))


@pytest.mark.asyncio
async def test_first_file_structure_is_saved_while_second_file_is_still_reading(monkeypatch):
    owner, task = _seed_task(with_question=True)
    saved = _saved_signal(monkeypatch)
    registry = _registry(monkeypatch, lambda _: _response())
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("S1.txt", "first answer")
        archive.writestr("S2.txt", "second answer")
    async def read(**kwargs):
        if kwargs["filename"] == "S2.txt":
            await asyncio.wait_for(saved.wait(), timeout=3)
            raise ValidationError("Unread second source", code="submission_source_empty")
        return SimpleNamespace(text=kwargs["content"].decode(), recognition=None)
    monkeypatch.setattr(task_facade, "read_question_source", read)
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename="answers.zip", content=buffer.getvalue(), content_type="application/zip", registry=registry)
    await task_facade.run_durable_submission_recognition(_claim(owner, queued["job_id"]))
    before = task_facade.get_task(task_id=task, owner_id=owner)
    assert before["status"] == "error" and saved.is_set()
    assert registry.provider.ainvoke.await_count == 1
    await continue_manually(task_id=task, owner_id=owner, job_id=queued["job_id"],
                            expected_revision=before["workflow_revision"])
    after = task_facade.get_task(task_id=task, owner_id=owner)
    answers = [student["stu_ans"][0] for student in after["student_data"].values()]
    assert sorted(answer["content"] for answer in answers) == ["", "Saved answer"]
    assert "recognition_manual_pending" in next(answer for answer in answers if not answer["content"])["flag"]
    assert registry.provider.ainvoke.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("manual", [False, True])
async def test_one_question_is_durable_before_next_question_finishes(monkeypatch, manual):
    owner, task = _seed_task()
    assignment_repository.add_question(task, teacher_id=owner, q_id="q1", order_index=0,
        number="1", type="short", stem="First", criterion="Award 10 points", max_score=10)
    assignment_repository.add_question(task, teacher_id=owner, q_id="q2", order_index=1,
        number="2", type="short", stem="Second", criterion="Award 10 points", max_score=10)
    saved = _saved_signal(monkeypatch)
    calls, resumed = [], False
    async def invoke(messages):
        q_ids = [question["q_id"] for question in _questions(messages)]
        calls.append(q_ids)
        assert len(q_ids) == 1
        if q_ids == ["q2"] and not resumed:
            await asyncio.wait_for(saved.wait(), timeout=3)
            raise asyncio.CancelledError()
        return _response(q_ids[0], "Saved " + q_ids[0])
    registry = _registry(monkeypatch, invoke)
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename="S1.txt", content=b"Question 1: First answer\nQuestion 2: Second answer",
        content_type="text/plain", registry=registry)
    with pytest.raises(asyncio.CancelledError):
        await task_facade.run_durable_submission_recognition(_claim(owner, queued["job_id"]))
    stop_task_run(task_id=task, owner_id=owner, job_id=queued["job_id"],
        expected_revision=workflow_repository.get_workflow(task, owner_id=owner).workflow_revision)
    before = task_facade.get_task(task_id=task, owner_id=owner)
    if manual:
        await continue_manually(task_id=task, owner_id=owner, job_id=queued["job_id"],
                                expected_revision=before["workflow_revision"])
    else:
        resumed = True
        denied = await tasks.retry_submission_recognition_endpoint(task_id=task, job_id=queued["job_id"],
            request=tasks.RetrySubmissionRecognitionRequest(expected_workflow_revision=before["workflow_revision"]),
            background_tasks=_BackgroundTasks(), current=SimpleNamespace(id=owner), registry=registry)
        assert denied.status_code == 409
        assert json.loads(denied.body)["error"]["code"] == "provider_submit_uncertain"
        assert len(calls) == 2
        retry = await tasks.retry_submission_recognition_endpoint(task_id=task, job_id=queued["job_id"],
            request=tasks.RetrySubmissionRecognitionRequest(expected_workflow_revision=before["workflow_revision"],
                acknowledge_possible_duplicate_call=True), background_tasks=_BackgroundTasks(),
            current=SimpleNamespace(id=owner), registry=registry)
        await task_facade.run_durable_submission_recognition(_claim(owner, retry["job_id"]))
    after = task_facade.get_task(task_id=task, owner_id=owner)
    assert after["status"] == "submissions_ready"
    answers = {answer["q_id"]: answer for answer in after["student_data"]["S1"]["stu_ans"]}
    assert answers["q1"]["content"] == "Saved q1" and answers["q1"]["flag"] == []
    assert calls.count(["q1"]) == 1
    if manual:
        assert calls.count(["q2"]) == 1
        assert answers["q2"]["content"] == "" and "recognition_manual_pending" in answers["q2"]["flag"]
    else:
        assert calls.count(["q2"]) == 2
        assert answers["q2"]["content"] == "Saved q2" and answers["q2"]["flag"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_kind", ["saved_text", "unversioned"])
async def test_retired_saved_text_job_cannot_run_or_retry_and_direct_creation_is_rejected(monkeypatch, legacy_kind):
    owner, task = _seed_task(with_question=True)
    registry = _Registry()
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename="S1.txt", content=b"original", content_type="text/plain", registry=registry)
    operation = workflow_repository.get_operation(queued["job_id"], owner_id=owner)
    payload = dict(operation.payload)
    if legacy_kind == "saved_text":
        payload["manual_saved_sources"] = {"artifacts": {}, "before": 1}
    else:
        payload.pop("submission_structure_version")
    workflow_repository.update_operation(operation.id, owner_id=owner, expected_attempt=operation.attempt, payload=payload)
    def forbidden(*args, **kwargs):
        raise AssertionError("retired recovery must not resolve a model or read originals")
    monkeypatch.setattr(task_facade, "_registry_for_owner", forbidden)
    monkeypatch.setattr(task_facade, "resolve_stage_provider_route", forbidden)
    await task_facade.run_durable_submission_recognition(_claim(owner, operation.id))
    stopped = workflow_repository.get_operation(operation.id, owner_id=owner)
    assert stopped.status == "error" and stopped.error_code == "saved_text_recovery_retired"
    response = await tasks.retry_submission_recognition_endpoint(task_id=task, job_id=operation.id,
        request=tasks.RetrySubmissionRecognitionRequest(expected_workflow_revision=
            workflow_repository.get_workflow(task, owner_id=owner).workflow_revision),
        background_tasks=_BackgroundTasks(), current=SimpleNamespace(id=owner), registry=registry)
    assert response.status_code == 409
    assert json.loads(response.body)["error"]["code"] == "saved_text_recovery_retired"
    manual = await tasks.manually_complete_submission_recognition(task_id=task, job_id=operation.id,
        request=tasks.ManualSubmissionRecognitionRequest(expected_workflow_revision=
            workflow_repository.get_workflow(task, owner_id=owner).workflow_revision), current=SimpleNamespace(id=owner))
    assert manual.status_code == 409
    assert json.loads(manual.body)["error"]["code"] == "saved_text_recovery_retired"
    for payload in ({}, {"artifacts": {}}):
        with pytest.raises(InvalidTransition) as rejected:
            task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
                filename="S1.txt", content=b"original", content_type="text/plain",
                registry=registry, manual_saved_sources=payload)
        assert rejected.value.code == "saved_text_recovery_retired"


def test_numbered_solution_steps_and_missing_pages_are_not_split_into_complete_questions():
    from backend.services.submission_incremental import split_submission_question_units
    from backend.services.submission_source_pipeline import PreparedSubmissionSource
    problems = {f"q{i}": {"number": str(i)} for i in (1, 2)}
    for text, recognition in (
        ("1. First calculation\n2. Second calculation", None),
        ("Question 1: First answer\nQuestion 1: Continuation", None),
        ("Question 1: First answer\nQuestion 2: Second answer", {"coverage": {"failed_pages": [2]}}),
    ):
        source = PreparedSubmissionSource("source", "file", "S1.txt", "text/plain", text, recognition=recognition)
        assert split_submission_question_units(source, problems) == [(source, set(problems))]


def test_source_failure_cannot_hide_an_unknown_structure_request_or_erase_missing_ids():
    from backend.agents.ingest_agent import SubmissionSourceParseResult
    from backend.services.submission_source_pipeline import PreparedSubmissionSource, attach_submission_recognition
    source = PreparedSubmissionSource("source", "file", "S1.pdf", "application/pdf", "Saved fragment",
        recognition={"error_code": "provider_recitation_blocked", "coverage": {"failed_pages": [2]}})
    result = SubmissionSourceParseResult("source", "file", "S1.pdf", "parse_failed",
        {"stu_ans": [{"q_id": "q2", "content": "Saved answer", "flag": []}],
         "recognition": {"missing_question_ids": ["q1"]}}, "S1", 1, (),
        "provider_submit_uncertain", "recognition", False)
    merged = attach_submission_recognition([result], [source], verified_question_ids={"q2"})[0]
    assert merged.stable_error_code == "provider_submit_uncertain" and not merged.retryable
    assert merged.student["recognition"]["missing_question_ids"] == ["q1"]
    assert not merged.student["stu_ans"][0]["flag"]


@pytest.mark.asyncio
async def test_response_arriving_after_stop_cannot_write_a_source_structure(monkeypatch):
    from backend.domain.errors import LeaseLost
    owner, task = _seed_task(with_question=True)
    queued = None
    async def invoke(_messages):
        stop_task_run(task_id=task, owner_id=owner, job_id=queued["job_id"],
            expected_revision=workflow_repository.get_workflow(task, owner_id=owner).workflow_revision)
        return _response()
    registry = _registry(monkeypatch, invoke)
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename="S1.txt", content=b"Student answer", content_type="text/plain", registry=registry)
    with pytest.raises(LeaseLost):
        await task_facade.run_durable_submission_recognition(_claim(owner, queued["job_id"]))
    assert not [artifact for artifact in file_repository.list_files(owner_id=owner, assignment_id=task)
                if artifact.kind in {"submission_source_parse_v1", "submission_recognition_result"}]
    row = workflow_repository.get_operation(queued["job_id"], owner_id=owner)
    assert row.error_code == "operation_cancelled" and row.checkpoint["structure_inflight"]
    assert task_facade.get_task(task_id=task, owner_id=owner)["student_data"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("later_page_fails", [False, True])
async def test_closed_answer_is_saved_before_later_ocr_and_is_not_invalidated_by_its_failure(monkeypatch, later_page_fails):
    owner, task = _seed_task()
    for number in (1, 2):
        assignment_repository.add_question(task, teacher_id=owner, q_id=f"q{number}", order_index=number - 1,
            number=str(number), type="short", stem=f"Question {number}", criterion="Award 10 points", max_score=10)
    saved, calls = _saved_signal(monkeypatch), []
    async def invoke(messages):
        q_ids = [question["q_id"] for question in _questions(messages)]
        calls.append(q_ids)
        assert len(q_ids) == 1
        return _response(q_ids[0], "Saved " + q_ids[0])
    registry = _registry(monkeypatch, invoke)
    async def read(**kwargs):
        assert kwargs["allow_partial"]
        await kwargs["on_pages"]([dict(page_number=1, text="Question 1: Complete first answer"),
                                    dict(page_number=2, text="Question 2: Next answer")])
        await asyncio.wait_for(saved.wait(), timeout=3)
        if later_page_fails:
            return SimpleNamespace(text="Question 1: Complete first answer\nQuestion 2: Next answer",
                recognition={"error_code": "provider_submit_uncertain", "coverage": {"processed_pages": [1, 2], "failed_pages": [3]}})
        return SimpleNamespace(text="Question 1: Complete first answer\nQuestion 2: Next answer\nFinal part", recognition=None)
    monkeypatch.setattr(task_facade, "read_question_source", read)
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename="S1.txt", content=b"Original two-page source", content_type="text/plain", registry=registry)
    await task_facade.run_durable_submission_recognition(_claim(owner, queued["job_id"]))
    before = task_facade.get_task(task_id=task, owner_id=owner)
    assert calls.count(["q1"]) == 1 and calls.count(["q2"]) == 1
    if later_page_fails:
        assert before["status"] == "error"
        await continue_manually(task_id=task, owner_id=owner, job_id=queued["job_id"],
                                expected_revision=before["workflow_revision"])
    after = task_facade.get_task(task_id=task, owner_id=owner)
    assert after["status"] == "submissions_ready"
    answers = {answer["q_id"]: answer for answer in after["student_data"]["S1"]["stu_ans"]}
    assert answers["q1"]["content"] == "Saved q1" and not task_facade._answer_recognition_failures(answers["q1"]["flag"])
    assert bool(task_facade._answer_recognition_failures(answers["q2"]["flag"])) == later_page_fails
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_unknown_structure_request_keeps_retry_guard_and_independent_question_survives(monkeypatch):
    from backend.llm.providers import ProviderRequestError
    from backend.llm.request_timeouts import current_response_scope
    owner, task = _seed_task()
    for number in (1, 2):
        assignment_repository.add_question(task, teacher_id=owner, q_id=f"q{number}", order_index=number - 1,
            number=str(number), type="short", stem=f"Question {number}", criterion="Award 10 points", max_score=10)
    calls = []
    async def invoke(messages):
        q_id = _questions(messages)[0]["q_id"]
        calls.append(q_id)
        async def response():
            if q_id == "q1":
                raise ProviderRequestError("provider_upstream_unavailable", status_code=503)
            return _response(q_id, "Saved " + q_id)
        return await current_response_scope().run_response(response)
    registry = _registry(monkeypatch, invoke)
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename="S1.txt", content=b"Question 1: First\nQuestion 2: Second", content_type="text/plain", registry=registry)
    await task_facade.run_durable_submission_recognition(_claim(owner, queued["job_id"]))
    row = workflow_repository.get_operation(queued["job_id"], owner_id=owner)
    assert row.error_code == "provider_submit_uncertain" and sorted(calls) == ["q1", "q2"]
    before = task_facade.get_task(task_id=task, owner_id=owner)
    denied = await tasks.retry_submission_recognition_endpoint(task_id=task, job_id=queued["job_id"],
        request=tasks.RetrySubmissionRecognitionRequest(expected_workflow_revision=before["workflow_revision"]),
        background_tasks=_BackgroundTasks(), current=SimpleNamespace(id=owner), registry=registry)
    assert denied.status_code == 409 and len(calls) == 2
    assert json.loads(denied.body)["error"]["code"] == "provider_submit_uncertain"
    await continue_manually(task_id=task, owner_id=owner, job_id=queued["job_id"],
                            expected_revision=before["workflow_revision"])
    answers = {answer["q_id"]: answer for answer in task_facade.get_task(task_id=task, owner_id=owner)
               ["student_data"]["S1"]["stu_ans"]}
    assert answers["q2"]["content"] == "Saved q2" and not answers["q2"]["flag"]
    assert answers["q1"]["content"] == "" and "recognition_manual_pending" in answers["q1"]["flag"]
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("paused", [False, True])
async def test_later_repeated_heading_revokes_saved_answer_completeness_even_if_stopped(monkeypatch, paused):
    owner, task = _seed_task()
    for number in (1, 2):
        assignment_repository.add_question(task, teacher_id=owner, q_id=f"q{number}", order_index=number - 1,
            number=str(number), type="short", stem=f"Question {number}", criterion="Award 10 points", max_score=10)
    saved, calls = _saved_signal(monkeypatch), []
    async def invoke(messages):
        q_id = _questions(messages)[0]["q_id"]
        calls.append(q_id)
        return _response(q_id, "Saved " + q_id)
    registry = _registry(monkeypatch, invoke)
    async def read(**kwargs):
        first = "Question 1: First part"
        last = "Question 1: Continued elsewhere"
        await kwargs["on_pages"]([dict(page_number=1, text=first), dict(page_number=2, text="Question 2: Second")])
        await asyncio.wait_for(saved.wait(), timeout=3)
        await kwargs["on_pages"]([dict(page_number=3, text=last)])
        if paused:
            raise asyncio.CancelledError()
        return SimpleNamespace(text=first + "\n\n" + last, recognition=None)
    monkeypatch.setattr(task_facade, "read_question_source", read)
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename="S1.txt", content=b"Original source", content_type="text/plain", registry=registry)
    if paused:
        with pytest.raises(asyncio.CancelledError):
            await task_facade.run_durable_submission_recognition(_claim(owner, queued["job_id"]))
        stop_task_run(task_id=task, owner_id=owner, job_id=queued["job_id"],
            expected_revision=workflow_repository.get_workflow(task, owner_id=owner).workflow_revision)
    else:
        await task_facade.run_durable_submission_recognition(_claim(owner, queued["job_id"]))
    before = task_facade.get_task(task_id=task, owner_id=owner)
    await continue_manually(task_id=task, owner_id=owner, job_id=queued["job_id"],
                            expected_revision=before["workflow_revision"])
    answers = {answer["q_id"]: answer for answer in task_facade.get_task(task_id=task, owner_id=owner)
               ["student_data"]["S1"]["stu_ans"]}
    assert answers["q1"]["content"] == "Saved q1"
    assert "recognition_manual_pending" in answers["q1"]["flag"]
    assert calls.count("q1") == 1


@pytest.mark.asyncio
async def test_two_page_answer_is_saved_while_later_page_provider_is_waiting(monkeypatch):
    from backend.domain.errors import RecognitionError
    from backend.progress.tracker import ProgressReporter
    from backend.services.question_source_batches import read_question_pdf_batches
    from backend.tests.test_question_source_batches import fake_result
    from backend.tests.test_recognition_agent import request

    owner, task = _seed_task()
    for number in (1, 2):
        assignment_repository.add_question(task, teacher_id=owner, q_id=f"q{number}", order_index=number - 1,
            number=str(number), type="short", stem=f"Question {number}", criterion="Award 10 points", max_score=10)
    saved, page_calls, structure_calls = _saved_signal(monkeypatch), [], []
    async def invoke(messages):
        q_id = _questions(messages)[0]["q_id"]
        structure_calls.append(q_id)
        if q_id == "q1":
            assert "First page part" in messages[-1].content and "Second page continuation" in messages[-1].content
        return _response(q_id, "Saved " + q_id)
    registry = _registry(monkeypatch, invoke)
    async def run(scoped, *_args, **_kwargs):
        page_calls.append(scoped.pages)
        assert len(scoped.pages) == 1
        page = scoped.pages[0]
        if page == 4:
            # The source reader is still inside the provider wait when q1's
            # complete cross-page structure reaches its durable checkpoint.
            await asyncio.wait_for(saved.wait(), timeout=3)
            raise RecognitionError("provider_submit_uncertain")
        result = fake_result(scoped.pages)
        result.assembly.document.pages[0].spans[0].final_text = (
            "Question 1: First page part" if page == 1 else
            "Second page continuation" if page == 2 else "Question 2: Open tail")
        return result
    async def read(**kwargs):
        text, summary = await read_question_pdf_batches(service=SimpleNamespace(run=run),
            request=request(b"fixture"), content=b"fixture", engine=SimpleNamespace(), total_pages=4,
            reporter=ProgressReporter("incremental-pages"), allow_partial=True, on_pages=kwargs["on_pages"])
        return SimpleNamespace(text=text, recognition=summary)
    monkeypatch.setattr(task_facade, "read_question_source", read)
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename="S1.txt", content=b"Three-page original", content_type="text/plain", registry=registry)
    await task_facade.run_durable_submission_recognition(_claim(owner, queued["job_id"]))
    before = task_facade.get_task(task_id=task, owner_id=owner)
    await continue_manually(task_id=task, owner_id=owner, job_id=queued["job_id"],
                            expected_revision=before["workflow_revision"])
    answers = {answer["q_id"]: answer for answer in task_facade.get_task(task_id=task, owner_id=owner)
               ["student_data"]["S1"]["stu_ans"]}
    assert page_calls == [[1], [2], [3], [4]] and structure_calls.count("q1") == 1
    assert answers["q1"]["content"] == "Saved q1" and not answers["q1"]["flag"]
    assert "recognition_manual_pending" in answers["q2"]["flag"]


@pytest.mark.asyncio
@pytest.mark.parametrize("large_aggregate", [False, True])
async def test_many_saved_units_do_not_fill_parent_artifact_reference_budget(monkeypatch, large_aggregate):
    owner, task = _seed_task()
    for number in range(1, 7):
        assignment_repository.add_question(task, teacher_id=owner, q_id=f"q{number}", order_index=number - 1,
            number=str(number), type="short", stem=f"Question {number}", criterion="Award 10 points", max_score=10)
    registry = _registry(monkeypatch, lambda messages: _response(_questions(messages)[0]["q_id"], "A" * 400))
    # The production boundary is 2,048 refs (500 students x 5 questions crosses
    # it). Lowering the same repository validator exercises the real workflow
    # and source artifacts without thousands of mocked provider calls.
    monkeypatch.setattr(workflow_repository, "MAX_OPERATION_ARTIFACT_REFS", 3)
    if large_aggregate:
        monkeypatch.setattr(task_facade, "SUBMISSION_RESULT_ARTIFACT_MAX_BYTES", 2000)
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner, filename="S1.txt",
        content="\n".join(f"Question {number}: Answer" for number in range(1, 7)).encode(),
        content_type="text/plain", registry=registry)
    await task_facade.run_durable_submission_recognition(_claim(owner, queued["job_id"]))
    operation = workflow_repository.get_operation(queued["job_id"], owner_id=owner)
    assert operation.status == "done" and len(operation.artifact_refs) == (1 if large_aggregate else 2)
    units = [item for item in file_repository.list_files(owner_id=owner, assignment_id=task)
             if item.kind == "submission_source_parse_v1"]
    assert len(units) == 6 and registry.provider.ainvoke.await_count == 6
    restored = task_facade._reusable_submission_results(owner_id=owner, task_id=task,
        payload={"retry_from": {"operation_id": operation.id, "attempt": operation.attempt}},
        questions=assignment_repository.list_questions(task, teacher_id=owner), include_incomplete=True)
    assert len(next(iter(restored.values())).student["stu_ans"]) == 6
    assert registry.provider.ainvoke.await_count == 6


def test_maximum_source_diagnostics_fit_parent_without_discarding_page_evidence():
    diagnostics = dict(total_pages=500, processed_pages=[], failed_pages=list(range(1, 501)), unprocessed_pages=[],
        page_errors=[dict(page=page, error_code="provider_submit_uncertain", status_code=503) for page in range(1, 501)])
    old = {"source_page_diagnostics": {f"{index:032x}": diagnostics for index in range(500)}}
    with pytest.raises(ValidationError, match="storage limit"):
        workflow_repository._validate_json_object(old, field="checkpoint",
            max_bytes=workflow_repository.MAX_OPERATION_CHECKPOINT_BYTES)
    parent = {"source_state_refs": {f"{index:032x}": f"op_{index:032x}" for index in range(500)}}
    assert workflow_repository._validate_json_object(parent, field="checkpoint",
        max_bytes=workflow_repository.MAX_OPERATION_CHECKPOINT_BYTES) == parent
    # Every complete source keeps all 500 detailed failures in its own record.
    source = {"page_diagnostics": diagnostics, "invalidated_question_ids": [f"q{i}" for i in range(500)]}
    assert workflow_repository._validate_json_object(source, field="payload",
        max_bytes=workflow_repository.MAX_OPERATION_PAYLOAD_BYTES) == source
