from __future__ import annotations

import io
import zipfile

import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers

from backend.api import tasks
from backend.agents.ingest_agent import SubmissionSourceParseResult
from backend.db import file_repository, source_outcome_repository, workflow_repository
from backend.services import task_facade
from backend.services.workflow_worker import LeasedOperation
from backend.storage import get_storage
from backend.tests.test_task_background_workflows import _BackgroundTasks, _Registry, _seed_task


def _archive_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("student.txt", "answer")
    return buffer.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize("started", [False, True])
async def test_manual_handoff_preserves_saved_answers_and_queues_every_unfinished_source(monkeypatch, started):
    import asyncio
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from backend.domain.errors import DomainError
    from backend.services.submission_manual_review import continue_manually
    from backend.services.task_execution import stop_task_run

    owner, task = _seed_task(with_question=True)
    registry = _Registry()
    calls = []
    async def invoke(messages):
        calls.append(messages[-1].content)
        if len(calls) > 1:
            raise asyncio.CancelledError()
        return SimpleNamespace(content=json.dumps({"stu_id": "S1", "stu_name": "Synthetic one",
            "stu_ans": [{"q_id": "q1", "number": "1", "type": "short", "content": "saved wrong answer -2", "flag": []}]}))
    provider = SimpleNamespace(provider_id="test-provider", supports_vision=False, ainvoke=AsyncMock(side_effect=invoke))
    registry.provider = provider
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _owner: registry)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("S1.txt", "first answer")
        archive.writestr("S2.txt", "second answer")
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename="answers.zip", content=buffer.getvalue(), content_type="application/zip", registry=registry)
    if started:
        row = workflow_repository.claim_operation(queued["job_id"], owner_id=owner, worker_id="test", lease_seconds=60)
        with pytest.raises(asyncio.CancelledError):
            await task_facade.run_durable_submission_recognition(LeasedOperation(row, worker_id="test", lease_seconds=60))
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    stop_task_run(task_id=task, owner_id=owner, job_id=queued["job_id"], expected_revision=workflow.workflow_revision)
    paused = task_facade.get_task(task_id=task, owner_id=owner)
    origin = workflow_repository.get_operation(queued["job_id"], owner_id=owner)
    saved_calls = list(calls)
    with pytest.raises(DomainError):
        await continue_manually(task_id=task, owner_id=owner, job_id=origin.id, expected_revision=paused["workflow_revision"] + 1)
    if not started:
        publish = task_facade._commit_imported_submissions
        def fail_publish(**_kwargs):
            raise OSError("synthetic local persistence interruption")
        monkeypatch.setattr(task_facade, "_commit_imported_submissions", fail_publish)
        with pytest.raises(OSError):
            await continue_manually(task_id=task, owner_id=owner, job_id=origin.id, expected_revision=paused["workflow_revision"])
        assert not task_facade.get_task(task_id=task, owner_id=owner)["student_data"]
        monkeypatch.setattr(task_facade, "_commit_imported_submissions", publish)
    result = await continue_manually(task_id=task, owner_id=owner, job_id=origin.id,
                                     expected_revision=paused["workflow_revision"])
    assert result["status"] == "ready_for_review" and calls == saved_calls
    after = task_facade.get_task(task_id=task, owner_id=owner)
    assert after["status"] == "submissions_ready"
    students = list(after["student_data"].values())
    assert len(students) == 2 and len(after["submission_sources"]) == 2
    answers = [student["stu_ans"][0] for student in students]
    assert sum(answer["content"] == "saved wrong answer -2" for answer in answers) == int(started)
    pending = [student for student in students if "recognition_manual_pending" in student["stu_ans"][0]["flag"]]
    assert len(pending) == 2 - int(started)
    assert all("recognition_paused" in student["stu_ans"][0]["flag"] for student in pending)
    assert all(student["identity_status"] == "needs_review" and student["stu_name"] == "[Unknown Student]" for student in pending)
    assert all(source["recognition_reason_code"] == "operation_cancelled" for source in after["submission_sources"] if source["status"] == "manual_review")
    assert "submission_recognition_blocked" in task_facade.grading_readiness(task_id=task, owner_id=owner)["blocking_issues"]
    target = pending[0]
    with pytest.raises(DomainError) as blocked:
        task_facade.update_student_answer(task_id=task, owner_id=owner, display_student_id=target["stu_id"],
            q_id="q1", patch={"review_status": "confirmed"}, expected_revision=after["workflow_revision"])
    assert blocked.value.code == "answer_review_blocked"
    task_facade.update_student_answer(task_id=task, owner_id=owner, display_student_id=target["stu_id"],
        q_id="q1", patch={"content": "", "flag": ["manual_blank_confirmed"], "review_status": "confirmed"},
        expected_revision=after["workflow_revision"])
    refreshed = task_facade.get_task(task_id=task, owner_id=owner)
    repaired = refreshed["student_data"][target["stu_id"]]["stu_ans"][0]
    assert repaired["content"] == "" and repaired["review_status"] == "confirmed"
    assert "recognition_failed" not in repaired["flag"]
    assert "recognition_manual_pending" not in repaired["flag"]
    for index, student in enumerate(pending):
        current = task_facade.get_task(task_id=task, owner_id=owner)
        if student is not target:
            task_facade.update_student_answer(task_id=task, owner_id=owner, display_student_id=student["stu_id"],
                q_id="q1", patch={"content": "Teacher transcribed answer", "review_status": "confirmed"},
                expected_revision=current["workflow_revision"])
            current = task_facade.get_task(task_id=task, owner_id=owner)
        task_facade.update_student_identity(task_id=task, owner_id=owner, current_display_id=student["stu_id"],
            new_display_id=f"Confirmed-{index}", new_display_name=f"Synthetic learner {index}", expected_revision=current["workflow_revision"])
    refreshed = task_facade.get_task(task_id=task, owner_id=owner)
    assert not refreshed["submission_source_summary"].get("manual_review")
    assert all(source["status"] == "parsed" for source in refreshed["submission_sources"])
    readiness = task_facade.grading_readiness(task_id=task, owner_id=owner)
    assert not {"submission_recognition_blocked", "submission_identities_unresolved"}.intersection(readiness["blocking_issues"])
    from backend.services.workflow_inputs import submission_inputs
    assert submission_inputs(task_id=task, owner_id=owner)["input"]["stored_file_id"] == origin.payload["input_file_id"]
    original = workflow_repository.get_operation(origin.id, owner_id=owner)
    assert original.status == "error" and original.error_code == "operation_cancelled"
    assert original.checkpoint == origin.checkpoint and original.artifact_refs == origin.artifact_refs
    with pytest.raises(DomainError):
        await continue_manually(task_id=task, owner_id=owner, job_id=origin.id,
                                expected_revision=refreshed["workflow_revision"])
    assert calls == saved_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode,edited_after_restart,from_retry", [
    ("503", False, False), ("503", True, False), ("stopped", False, False), ("stopped", False, True),
])
async def test_acknowledged_restart_retries_only_failed_page_with_durable_worker(monkeypatch, failure_mode, edited_after_restart, from_retry):
    import asyncio
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import fitz
    from PIL import Image

    from backend.db import assignment_repository
    from backend.llm.providers import ProviderRequestError
    from backend.tests.test_recognition_agent import pdf
    from backend.tests.test_recognition_reader import llm

    owner, task = _seed_task(with_question=True)
    with fitz.open(stream=pdf(["First page answer", "Second page answer"]), filetype="pdf") as original, fitz.open() as scanned:
        for page in original:
            copied = scanned.new_page(width=page.rect.width + 50 * page.number, height=page.rect.height)
            copied.insert_image(copied.rect, stream=page.get_pixmap().tobytes("png"))
        content = scanned.tobytes()
    provider, _engine = llm(text="1. Literal answer, including a mistake.", provider="test-provider")
    provider.provider_id = "test-provider"
    failed_page_images = []

    async def recognize(_prompt, images, **_kwargs):
        # Select page 2 by its distinct aspect ratio rather than call order.
        with Image.open(io.BytesIO(images[0].data)) as image:
            second_page = image.width / image.height > 0.8
        if second_page:
            failed_page_images.append(images[0].data)
            if len(failed_page_images) == 1:
                if failure_mode == "stopped":
                    raise asyncio.CancelledError()
                raise ProviderRequestError("provider_upstream_unavailable", status_code=503)
        return provider.ainvoke_vision.return_value

    provider.ainvoke_vision.side_effect = recognize
    provider.ainvoke = AsyncMock(return_value=SimpleNamespace(content=json.dumps({
        "stu_id": "S1", "stu_name": "Synthetic learner",
        "stu_ans": [{"q_id": "q1", "number": "1", "type": "short",
                     "content": "Literal answer, including a mistake.", "flag": []}],
    })))
    registry = _Registry()
    registry.provider = provider
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _owner: registry)

    async def finish(job):
        row = workflow_repository.claim_operation(job, owner_id=owner, worker_id="test", lease_seconds=60)
        await task_facade.run_durable_submission_recognition(LeasedOperation(row, worker_id="test", lease_seconds=60))

    first = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename="student.pdf", content=content, content_type="application/pdf", registry=registry)
    if failure_mode == "stopped":
        from backend.services.task_execution import stop_task_run
        with pytest.raises(asyncio.CancelledError):
            await finish(first["job_id"])
        workflow = workflow_repository.get_live_workflow(task, owner_id=owner)
        stop_task_run(task_id=task, owner_id=owner, job_id=first["job_id"],
                      expected_revision=workflow.workflow_revision)
    else:
        await finish(first["job_id"])
    failed = workflow_repository.get_operation(first["job_id"], owner_id=owner)
    assert failed.error_code == ("operation_cancelled" if failure_mode == "stopped" else "provider_submit_uncertain")
    before = task_facade.get_task(task_id=task, owner_id=owner)
    assert before["status"] == ("submissions_paused" if failure_mode == "stopped" else "error")
    assert before["student_data"] == {}
    if failure_mode != "stopped":
        assert before["submission_sources"][0]["page_diagnostics"]["failed_pages"] == [2]
    assert provider.ainvoke_vision.await_count == 2
    provider.ainvoke.assert_not_awaited()

    async def restart(acknowledge):
        if from_retry:
            return await tasks.retry_submission_recognition_endpoint(task_id=task, job_id=failed.id,
                request=tasks.RetrySubmissionRecognitionRequest(expected_workflow_revision=before["workflow_revision"],
                    acknowledge_possible_duplicate_call=acknowledge), background_tasks=_BackgroundTasks(),
                current=SimpleNamespace(id=owner), registry=registry)
        return await tasks.parse_submissions_endpoint(task_id=task, file=None,
            stored_file_id=failed.payload["input_file_id"], identity_mode="filename", roster_file=None,
            recognition_provider_id="test-provider", replace_confirmed=False,
            expected_workflow_revision=before["workflow_revision"],
            acknowledge_possible_duplicate_call=acknowledge, background_tasks=_BackgroundTasks(),
            current=SimpleNamespace(id=owner), registry=registry)

    if failure_mode != "stopped":
        refused = await restart(False)
        assert refused.status_code == 409 and "provider_submit_uncertain" in refused.body.decode()
    restarted = await restart(True)
    assert isinstance(restarted, dict), getattr(restarted, "body", restarted)
    assert restarted["status"] == "started"
    if failure_mode != "stopped":
        assert restarted["job_id"] != failed.id
    # Replaying the same HTTP form cannot dispatch another paid operation.
    duplicate = await restart(True)
    assert duplicate.status_code == (422 if from_retry else 409)
    assert "stale_revision" in duplicate.body.decode()
    assert provider.ainvoke_vision.await_count == 2

    if edited_after_restart:
        question = assignment_repository.list_questions(task, teacher_id=owner)[0]
        assignment_repository.update_question(task, teacher_id=owner, q_id="q1",
            expected_version=question.version, stem="Teacher's concurrent correction")
    await finish(restarted["job_id"])
    operation = workflow_repository.get_operation(restarted["job_id"], owner_id=owner)
    after = task_facade.get_task(task_id=task, owner_id=owner)
    if edited_after_restart:
        assert operation.error_code == "recognition_plan_changed"
        assert provider.ainvoke_vision.await_count == 2
        provider.ainvoke.assert_not_awaited()
        assert assignment_repository.list_questions(task, teacher_id=owner)[0].stem == "Teacher's concurrent correction"
        assert after["student_data"] == {}
    else:
        assert after["status"] == "submissions_ready"
        assert after["submission_source_summary"]["failed"] == 0
        assert provider.ainvoke_vision.await_count == 3
        assert provider.ainvoke.await_count == 1
        assert len(failed_page_images) == 2 and failed_page_images[0] == failed_page_images[1]
        assert provider.ainvoke_vision.await_args_list[2].args[1][0].data == failed_page_images[0]
    # Restarting never rewrites the old failure or its page evidence.
    if restarted["job_id"] != failed.id:
        original = workflow_repository.get_operation(failed.id, owner_id=owner)
        assert original.status == "error" and original.error_code == failed.error_code
        assert original.checkpoint == failed.checkpoint


@pytest.mark.asyncio
@pytest.mark.parametrize("earlier_failure", [False, True])
async def test_page_failure_details_survive_worker_restart_and_retry_is_explicit(monkeypatch, earlier_failure):
    from types import SimpleNamespace
    from backend.domain.errors import RecognitionError
    import json

    owner, task = _seed_task(with_question=True)
    diagnostics = dict(total_pages=2, processed_pages=[1], failed_pages=[2], unprocessed_pages=[],
        page_errors=[dict(page=2, error_code="provider_upstream_unavailable", status_code=503)])
    reads = []

    async def read(**kw):
        if kw["filename"] == "earlier.txt":
            raise RecognitionError("provider_rate_limited")
        reads.append(kw["retry_key"])
        raise RecognitionError("provider_submit_uncertain", failed_pages=[2], processed_pages=[1],
                               page_diagnostics=diagnostics)

    monkeypatch.setattr(task_facade, "read_question_source", read)
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _owner: _Registry())
    content, filename, content_type = b"original", "student.txt", "text/plain"
    if earlier_failure:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("earlier.txt", "another file")
            archive.writestr("student.txt", "original")
        content, filename, content_type = buffer.getvalue(), "answers.zip", "application/zip"
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename=filename, content=content, content_type=content_type, registry=_Registry())

    async def finish(job):
        row = workflow_repository.claim_operation(job, owner_id=owner, worker_id="test", lease_seconds=60)
        await task_facade.run_durable_submission_recognition(LeasedOperation(row, worker_id="test", lease_seconds=60))

    # Real source preparation and structure parser must stop before any LLM
    # structure dispatch; this registry deliberately has no ainvoke method.
    await finish(queued["job_id"])
    before = task_facade.get_task(task_id=task, owner_id=owner)
    assert before["status"] == "error" and before["student_data"] == {}
    assert before["submission_source_summary"]["failed"] == (2 if earlier_failure else 1)
    row = next(row for row in before["submission_sources"] if row["file_name"] == "student.txt")
    assert row["status"] == "failed" and row["matched_answer_count"] == 0
    assert row["page_diagnostics"] == diagnostics
    assert row["reason_code"] == "provider_submit_uncertain"
    persisted = workflow_repository.get_operation(queued["job_id"], owner_id=owner)
    assert persisted.error_code == "provider_submit_uncertain"
    assert persisted.checkpoint["source_page_diagnostics"][row["file_id"]] == diagnostics

    args = dict(task_id=task, job_id=queued["job_id"], background_tasks=_BackgroundTasks(),
                current=SimpleNamespace(id=owner), registry=_Registry())
    denied = await tasks.retry_submission_recognition_endpoint(**args,
        request=tasks.RetrySubmissionRecognitionRequest(expected_workflow_revision=before["workflow_revision"]))
    assert denied.status_code == 409
    assert json.loads(denied.body)["error"]["code"] == "provider_submit_uncertain"
    assert reads == [None]
    retried = await tasks.retry_submission_recognition_endpoint(**args,
        request=tasks.RetrySubmissionRecognitionRequest(expected_workflow_revision=before["workflow_revision"],
                                                       acknowledge_possible_duplicate_call=True))
    assert isinstance(retried, dict), json.loads(retried.body)
    await finish(retried["job_id"])
    assert reads == [None, f"{retried['job_id']}:1"]
    after = task_facade.get_task(task_id=task, owner_id=owner)
    assert after["status"] == "error"  # another failure still cannot escape into review
    assert next(row for row in after["submission_sources"] if row["file_name"] == "student.txt")["page_diagnostics"] == diagnostics


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_partial", [False, True])
async def test_partial_batch_stays_failed_and_explicit_retry_calls_only_failed_source(monkeypatch, legacy_partial):
    from types import SimpleNamespace
    owner, task = _seed_task(with_question=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("one.txt", "first answer")
        archive.writestr("two.txt", "second answer")
    reads, parsed = [], []
    round_number = 0

    async def read(**kw):
        reads.append(kw["filename"])
        recognition = ({"confidence": "low", "coverage": {"processed_pages": [1], "failed_pages": [2]}}
                       if legacy_partial and round_number == 0 and kw["filename"] == "one.txt" else None)
        return SimpleNamespace(text=kw["content"].decode(), recognition=recognition)

    async def parse(sources, *_args, **_kwargs):
        result = []
        for source in sources:
            parsed.append(source.filename)
            failed = source.filename == "two.txt" and round_number == 0
            student = None if failed else {"stu_id": source.filename, "stu_name": source.filename,
                "stu_ans": [{"q_id": "q1", "content": source.text, "flag": []}],
                "source_id": source.source_id, "stored_file_id": source.stored_file_id,
                "source_filename": source.filename, "identity_status": "matched"}
            result.append(SubmissionSourceParseResult(source.source_id, source.stored_file_id,
                source.filename, "parse_failed" if failed else "parsed", student,
                None if failed else source.filename, 0 if failed else 1, (),
                "provider_rate_limited" if failed else None, "recognition" if failed else None, failed))
        return result

    monkeypatch.setattr(task_facade, "read_question_source", read)
    monkeypatch.setattr(task_facade, "parse_student_answer_sources", parse)
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _owner: _Registry())
    first = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename="answers.zip", content=buf.getvalue(), content_type="application/zip", registry=_Registry())

    async def finish(job):
        row = workflow_repository.claim_operation(job, owner_id=owner, worker_id="test", lease_seconds=60)
        await task_facade.run_durable_submission_recognition(LeasedOperation(row, worker_id="test", lease_seconds=60))

    await finish(first["job_id"])
    snapshot = task_facade.get_task(task_id=task, owner_id=owner)
    assert snapshot["status"] == "error"
    assert snapshot["student_data"] == {}
    assert snapshot["submission_source_summary"]["parsed"] == 1
    assert snapshot["submission_source_summary"]["failed"] == 1
    round_number = 1
    retried = await tasks.retry_submission_recognition_endpoint(task_id=task, job_id=first["job_id"],
        request=tasks.RetrySubmissionRecognitionRequest(expected_workflow_revision=snapshot["workflow_revision"]),
        background_tasks=_BackgroundTasks(), current=SimpleNamespace(id=owner), registry=_Registry())
    await finish(retried["job_id"])
    expected = ["one.txt", "two.txt", "one.txt", "two.txt"] if legacy_partial else ["one.txt", "two.txt", "two.txt"]
    assert reads == parsed == expected
    result = task_facade.get_task(task_id=task, owner_id=owner)
    assert result["status"] == "submissions_ready"
    assert len(result["student_data"]) == 2
    assert result["submission_source_summary"]["failed"] == 0


@pytest.mark.asyncio
async def test_submission_queue_persists_archive_and_has_no_request_task():
    owner_id, task_id = _seed_task(with_question=True)
    background = _BackgroundTasks()
    upload = UploadFile(
        file=io.BytesIO(b"archive-bytes"), filename="answers.zip",
        headers=Headers({"content-type": "application/zip"}),
    )

    response = await tasks.parse_submissions_endpoint(
        task_id=task_id, background_tasks=background, file=upload,
        identity_mode="filename", roster_file=None,
        recognition_provider_id=None, replace_confirmed=False,
        current=type("User", (), {"id": owner_id})(), registry=_Registry(),
    )

    assert response["status"] == "started"
    assert background.calls == []
    operation = workflow_repository.get_operation(response["job_id"], owner_id=owner_id)
    assert operation.status == "pending"
    assert set(operation.payload) == {
        "input_file_id", "source_ids", "base_workflow_revision", "identity_mode",
        "roster_entries", "roster_name", "recognition_provider_id",
        "replace_confirmed",
        "provider_configuration_fingerprint", "question_snapshot",
    }
    sources = source_outcome_repository.list_sources(
        operation_id=operation.id, owner_id=owner_id, attempt=operation.attempt,
    )
    assert sources == []
    stored = file_repository.get_file(
        file_id=operation.payload["input_file_id"], owner_id=owner_id
    )
    assert stored is not None
    assert stored.id in operation.artifact_refs
    with get_storage().open(stored.storage_key) as stream:
        assert stream.read() == b"archive-bytes"


@pytest.mark.asyncio
@pytest.mark.parametrize("stopped", [False, True])
async def test_submission_retry_republishes_only_for_durable_worker(stopped):
    owner_id, task_id = _seed_task(with_question=True)
    queued = task_facade.queue_task_submission_parsing(
        task_id=task_id,
        owner_id=owner_id,
        filename="student.txt",
        content=b"answer",
        content_type="text/plain",
        registry=_Registry(),
    )
    operation = workflow_repository.get_operation(
        queued["job_id"], owner_id=owner_id
    )
    if stopped:
        from backend.services.task_execution import stop_task_run
        workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
        stop_task_run(task_id=task_id, owner_id=owner_id, job_id=operation.id,
                      expected_revision=workflow.workflow_revision)
        assert task_facade.get_task(task_id=task_id, owner_id=owner_id)["status"] == "submissions_paused"
    else:
        assert task_facade._fail_operation(
            task_id, owner_id, operation.id, operation.attempt, "provider_timeout",
        ) is True
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    background = _BackgroundTasks()

    retried = await tasks.retry_submission_recognition_endpoint(
        task_id=task_id,
        job_id=operation.id,
        request=tasks.RetrySubmissionRecognitionRequest(
            recognition_provider_id=None,
            expected_workflow_revision=workflow.workflow_revision,
        ),
        background_tasks=background,
        current=type("User", (), {"id": owner_id})(),
        registry=_Registry(),
    )

    assert retried["status"] == "started"
    assert retried["reused_original_upload"] is True
    assert background.calls == []
    retry_operation = workflow_repository.get_operation(
        retried["job_id"], owner_id=owner_id
    )
    assert retry_operation.status == "pending"
    assert retry_operation.payload["retry_from"] == {"operation_id": operation.id, "attempt": operation.attempt}
    assert workflow_repository.get_operation(operation.id, owner_id=owner_id).status == "error"
    assert retry_operation.payload["recognition_provider_id"] == "test-provider"


@pytest.mark.asyncio
async def test_fresh_submission_worker_recovers_archive_and_commits_once(monkeypatch):
    owner_id, task_id = _seed_task(with_question=True)
    content = _archive_bytes()
    queued = task_facade.queue_task_submission_parsing(
        task_id=task_id, owner_id=owner_id, filename="answers.zip",
        content=content, content_type="application/zip",
        registry=_Registry(),
    )
    calls = {"parse": 0}

    async def fake_parse(sources, problems, provider, **kwargs):
        calls["parse"] += 1
        source = sources[0]
        student = {
            "stu_id": "student-1", "stu_name": "Student One",
            "stu_ans": [{"q_id": "q1", "content": "answer", "flag": []}],
            "source_filename": source.filename,
            "source_id": source.source_id,
            "stored_file_id": source.stored_file_id,
            "identity_status": "matched",
        }
        return [SubmissionSourceParseResult(
            source_id=source.source_id,
            stored_file_id=source.stored_file_id,
            filename=source.filename,
            status="parsed",
            student=student,
            student_candidate="student-1",
            matched_answer_count=1,
            unknown_question_ids=(),
            stable_error_code=None,
            failure_phase=None,
            retryable=False,
        )]

    monkeypatch.setattr(task_facade, "parse_student_answer_sources", fake_parse)
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _owner: _Registry())

    claimed = workflow_repository.claim_operation(
        queued["job_id"], owner_id=owner_id, worker_id="fresh-worker", lease_seconds=60,
    )
    await task_facade.run_durable_submission_recognition(
        LeasedOperation(claimed, worker_id="fresh-worker", lease_seconds=60)
    )

    operation = workflow_repository.get_operation(queued["job_id"], owner_id=owner_id)
    assert calls == {"parse": 1}
    assert operation.status == "done"
    assert operation.lease_token is None


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_partial", [False, True])
async def test_submission_worker_reuses_result_artifact_without_provider_call(monkeypatch, legacy_partial):
    owner_id, task_id = _seed_task(with_question=True)
    queued = task_facade.queue_task_submission_parsing(
        task_id=task_id, owner_id=owner_id, filename="student.txt",
        content=b"answer", content_type="text/plain",
        registry=_Registry(),
    )
    operation = workflow_repository.get_operation(queued["job_id"], owner_id=owner_id)
    source = source_outcome_repository.list_sources(
        operation_id=operation.id,
        owner_id=owner_id,
        attempt=operation.attempt,
    )[0]
    result = SubmissionSourceParseResult(
        source_id=source.id,
        stored_file_id=source.stored_file_id,
        filename=source.original_name,
        status="parsed",
        student={
            "stu_id": "student-1", "stu_name": "Student One",
            "stu_ans": [{"q_id": "q1", "content": "answer", "flag": []}],
            "source_filename": source.original_name,
            "source_id": source.id,
            "stored_file_id": source.stored_file_id,
            "identity_status": "matched",
        },
        student_candidate="student-1",
        matched_answer_count=1,
        unknown_question_ids=(),
        stable_error_code=None,
        failure_phase=None,
        retryable=False,
    )
    if legacy_partial:
        result.student["recognition"] = {"coverage": {"processed_pages": [1], "failed_pages": [2]}}
        result.student["stu_ans"][0]["content"] = ""
        result.student["stu_ans"][0]["flag"] = ["recognition_needs_review", "recognition_failed_pages:2"]
    artifact = file_repository.save_file(
        storage=get_storage(), owner_id=owner_id,
        kind="submission_recognition_result",
        original_name=f"{operation.id}-attempt-{operation.attempt}-parsed.json",
        content=task_facade._serialize_submission_results([result]),
        content_type="application/json", assignment_id=task_id,
    )
    operation = workflow_repository.save_operation_checkpoint(
        operation.id,
        owner_id=owner_id,
        expected_attempt=operation.attempt,
        expected_checkpoint_revision=operation.checkpoint_revision,
        stage="submissions_parsed",
        checkpoint={"parsed_artifact_id": artifact.id},
        artifact_refs=[*operation.artifact_refs, artifact.id],
    )

    async def unexpected(*args, **kwargs):
        raise AssertionError("completed provider stage was repeated")

    monkeypatch.setattr(task_facade, "prepare_submission_sources", unexpected)
    monkeypatch.setattr(task_facade, "parse_student_answer_sources", unexpected)
    claimed = workflow_repository.claim_operation(
        operation.id, owner_id=owner_id, worker_id="fresh-worker", lease_seconds=60,
    )
    await task_facade.run_durable_submission_recognition(
        LeasedOperation(claimed, worker_id="fresh-worker", lease_seconds=60)
    )

    completed = workflow_repository.get_operation(operation.id, owner_id=owner_id)
    outcome = source_outcome_repository.get_outcome(source.id, owner_id=owner_id)
    assert completed.status == ("error" if legacy_partial else "done")
    assert completed.checkpoint["parsed_artifact_id"] == artifact.id
    assert outcome is not None
    assert outcome.artifact_file_id == artifact.id
    if legacy_partial:
        task = task_facade.get_task(task_id=task_id, owner_id=owner_id)
        assert task["submission_source_summary"]["failed"] == 1
        assert task["status"] == "error"


def test_stale_submission_lease_cannot_commit_or_fail_current_attempt():
    owner_id, task_id = _seed_task(with_question=True)
    queued = task_facade.queue_task_submission_parsing(
        task_id=task_id, owner_id=owner_id, filename="student.txt",
        content=b"answer", content_type="text/plain",
        registry=_Registry(),
    )
    old = workflow_repository.claim_operation(
        queued["job_id"], owner_id=owner_id, worker_id="old", lease_seconds=60,
    )
    workflow_repository.release_operation(
        old.id, owner_id=owner_id, worker_id="old", lease_token=old.lease_token,
    )
    current = workflow_repository.claim_operation(
        old.id, owner_id=owner_id, worker_id="current", lease_seconds=60,
    )

    from backend.domain.errors import LeaseLost
    assignment = task_facade.assignment_repository.get_assignment(task_id, actor_id=owner_id)
    with pytest.raises(LeaseLost):
        task_facade._commit_imported_submissions(
            task_id=task_id, owner_id=owner_id, course_id=assignment.course_id,
            students=[], expected_workflow_revision=1,
            operation_id=old.id, expected_operation_attempt=old.attempt,
            expected_lease_token=old.lease_token,
        )
    with pytest.raises(LeaseLost):
        task_facade._fail_operation(
            task_id, owner_id, old.id, old.attempt, "submission_parse_failed",
            expected_lease_token=old.lease_token,
        )
    assert workflow_repository.get_operation(
        old.id, owner_id=owner_id
    ).lease_token == current.lease_token
    source = source_outcome_repository.list_sources(
        operation_id=old.id, owner_id=owner_id, attempt=old.attempt,
    )[0]
    assert source_outcome_repository.get_outcome(source.id, owner_id=owner_id) is None


@pytest.mark.asyncio
async def test_submission_failure_records_fenced_source_outcome(monkeypatch):
    owner_id, task_id = _seed_task(with_question=True)
    queued = task_facade.queue_task_submission_parsing(
        task_id=task_id, owner_id=owner_id, filename="answers.zip",
        content=b"archive-bytes", content_type="application/zip",
        registry=_Registry(),
    )
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _owner: _Registry())
    claimed = workflow_repository.claim_operation(
        queued["job_id"], owner_id=owner_id,
        worker_id="fresh-worker", lease_seconds=60,
    )
    await task_facade.run_durable_submission_recognition(
        LeasedOperation(claimed, worker_id="fresh-worker", lease_seconds=60)
    )

    source = source_outcome_repository.list_sources(
        operation_id=queued["job_id"], owner_id=owner_id, attempt=1,
    )[0]
    outcome = source_outcome_repository.get_outcome(source.id, owner_id=owner_id)
    operation = workflow_repository.get_operation(queued["job_id"], owner_id=owner_id)
    assert operation.status == "error"
    assert outcome is not None
    assert outcome.status == "parse_failed"
    assert outcome.stable_error_code == "submission_source_content_type_mismatch"
    assert outcome.failure_phase == "source_read"
    assert outcome.retryable is False
