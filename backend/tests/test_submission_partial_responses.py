"""Independent fields survive malformed siblings, repairs and manual handoff."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from backend.agents.ingest_agent import _IndependentStudentSubmission, _independent_submission_payload
from backend.api import tasks
from backend.db import assignment_repository, file_repository, workflow_repository
from backend.domain.errors import ValidationError
from backend.services import task_facade
from backend.services.submission_manual_review import continue_manually
from backend.services.task_execution import stop_task_run
from backend.tests.test_submission_incremental import _claim, _questions, _registry
from backend.tests.test_task_background_workflows import _BackgroundTasks, _seed_task


def _answer(q_id, content, **overrides):
    return dict(q_id=q_id, number=q_id[1:], type="short", content=content, flag=[], **overrides)


def _response(answers, **identity):
    return SimpleNamespace(content=json.dumps(dict(stu_id="S1", stu_name="Student One", stu_ans=answers) | identity))


def _setup():
    owner, task = _seed_task()
    for index in (1, 2):
        assignment_repository.add_question(task, teacher_id=owner, q_id=f"q{index}", order_index=index - 1,
            number=str(index), type="short", stem=f"Question {index}", criterion="Award 10 points", max_score=10)
    return owner, task


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", [False, True])
@pytest.mark.parametrize("bad_field", ["content", "type"])
async def test_unlabelled_response_persists_valid_siblings_before_repair_or_manual(monkeypatch, stop, bad_field):
    owner, task = _setup()
    calls, queued = [], None

    async def invoke(messages):
        requested = [q["q_id"] for q in _questions(messages)]
        calls.append(requested)
        if len(calls) == 1:
            broken = _answer("q2", "Original second answer")
            broken[bad_field] = {"invalid": "field"}
            return _response([_answer("q1", "Original first answer"), broken])
        assert requested == ["q2"]
        artifacts = [file for file in file_repository.list_files(owner_id=owner, assignment_id=task)
                     if file.kind == "submission_source_parse_v1"]
        assert len(artifacts) == 1  # Real fenced object + DB row, before the repair call.
        saved = task_facade._load_submission_results(artifacts[0])[0]
        answers = {answer["q_id"]: answer for answer in saved.student["stu_ans"]}
        assert answers["q1"]["content"] == "Original first answer"
        assert not answers["q1"]["flag"]
        assert "recognition_manual_pending" in answers["q2"]["flag"]
        if bad_field == "type":
            assert answers["q2"]["content"] == "Original second answer"
        if stop:
            raise asyncio.CancelledError()
        # Even an unsolicited rewrite of a good sibling/field cannot replace it.
        return _response([_answer("q1", "Wrong rewritten first answer"), _answer("q2", "Repaired second answer")])

    registry = _registry(monkeypatch, invoke)
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename="S1.txt", content=b"Answers without any explicit question headings.",
        content_type="text/plain", registry=registry)
    runner = task_facade.run_durable_submission_recognition(_claim(owner, queued["job_id"]))
    if stop:
        with pytest.raises(asyncio.CancelledError):
            await runner
        workflow = workflow_repository.get_workflow(task, owner_id=owner)
        stop_task_run(task_id=task, owner_id=owner, job_id=queued["job_id"], expected_revision=workflow.workflow_revision)
        before = task_facade.get_task(task_id=task, owner_id=owner)
        await continue_manually(task_id=task, owner_id=owner, job_id=queued["job_id"],
                                expected_revision=before["workflow_revision"])
    else:
        await runner
    after = task_facade.get_task(task_id=task, owner_id=owner)
    assert after["status"] == "submissions_ready"
    answers = {answer["q_id"]: answer for answer in after["student_data"]["S1"]["stu_ans"]}
    assert answers["q1"]["content"] == "Original first answer" and not answers["q1"]["flag"]
    assert calls == [["q1", "q2"], ["q2"]]
    assert answers["q2"]["content"] == ("Original second answer" if bad_field == "type"
        else "" if stop else "Repaired second answer")
    assert ("recognition_manual_pending" in answers["q2"]["flag"]) is stop
    if stop:
        if bad_field == "content":
            with pytest.raises(ValidationError):
                task_facade.update_student_answer(task_id=task, owner_id=owner, display_student_id="S1", q_id="q2",
                    patch={"review_status": "confirmed"}, expected_revision=after["workflow_revision"])
        else:
            assert "recognition_metadata_review" in answers["q2"]["flag"]
        repaired = task_facade.update_student_answer(task_id=task, owner_id=owner, display_student_id="S1", q_id="q2",
            patch={"content": "Teacher supplied corrected answer", "review_status": "confirmed"},
            expected_revision=after["workflow_revision"])
        assert not any(flag.startswith("recognition_invalid_fields:") or flag == "recognition_manual_pending"
                       for flag in repaired["answer"]["flag"])


@pytest.mark.asyncio
async def test_partial_repairs_are_bounded_and_keep_valid_identity_fields(monkeypatch):
    owner, task = _setup()
    calls = []
    async def invoke(messages):
        calls.append([q["q_id"] for q in _questions(messages)])
        return _response([_answer("q1", "Verified first answer"), _answer("q2", {"invalid": True})],
                         stu_id="x" * 161, stu_name="Verified student name")
    registry = _registry(monkeypatch, invoke)
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename="S1.txt", content=b"No heading or safe boundary.", content_type="text/plain", registry=registry)
    await task_facade.run_durable_submission_recognition(_claim(owner, queued["job_id"]))
    before = task_facade.get_task(task_id=task, owner_id=owner)
    assert before["status"] == "error"
    await continue_manually(task_id=task, owner_id=owner, job_id=queued["job_id"],
                            expected_revision=before["workflow_revision"])
    after = task_facade.get_task(task_id=task, owner_id=owner)
    student = after["student_data"]["S1"]
    assert student["stu_name"] == "Verified student name" and student["identity_status"] == "needs_review"
    answers = {answer["q_id"]: answer for answer in student["stu_ans"]}
    assert answers["q1"]["content"] == "Verified first answer", student
    assert "recognition_manual_pending" in answers["q2"]["flag"]
    assert calls == [["q1", "q2"], ["q2"], ["q2"]]


def test_oversized_field_preserves_bounded_siblings_and_safe_fields():
    source = SimpleNamespace(text="", filename="S1.txt")
    problems = {q: {} for q in ("q1", "q2")}
    parsed = _IndependentStudentSubmission.model_validate(dict(stu_id="S1", stu_name="Name",
        stu_ans=[_answer("q1", "Good answer"), _answer("q2", "x" * 500_001)]))
    payload, _ = _independent_submission_payload(parsed, source, problems, set(problems))
    assert payload["stu_ans"][0]["content"] == "Good answer"
    assert payload["stu_ans"][1]["content"] == "" and payload["stu_ans"][1]["type"] == "short"
    assert payload["recognition"]["repair_question_ids"] == ["q2"]


@pytest.mark.parametrize("raw_answers", [{"bad": "container"}, [None] * 1001])
def test_invalid_answer_container_preserves_valid_identity_without_claiming_blank_answers(raw_answers):
    source = SimpleNamespace(text="", filename="S1.txt")
    parsed = _IndependentStudentSubmission.model_validate(dict(stu_id="S1", stu_name="Verified name", stu_ans=raw_answers))
    payload, _ = _independent_submission_payload(parsed, source, {"q1": {}}, {"q1"})
    assert payload["stu_id"] == "S1" and payload["stu_name"] == "Verified name"
    assert payload["stu_ans"][0]["content"] == ""
    assert payload["stu_ans"][0]["flag"] == ["recognition_manual_pending"]
    assert payload["recognition"]["repair_question_ids"] == ["q1"]


@pytest.mark.asyncio
async def test_explicit_retry_repairs_only_invalid_fields_and_keeps_original_identity(monkeypatch):
    owner, task = _setup()
    calls, repair = [], False
    async def invoke(messages):
        calls.append([q["q_id"] for q in _questions(messages)])
        if repair:
            assert calls[-1] == ["q2"]
            return _response([_answer("q2", "Unwanted rewrite")], stu_id="S2", stu_name="Wrong identity")
        broken = _answer("q2", "Keep second answer")
        broken["type"] = {"invalid": True}
        return _response([_answer("q1", "Keep first answer"), broken])
    registry = _registry(monkeypatch, invoke)
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename="S1.txt", content=b"No headings.", content_type="text/plain", registry=registry)
    await task_facade.run_durable_submission_recognition(_claim(owner, queued["job_id"]))
    repair = True
    before = task_facade.get_task(task_id=task, owner_id=owner)
    retry = await tasks.retry_submission_recognition_endpoint(task_id=task, job_id=queued["job_id"],
        request=tasks.RetrySubmissionRecognitionRequest(expected_workflow_revision=before["workflow_revision"]),
        background_tasks=_BackgroundTasks(), current=SimpleNamespace(id=owner), registry=registry)
    await task_facade.run_durable_submission_recognition(_claim(owner, retry["job_id"]))
    after = task_facade.get_task(task_id=task, owner_id=owner)
    student = after["student_data"]["S1"]
    assert student["stu_name"] == "Student One"
    answers = {answer["q_id"]: answer for answer in student["stu_ans"]}
    assert answers["q1"]["content"] == "Keep first answer"
    assert answers["q2"]["content"] == "Keep second answer"
    assert not answers["q2"]["flag"]
    assert calls == [["q1", "q2"], ["q2"], ["q2"], ["q2"]]


@pytest.mark.asyncio
async def test_unknown_repair_submission_never_replays_and_manual_keeps_valid_sibling(monkeypatch):
    from backend.llm.providers import ProviderRequestError
    from backend.llm.request_timeouts import current_response_scope
    owner, task = _setup()
    calls = []
    async def invoke(messages):
        calls.append([q["q_id"] for q in _questions(messages)])
        async def response():
            if len(calls) > 1:
                raise ProviderRequestError("provider_upstream_unavailable", status_code=503)
            return _response([_answer("q1", "Saved first answer"), _answer("q2", {"invalid": True})])
        return await current_response_scope().run_response(response)
    registry = _registry(monkeypatch, invoke)
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename="S1.txt", content=b"No headings.", content_type="text/plain", registry=registry)
    await task_facade.run_durable_submission_recognition(_claim(owner, queued["job_id"]))
    stopped = workflow_repository.get_operation(queued["job_id"], owner_id=owner)
    assert stopped.error_code == "provider_submit_uncertain"
    before = task_facade.get_task(task_id=task, owner_id=owner)
    await continue_manually(task_id=task, owner_id=owner, job_id=queued["job_id"],
                            expected_revision=before["workflow_revision"])
    after = task_facade.get_task(task_id=task, owner_id=owner)
    answers = {answer["q_id"]: answer for answer in after["student_data"]["S1"]["stu_ans"]}
    assert answers["q1"]["content"] == "Saved first answer" and not answers["q1"]["flag"]
    assert "recognition_manual_pending" in answers["q2"]["flag"]
    assert calls == [["q1", "q2"], ["q2"]]


def test_repaired_fields_cannot_exceed_the_total_submission_bound():
    from backend.agents.ingest_agent import SubmissionSourceParseResult
    from backend.models import StudentSubmission
    from backend.services.submission_incremental import merge_parse_repair
    source = SimpleNamespace(text="", filename="S1.txt")
    problems = {q: {} for q in ("q1", "q2")}
    def parsed(answers, targets):
        payload, _ = _independent_submission_payload(_IndependentStudentSubmission.model_validate(
            dict(stu_id="S1", stu_name="Name", stu_ans=answers)), source, problems, targets)
        return SubmissionSourceParseResult("source", "file", "S1.txt", "parse_failed", payload,
            "S1", len(answers), (), "submission_parse_invalid", "structured_parse", True)
    before = parsed([_answer("q1", "a" * 500_000), _answer("q2", {})], set(problems))
    incoming = parsed([_answer("q2", "b" * 500_000)], {"q2"})
    merged = merge_parse_repair(before, incoming, set(problems))
    StudentSubmission.model_validate(merged.student)
    assert merged.student["stu_ans"][0]["content"] == "a" * 500_000
    assert merged.student["stu_ans"][1]["content"] == ""
    assert merged.student["recognition"]["repair_question_ids"] == ["q2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["number", "type", "flag"])
@pytest.mark.parametrize("stop", [False, True])
async def test_metadata_only_failure_confirms_original_text_without_model_or_fake_edit(monkeypatch, field, stop):
    owner, task = _setup()
    count = 0
    def invoke(messages):
        nonlocal count
        count += 1
        if stop and count == 2:
            raise asyncio.CancelledError()
        broken = _answer("q2", "The original answer is correct.")
        broken[field] = {"invalid": True}
        return _response([_answer("q1", "Verified first answer"), broken])
    registry = _registry(monkeypatch, invoke)
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename="S1.txt", content=b"Answers without explicit headings", content_type="text/plain", registry=registry)
    runner = task_facade.run_durable_submission_recognition(_claim(owner, queued["job_id"]))
    if stop:
        with pytest.raises(asyncio.CancelledError):
            await runner
        workflow = workflow_repository.get_workflow(task, owner_id=owner)
        stop_task_run(task_id=task, owner_id=owner, job_id=queued["job_id"], expected_revision=workflow.workflow_revision)
    else:
        await runner
    before = task_facade.get_task(task_id=task, owner_id=owner)
    calls = registry.provider.ainvoke.await_count
    await continue_manually(task_id=task, owner_id=owner, job_id=queued["job_id"], expected_revision=before["workflow_revision"])
    state = task_facade.get_task(task_id=task, owner_id=owner)
    answer = next(a for a in state["student_data"]["S1"]["stu_ans"] if a["q_id"] == "q2")
    assert "recognition_metadata_review" in answer["flag"]
    result = task_facade.update_student_answer(task_id=task, owner_id=owner, display_student_id="S1", q_id="q2",
        patch={"content": answer["content"], "review_status": "confirmed"}, expected_revision=state["workflow_revision"])
    assert result["answer"]["content"] == "The original answer is correct."
    assert result["answer"]["number"] == "2" and result["answer"]["type"] == "short"
    assert result["answer"]["review_status"] == "confirmed"
    assert not task_facade._answer_recognition_failures(result["answer"]["flag"])
    assert "recognition_metadata_review" not in result["answer"]["flag"]
    assert registry.provider.ainvoke.await_count == calls
    stored = {a["q_id"]: a for a in task_facade.get_task(task_id=task, owner_id=owner)["student_data"]["S1"]["stu_ans"]}
    assert stored["q1"]["content"] == "Verified first answer"
    assert stored["q2"]["content"] == "The original answer is correct."


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["content", "pages", "repeated", "uncertain"])
async def test_metadata_allowance_cannot_hide_content_or_source_failure(monkeypatch, failure):
    owner, task = _setup()
    count = 0
    def invoke(messages):
        nonlocal count
        count += 1
        if failure == "uncertain" and count == 2:
            from backend.domain.errors import RecognitionError
            raise RecognitionError(code="provider_submit_uncertain")
        broken = _answer("q2", "Saved fragment")
        broken["type"] = {"invalid": True}
        # Even model/client supplied allowance markers cannot waive real evidence.
        broken["flag"] = ["recognition_metadata_review"]
        if failure == "content":
            broken["content"] = {"invalid": True}
        elif failure == "pages":
            broken["flag"].append("recognition_failed_pages:2")
        elif failure == "repeated":
            broken["flag"].append("recognition_failed")
        return _response([_answer("q1", "Verified first answer"), broken])
    registry = _registry(monkeypatch, invoke)
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner, filename="S1.txt",
        content=b"No explicit heading", content_type="text/plain", registry=registry)
    await task_facade.run_durable_submission_recognition(_claim(owner, queued["job_id"]))
    before = task_facade.get_task(task_id=task, owner_id=owner)
    calls = registry.provider.ainvoke.await_count
    await continue_manually(task_id=task, owner_id=owner, job_id=queued["job_id"], expected_revision=before["workflow_revision"])
    state = task_facade.get_task(task_id=task, owner_id=owner)
    answer = next(a for a in state["student_data"]["S1"]["stu_ans"] if a["q_id"] == "q2")
    assert "recognition_metadata_review" not in answer["flag"]
    with pytest.raises(ValidationError, match="Retry or correct"):
        task_facade.update_student_answer(task_id=task, owner_id=owner, display_student_id="S1", q_id="q2",
            patch={"content": answer["content"], "flag": ["recognition_metadata_review"], "review_status": "confirmed"},
            expected_revision=state["workflow_revision"])
    assert registry.provider.ainvoke.await_count == calls
