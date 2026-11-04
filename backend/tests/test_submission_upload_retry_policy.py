"""Exercise immutable mixed uploads through persisted outcomes and the retry API."""
import json
from types import SimpleNamespace

import pytest

from backend.agents.ingest_agent import SubmissionSourceParseResult
from backend.api import tasks
from backend.db import workflow_repository
from backend.services import task_facade
from backend.services.workflow_worker import LeasedOperation
from backend.tests.test_archive_content_detection import archive_bytes
from backend.tests.test_task_background_workflows import _BackgroundTasks, _Registry, _seed_task


BAD_FILES = [("empty.txt", b""), ("fake.pdf", b"this is ordinary text"), ("unsupported.bin", b"\0\x01\x02binary")]


async def finish(job_id, owner):
    row = workflow_repository.claim_operation(job_id, owner_id=owner, worker_id="retry-policy", lease_seconds=60)
    await task_facade.run_durable_submission_recognition(LeasedOperation(row, worker_id="retry-policy", lease_seconds=60))


async def retry(task, owner, snapshot, *, acknowledge=False):
    return await tasks.retry_submission_recognition_endpoint(
        task_id=task, job_id=snapshot["parse_job_id"],
        request=tasks.RetrySubmissionRecognitionRequest(expected_workflow_revision=snapshot["workflow_revision"],
            acknowledge_possible_duplicate_call=acknowledge),
        background_tasks=_BackgroundTasks(), current=SimpleNamespace(id=owner), registry=_Registry(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("source_field", ["stored_file_id", "reuse_input_from_job_id"])
async def test_configuration_restart_cannot_reopen_immutable_failed_upload(monkeypatch, source_field):
    from backend.services import workflow_inputs

    owner, task = _seed_task(with_question=True)
    body, filename = archive_bytes("zip", {"fake.pdf": b"ordinary text"})
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: _Registry())
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename=filename, content=body, content_type="application/zip", registry=_Registry())
    await finish(queued["job_id"], owner)
    before = task_facade.get_task(task_id=task, owner_id=owner)
    saved = workflow_inputs.submission_inputs(task_id=task, owner_id=owner)["input"]

    def forbidden(*args, **kwargs):
        pytest.fail("Configuration restart must reject immutable input before reading or requeueing")

    with monkeypatch.context() as guard:
        guard.setattr(workflow_inputs, "get_storage", forbidden)
        guard.setattr(task_facade, "queue_task_submission_parsing", forbidden)
        response = await tasks.parse_submissions_endpoint(task_id=task, background_tasks=_BackgroundTasks(),
            file=None, **{source_field: saved["stored_file_id"] if source_field == "stored_file_id" else queued["job_id"]},
            identity_mode="filename", roster_file=None, recognition_provider_id="test-provider",
            replace_confirmed=False, expected_workflow_revision=before["workflow_revision"],
            current=SimpleNamespace(id=owner), registry=_Registry())
    assert response.status_code == 409
    assert json.loads(response.body)["error"]["code"] == "submission_retry_requires_reupload"
    after = task_facade.get_task(task_id=task, owner_id=owner)
    assert {key: after[key] for key in ("workflow_revision", "parse_job_id", "submission_sources")} == {
        key: before[key] for key in ("workflow_revision", "parse_job_id", "submission_sources")}
    # A genuinely selected replacement is a new input, not a blocked replay.
    import io
    from fastapi import UploadFile
    replacement = await tasks.parse_submissions_endpoint(task_id=task, background_tasks=_BackgroundTasks(),
        file=UploadFile(file=io.BytesIO(b"Question 1: corrected answer"), filename="fixed.txt"),
        identity_mode="filename", roster_file=None, recognition_provider_id="test-provider",
        replace_confirmed=False, expected_workflow_revision=after["workflow_revision"],
        current=SimpleNamespace(id=owner), registry=_Registry())
    assert isinstance(replacement, dict) and replacement["status"] == "started"


@pytest.mark.asyncio
@pytest.mark.parametrize("name,content", BAD_FILES)
@pytest.mark.parametrize("nested", [False, True])
async def test_immutable_file_failures_require_reupload_before_original_read_or_queue(monkeypatch, name, content, nested):
    owner, task = _seed_task(with_question=True)
    inner, _ = archive_bytes("zip", {"answer.txt": b"answer"})
    entries = {"inner.zip": inner, name: content} if nested else {name: content}
    body, filename = archive_bytes("zip", entries)
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: _Registry())
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner, filename=filename,
        content=body, content_type="application/zip", registry=_Registry())
    await finish(queued["job_id"], owner)
    before = task_facade.get_task(task_id=task, owner_id=owner)
    assert before["status"] == "error"
    assert len(before["submission_sources"]) == len(entries)
    assert all(source["status"] == "failed" and not source["retryable"] for source in before["submission_sources"])
    assert any(source["failure_phase"] == "source_read" for source in before["submission_sources"])

    def forbidden(*args, **kwargs):
        pytest.fail("Immutable input retry must not read the original or enqueue another operation")
    with monkeypatch.context() as guard:
        guard.setattr(task_facade, "get_storage", forbidden)
        guard.setattr(task_facade, "queue_task_submission_parsing", forbidden)
        response = await retry(task, owner, before)
    assert response.status_code == 409
    assert json.loads(response.body)["error"]["code"] == "submission_retry_requires_reupload"
    after = task_facade.get_task(task_id=task, owner_id=owner)
    for key in ("workflow_revision", "parse_job_id", "submission_sources"):
        assert after[key] == before[key]
    for snapshot in (before, await task_facade.async_task_state(task_id=task, owner_id=owner)):
        assert all(source["requires_reupload"] for source in snapshot["submission_sources"])
    # Correcting an input manually must remain possible even when automatic
    # recognition cannot retry it. This must not dispatch another model call.
    monkeypatch.setattr(task_facade, "_registry_for_owner", forbidden)
    monkeypatch.setattr(task_facade, "parse_student_answer_sources", forbidden)
    manual = await tasks.manually_complete_submission_recognition(task_id=task, job_id=queued["job_id"],
        request=tasks.ManualSubmissionRecognitionRequest(expected_workflow_revision=after["workflow_revision"]),
        current=SimpleNamespace(id=owner))
    assert isinstance(manual, dict) and manual["status"] == "ready_for_review"
    assert task_facade.get_task(task_id=task, owner_id=owner)["status"] == "submissions_ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_code", ["provider_rate_limited", "provider_submit_uncertain", "provider_auth_failed"])
async def test_mixed_upload_recovers_model_failure_keeps_success_and_then_blocks_immutable_remainder(monkeypatch, failure_code):
    owner, task = _seed_task(with_question=True)
    inner, _ = archive_bytes("zip", {"answer.txt": b"answer"})
    body, filename = archive_bytes("zip", {"inner.zip": inner, "empty.txt": b"", "success.txt": b"saved answer", "recover.txt": b"recover answer"})
    calls = []
    round_number = 0

    async def parse(sources, *_args, **_kwargs):
        results = []
        for source in sources:
            if source.pre_error_code:
                results.append(SubmissionSourceParseResult(source.source_id, source.stored_file_id,
                    source.filename, "parse_failed", None, None, 0, (), source.pre_error_code, source.failure_phase, source.retryable))
                continue
            calls.append(source.filename)
            failed = source.filename == "recover.txt" and round_number == 0
            student = None if failed else {"stu_id": source.filename, "stu_name": source.filename,
                "stu_ans": [{"q_id": "q1", "content": source.text, "flag": []}], "identity_status": "matched",
                "source_id": source.source_id, "stored_file_id": source.stored_file_id, "source_filename": source.filename}
            results.append(SubmissionSourceParseResult(source.source_id, source.stored_file_id, source.filename,
                "parse_failed" if failed else "parsed", student, None if failed else source.filename,
                0 if failed else 1, (), failure_code if failed else None, "recognition" if failed else None,
                failed and failure_code == "provider_rate_limited"))
        return results

    monkeypatch.setattr(task_facade, "parse_student_answer_sources", parse)
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: _Registry())
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner, filename=filename,
        content=body, content_type="application/zip", registry=_Registry())
    await finish(queued["job_id"], owner)
    before = task_facade.get_task(task_id=task, owner_id=owner)
    by_name = {source["file_name"]: source for source in before["submission_sources"]}
    assert by_name["inner.zip"]["requires_reupload"] and by_name["empty.txt"]["requires_reupload"]
    assert not by_name["success.txt"]["requires_reupload"] and not by_name["recover.txt"]["requires_reupload"]
    original = workflow_repository.get_operation(queued["job_id"], owner_id=owner)
    def saved_answers(operation):
        artifact = task_facade._submission_result_artifact(owner_id=owner, task_id=task,
            job_id=operation.id, job_attempt=operation.attempt)
        return next(result.student["stu_ans"] for result in task_facade._load_submission_results(artifact)
                    if result.filename == "success.txt")
    successful_answers = saved_answers(original)
    if failure_code == "provider_submit_uncertain":
        blocked = await retry(task, owner, before)
        assert blocked.status_code == 409 and "provider_submit_uncertain" in blocked.body.decode()
        assert task_facade.get_task(task_id=task, owner_id=owner)["workflow_revision"] == before["workflow_revision"]
    round_number = 1
    restarted = await retry(task, owner, before, acknowledge=failure_code == "provider_submit_uncertain")
    assert isinstance(restarted, dict), getattr(restarted, "body", restarted)
    await finish(restarted["job_id"], owner)
    assert sorted(calls) == ["recover.txt", "recover.txt", "success.txt"]
    after = task_facade.get_task(task_id=task, owner_id=owner)
    assert after["submission_source_summary"]["parsed"] == 2
    assert after["submission_source_summary"]["failed"] == 2
    assert saved_answers(workflow_repository.get_operation(restarted["job_id"], owner_id=owner)) == successful_answers
    blocked = await retry(task, owner, after)
    assert blocked.status_code == 409 and "submission_retry_requires_reupload" in blocked.body.decode()
    assert task_facade.get_task(task_id=task, owner_id=owner)["submission_sources"] == after["submission_sources"]
    from backend.services.submission_manual_review import continue_manually
    await continue_manually(task_id=task, owner_id=owner, job_id=restarted["job_id"], expected_revision=after["workflow_revision"])
    manual = task_facade.get_task(task_id=task, owner_id=owner)
    assert manual["status"] == "submissions_ready"
    manual_answers = next(student["stu_ans"] for student in manual["student_data"].values()
                          if student["source_filename"] == "success.txt")
    assert [{key: answer[key] for key in ("q_id", "content", "flag")} for answer in manual_answers] == successful_answers
    assert sorted(calls) == ["recover.txt", "recover.txt", "success.txt"]


@pytest.mark.parametrize("reason", [None, "unknown_failure", "provider_submit_uncertain", "provider_auth_failed",
    "provider_rate_limited", "ocr_empty_result", "pdf_evidence_timeout", "pdf_processing_failed",
    "media_inspection_failed", "submission_source_persistence_failed", "no_matching_answer"])
def test_non_input_failures_are_not_inferred_to_require_reupload(reason):
    from backend.domain.source_outcomes import source_requires_reupload
    assert not source_requires_reupload(reason, retryable=False)


def test_reupload_policy_only_classifies_public_diagnostics_and_honors_retryability():
    from backend.domain.source_outcomes import SAFE_SOURCE_REASON_CODES, SOURCE_REUPLOAD_REASON_CODES, source_requires_reupload
    assert SOURCE_REUPLOAD_REASON_CODES <= SAFE_SOURCE_REASON_CODES
    assert not any(source_requires_reupload(code, retryable=True) for code in SOURCE_REUPLOAD_REASON_CODES)
