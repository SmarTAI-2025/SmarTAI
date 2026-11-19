import asyncio
from backend.tests.rubric_fixtures import add_reviewed_question

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
        stu_ans=[dict(q_id=q_id, content=content, number=q_id[1:], type="concept", flag=[])])))


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
        add_reviewed_question(task, teacher_id=owner, q_id=f"q{number}", order_index=number - 1,
            number=str(number), type="concept", stem=f"Question {number}", criterion="Award 10 points", max_score=10)
    saved, calls = _saved_signal(monkeypatch), []
    async def invoke(messages):
        ids = [item["q_id"] for item in _questions(messages)]
        calls.append(ids)
        if pause and ids == ["q3", "q4"]:
            await asyncio.wait_for(saved.wait(), 3)
            raise asyncio.CancelledError()
        return SimpleNamespace(content=json.dumps(dict(stu_id="S1", stu_name="Student One",
            stu_ans=[dict(q_id=q_id, number=q_id[1:], type="concept", content="Saved " + q_id, flag=[]) for q_id in ids])))
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
    reporter = task_facade.get_or_create_reporter(queued["job_id"])
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
    progress = await reporter.snapshot()
    assert progress.completed_units == (0 if pause else 1)
    assert progress.total_students == 1
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
@pytest.mark.parametrize("second_fails", [False, True])
async def test_parallel_submission_progress_preserves_saved_counts_and_counts_finished_sources(monkeypatch, second_fails):
    from backend.progress.tracker import get_or_create_reporter

    owner, task = _seed_task(with_question=True)
    first_finished = asyncio.get_running_loop().create_future()
    original_attach = task_facade.attach_submission_recognition
    snapshots = {}

    def attach(results, sources, **kwargs):
        result = original_attach(results, sources, **kwargs)
        if sources[0].filename == "S1_Alice.txt" and "verified_question_ids" in kwargs:
            first_finished.set_result(asyncio.current_task())
        return result

    monkeypatch.setattr(task_facade, "attach_submission_recognition", attach)

    async def invoke(messages):
        second = "second answer" in messages[-1].content
        if second:
            snapshots["second_read"] = await reporter.snapshot()
        return SimpleNamespace(content=json.dumps(dict(
            stu_id="S2" if second else "S1", stu_name="Bob" if second else "Alice",
            stu_ans=[dict(q_id="q1", number="1", type="concept", content="Saved answer", flag=[])])))

    registry = _registry(monkeypatch, invoke)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("S1_Alice.txt", "first answer")
        archive.writestr("S2_Bob.txt", "second answer")

    async def read(**kwargs):
        if kwargs["filename"] == "S2_Bob.txt":
            first_task = await asyncio.wait_for(first_finished, 3)
            await asyncio.wait_for(asyncio.shield(first_task), 3)
            snapshots["first_finished"] = await reporter.snapshot()
            if second_fails:
                raise ValidationError("Temporary read timeout", code="provider_timeout")
        return SimpleNamespace(text=kwargs["content"].decode(), recognition=None)

    monkeypatch.setattr(task_facade, "read_question_source", read)
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename="answers.zip", content=buffer.getvalue(), content_type="application/zip", registry=registry)
    reporter = get_or_create_reporter(queued["job_id"])
    await task_facade.run_durable_submission_recognition(_claim(owner, queued["job_id"]))

    assert {name: dict(completed_units=progress.completed_units, total_students=progress.total_students,
                       **progress.stage_metrics) for name, progress in snapshots.items()} == {
        name: dict(completed_units=1, total_students=2, files_total=2, files_processed=files,
                   submissions_recognized=1, identities_matched=1, identities_needing_review=0, answers_split=1)
        for name, files in [("first_finished", 1), *([] if second_fails else [("second_read", 2)])]
    }
    operation = workflow_repository.get_operation(queued["job_id"], owner_id=owner)
    assert operation.status == ("error" if second_fails else "done")
    assert operation.progress["completed_units"] == operation.progress["total_students"] == 2
    assert operation.progress["stage_metrics"] == dict(files_total=2, files_processed=2,
        submissions_recognized=1 if second_fails else 2, identities_matched=1 if second_fails else 2,
        identities_needing_review=0, answers_split=1 if second_fails else 2, parse_failures=int(second_fails))

    if second_fails:
        second_fails = False
        before = task_facade.get_task(task_id=task, owner_id=owner)
        retry = await tasks.retry_submission_recognition_endpoint(task_id=task, job_id=queued["job_id"],
            request=tasks.RetrySubmissionRecognitionRequest(expected_workflow_revision=before["workflow_revision"]),
            background_tasks=_BackgroundTasks(), current=SimpleNamespace(id=owner), registry=registry)
        reporter = get_or_create_reporter(retry["job_id"])
        await task_facade.run_durable_submission_recognition(_claim(owner, retry["job_id"]))
        recovered = await reporter.snapshot()
        assert recovered.completed_units == recovered.total_students == 2
        assert recovered.stage_metrics["submissions_recognized"] == 2
        assert recovered.stage_metrics["identities_matched"] == 2
        assert recovered.stage_metrics["answers_split"] == 2
        assert registry.provider.ainvoke.await_count == 2  # The first file was reused.


@pytest.mark.asyncio
async def test_restored_submission_aggregate_restores_progress_without_model_calls(monkeypatch):
    from backend.progress.tracker import get_or_create_reporter, remove_reporter

    owner, task = _seed_task(with_question=True)
    registry = _registry(monkeypatch, lambda _: _response())
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename="S1_Student.txt", content=b"Saved answer", content_type="text/plain", registry=registry)
    operation = _claim(owner, queued["job_id"])
    original = task_facade.source_outcome_repository.record_outcome

    def interrupted(**kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(task_facade.source_outcome_repository, "record_outcome", interrupted)
    with pytest.raises(asyncio.CancelledError):
        await task_facade.run_durable_submission_recognition(operation)
    monkeypatch.setattr(task_facade.source_outcome_repository, "record_outcome", original)
    remove_reporter(queued["job_id"])
    reporter = get_or_create_reporter(queued["job_id"])
    await task_facade.run_durable_submission_recognition(operation)
    recovered = await reporter.snapshot()
    assert registry.provider.ainvoke.await_count == 1
    assert recovered.completed_units == recovered.total_students == 1
    assert recovered.stage_metrics == dict(files_total=1, files_processed=1,
        submissions_recognized=1, identities_matched=1, identities_needing_review=0, answers_split=1, parse_failures=0)


@pytest.mark.asyncio
@pytest.mark.parametrize("manual", [False, True])
async def test_one_question_is_durable_before_next_question_finishes(monkeypatch, manual):
    owner, task = _seed_task()
    add_reviewed_question(task, teacher_id=owner, q_id="q1", order_index=0,
        number="1", type="concept", stem="First", criterion="Award 10 points", max_score=10)
    add_reviewed_question(task, teacher_id=owner, q_id="q2", order_index=1,
        number="2", type="concept", stem="Second", criterion="Award 10 points", max_score=10)
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
@pytest.mark.parametrize(("blank_heading", "missing_answer"), [(False, True), (True, True), (True, False)])
async def test_pdf_page_gap_advises_at_page_boundaries_and_blocks_missing_work(monkeypatch, blank_heading, missing_answer):
    from backend.tests.test_recognition_agent import pdf

    owner, task = _seed_task()
    numbers = ("1.1.5", "1.1.7", "1.1.20", "1.1.29", "1.1.31")
    for index, number in enumerate(numbers, 1):
        add_reviewed_question(task, teacher_id=owner, q_id=f"q{index}", order_index=index - 1,
            number=number, type="concept", stem=f"Question {number}", criterion="Award 10 points", max_score=10)
    pages = {1: "1.1.5: Complete\n1.1.7 Some unclear marks\n1.1.20 Text visible before the gap",
             3: "1.1.29 A complete final answer"}
    if blank_heading:
        pages[3] += "\n1.1.31" if missing_answer else "\n1.1.31 The other full answer"
    calls = []

    async def read(**kwargs):
        await kwargs["on_pages"]([dict(page_number=page, text=text) for page, text in pages.items()])
        coverage = {"processed_pages": [1, 3], "failed_pages": [2], "unprocessed_pages": [], "total_pages": 3}
        return SimpleNamespace(text="\n\n".join(pages.values()), recognition={
            "error_code": "image_recognition_unconfirmed",
            "coverage": coverage, "page_diagnostics": coverage,
        })

    async def invoke(messages):
        questions = _questions(messages)
        calls.append([question["q_id"] for question in questions])
        return SimpleNamespace(content=json.dumps(dict(stu_id="S1", stu_name="Student One", stu_ans=[
            dict(q_id=q["q_id"], number=q["number"], type="concept",
                 content=content, flag={"q2": ["unclear_text"], "q4": ["recognition_uncertain", "unreadable_crossed_out_text", "recognition_incomplete"]}.get(q["q_id"], []),
                 recognition_confidence={"q3": 1, "q4": .95}.get(q["q_id"]),
                 **({"source_opening": content} if q["q_id"] == "q5" and content else {}))
            for q, content in zip(questions, ("Complete", "Some unclear marks", "Text visible before the gap",
                                              "A complete final answer", "" if missing_answer else "The other full answer"))
        ])))

    registry = _registry(monkeypatch, invoke)
    monkeypatch.setattr(task_facade, "read_question_source", read)
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename="S1.pdf", content=pdf(["First", "Unread", "Last"]), content_type="application/pdf", registry=registry)
    await task_facade.run_durable_submission_recognition(_claim(owner, queued["job_id"]))
    before = task_facade.get_task(task_id=task, owner_id=owner)
    assert before["status"] == "error"
    assert before["submission_sources"][0]["page_diagnostics"]["failed_pages"] == [2]
    await continue_manually(task_id=task, owner_id=owner, job_id=queued["job_id"],
                            expected_revision=before["workflow_revision"])
    after = task_facade.get_task(task_id=task, owner_id=owner, full=True)
    answers = {a["q_id"]: a for a in after["student_data"]["S1"]["stu_ans"]}
    assert calls == [["q1", "q2", "q3", "q4", "q5"]]
    assert not answers["q1"]["flag"]
    assert answers["q2"]["flag"] == ["unclear_text"]
    assert answers["q3"]["content"] == "Text visible before the gap"
    assert answers["q3"]["flag"] == ["recognition_boundary_uncertain", "recognition_boundary_next_page_failed"]
    assert answers["q4"]["flag"] == ["recognition_uncertain", "unreadable_crossed_out_text",
        "recognition_boundary_uncertain", "recognition_boundary_previous_page_failed"]
    if missing_answer:
        assert not answers["q5"]["content"]
        assert {"recognition_failed_pages:2", "recognition_incomplete"} <= set(answers["q5"]["flag"])
    else:
        assert answers["q5"]["content"] == "The other full answer" and not answers["q5"]["flag"]
    readiness = task_facade.grading_readiness(task_id=task, owner_id=owner)
    assert readiness["ready"] is not missing_answer
    assert ("submission_recognition_blocked" in readiness["blocking_issues"]) == missing_answer
    assert "submission_recognition_needs_review" in readiness["warnings"]


@pytest.mark.asyncio
@pytest.mark.parametrize("headings", [
    ("Question 4:", "Question 2:", "Question 3:", "Question 1:"),
    ("4.", "2.", "3.", "1."),
    ("Question 4:", "2.", "3.", "Question 1:"),
    ("4.", "2.", "", "1."),
])
async def test_pdf_page_gap_uses_the_positions_of_matched_answers(monkeypatch, headings):
    from backend.tests.test_recognition_agent import pdf

    owner, task = _seed_task()
    for number in range(1, 5):
        add_reviewed_question(task, teacher_id=owner, q_id=f"q{number}", order_index=number - 1,
            number=str(number), type="concept", stem=f"Question {number}", criterion="Award 10 points", max_score=10)
    contents = {
        "q1": "The final answer is complete.",
        "q2": "The first half of the group proof.\n1. Pair each element with its inverse.",
        "q3": "The proof resumes with its last claim.",
        "q4": "A complete answer before the proof.",
    }
    pages = {
        1: f"{headings[0]} {contents['q4']}\n{headings[1]}\n{contents['q2']}",
        3: f"{headings[2]} {contents['q3']}\n{headings[3]} {contents['q1']}",
    }

    async def read(**kwargs):
        await kwargs["on_pages"]([dict(page_number=page, text=value) for page, value in pages.items()])
        coverage = {"processed_pages": [1, 3], "failed_pages": [2], "unprocessed_pages": [], "total_pages": 3}
        return SimpleNamespace(text="\n\n".join(pages.values()), recognition={
            "error_code": "image_recognition_unconfirmed", "coverage": coverage, "page_diagnostics": coverage,
        })

    async def invoke(messages):
        return SimpleNamespace(content=json.dumps(dict(stu_id="S1", stu_name="Student One", stu_ans=[
            dict(q_id=q["q_id"], number=q["number"], type="concept", content=contents[q["q_id"]],
                 flag=["recognition_incomplete"] if q["q_id"] == "q2" else [])
            for q in _questions(messages)
        ])))

    registry = _registry(monkeypatch, invoke)
    monkeypatch.setattr(task_facade, "read_question_source", read)
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename="S1.pdf", content=pdf([pages[1], "Unread", pages[3]]),
        content_type="application/pdf", registry=registry)
    await task_facade.run_durable_submission_recognition(_claim(owner, queued["job_id"]))
    before = task_facade.get_task(task_id=task, owner_id=owner)
    assert before["submission_sources"][0]["page_diagnostics"]["failed_pages"] == [2]
    await continue_manually(task_id=task, owner_id=owner, job_id=queued["job_id"],
                            expected_revision=before["workflow_revision"])
    after = task_facade.get_task(task_id=task, owner_id=owner, full=True)
    answers = {answer["q_id"]: answer for answer in after["student_data"]["S1"]["stu_ans"]}
    assert {q_id: answer["content"] for q_id, answer in answers.items()} == contents
    assert {q_id: answer["flag"] for q_id, answer in answers.items()} == {
        "q1": [], "q2": ["recognition_boundary_uncertain", "recognition_boundary_next_page_failed"],
        "q3": ["recognition_boundary_uncertain", "recognition_boundary_previous_page_failed"], "q4": [],
    }
    registry.provider.ainvoke.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(("continued_heading", "next_answer"), [
    ("1.1.29（续）", True),
    ("1.1.29（续上页）", True),
    ("1.1.29 （续上页）", True),
    ("1.1.29 （续上页）", False),
    ("Question 1.1.29: (continued)", True),
    ("Question 1.1.29: (continued) Proof fragment", False),
    ("1.1.29 (a) （续） Proof fragment", True),
    ("Question 1.1.29: Proof ending", False),
])
async def test_pdf_continuation_after_unread_page_is_advisory_on_handoff(
    monkeypatch, continued_heading, next_answer,
):
    from backend.tests.test_recognition_agent import pdf

    owner, task = _seed_task()
    numbers = ["1.1.5", "1.1.7", "1.1.29"]
    if next_answer:
        numbers.append("1.1.31")
    contents = {"1.1.5": "Complete first answer", "1.1.7": "Opening fragment",
                "1.1.29": "Proof ending only", "1.1.31": "Complete next answer"}
    for index, number in enumerate(numbers, 1):
        add_reviewed_question(task, teacher_id=owner, q_id=f"q{index}", order_index=index - 1,
            number=number, type="concept", stem=f"Question {number}", criterion="Award 10 points", max_score=10)
    pages = {1: "Question 1.1.5: Complete first answer\n1.1.7 Opening fragment",
             3: f"{continued_heading}\nProof ending only"}
    if next_answer:
        pages[3] += "\n1.1.31 Complete next answer"

    async def read(**kwargs):
        await kwargs["on_pages"]([dict(page_number=page, text=value) for page, value in pages.items()])
        coverage = {"processed_pages": [1, 3], "failed_pages": [2], "unprocessed_pages": [], "total_pages": 3}
        return SimpleNamespace(text="\n\n".join(pages.values()), recognition={
            "error_code": "image_recognition_unconfirmed", "coverage": coverage, "page_diagnostics": coverage,
        })

    async def invoke(messages):
        return SimpleNamespace(content=json.dumps(dict(stu_id="S1", stu_name="Student One", stu_ans=[
            dict(q_id=q["q_id"], number=q["number"], type="concept", content=contents[q["number"]],
                 flag=["recognition_failed"] if continued_heading.endswith("Proof ending") and q["q_id"] == "q3" else [],
                 **({"source_opening": contents["1.1.31"]} if q["number"] == "1.1.31" else {}))
            for q in _questions(messages)
        ])))

    registry = _registry(monkeypatch, invoke)
    monkeypatch.setattr(task_facade, "read_question_source", read)
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename="S1.pdf", content=pdf([pages[1], "Unread opening", pages[3]]),
        content_type="application/pdf", registry=registry)
    await task_facade.run_durable_submission_recognition(_claim(owner, queued["job_id"]))
    before = task_facade.get_task(task_id=task, owner_id=owner)
    assert before["status"] == "error"
    assert before["submission_sources"][0]["page_diagnostics"]["failed_pages"] == [2]
    await continue_manually(task_id=task, owner_id=owner, job_id=queued["job_id"],
                            expected_revision=before["workflow_revision"])
    after = task_facade.get_task(task_id=task, owner_id=owner, full=True)
    answers = {a["q_id"]: a for a in after["student_data"]["S1"]["stu_ans"]}
    assert answers["q3"]["content"] == "Proof ending only"
    assert answers["q2"]["flag"] == ["recognition_boundary_uncertain", "recognition_boundary_next_page_failed"]
    assert answers["q3"]["flag"] == ["recognition_boundary_uncertain", "recognition_boundary_previous_page_failed"]
    assert not answers["q1"]["flag"]
    if next_answer:
        assert not answers["q4"]["flag"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("unread", "continuation_first", "opening_evidence", "part_label", "separator"), [
    (True, False, "valid", "", "\n"),
    (True, True, "valid", "", "\n"),
    (True, True, "missing", "", "\n"),
    (True, False, "unbound", "", "\n"),
    (True, False, "missing_from_transcription", "", "\n"),
    (False, False, "missing", "", "\n"),
    (True, False, "valid", "(a)", "\n"),
    (True, True, "labelled", "(a)", "\n"),
    (True, False, "label_only", "(a)", "\n"),
    (True, False, "wrong_label", "(a)", "\n"),
    (True, False, "valid", "(a)", " "),
    (True, True, "valid", "(a)", " "),
    (True, False, "label_only", "(a)", " "),
    (True, True, "wrong_label", "(a)", " "),
])
async def test_pdf_gap_handoff_advises_only_at_failed_page_edges(
    monkeypatch, unread, continuation_first, opening_evidence, part_label, separator,
):
    import fitz

    owner, task = _seed_task()
    for index, number in enumerate(("1.1.29", "1.1.31"), 1):
        add_reviewed_question(task, teacher_id=owner, q_id=f"q{index}", order_index=index - 1,
            number=number, type="concept", stem=f"Question {number}", criterion="Award 10 points", max_score=10)
    opening = "Let G be a finite group of even order."
    body = "Pair every element with its inverse. One nonidentity element must be self-inverse."
    full_answer = f"{opening} {body}"
    saved_answer = body if opening_evidence == "missing_from_transcription" else full_answer
    if opening_evidence in {"labelled", "label_only", "wrong_label"}:
        saved_answer = f"{part_label}\n{saved_answer}"
    quotes = {"unbound": "An invented opening.", "labelled": f"{part_label} {opening}",
              "label_only": part_label, "wrong_label": f"(b) {opening}"}
    proof_tail = "By the identity already established, the direct product is abelian."
    partial = f"1.1.29（续）\n{proof_tail}"
    heading = f"1.1.31 {part_label}".rstrip()
    full = f"{heading}{separator}{full_answer}"
    pages = {1: "Name: Student One\nStudent ID: S1", 2: "1.1.29\nThe full direct-product proof."}
    pages[3] = "\n".join((partial, full) if continuation_first else (full, partial)) if unread else full
    with fitz.open() as document:
        for page_text in pages.values():
            page = document.new_page(width=900, height=350)
            assert page.insert_textbox(fitz.Rect(20, 20, 880, 320), page_text, fontname="china-s", fontsize=10) > 0
        pdf_bytes = document.tobytes()
    calls = []

    async def read(**kwargs):
        with fitz.open(stream=kwargs["content"], filetype="pdf") as document:
            assert document.page_count == 3 and "1.1.29" in document[1].get_text()
            seen = {index: page.get_text().strip() for index, page in enumerate(document, 1) if not unread or index != 2}
        await kwargs["on_pages"]([dict(page_number=page, text=value) for page, value in seen.items()])
        coverage = {"processed_pages": list(seen), "failed_pages": [2] if unread else [],
                    "unprocessed_pages": [], "total_pages": 3}
        return SimpleNamespace(text="\n\n".join(seen.values()), recognition={
            "error_code": "image_recognition_unconfirmed" if unread else None,
            "coverage": coverage, "page_diagnostics": coverage,
        })

    async def invoke(messages):
        calls.append(messages[0].content)
        answers = []
        for question in _questions(messages):
            number = question["number"]
            content = (proof_tail if unread else "The full direct-product proof.") if number == "1.1.29" else saved_answer
            answer = dict(q_id=question["q_id"], number=number, type="concept", content=content, flag=[])
            if number == "1.1.29" and unread:
                # The explicit continuation heading must not certify this tail.
                answer["source_opening"] = proof_tail
            elif number == "1.1.31" and opening_evidence != "missing":
                answer["source_opening"] = quotes.get(opening_evidence, opening)
            answers.append(answer)
        return SimpleNamespace(content=json.dumps(dict(stu_id="S1", stu_name="Student One", stu_ans=answers)))

    registry = _registry(monkeypatch, invoke)
    monkeypatch.setattr(task_facade, "read_question_source", read)
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename="S1.pdf", content=pdf_bytes, content_type="application/pdf", registry=registry)
    await task_facade.run_durable_submission_recognition(_claim(owner, queued["job_id"]))
    task_state = task_facade.get_task(task_id=task, owner_id=owner)
    if unread:
        assert task_state["status"] == "error"
        assert task_state["submission_sources"][0]["page_diagnostics"]["failed_pages"] == [2]
        await continue_manually(task_id=task, owner_id=owner, job_id=queued["job_id"],
                                expected_revision=task_state["workflow_revision"])
    after = task_facade.get_task(task_id=task, owner_id=owner, full=True)
    answers = {answer["number"]: answer for answer in after["student_data"]["S1"]["stu_ans"]}
    assert answers["1.1.31"]["content"] == saved_answer
    boundary = ["recognition_boundary_uncertain", "recognition_boundary_previous_page_failed"]
    assert answers["1.1.31"]["flag"] == (boundary if unread and not continuation_first else [])
    if unread:
        assert answers["1.1.29"]["content"] == proof_tail
        assert answers["1.1.29"]["flag"] == (boundary if continuation_first else [])
    else:
        assert not answers["1.1.29"]["flag"]
    assert len(calls) == 1 and ("source_opening" in calls[0]) == unread


@pytest.mark.asyncio
@pytest.mark.parametrize("later_page_fails", [False, True])
async def test_closed_answer_is_saved_before_later_ocr_and_is_not_invalidated_by_its_failure(monkeypatch, later_page_fails):
    owner, task = _seed_task()
    for number in (1, 2):
        add_reviewed_question(task, teacher_id=owner, q_id=f"q{number}", order_index=number - 1,
            number=str(number), type="concept", stem=f"Question {number}", criterion="Award 10 points", max_score=10)
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
        add_reviewed_question(task, teacher_id=owner, q_id=f"q{number}", order_index=number - 1,
            number=str(number), type="concept", stem=f"Question {number}", criterion="Award 10 points", max_score=10)
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
        add_reviewed_question(task, teacher_id=owner, q_id=f"q{number}", order_index=number - 1,
            number=str(number), type="concept", stem=f"Question {number}", criterion="Award 10 points", max_score=10)
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
        add_reviewed_question(task, teacher_id=owner, q_id=f"q{number}", order_index=number - 1,
            number=str(number), type="concept", stem=f"Question {number}", criterion="Award 10 points", max_score=10)
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
        add_reviewed_question(task, teacher_id=owner, q_id=f"q{number}", order_index=number - 1,
            number=str(number), type="concept", stem=f"Question {number}", criterion="Award 10 points", max_score=10)
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


@pytest.mark.asyncio
@pytest.mark.parametrize("confirm_blank", [False, True])
@pytest.mark.parametrize("second_unresolved", [False, True])
async def test_source_attention_tracks_current_answers_after_teacher_repairs(monkeypatch, confirm_blank, second_unresolved):
    from backend.services.submission_source_state import load_source_states
    owner, task = _seed_task()
    for i in range(1, 4):
        add_reviewed_question(task, teacher_id=owner, q_id=f"q{i}", order_index=i-1,
            number=str(i), type="programming", stem=f"Question {i}: print the requested value.",
            criterion="Award up to 10 points for correct code.", max_score=10)
    registry = _registry(monkeypatch, lambda _: SimpleNamespace(content=json.dumps(dict(
        stu_id="S1", stu_name="Sample Student", stu_ans=[dict(q_id="q1", number="1", type="programming", content="print(1)", flag=[])]))))
    content = b"# Question 1\ndef solve():\n    return 1\n"
    filename, content_type = "S1_Sample.py", "text/x-python"
    if second_unresolved:
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr(filename, content)
            zipped.writestr("S9_Other.py", content)
        filename, content, content_type = "submissions.zip", archive.getvalue(), "application/zip"
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename=filename, content=content, content_type=content_type, registry=registry)
    await task_facade.run_durable_submission_recognition(_claim(owner, queued["job_id"]))
    args = dict(task_id=task, owner_id=owner)
    before = task_facade.get_task(**args)
    source = before["submission_sources"][0]
    assert source["status"] == "parsed", (source["reason_code"], source["internal_status"], registry.provider.ainvoke.await_count)
    assert source["answer_required_diagnostic_count"] == 2
    original = load_source_states(workflow_repository.get_operation(queued["job_id"], owner_id=owner))
    calls = registry.provider.ainvoke.await_count
    display_id = next(p.display_student_id for p in workflow_repository.list_student_presentations(task).values()
                      if p.source_id == source["source_id"])
    task_facade.update_student_answer(**args, display_student_id=display_id, q_id="q2",
        patch={"content": "print(2)", "review_status": "confirmed"}, expected_revision=before["workflow_revision"])
    after_one = task_facade.get_task(**args)
    current = after_one["submission_sources"][0]
    assert current["answer_required_diagnostic_count"] == 1
    assert [a["q_id"] for a in current["answer_diagnostics"]] == ["q3"]
    reads = []
    original_serializer = task_facade._serialize_student_data
    def counted_serializer(*args, **kwargs):
        reads.append(1)
        return original_serializer(*args, **kwargs)
    with monkeypatch.context() as scope:
        scope.setattr(task_facade, "_serialize_student_data", counted_serializer)
        assert task_facade.task_state(**args)["submission_sources"][0]["answer_required_diagnostic_count"] == 1
    assert reads == []  # A lightweight poll never builds the full student/answer DTOs.
    task_facade.update_student_identity(**args, current_display_id=display_id,
        new_display_id="RepairedS1", new_display_name="Corrected Student", expected_revision=after_one["workflow_revision"])
    current_task = task_facade.get_task(**args)
    assert current_task["submission_sources"][0]["answer_required_diagnostic_count"] == 1
    patch = {"content": "" if confirm_blank else "print(3)", "review_status": "confirmed"}
    if confirm_blank:
        patch["flag"] = ["manual_blank_confirmed"]
    task_facade.update_student_answer(**args, display_student_id="RepairedS1", q_id="q3", patch=patch,
        expected_revision=current_task["workflow_revision"])
    for final in [task_facade.get_task(**args), task_facade.task_state(**args)]:
        projected = final["submission_sources"][0]
        assert projected["answer_diagnostics"] == []
        assert projected["answer_diagnostic_count"] == projected["answer_required_diagnostic_count"] == 0
        if second_unresolved:
            other = final["submission_sources"][1]
            assert other["answer_required_diagnostic_count"] == 2
            assert {a["q_id"] for a in other["answer_diagnostics"]} == {"q2", "q3"}
    assert ("submission_recognition_blocked" in task_facade.grading_readiness(**args)["blocking_issues"]) is second_unresolved
    assert load_source_states(workflow_repository.get_operation(queued["job_id"], owner_id=owner)) == original
    assert registry.provider.ainvoke.await_count == calls  # Reading/repairing does not rerun recognition.


@pytest.mark.asyncio
async def test_clean_source_discovers_new_assignment_conflicts_and_clears_them_after_repair(monkeypatch):
    from backend.tests.test_programming_source_answers import seed, SUM, COUNT, SOURCE
    from backend.services.submission_source_state import load_source_states
    owner, task = seed(SUM, COUNT)
    registry = _registry(monkeypatch, lambda _: (_ for _ in ()).throw(AssertionError("Native input called a model")))
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename="S1_Alice.py", content=SOURCE.encode(), content_type="text/x-python", registry=registry)
    await task_facade.run_durable_submission_recognition(_claim(owner, queued["job_id"]))
    args = dict(task_id=task, owner_id=owner)
    before = task_facade.get_task(**args)
    assert before["submission_sources"][0]["status"] == "parsed"
    assert before["submission_sources"][0]["answer_diagnostic_count"] == 0
    assert before["submission_sources"][0]["answer_required_diagnostic_count"] == 0
    history = load_source_states(workflow_repository.get_operation(queued["job_id"], owner_id=owner))
    original = next(a for a in before["student_data"]["S1"]["stu_ans"] if a["q_id"] == "q2")
    task_facade.update_student_answer(**args, display_student_id="S1", q_id="q2",
        patch={"answer_id": original["answer_id"], "target_q_id": "q3"}, expected_revision=before["workflow_revision"])
    conflicted = task_facade.get_task(**args)
    assert all("answer_assignment_conflict" in a["flag"] for a in conflicted["student_data"]["S1"]["stu_ans"])
    assert "submission_recognition_blocked" in task_facade.grading_readiness(**args)["blocking_issues"]
    for snapshot in [conflicted, task_facade.task_state(**args)]:
        source = snapshot["submission_sources"][0]
        assert source["answer_required_diagnostic_count"] == source["answer_diagnostic_count"] == 2
        assert len(source["answer_diagnostics"]) == 2
        assert all("answer_assignment_conflict" in a["flag"] for a in source["answer_diagnostics"])
    task_facade.update_student_answer(**args, display_student_id="S1", q_id="q3",
        patch={"answer_id": original["answer_id"], "target_q_id": "q2"}, expected_revision=conflicted["workflow_revision"])
    for snapshot in [task_facade.get_task(**args), task_facade.task_state(**args)]:
        source = snapshot["submission_sources"][0]
        assert source["answer_required_diagnostic_count"] == source["answer_diagnostic_count"] == 0
        assert source["answer_diagnostics"] == []
    assert load_source_states(workflow_repository.get_operation(queued["job_id"], owner_id=owner)) == history
    registry.provider.ainvoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_current_source_review_fields_match_full_review_policy_and_scope(monkeypatch):
    from sqlalchemy import delete, select, update
    from backend.db.models import SubmissionAnswerRecord
    from backend.db.session import session_scope
    from backend.tests.test_programming_source_answers import seed, SUM, COUNT, SOURCE

    owner, task = seed(SUM, COUNT)
    registry = _registry(monkeypatch, lambda _: pytest.fail("Native input called a model"))
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename="S1_Alice.py", content=SOURCE.encode(), content_type="text/x-python", registry=registry)
    await task_facade.run_durable_submission_recognition(_claim(owner, queued["job_id"]))
    args = dict(task_id=task, owner_id=owner)
    source_id = task_facade.get_task(**args)["submission_sources"][0]["source_id"]
    all_whitespace = "".join(chr(cp) for cp in range(0x3001) if chr(cp).isspace())
    cases = [
        ([], "pending", SOURCE),
        (["answer_recognition_confidence:0.4"], "pending", SOURCE),
        (["answer_recognition_confidence:0.4"], "confirmed", SOURCE),
        (["recognition_manual_pending"], "pending", SOURCE),
        (["recognition_manual_pending"], "confirmed", SOURCE),
        (["recognition_manual_pending"], "confirmed", all_whitespace),
        (["recognition_invalid_fields:number,type"], "pending", SOURCE),
        (["recognition_invalid_fields:number,type"], "pending", all_whitespace),
        (["recognition_failed"], "confirmed", SOURCE),
        (["manual_blank_confirmed"], "confirmed", ""),
        (["recognition_manual_pending"], "confirmed", all_whitespace + "x" + all_whitespace),
    ]
    for flags, status, content in cases:
        with session_scope() as session:
            session.execute(update(SubmissionAnswerRecord).values(content=content, flag=flags))
            answer_ids = session.scalars(select(SubmissionAnswerRecord.id)).all()
        for answer_id in answer_ids:
            workflow_repository.set_answer_review_status(answer_id, status)
        assert task_facade.task_state(**args)["submission_sources"] == task_facade.get_task(**args)["submission_sources"], (flags, status, repr(content[:40]))
        fields = workflow_repository.current_source_answer_review_fields(task, owner_id=owner, source_ids=[source_id])
        assert all(row.has_content == bool(content.strip()) and "content" not in row._mapping for row in fields)

    assert workflow_repository.current_source_answer_review_fields(task, owner_id="other-owner", source_ids=[source_id]) == []
    assert workflow_repository.current_source_answer_review_fields(task, owner_id=owner, source_ids=["other-source"]) == []
    # Empty current revisions must still synthesize missing-question diagnostics.
    with session_scope() as session:
        session.execute(delete(SubmissionAnswerRecord))
    for snapshot in (task_facade.get_task(**args), task_facade.task_state(**args)):
        assert snapshot["submission_sources"][0]["answer_required_diagnostic_count"] == 2
    with session_scope() as session:
        session.execute(update(workflow_repository.AssignmentStudentPresentationRecord).values(is_active=False))
    assert workflow_repository.current_source_answer_review_fields(task, owner_id=owner, source_ids=[source_id]) == []
    assert task_facade.task_state(**args)["submission_sources"] == []
    registry.provider.ainvoke.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("student_count", [1, 10])
async def test_state_poll_batches_current_diagnostics_without_serializing_answer_bodies(monkeypatch, student_count):
    from sqlalchemy import event
    from backend.db.session import get_engine
    from backend.tests.test_programming_source_answers import seed, SUM, COUNT, SOURCE

    owner, task = seed(SUM, COUNT)
    registry = _registry(monkeypatch, lambda _: pytest.fail("Native input called a model"))
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        for index in range(student_count):
            zipped.writestr(f"S{index}_Student.py", SOURCE + "\n# " + "large body " * 1000)
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename="submissions.zip", content=archive.getvalue(), content_type="application/zip", registry=registry)
    await task_facade.run_durable_submission_recognition(_claim(owner, queued["job_id"]))
    args = dict(task_id=task, owner_id=owner)
    before = task_facade.get_task(**args)
    # A clean source must still discover newly introduced conflicts.
    original = next(a for a in before["student_data"]["S0"]["stu_ans"] if a["q_id"] == "q2")
    task_facade.update_student_answer(**args, display_student_id="S0", q_id="q2",
        patch={"answer_id": original["answer_id"], "target_q_id": "q3"}, expected_revision=before["workflow_revision"])

    statements, serialized_bodies, body_columns = [], [], []
    serializer = task_facade._serialize_student_data
    def count_serialized_bodies(*args, **kwargs):
        students = serializer(*args, **kwargs)
        serialized_bodies.extend(answer["content"] for student in students.values() for answer in student["stu_ans"])
        return students
    def count_sql(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    def inspect_columns(conn, cursor, statement, parameters, context, executemany):
        body_columns.extend(column[0] for column in cursor.description or [] if column[0] == "content")
    engine = get_engine()
    event.listen(engine, "before_cursor_execute", count_sql)
    event.listen(engine, "after_cursor_execute", inspect_columns)
    try:
        # Isolate the cost added by current-source diagnostics from pre-existing state queries.
        with monkeypatch.context() as scope:
            scope.setattr(workflow_repository, "current_source_answer_review_fields", lambda *a, **kw: [])
            task_facade.task_state(**args)
        baseline = len(statements)
        statements.clear()
        monkeypatch.setattr(task_facade, "_serialize_student_data", count_serialized_bodies)
        state = task_facade.task_state(**args)
    finally:
        event.remove(engine, "before_cursor_execute", count_sql)
        event.remove(engine, "after_cursor_execute", inspect_columns)
    print(f"poll students={student_count}: baseline={baseline}, actual={len(statements)}, "
          f"serialized_answer_characters={sum(map(len, serialized_bodies))}")
    assert serialized_bodies == []
    assert body_columns == []
    assert len(statements) - baseline == 1  # One batch query, independent of student count.
    assert "student_data" not in state
    sources = state["submission_sources"]
    assert len(sources) == student_count
    assert sorted(source["answer_required_diagnostic_count"] for source in sources) == [0] * (student_count - 1) + [2]
    conflict = next(source for source in sources if source["answer_required_diagnostic_count"])
    assert len(conflict["answer_diagnostics"]) == 2
    assert all("answer_assignment_conflict" in answer["flag"] for answer in conflict["answer_diagnostics"])
    registry.provider.ainvoke.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("container", [False, True])
async def test_manual_source_diagnostics_follow_new_conflicts_and_repairs_without_full_answer_reads(monkeypatch, container):
    from backend.services.submission_source_state import load_source_states
    from backend.db import source_outcome_repository

    owner, task = _seed_task(with_question=True)
    add_reviewed_question(task, teacher_id=owner, q_id="q2", order_index=1,
        number="2", type="concept", stem="Second question", criterion="Correct answer: 10 points", max_score=10)
    registry = _registry(monkeypatch, lambda _: pytest.fail("Manual handoff called a model"))
    filename, content, content_type = "S1_Alice.txt", b"saved original", "text/plain"
    if container:
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w"):
            pass  # A real empty archive cannot establish a student count.
        filename, content, content_type = "answers.zip", archive.getvalue(), "application/zip"
    args = dict(task_id=task, owner_id=owner)
    queued = task_facade.queue_task_submission_parsing(**args, filename=filename, content=content,
        content_type=content_type, registry=registry)
    stop_task_run(**args, job_id=queued["job_id"],
        expected_revision=workflow_repository.get_workflow(task, owner_id=owner).workflow_revision)
    paused = task_facade.get_task(**args)
    await continue_manually(**args, job_id=queued["job_id"], expected_revision=paused["workflow_revision"])
    manual = task_facade.get_task(**args)
    if container:
        assert manual["manual_student_count_required"]
        await continue_manually(**args, job_id=manual["parse_job_id"],
            expected_revision=manual["workflow_revision"], student_count=2)
        manual = task_facade.get_task(**args)
    for index, display_id in enumerate(manual["student_data"]):
        for q_id in ("q1", "q2"):
            current = task_facade.get_task(**args)
            task_facade.update_student_answer(**args, display_student_id=display_id, q_id=q_id,
                patch={"content": "Teacher transcribed answer " * 1000, "review_status": "confirmed"},
                expected_revision=current["workflow_revision"])
        current = task_facade.get_task(**args)
        task_facade.update_student_identity(**args, current_display_id=display_id,
            new_display_id=f"S{index+1}", new_display_name=f"Synthetic learner {index+1}",
            expected_revision=current["workflow_revision"])
    clean = task_facade.get_task(**args)
    assert clean["submission_sources"][0]["resolution_status"] == "manual_resolved"
    assert clean["submission_sources"][0]["answer_required_diagnostic_count"] == 0
    assert task_facade.grading_readiness(**args)["ready"]
    operation = workflow_repository.get_operation(clean["parse_job_id"], owner_id=owner)
    history = load_source_states(operation)
    outcomes = source_outcome_repository.list_source_results(operation_id=operation.id, owner_id=owner, attempt=operation.attempt)
    original = next(answer for answer in clean["student_data"]["S1"]["stu_ans"] if answer["q_id"] == "q1")
    task_facade.update_student_answer(**args, display_student_id="S1", q_id="q1",
        patch={"answer_id": original["answer_id"], "target_q_id": "q2"}, expected_revision=clean["workflow_revision"])
    conflicted = task_facade.get_task(**args)
    source = conflicted["submission_sources"][0]
    print(f"manual container={container}: status={source['resolution_status']}, required={source['answer_required_diagnostic_count']}")
    assert source["answer_required_diagnostic_count"] == source["answer_diagnostic_count"] == 2
    assert source["resolution_status"] == source["status"] == "manual_review"
    assert len(source["answer_diagnostics"]) == 2
    assert all("answer_assignment_conflict" in answer["flag"] for answer in source["answer_diagnostics"])
    assert "submission_recognition_blocked" in task_facade.grading_readiness(**args)["blocking_issues"]
    with monkeypatch.context() as scope:
        scope.setattr(task_facade, "_serialize_student_data", lambda *a, **kw: pytest.fail("Manual poll loaded complete answers"))
        assert task_facade.task_state(**args)["submission_sources"] == conflicted["submission_sources"]
    task_facade.update_student_answer(**args, display_student_id="S1", q_id="q2",
        patch={"answer_id": original["answer_id"], "target_q_id": "q1"}, expected_revision=conflicted["workflow_revision"])
    repaired = task_facade.get_task(**args)
    with monkeypatch.context() as scope:
        scope.setattr(task_facade, "_serialize_student_data", lambda *a, **kw: pytest.fail("Manual poll loaded complete answers"))
        assert task_facade.task_state(**args)["submission_sources"] == repaired["submission_sources"]
    assert repaired["submission_sources"][0]["resolution_status"] == "manual_resolved"
    assert repaired["submission_sources"][0]["answer_required_diagnostic_count"] == 0
    assert task_facade.grading_readiness(**args)["ready"]
    assert load_source_states(workflow_repository.get_operation(operation.id, owner_id=owner)) == history
    assert source_outcome_repository.list_source_results(operation_id=operation.id, owner_id=owner, attempt=operation.attempt) == outcomes
    registry.provider.ainvoke.assert_not_awaited()
