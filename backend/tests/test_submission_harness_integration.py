"""G-stage contracts: fidelity, paid-call recovery, and immutable publication."""
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.db import file_repository, submission_repository, workflow_repository
from backend.db.models import AssignmentRecord
from backend.db.session import session_scope
from backend.domain.errors import DomainError, ValidationError, VersionConflict
from backend.services import grading_runs, submissions
from backend.services import submission_uploads as uploads
from backend.tests.test_ocr_normalized_ingest import _published_assignment
from backend.tests.test_recognition_artifact_store import seeded
from backend.services.recognition_artifacts import RecognitionArtifactStore


def provider_fixture(answer="x = -2^3; wrong on purpose", flags=None):
    provider = SimpleNamespace(provider_id="test:parser", config=None, supports_vision=False,
        ainvoke=AsyncMock(return_value=SimpleNamespace(content=json.dumps({
            "stu_id": "model-cannot-change-owner", "stu_name": "Untrusted",
            "stu_ans": [{"q_id": "q1", "number": "1", "type": "short", "content": answer, "flag": flags or []}],
        }))))
    registry = SimpleNamespace(list_configs=lambda: [dict(provider_id=provider.provider_id, enabled=True)])
    return provider, registry


def upload_args(student, assignment, provider, registry):
    return dict(student_id=student, assignment_id=assignment, filename="answer.txt",
                content=b"x = -2^3; wrong on purpose", content_type="text/plain",
                provider=provider, registry=registry)


@pytest.mark.asyncio
async def test_uncertain_parser_is_not_replayed_and_old_revision_remains():
    _, student, assignment = _published_assignment("uncertain_parser")
    previous = submissions.submit_online(student_id=student, assignment_id=assignment,
        answers=[{"q_id": "q1", "content": "old answer"}])
    provider, registry = provider_fixture()
    provider.ainvoke.side_effect = TimeoutError("remote response lost")
    args = upload_args(student, assignment, provider, registry)
    with pytest.raises(ValidationError):
        await submissions.submit_student_file_with_ocr(**args)
    with pytest.raises(ValidationError) as error:
        await submissions.submit_student_file_with_ocr(**args)
    assert error.value.code == "provider_submit_uncertain"
    assert provider.ainvoke.await_count == 1
    assert submission_repository.get_submission_for_student(assignment, student_id=student).current_revision_id == previous.id


@pytest.mark.asyncio
async def test_candidate_recovery_does_not_repeat_parser(monkeypatch):
    _, student, assignment = _published_assignment("candidate_recovery")
    provider, registry = provider_fixture()
    original = uploads.submission_upload_repository.publish_revision
    calls = 0

    def interrupt_once(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated process interruption after candidate write")
        return original(**kwargs)

    monkeypatch.setattr(uploads.submission_upload_repository, "publish_revision", interrupt_once)
    args = upload_args(student, assignment, provider, registry)
    with pytest.raises(OSError):
        await submissions.submit_student_file_with_ocr(**args)
    assert submission_repository.get_submission_for_student(assignment, student_id=student).current_revision_id is None
    revision = await submissions.submit_student_file_with_ocr(**args)
    assert revision.answers[0].content == "x = -2^3; wrong on purpose"
    assert provider.ainvoke.await_count == 1
    assert "never solve" in provider.ainvoke.call_args.args[0][0].content


@pytest.mark.asyncio
async def test_concurrent_manual_correction_is_never_overwritten(monkeypatch):
    _, student, assignment = _published_assignment("manual_correction")
    provider, registry = provider_fixture()
    response = provider.ainvoke.return_value
    manual = None

    async def correcting_parser(_messages):
        nonlocal manual
        manual = submissions.submit_online(student_id=student, assignment_id=assignment,
            answers=[{"q_id": "q1", "content": "manual version"}])
        return response

    provider.ainvoke.side_effect = correcting_parser
    with pytest.raises(VersionConflict):
        await submissions.submit_student_file_with_ocr(**upload_args(student, assignment, provider, registry))
    assert submission_repository.get_submission_for_student(assignment, student_id=student).current_revision_id == manual.id


@pytest.mark.asyncio
async def test_unknown_question_is_not_silently_discarded():
    _, student, assignment = _published_assignment("unknown_question")
    provider, registry = provider_fixture()
    data = json.loads(provider.ainvoke.return_value.content)
    data["stu_ans"].append({"q_id": "not-in-task", "content": "must not disappear", "flag": []})
    provider.ainvoke.return_value.content = json.dumps(data)
    with pytest.raises(ValidationError):
        await submissions.submit_student_file_with_ocr(**upload_args(student, assignment, provider, registry))
    assert submission_repository.get_submission_for_student(assignment, student_id=student).current_revision_id is None


@pytest.mark.asyncio
async def test_uncertain_transcription_can_be_graded_without_confirming_review():
    teacher, student, assignment = _published_assignment("review_gate")
    provider, registry = provider_fixture(flags=["recognition_needs_review"])
    revision = await submissions.submit_student_file_with_ocr(**upload_args(student, assignment, provider, registry))
    run = grading_runs.start_run(assignment_id=assignment, teacher_id=teacher)
    assert run.id
    assert workflow_repository.answer_review_statuses([revision.answers[0].id]).get(revision.answers[0].id) != "confirmed"
    saved = submission_repository.get_revision(revision.id, actor_id=teacher)
    assert saved.answers[0].flag == ["recognition_needs_review"]
    assert saved.answers[0].content == revision.answers[0].content
    assert provider.ainvoke.await_count == 1


@pytest.mark.asyncio
async def test_revision_artifact_late_put_cannot_publish_after_task_deletion(tmp_path, monkeypatch):
    storage, binding, _, envelope = seeded(tmp_path, "submission_revision")
    before = sorted(storage.list_keys(""))
    original = storage.save

    def deleting_put(key, content):
        original(key, content)
        with session_scope() as session:
            from sqlalchemy import select
            assignment = session.scalar(select(AssignmentRecord).where(AssignmentRecord.teacher_id == envelope.source.owner_id))
            assignment.deletion_requested_at = time.time()

    monkeypatch.setattr(storage, "save", deleting_put)
    with pytest.raises(DomainError):
        await RecognitionArtifactStore(storage).save(envelope, binding=binding, authorized_owner_id=envelope.source.owner_id)
    assert sorted(storage.list_keys("")) == before
    assert not [row for row in file_repository.list_files(owner_id=envelope.source.owner_id,
        submission_revision_id=binding.business_id) if row.kind == "recognition_artifact_v1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_only", [True, False])
async def test_batch_retry_reuses_saved_parse_but_full_restart_reprocesses(monkeypatch, failed_only):
    import io
    import zipfile
    from backend.services import task_facade
    from backend.services.workflow_worker import LeasedOperation
    from backend.tests.test_task_background_workflows import _Registry, _seed_task

    owner, task = _seed_task(with_question=True)
    registry = _Registry()
    async def invoke(messages):
        student = "S001" if "first.txt" in messages[-1].content else "S002"
        return SimpleNamespace(content=json.dumps({"stu_id": student, "stu_name": student,
            "stu_ans": [{"q_id": "q1", "number": "1", "type": "short", "content": "wrong -2", "flag": []}]}))
    provider = SimpleNamespace(provider_id="test-provider", supports_vision=False, ainvoke=AsyncMock(side_effect=invoke))
    registry.provider = provider
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _owner: registry)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("first.txt", "first answer -2")
        archive.writestr("second.txt", "second answer -2")
    content = buffer.getvalue()
    original = file_repository.save_file
    failures = 2 if failed_only else 1
    attempts = failures + 1
    def fail_aggregate_once(**kwargs):
        nonlocal failures
        if kwargs["kind"] == "submission_recognition_result" and failures:
            failures -= 1
            raise RuntimeError("submission_persistence_failed")
        return original(**kwargs)
    monkeypatch.setattr(file_repository, "save_file", fail_aggregate_once)
    for attempt in range(attempts):
        if attempt and failed_only:
            from fastapi import BackgroundTasks
            from backend.api import tasks
            workflow = workflow_repository.get_workflow(task, owner_id=owner)
            queued = await tasks.retry_submission_recognition_endpoint(
                task_id=task, job_id=queued["job_id"],
                request=tasks.RetrySubmissionRecognitionRequest(
                    expected_workflow_revision=workflow.workflow_revision),
                background_tasks=BackgroundTasks(), current=SimpleNamespace(id=owner), registry=registry,
            )
        else:
            queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
                filename="answers.zip", content=content, content_type="application/zip", registry=registry)
        worker = f"batch-worker-{attempt}"
        row = workflow_repository.claim_operation(queued["job_id"], owner_id=owner, worker_id=worker, lease_seconds=60)
        await task_facade.run_durable_submission_recognition(LeasedOperation(row, worker_id=worker, lease_seconds=60))
        result = workflow_repository.get_operation(row.id, owner_id=owner)
        assert result.status == ("done" if attempt == attempts - 1 else "error")
    assert provider.ainvoke.await_count == (2 if failed_only else 4)


def test_teacher_can_preview_current_student_original_but_not_replaced_revision():
    from backend.services import source_files
    from backend.storage import get_storage
    from backend.tests.test_recognition_recheck import source_file
    teacher, student, assignment = _published_assignment("revision_preview")
    workflow_repository.ensure_workflow(assignment_id=assignment, owner_id=teacher)
    image, _ = source_file()
    revision = submissions.submit_student_file(student_id=student, assignment_id=assignment,
        filename="work.png", content=image, content_type="image/png", answers=[{"q_id": "q1", "content": "-2"}])
    stored = file_repository.list_current_submission_originals(assignment_id=assignment, teacher_id=teacher)[0]
    assert stored.owner_id == student and stored.submission_revision_id == revision.id
    assert source_files.read_source_file_content(task_id=assignment, file_id=stored.id,
        owner_id=teacher, storage=get_storage()).content == image
    with pytest.raises(source_files.SourcePreviewNotFound):
        source_files.read_source_file_content(task_id=assignment, file_id=stored.id,
            owner_id="another-teacher", storage=get_storage())
    submissions.submit_online(student_id=student, assignment_id=assignment,
        answers=[{"q_id": "q1", "content": "new answer"}])
    with pytest.raises(source_files.SourcePreviewNotFound):
        source_files.read_source_file_content(task_id=assignment, file_id=stored.id,
            owner_id=teacher, storage=get_storage())
