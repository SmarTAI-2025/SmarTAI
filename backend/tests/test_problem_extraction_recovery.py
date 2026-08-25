from __future__ import annotations

import io
import json
import time

import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers

from backend.api import tasks
from backend.db import file_repository, source_outcome_repository, workflow_repository
from backend.domain.errors import LeaseLost
from backend.services import problem_extraction, task_facade
from backend.services.workflow_worker import LeasedOperation
from backend.storage import get_storage
from backend.tests.test_task_background_workflows import (
    _BackgroundTasks,
    _Registry,
    _seed_task,
)


def test_problem_queue_persists_owner_scoped_source_before_dispatch():
    owner_id, task_id = _seed_task()
    content = b"Question 1: durable input"

    queued = task_facade.queue_task_problem_extraction(
        task_id=task_id,
        owner_id=owner_id,
        filename="questions.txt",
        content=content,
        content_type="text/plain",
        registry=_Registry(),
    )

    operation = workflow_repository.get_operation(
        queued["job_id"], owner_id=owner_id
    )
    assert operation.status == "pending"
    assert set(operation.payload) == {
        "source_id",
        "base_workflow_revision",
        "replace_confirmed",
        "extraction_options",
        "recognition_provider_id",
    }
    assert operation.payload["recognition_provider_id"] == "test-provider"
    sources = source_outcome_repository.list_sources(
        operation_id=operation.id,
        owner_id=owner_id,
        attempt=operation.attempt,
    )
    assert [source.id for source in sources] == [operation.payload["source_id"]]
    stored = file_repository.get_file(
        file_id=sources[0].stored_file_id, owner_id=owner_id
    )
    assert stored is not None
    assert stored.assignment_id == task_id
    assert stored.sha256 == sources[0].sha256
    with get_storage().open(stored.storage_key) as stream:
        assert stream.read() == content


def test_problem_queue_storage_failure_leaves_retryable_operation(monkeypatch):
    owner_id, task_id = _seed_task()

    def fail_save(**kwargs):
        raise OSError("storage unavailable")

    monkeypatch.setattr(file_repository, "save_file", fail_save)

    with pytest.raises(OSError, match="storage unavailable"):
        task_facade.queue_task_problem_extraction(
            task_id=task_id,
            owner_id=owner_id,
            filename="questions.txt",
            content=b"source",
            content_type="text/plain",
            registry=_Registry(),
        )

    operation = task_facade.find_task_operation(
        task_id=task_id,
        owner_id=owner_id,
        operation_type="problem_extraction",
        input_hash=task_facade._hash_json({
            "source": task_facade.hashlib.sha256(b"source").hexdigest(),
            "replace_confirmed": False,
            "extraction_options": {},
            "recognition_provider_id": "test-provider",
        }),
    )
    assert operation is not None
    assert operation.status == "error"
    assert operation.error_code == "problem_extraction_failed"


def test_expired_preparing_problem_operation_is_reclaimed():
    owner_id, task_id = _seed_task()
    digest = task_facade._hash_json({
        "source": task_facade.hashlib.sha256(b"source").hexdigest(),
        "replace_confirmed": False,
        "extraction_options": {},
        "recognition_provider_id": "test-provider",
    })
    abandoned, created = workflow_repository.create_operation(
        assignment_id=task_id, owner_id=owner_id,
        operation_type="problem_extraction", input_hash=digest,
        payload={}, expires_at=time.time() - 1,
        initial_status="preparing",
    )
    assert created is True

    queued = task_facade.queue_task_problem_extraction(
        task_id=task_id, owner_id=owner_id,
        filename="questions.txt", content=b"source",
        content_type="text/plain", registry=_Registry(),
    )

    assert queued["job_id"] == abandoned.id
    recovered = workflow_repository.get_operation(
        abandoned.id, owner_id=owner_id
    )
    assert recovered.attempt == abandoned.attempt + 1
    assert recovered.status == "pending"


def test_expired_preparing_retry_reuses_already_saved_source_object():
    owner_id, task_id = _seed_task()
    content = b"source"
    digest = task_facade._hash_json({
        "source": task_facade.hashlib.sha256(content).hexdigest(),
        "replace_confirmed": False,
        "extraction_options": {},
        "recognition_provider_id": "test-provider",
    })
    abandoned, _ = workflow_repository.create_operation(
        assignment_id=task_id, owner_id=owner_id,
        operation_type="problem_extraction", input_hash=digest,
        payload={}, expires_at=time.time() - 1,
        initial_status="preparing",
    )
    stored = file_repository.save_file(
        storage=get_storage(), owner_id=owner_id,
        kind="problem_source", original_name="questions.txt",
        content=content, content_type="text/plain", assignment_id=task_id,
    )
    source_outcome_repository.register_source(
        owner_id=owner_id, assignment_id=task_id,
        operation_id=abandoned.id, expected_attempt=abandoned.attempt,
        order_index=0, stored_file_id=stored.id,
    )

    queued = task_facade.queue_task_problem_extraction(
        task_id=task_id, owner_id=owner_id,
        filename="questions.txt", content=content,
        content_type="text/plain", registry=_Registry(),
    )

    files = [
        item for item in file_repository.list_files(
            owner_id=owner_id, assignment_id=task_id
        )
        if item.kind == "problem_source" and item.sha256 == stored.sha256
    ]
    assert [item.id for item in files] == [stored.id]
    sources = source_outcome_repository.list_sources(
        operation_id=queued["job_id"], owner_id=owner_id, attempt=2
    )
    assert sources[0].stored_file_id == stored.id


@pytest.mark.asyncio
async def test_problem_endpoint_does_not_schedule_request_memory_work():
    owner_id, task_id = _seed_task()
    background = _BackgroundTasks()
    upload = UploadFile(
        file=io.BytesIO(b"Question 1: durable input"),
        filename="questions.txt",
        headers=Headers({"content-type": "text/plain"}),
    )

    response = await tasks.extract_problems_endpoint(
        task_id=task_id,
        background_tasks=background,
        file=upload,
        source_token=None,
        confirmed_candidate_ids="[]",
        replace_confirmed=False,
        current=type("User", (), {"id": owner_id})(),
        registry=_Registry(),
    )

    assert response["status"] == "started"
    assert background.calls == []
    assert workflow_repository.get_operation(
        response["job_id"], owner_id=owner_id
    ).status == "pending"


@pytest.mark.asyncio
async def test_problem_endpoint_replay_uses_stable_operation_identity():
    owner_id, task_id = _seed_task()

    async def submit():
        return await tasks.extract_problems_endpoint(
            task_id=task_id,
            background_tasks=_BackgroundTasks(),
            file=UploadFile(
                file=io.BytesIO(b"same source"),
                filename="questions.txt",
                headers=Headers({"content-type": "text/plain"}),
            ),
            source_token=None,
            confirmed_candidate_ids="[]",
            replace_confirmed=False,
            current=type("User", (), {"id": owner_id})(),
            registry=_Registry(),
        )

    first = await submit()
    running_replay = await submit()
    assert running_replay["status"] == "already_running"
    assert running_replay["job_id"] == first["job_id"]

    ctx = _claim_context(owner_id, first["job_id"])
    task_facade._replace_draft_questions(
        task_id, owner_id, _problem_data(), "questions.txt",
        expected_workflow_revision=first["workflow_revision"],
        operation_id=ctx.operation_id,
        expected_operation_attempt=ctx.attempt,
        expected_lease_token=ctx.lease_token,
    )
    terminal_replay = await submit()
    assert terminal_replay["status"] == "already_done"
    assert terminal_replay["job_id"] == first["job_id"]


def _claim_context(owner_id: str, job_id: str, *, worker_id: str = "test-worker"):
    claimed = workflow_repository.claim_operation(
        job_id,
        owner_id=owner_id,
        worker_id=worker_id,
        lease_seconds=60,
    )
    return LeasedOperation(
        claimed, worker_id=worker_id, lease_seconds=60
    )


def _problem_data(stem: str = "Recovered") -> dict[str, dict]:
    return {
        "q1": {
            "q_id": "q1",
            "number": "1",
            "type": "short",
            "stem": stem,
            "criterion": "",
            "max_score": 10,
        }
    }


@pytest.mark.asyncio
async def test_fresh_problem_worker_recovers_source_from_database(monkeypatch):
    owner_id, task_id = _seed_task()
    queued = task_facade.queue_task_problem_extraction(
        task_id=task_id,
        owner_id=owner_id,
        filename="questions.txt",
        content=b"Question 1: durable input",
        content_type="text/plain",
        registry=_Registry(),
    )
    calls = {"text": 0, "problems": 0}

    class Registry(_Registry):
        def pick_vision(self, provider):
            return None

    async def fake_text(content, filename, **kwargs):
        calls["text"] += 1
        assert content == b"Question 1: durable input"
        assert filename == "questions.txt"
        return "Question 1: durable input"

    async def fake_problems(text, provider, problem_data, **kwargs):
        calls["problems"] += 1
        assert text == "Question 1: durable input"
        problem_data.update(_problem_data())

    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _owner: Registry())
    monkeypatch.setattr(task_facade, "extract_text_from_upload", fake_text)
    monkeypatch.setattr(task_facade, "extract_problems", fake_problems)

    ctx = _claim_context(owner_id, queued["job_id"])
    from backend.services.problem_extraction import run_problem_extraction

    await run_problem_extraction(
        ctx,
        registry_factory=task_facade._registry_for_owner,
        extract_text=task_facade.extract_text_from_upload,
        extract_questions=task_facade.extract_problems,
        commit=task_facade._replace_draft_questions,
    )

    assert calls == {"text": 1, "problems": 1}
    operation = workflow_repository.get_operation(
        queued["job_id"], owner_id=owner_id
    )
    assert operation.status == "done"
    assert operation.lease_token is None
    assert operation.checkpoint_stage == "completed"
    assert len(operation.artifact_refs) == 2
    assert operation.progress == {
        "workflow": "problem_recognition",
        "stage": "completed",
        "completed_steps": 4,
        "total_steps": 4,
    }
    questions = task_facade.assignment_repository.list_questions(
        task_id, teacher_id=owner_id
    )
    assert [question.stem for question in questions] == ["Recovered"]


@pytest.mark.asyncio
async def test_problem_worker_reuses_durable_text_and_structure_artifacts(monkeypatch):
    owner_id, task_id = _seed_task()
    queued = task_facade.queue_task_problem_extraction(
        task_id=task_id,
        owner_id=owner_id,
        filename="questions.txt",
        content=b"source",
        content_type="text/plain",
        registry=_Registry(),
    )
    ctx = _claim_context(owner_id, queued["job_id"])
    text_artifact = file_repository.save_file(
        storage=get_storage(), owner_id=owner_id,
        kind="problem_extraction_text", original_name="extracted.txt",
        content=b"durable text", content_type="text/plain",
        assignment_id=task_id,
    )
    structured_artifact = file_repository.save_file(
        storage=get_storage(), owner_id=owner_id,
        kind="problem_extraction_structure", original_name="problems.json",
        content=json.dumps(_problem_data("Checkpointed")).encode("utf-8"),
        content_type="application/json", assignment_id=task_id,
    )
    await ctx.checkpoint(
        stage="problems_structured",
        checkpoint={
            "text_artifact_id": text_artifact.id,
            "structured_artifact_id": structured_artifact.id,
        },
        artifact_refs=[text_artifact.id, structured_artifact.id],
    )

    async def forbidden(*args, **kwargs):
        raise AssertionError("completed provider stage was repeated")

    monkeypatch.setattr(task_facade, "extract_text_from_upload", forbidden)
    monkeypatch.setattr(task_facade, "extract_problems", forbidden)
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _owner: _Registry())

    from backend.services.problem_extraction import run_problem_extraction

    await run_problem_extraction(
        ctx,
        registry_factory=task_facade._registry_for_owner,
        extract_text=task_facade.extract_text_from_upload,
        extract_questions=task_facade.extract_problems,
        commit=task_facade._replace_draft_questions,
    )

    operation = workflow_repository.get_operation(
        queued["job_id"], owner_id=owner_id
    )
    assert operation.status == "done"
    assert task_facade.assignment_repository.list_questions(
        task_id, teacher_id=owner_id
    )[0].stem == "Checkpointed"


@pytest.mark.asyncio
async def test_problem_worker_discovers_artifact_saved_before_checkpoint(monkeypatch):
    owner_id, task_id = _seed_task()
    queued = task_facade.queue_task_problem_extraction(
        task_id=task_id, owner_id=owner_id,
        filename="questions.txt", content=b"source",
        content_type="text/plain", registry=_Registry(),
    )
    operation = workflow_repository.get_operation(
        queued["job_id"], owner_id=owner_id
    )
    deterministic_name = problem_extraction.artifact_name(
        operation.id, operation.attempt, "source_text", "txt"
    )
    file_repository.save_file(
        storage=get_storage(), owner_id=owner_id,
        kind="problem_extraction_text", original_name=deterministic_name,
        content=b"recovered text", content_type="text/plain",
        assignment_id=task_id,
    )
    calls = {"text": 0, "problems": 0}

    class Registry(_Registry):
        def pick_vision(self, provider):
            return None

    async def forbidden_text(*args, **kwargs):
        calls["text"] += 1
        raise AssertionError("durable text artifact was not discovered")

    async def fake_problems(text, provider, result, **kwargs):
        calls["problems"] += 1
        assert text == "recovered text"
        result.update(_problem_data("Discovered"))

    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _owner: Registry())
    monkeypatch.setattr(task_facade, "extract_text_from_upload", forbidden_text)
    monkeypatch.setattr(task_facade, "extract_problems", fake_problems)

    await task_facade.run_durable_problem_extraction(
        _claim_context(owner_id, queued["job_id"])
    )

    assert calls == {"text": 0, "problems": 1}
    assert task_facade.assignment_repository.list_questions(
        task_id, teacher_id=owner_id
    )[0].stem == "Discovered"


@pytest.mark.asyncio
async def test_problem_worker_failure_atomically_clears_active_workflow(monkeypatch):
    owner_id, task_id = _seed_task()
    queued = task_facade.queue_task_problem_extraction(
        task_id=task_id, owner_id=owner_id,
        filename="questions.txt", content=b"source",
        content_type="text/plain", registry=_Registry(),
    )
    ctx = _claim_context(owner_id, queued["job_id"])

    class Registry(_Registry):
        def pick_vision(self, provider):
            return None

    async def fail_text(*args, **kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _owner: Registry())
    monkeypatch.setattr(task_facade, "extract_text_from_upload", fail_text)

    await task_facade.run_durable_problem_extraction(ctx)

    operation = workflow_repository.get_operation(
        queued["job_id"], owner_id=owner_id
    )
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    assert operation.status == "error"
    assert operation.error_code == "problem_extraction_failed"
    assert operation.lease_token is None
    assert workflow.presentation_status == "error"
    assert workflow.active_job_id is None
    assert workflow.error_code == "problem_extraction_failed"


@pytest.mark.asyncio
async def test_problem_worker_preserves_no_provider_error_code(monkeypatch):
    owner_id, task_id = _seed_task()
    queued = task_facade.queue_task_problem_extraction(
        task_id=task_id, owner_id=owner_id,
        filename="questions.txt", content=b"source",
        content_type="text/plain", registry=_Registry(),
    )

    class EmptyRegistry:
        def pick_default(self):
            return None

    monkeypatch.setattr(
        task_facade, "_registry_for_owner", lambda _owner: EmptyRegistry()
    )
    await task_facade.run_durable_problem_extraction(
        _claim_context(owner_id, queued["job_id"])
    )

    operation = workflow_repository.get_operation(
        queued["job_id"], owner_id=owner_id
    )
    assert operation.status == "error"
    assert operation.error_code == "no_provider_configured"


def test_completed_problem_replay_creates_no_duplicate_questions():
    owner_id, task_id = _seed_task()
    queued = task_facade.queue_task_problem_extraction(
        task_id=task_id, owner_id=owner_id,
        filename="questions.txt", content=b"source",
        content_type="text/plain", registry=_Registry(),
    )
    ctx = _claim_context(owner_id, queued["job_id"])
    task_facade._replace_draft_questions(
        task_id, owner_id, _problem_data(), "questions.txt",
        expected_workflow_revision=queued["workflow_revision"],
        operation_id=ctx.operation_id,
        expected_operation_attempt=ctx.attempt,
        expected_lease_token=ctx.lease_token,
    )

    replay = task_facade.queue_task_problem_extraction(
        task_id=task_id, owner_id=owner_id,
        filename="questions.txt", content=b"source",
        content_type="text/plain", registry=_Registry(),
        expected_workflow_revision=0,
    )

    assert replay["status"] == "already_done"
    assert replay["job_id"] == queued["job_id"]
    questions = task_facade.assignment_repository.list_questions(
        task_id, teacher_id=owner_id
    )
    assert len(questions) == 1


def test_stale_problem_lease_cannot_commit_questions():
    owner_id, task_id = _seed_task()
    queued = task_facade.queue_task_problem_extraction(
        task_id=task_id,
        owner_id=owner_id,
        filename="questions.txt",
        content=b"source",
        content_type="text/plain",
        registry=_Registry(),
    )
    old = workflow_repository.claim_operation(
        queued["job_id"], owner_id=owner_id,
        worker_id="old-worker", lease_seconds=60,
    )
    workflow_repository.release_operation(
        old.id, owner_id=owner_id, worker_id="old-worker",
        lease_token=old.lease_token,
    )
    current = workflow_repository.claim_operation(
        old.id, owner_id=owner_id, worker_id="new-worker", lease_seconds=60,
    )

    with pytest.raises(LeaseLost):
        task_facade._replace_draft_questions(
            task_id, owner_id, _problem_data("Stale"), "questions.txt",
            expected_workflow_revision=queued["workflow_revision"],
            operation_id=old.id,
            expected_operation_attempt=old.attempt,
            expected_lease_token=old.lease_token,
        )

    assert workflow_repository.get_operation(
        current.id, owner_id=owner_id
    ).lease_token == current.lease_token
    assert task_facade.assignment_repository.list_questions(
        task_id, teacher_id=owner_id
    ) == []


def test_problem_commit_rejects_wrong_owner_artifact_and_rolls_back():
    owner_id, task_id = _seed_task()
    other_owner, other_task = _seed_task()
    queued = task_facade.queue_task_problem_extraction(
        task_id=task_id, owner_id=owner_id,
        filename="questions.txt", content=b"source",
        content_type="text/plain", registry=_Registry(),
    )
    ctx = _claim_context(owner_id, queued["job_id"])
    foreign_artifact = file_repository.save_file(
        storage=get_storage(), owner_id=other_owner,
        kind="problem_extraction_text", original_name="foreign.txt",
        content=b"foreign", content_type="text/plain",
        assignment_id=other_task,
    )

    with pytest.raises(task_facade.NotFound):
        task_facade._replace_draft_questions(
            task_id, owner_id, _problem_data(), "questions.txt",
            expected_workflow_revision=queued["workflow_revision"],
            operation_id=ctx.operation_id,
            expected_operation_attempt=ctx.attempt,
            expected_lease_token=ctx.lease_token,
            operation_checkpoint={"text_artifact_id": foreign_artifact.id},
            operation_artifact_refs=[foreign_artifact.id],
        )

    assert task_facade.assignment_repository.list_questions(
        task_id, teacher_id=owner_id
    ) == []
    assert workflow_repository.get_operation(
        ctx.operation_id, owner_id=owner_id
    ).status == "running"


def test_stale_problem_lease_cannot_fail_or_clear_current_worker():
    owner_id, task_id = _seed_task()
    queued = task_facade.queue_task_problem_extraction(
        task_id=task_id, owner_id=owner_id,
        filename="questions.txt", content=b"source",
        content_type="text/plain", registry=_Registry(),
    )
    old = workflow_repository.claim_operation(
        queued["job_id"], owner_id=owner_id,
        worker_id="old-worker", lease_seconds=60,
    )
    workflow_repository.release_operation(
        old.id, owner_id=owner_id, worker_id="old-worker",
        lease_token=old.lease_token,
    )
    current = workflow_repository.claim_operation(
        old.id, owner_id=owner_id, worker_id="new-worker", lease_seconds=60,
    )

    with pytest.raises(LeaseLost):
        task_facade._fail_operation(
            task_id, owner_id, old.id, old.attempt,
            "problem_extraction_failed",
            expected_lease_token=old.lease_token,
        )

    operation = workflow_repository.get_operation(old.id, owner_id=owner_id)
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    assert operation.status == "running"
    assert operation.lease_token == current.lease_token
    assert workflow.active_job_id == old.id
    assert workflow.error_code is None
