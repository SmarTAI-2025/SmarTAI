"""Current task recognition artifact fencing and paid-call recovery."""
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.db import file_repository, workflow_repository
from backend.db.models import AssignmentRecord
from backend.db.session import session_scope
from backend.domain.errors import DomainError
from backend.tests.test_recognition_artifact_store import seeded
from backend.services.recognition_artifacts import RecognitionArtifactStore


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
            "stu_ans": [{"q_id": "q1", "number": "1", "type": "concept", "content": "wrong -2", "flag": []}]}))
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
