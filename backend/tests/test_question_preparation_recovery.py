from __future__ import annotations

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier, Lock, local
from types import SimpleNamespace

import pytest
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import event, select, update

from backend.agents.ingest_agent import AICompletionCandidateOutput
from backend.api import task_preparation
from backend.db import assignment_repository, file_repository, workflow_repository
from backend.db.session import get_engine, session_scope
from backend.domain.errors import DomainError, LeaseLost, VersionConflict
from backend.llm.providers import ProviderRequestError
from backend.models import QuestionScorePolicy
from backend.services import task_facade
from backend.services.question_structure import build_major_question_structure
from backend.storage import get_storage
from backend.storage.local import LocalStorage
from backend.tests.test_task_background_workflows import (
    _BackgroundTasks,
    _Registry,
    _seed_task,
)


class _RecoveryRegistry(_Registry):
    def __init__(self, model: str = "test-model") -> None:
        self.model = model

    def pick_vision(self, preferred=None):
        del preferred
        return None

    def list_configs(self):
        return [{
            "provider_id": "test-provider",
            "provider_type": "openai",
            "model": self.model,
            "enabled": True,
            "scope": "test",
        }]


def _questions(count: int = 2) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for index in range(1, count + 1):
        q_id = f"q{index}"
        stem = (
            "(a) Explain A.\n(b) Explain B."
            if index == 1
            else f"Solve major question {index}."
        )
        problem = {
            "q_id": q_id,
            "number": str(index),
            "type": "short",
            "stem": stem,
            "criterion": "",
            "max_score": 10,
            "max_score_source": "uniform",
            "max_score_review_status": "confirmed",
        }
        problem["question_structure"] = build_major_question_structure(
            problem,
            major_order=index - 1,
            structure_source="deterministic",
            review_status="confirmed",
        ).model_dump(mode="json")
        rows[q_id] = problem
    return rows


def _candidates(q_id: str) -> list[AICompletionCandidateOutput]:
    if q_id == "q1":
        criterion = "(a) 4 points\n(b) 6 points"
        answer = "(a) Explanation A.\n(b) Explanation B."
    else:
        criterion = "Correct method and result: 100%"
        answer = f"Complete answer for {q_id}."
    return [
        AICompletionCandidateOutput(
            target_id=f"{q_id}:criterion",
            q_id=q_id,
            target="criterion",
            text_value=criterion,
        ),
        AICompletionCandidateOutput(
            target_id=f"{q_id}:reference_answer",
            q_id=q_id,
            target="reference_answer",
            text_value=answer,
        ),
    ]


def _apply_candidates(
    problem_data: dict[str, dict],
    candidates_by_question: dict[str, list[AICompletionCandidateOutput]],
) -> dict[str, dict]:
    output = {q_id: dict(problem) for q_id, problem in problem_data.items()}
    for q_id, candidates in candidates_by_question.items():
        for candidate in candidates:
            output[q_id][candidate.target] = candidate.text_value
    return output


async def _queue_question_preparation(
    owner_id: str,
    task_id: str,
    *,
    source_text: str = "1. (a) Explain A. (b) Explain B.",
    registry: _RecoveryRegistry | None = None,
):
    registry = registry or _RecoveryRegistry()
    source = await task_preparation.preflight_problem_source(
        task_id=task_id,
        file=None,
        library_material_id=None,
        stored_file_id=None,
        inline_text=source_text,
        structure_mode="organized",
        role="problem",
        extraction_hint="",
        save_to_library=False,
        recognition_provider_id="test-provider",
        current=SimpleNamespace(id=owner_id),
        registry=registry,
    )
    background = _BackgroundTasks()
    response = await task_preparation._start_question_preparation(
        task_id=task_id,
        request=task_preparation.StartQuestionPreparationRequest(
            source_tokens=[source["source_token"]],
            expected_workflow_revision=0,
            score_policy=QuestionScorePolicy(
                mode="uniform",
                uniform_max_score=10,
            ),
            recognition_provider_id="test-provider",
        ),
        background_tasks=background,
        current=SimpleNamespace(id=owner_id),
        registry=registry,
        allow_prepared_source_reuse=False,
    )
    return source, response, background


def _claim(owner_id: str, operation_id: str, worker_id: str):
    from backend.services.workflow_worker import LeasedOperation

    claimed = workflow_repository.claim_operation(
        operation_id,
        owner_id=owner_id,
        worker_id=worker_id,
        lease_seconds=60,
    )
    return LeasedOperation(
        claimed,
        worker_id=worker_id,
        lease_seconds=60,
    )


def _expire_lease(operation_id: str) -> None:
    with session_scope() as session:
        session.execute(
            update(workflow_repository.WorkflowOperationRecord)
            .where(
                workflow_repository.WorkflowOperationRecord.id == operation_id
            )
            .values(lease_expires_at=time.time() - 1)
        )


def _publish_atomic_for_test(
    *,
    owner_id: str,
    task_id: str,
    input_hash: str,
    expected_revision: int = 0,
    retry_id: str | None = None,
    retry_attempt: int | None = None,
):
    return task_facade.publish_checkpointed_operation_atomic(
        task_id=task_id,
        owner_id=owner_id,
        operation_type="question_preparation",
        input_hash=input_hash,
        expected_workflow_revision=expected_revision,
        operation_payload={
            "base_workflow_revision": expected_revision,
            "input_marker": input_hash[:8],
        },
        initial_checkpoint_stage="sources_validated",
        initial_checkpoint={"stage": "sources_validated"},
        artifact_refs=[],
        workflow_changes={
            "active_operation": "question_preparation",
            "presentation_status": "extracting_problems",
            "error_code": None,
        },
        workflow_job_id_fields=("active_job_id", "extract_job_id"),
        retry_observed_operation_id=retry_id,
        retry_observed_attempt=retry_attempt,
    )


@contextmanager
def _force_two_publishers_to_reach_sqlite_write_gate():
    engine = get_engine()
    assert engine.dialect.name == "sqlite"
    operation_updates = Barrier(2)
    per_thread = local()
    hit_lock = Lock()
    hits = {"operation_gate": 0}

    def before_cursor_execute(
        _conn,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ):
        if not hasattr(per_thread, "seen"):
            per_thread.seen = set()
        sql = " ".join(statement.lower().split())
        if (
            not sql.startswith("update workflow_operations")
            or "set updated_at=workflow_operations.updated_at" not in sql
            or "operation_gate" in per_thread.seen
        ):
            return
        per_thread.seen.add("operation_gate")
        with hit_lock:
            hits["operation_gate"] += 1
        operation_updates.wait(timeout=10)

    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    try:
        yield hits
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)


def _run_publish_pair(first_call, second_call):
    start = Barrier(2)

    def invoke(call):
        start.wait(timeout=10)
        try:
            operation, published, revision = call()
            return {
                "kind": "ok",
                "id": operation.id,
                "published": published,
                "revision": revision,
                "attempt": operation.attempt,
                "status": operation.status,
                "error_code": operation.error_code,
            }
        except DomainError as exc:
            return {"kind": "domain_error", "code": exc.code}
        except Exception as exc:
            return {
                "kind": "unexpected",
                "type": type(exc).__name__,
                "message": str(exc),
            }

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (
            pool.submit(invoke, first_call),
            pool.submit(invoke, second_call),
        )
        return [future.result(timeout=20) for future in futures]


def _fake_preparer(
    provider_calls: list[str],
    *,
    question_count: int,
    cancel_after_question: str | None = None,
    cancel_after_question_started: str | None = None,
    cancel_after_extraction_started: bool = False,
    known_failure_question: str | None = None,
    record_base_provider_calls: bool = False,
):
    async def prepare(_sources, _provider, **kwargs):
        recovered_base = kwargs.get("recovered_base_problem_data")
        recovered_extracted = kwargs.get("recovered_extracted_problem_data")
        if recovered_base is None:
            if recovered_extracted is None:
                await kwargs["on_extraction_started"]()
                if cancel_after_extraction_started:
                    raise asyncio.CancelledError
                if record_base_provider_calls:
                    provider_calls.append("questions_extracted")
                extracted = _questions(question_count)
                await kwargs["on_questions_extracted"](extracted)
            else:
                extracted = {
                    q_id: dict(problem)
                    for q_id, problem in recovered_extracted.items()
                }
            base = extracted
            if record_base_provider_calls:
                await kwargs["on_base_alignment_started"]()
                provider_calls.append("uploaded_materials_aligned")
            await kwargs["on_base_prepared"](base, {})
        else:
            base = {
                q_id: dict(problem) for q_id, problem in recovered_base.items()
            }

        recovered = {
            q_id: [
                AICompletionCandidateOutput.model_validate(candidate)
                for candidate in candidates
            ]
            for q_id, candidates in (
                kwargs.get("recovered_candidates_by_question") or {}
            ).items()
        }
        all_candidates = dict(recovered)
        for q_id in base:
            if q_id in recovered:
                continue
            await kwargs["on_question_started"](q_id)
            if q_id == cancel_after_question_started:
                raise asyncio.CancelledError
            provider_calls.append(q_id)
            if q_id == known_failure_question:
                error = ProviderRequestError(
                    "provider_rate_limited",
                    status_code=429,
                )
                await kwargs["on_question_failed"](q_id, error)
                raise error
            candidates = _candidates(q_id)
            await kwargs["on_question_completed"](q_id, candidates)
            all_candidates[q_id] = candidates
            if q_id == cancel_after_question:
                raise asyncio.CancelledError
        return _apply_candidates(base, all_candidates)

    return prepare


@pytest.mark.asyncio
async def test_start_publishes_only_to_durable_worker_and_commits_one_major_artifact(
    monkeypatch,
):
    owner_id, task_id = _seed_task()
    source, response, background = await _queue_question_preparation(
        owner_id,
        task_id,
    )

    operation = workflow_repository.get_operation(
        response["job_id"], owner_id=owner_id
    )
    assert response["status"] == "started"
    assert background.calls == []
    assert operation.status == "pending"
    assert operation.checkpoint_stage == "sources_validated"
    assert operation.payload["source_tokens"] == [source["source_token"]]
    assert "text" not in operation.payload
    assert "provider" not in operation.payload
    assert "key" not in operation.payload

    provider_calls: list[str] = []
    monkeypatch.setattr(
        task_facade,
        "_registry_for_owner",
        lambda _owner: _RecoveryRegistry(),
    )
    monkeypatch.setattr(
        task_preparation,
        "prepare_question_packages",
        _fake_preparer(provider_calls, question_count=1),
    )
    await task_preparation.run_durable_question_preparation(
        _claim(owner_id, operation.id, "worker-one")
    )

    completed = workflow_repository.get_operation(operation.id, owner_id=owner_id)
    questions = assignment_repository.list_questions(task_id, teacher_id=owner_id)
    artifacts = file_repository.list_files(
        owner_id=owner_id,
        assignment_id=task_id,
    )
    major_artifacts = [
        item for item in artifacts
        if item.kind == "question_preparation_question_v1"
    ]
    assert provider_calls == ["q1"]
    assert completed.status == "done"
    assert completed.checkpoint_stage == "question_packages_committed"
    assert completed.checkpoint["stage"] == "question_packages_committed"
    assert completed.progress["completed_question_ids"] == ["q1"]
    assert [(item.q_id, item.max_score) for item in questions] == [("q1", 10)]
    assert len(major_artifacts) == 1


@pytest.mark.asyncio
async def test_restart_reuses_q1_artifact_and_calls_only_unfinished_q2(monkeypatch):
    owner_id, task_id = _seed_task()
    _source, response, _background = await _queue_question_preparation(
        owner_id,
        task_id,
        source_text="1. (a) A (b) B.\n2. Solve C.",
    )
    provider_calls: list[str] = []
    monkeypatch.setattr(
        task_facade,
        "_registry_for_owner",
        lambda _owner: _RecoveryRegistry(),
    )
    monkeypatch.setattr(
        task_preparation,
        "prepare_question_packages",
        _fake_preparer(
            provider_calls,
            question_count=2,
            cancel_after_question="q1",
        ),
    )
    first = _claim(owner_id, response["job_id"], "worker-old")
    with pytest.raises(asyncio.CancelledError):
        await task_preparation.run_durable_question_preparation(first)

    after_crash = workflow_repository.get_operation(
        response["job_id"], owner_id=owner_id
    )
    assert after_crash.status == "running"
    assert after_crash.checkpoint["completed_question_ids"] == ["q1"]
    assert provider_calls == ["q1"]

    _expire_lease(after_crash.id)
    monkeypatch.setattr(
        task_preparation,
        "prepare_question_packages",
        _fake_preparer(provider_calls, question_count=2),
    )
    await task_preparation.run_durable_question_preparation(
        _claim(owner_id, after_crash.id, "worker-new")
    )

    completed = workflow_repository.get_operation(after_crash.id, owner_id=owner_id)
    assert provider_calls == ["q1", "q2"]
    assert completed.status == "done"
    assert completed.checkpoint["completed_question_ids"] == ["q1", "q2"]
    questions = assignment_repository.list_questions(task_id, teacher_id=owner_id)
    assert [question.q_id for question in questions] == ["q1", "q2"]
    with pytest.raises(LeaseLost):
        task_facade._fail_operation(
            task_id,
            owner_id,
            first.operation_id,
            first.attempt,
            "workflow_failed",
            expected_lease_token=first.lease_token,
        )


@pytest.mark.asyncio
async def test_inflight_base_without_artifact_is_not_replayed(monkeypatch):
    owner_id, task_id = _seed_task()
    _source, response, _background = await _queue_question_preparation(
        owner_id,
        task_id,
    )
    provider_calls: list[str] = []
    monkeypatch.setattr(
        task_facade,
        "_registry_for_owner",
        lambda _owner: _RecoveryRegistry(),
    )
    monkeypatch.setattr(
        task_preparation,
        "prepare_question_packages",
        _fake_preparer(
            provider_calls,
            question_count=1,
            cancel_after_extraction_started=True,
        ),
    )
    first = _claim(owner_id, response["job_id"], "worker-old")
    with pytest.raises(asyncio.CancelledError):
        await task_preparation.run_durable_question_preparation(first)

    _expire_lease(first.operation_id)
    provider_was_replayed = False

    async def forbidden_prepare(*args, **kwargs):
        nonlocal provider_was_replayed
        provider_was_replayed = True
        raise AssertionError("provider must not be replayed")

    monkeypatch.setattr(
        task_preparation,
        "prepare_question_packages",
        forbidden_prepare,
    )
    await task_preparation.run_durable_question_preparation(
        _claim(owner_id, first.operation_id, "worker-new")
    )

    failed = workflow_repository.get_operation(first.operation_id, owner_id=owner_id)
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    assert provider_was_replayed is False
    assert failed.status == "error"
    assert failed.error_code == "provider_submit_uncertain"
    assert workflow.last_failed_job_id == failed.id
    assert task_facade._operation_is_retryable(failed) is False
    retry = await task_preparation.retry_question_preparation(
        task_id=task_id,
        job_id=failed.id,
        request=task_preparation.RetryQuestionPreparationRequest(
            recognition_provider_id="test-provider",
            expected_workflow_revision=workflow.workflow_revision,
        ),
        background_tasks=_BackgroundTasks(),
        current=SimpleNamespace(id=owner_id),
        registry=_RecoveryRegistry(),
    )
    assert retry.status_code == 409
    assert json.loads(retry.body)["error"]["code"] == "provider_submit_uncertain"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_code",
    ["provider_rate_limited", "provider_submit_uncertain"],
)
async def test_same_hash_failed_start_returns_failure_without_republishing(
    monkeypatch,
    error_code,
):
    owner_id, task_id = _seed_task()
    source, response, _background = await _queue_question_preparation(
        owner_id,
        task_id,
    )
    first = _claim(owner_id, response["job_id"], "worker-failed")
    assert task_facade._fail_operation(
        task_id,
        owner_id,
        first.operation_id,
        first.attempt,
        error_code,
        expected_lease_token=first.lease_token,
    )

    def forbidden_publish(**_kwargs):
        raise AssertionError("an exact failed start must not publish another attempt")

    monkeypatch.setattr(
        task_facade,
        "publish_checkpointed_operation_atomic",
        forbidden_publish,
    )
    replay = await task_preparation._start_question_preparation(
        task_id=task_id,
        request=task_preparation.StartQuestionPreparationRequest(
            source_tokens=[source["source_token"]],
            expected_workflow_revision=0,
            score_policy=QuestionScorePolicy(
                mode="uniform",
                uniform_max_score=10,
            ),
            recognition_provider_id="test-provider",
        ),
        background_tasks=_BackgroundTasks(),
        current=SimpleNamespace(id=owner_id),
        registry=_RecoveryRegistry(),
        allow_prepared_source_reuse=False,
    )

    persisted = workflow_repository.get_operation(
        first.operation_id,
        owner_id=owner_id,
    )
    with session_scope() as session:
        operation_ids = session.scalars(
            select(workflow_repository.WorkflowOperationRecord.id).where(
                workflow_repository.WorkflowOperationRecord.assignment_id
                == task_id,
                workflow_repository.WorkflowOperationRecord.operation_type
                == "question_preparation",
            )
        ).all()
    assert replay.status_code == 409
    assert json.loads(replay.body)["error"]["code"] == error_code
    assert operation_ids == [first.operation_id]
    assert persisted.status == "error"
    assert persisted.error_code == error_code
    assert persisted.attempt == first.attempt


@pytest.mark.asyncio
async def test_inflight_major_question_without_artifact_is_not_replayed(
    monkeypatch,
):
    owner_id, task_id = _seed_task()
    _source, response, _background = await _queue_question_preparation(
        owner_id,
        task_id,
    )
    provider_calls: list[str] = []
    monkeypatch.setattr(
        task_facade,
        "_registry_for_owner",
        lambda _owner: _RecoveryRegistry(),
    )
    monkeypatch.setattr(
        task_preparation,
        "prepare_question_packages",
        _fake_preparer(
            provider_calls,
            question_count=1,
            cancel_after_question_started="q1",
        ),
    )
    first = _claim(owner_id, response["job_id"], "worker-old")
    with pytest.raises(asyncio.CancelledError):
        await task_preparation.run_durable_question_preparation(first)

    crashed = workflow_repository.get_operation(first.operation_id, owner_id=owner_id)
    assert crashed.checkpoint["provider_inflight_question_ids"] == ["q1"]
    assert provider_calls == []
    _expire_lease(first.operation_id)

    async def forbidden_prepare(*args, **kwargs):
        raise AssertionError("an uncertain major question must not be replayed")

    monkeypatch.setattr(
        task_preparation,
        "prepare_question_packages",
        forbidden_prepare,
    )
    await task_preparation.run_durable_question_preparation(
        _claim(owner_id, first.operation_id, "worker-new")
    )
    failed = workflow_repository.get_operation(first.operation_id, owner_id=owner_id)
    assert failed.status == "error"
    assert failed.error_code == "provider_submit_uncertain"


@pytest.mark.asyncio
async def test_frozen_prepared_source_text_tamper_fails_before_provider(
    monkeypatch,
):
    owner_id, task_id = _seed_task()
    source, response, _background = await _queue_question_preparation(
        owner_id,
        task_id,
    )
    source_operation = workflow_repository.get_operation(
        source["source_token"],
        owner_id=owner_id,
    )
    tampered_payload = dict(source_operation.payload)
    tampered_payload["text"] = f'{tampered_payload["text"]}\nchanged'
    with session_scope() as session:
        session.execute(
            update(workflow_repository.WorkflowOperationRecord)
            .where(
                workflow_repository.WorkflowOperationRecord.id
                == source_operation.id
            )
            .values(payload=tampered_payload)
        )

    monkeypatch.setattr(
        task_facade,
        "_registry_for_owner",
        lambda _owner: _RecoveryRegistry(),
    )

    async def forbidden_prepare(*args, **kwargs):
        raise AssertionError("tampered source must fail before provider use")

    monkeypatch.setattr(
        task_preparation,
        "prepare_question_packages",
        forbidden_prepare,
    )
    operation = _claim(owner_id, response["job_id"], "worker-one")
    await task_preparation.run_durable_question_preparation(operation)
    failed = workflow_repository.get_operation(operation.operation_id, owner_id=owner_id)
    assert failed.status == "error"
    assert failed.error_code == "question_preparation_source_unavailable"


@pytest.mark.asyncio
async def test_same_provider_record_configuration_change_fails_closed(
    monkeypatch,
):
    owner_id, task_id = _seed_task()
    _source, response, _background = await _queue_question_preparation(
        owner_id,
        task_id,
        registry=_RecoveryRegistry(model="model-before"),
    )
    monkeypatch.setattr(
        task_facade,
        "_registry_for_owner",
        lambda _owner: _RecoveryRegistry(model="model-after"),
    )

    async def forbidden_prepare(*args, **kwargs):
        raise AssertionError("changed provider configuration must not be used")

    monkeypatch.setattr(
        task_preparation,
        "prepare_question_packages",
        forbidden_prepare,
    )
    operation = _claim(owner_id, response["job_id"], "worker-one")
    await task_preparation.run_durable_question_preparation(operation)
    failed = workflow_repository.get_operation(operation.operation_id, owner_id=owner_id)
    assert failed.status == "error"
    assert failed.error_code == (
        "question_preparation_provider_configuration_changed"
    )


@pytest.mark.asyncio
async def test_final_artifact_saved_before_crash_commits_without_provider_replay(
    monkeypatch,
):
    owner_id, task_id = _seed_task()
    _source, response, _background = await _queue_question_preparation(
        owner_id,
        task_id,
    )
    provider_calls: list[str] = []
    monkeypatch.setattr(
        task_facade,
        "_registry_for_owner",
        lambda _owner: _RecoveryRegistry(),
    )
    monkeypatch.setattr(
        task_preparation,
        "prepare_question_packages",
        _fake_preparer(provider_calls, question_count=1),
    )
    original_commit = task_facade._replace_draft_questions

    def crash_before_commit(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(task_facade, "_replace_draft_questions", crash_before_commit)
    first = _claim(owner_id, response["job_id"], "worker-old")
    with pytest.raises(asyncio.CancelledError):
        await task_preparation.run_durable_question_preparation(first)
    crashed = workflow_repository.get_operation(first.operation_id, owner_id=owner_id)
    assert crashed.checkpoint["final_artifact_id"] in crashed.artifact_refs
    assert provider_calls == ["q1"]

    _expire_lease(first.operation_id)
    monkeypatch.setattr(task_facade, "_replace_draft_questions", original_commit)

    async def forbidden_prepare(*args, **kwargs):
        raise AssertionError("final artifact must bypass provider preparation")

    monkeypatch.setattr(
        task_preparation,
        "prepare_question_packages",
        forbidden_prepare,
    )
    await task_preparation.run_durable_question_preparation(
        _claim(owner_id, first.operation_id, "worker-new")
    )
    completed = workflow_repository.get_operation(first.operation_id, owner_id=owner_id)
    assert completed.status == "done"
    assert provider_calls == ["q1"]


@pytest.mark.asyncio
async def test_orphan_final_artifact_is_discovered_without_provider_replay(
    monkeypatch,
):
    owner_id, task_id = _seed_task()
    _source, response, _background = await _queue_question_preparation(
        owner_id,
        task_id,
    )
    provider_calls: list[str] = []
    monkeypatch.setattr(
        task_facade,
        "_registry_for_owner",
        lambda _owner: _RecoveryRegistry(),
    )
    monkeypatch.setattr(
        task_preparation,
        "prepare_question_packages",
        _fake_preparer(provider_calls, question_count=1),
    )
    original_read = task_preparation.read_final_question_packages_artifact

    def crash_before_final_checkpoint(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(
        task_preparation,
        "read_final_question_packages_artifact",
        crash_before_final_checkpoint,
    )
    first = _claim(owner_id, response["job_id"], "worker-old")
    with pytest.raises(asyncio.CancelledError):
        await task_preparation.run_durable_question_preparation(first)

    crashed = workflow_repository.get_operation(first.operation_id, owner_id=owner_id)
    assert "final_artifact_id" not in crashed.checkpoint
    assert any(
        item.kind == "question_preparation_packages_v1"
        for item in file_repository.list_files(
            owner_id=owner_id,
            assignment_id=task_id,
        )
    )
    assert provider_calls == ["q1"]

    _expire_lease(first.operation_id)
    monkeypatch.setattr(
        task_preparation,
        "read_final_question_packages_artifact",
        original_read,
    )

    async def forbidden_prepare(*args, **kwargs):
        raise AssertionError("the orphan final artifact must bypass the provider")

    monkeypatch.setattr(
        task_preparation,
        "prepare_question_packages",
        forbidden_prepare,
    )
    await task_preparation.run_durable_question_preparation(
        _claim(owner_id, first.operation_id, "worker-new")
    )
    completed = workflow_repository.get_operation(first.operation_id, owner_id=owner_id)
    assert completed.status == "done"
    assert completed.checkpoint_stage == "question_packages_committed"
    assert provider_calls == ["q1"]


@pytest.mark.asyncio
async def test_expired_pending_http_replay_preserves_attempt_and_checkpoint():
    owner_id, task_id = _seed_task()
    source, response, _background = await _queue_question_preparation(
        owner_id,
        task_id,
    )
    operation = workflow_repository.get_operation(
        response["job_id"], owner_id=owner_id
    )
    with session_scope() as session:
        session.execute(
            update(workflow_repository.WorkflowOperationRecord)
            .where(workflow_repository.WorkflowOperationRecord.id == operation.id)
            .values(expires_at=time.time() - 1)
        )

    replay = await task_preparation._start_question_preparation(
        task_id=task_id,
        request=task_preparation.StartQuestionPreparationRequest(
            source_tokens=[source["source_token"]],
            expected_workflow_revision=0,
            score_policy=QuestionScorePolicy(
                mode="uniform",
                uniform_max_score=10,
            ),
            recognition_provider_id="test-provider",
        ),
        background_tasks=_BackgroundTasks(),
        current=SimpleNamespace(id=owner_id),
        registry=_RecoveryRegistry(),
        allow_prepared_source_reuse=False,
    )
    preserved = workflow_repository.get_operation(operation.id, owner_id=owner_id)
    assert replay["status"] == "already_running"
    assert replay["job_id"] == operation.id
    assert preserved.attempt == operation.attempt
    assert preserved.checkpoint_revision == operation.checkpoint_revision
    assert preserved.artifact_refs == operation.artifact_refs


@pytest.mark.asyncio
async def test_final_commit_rejects_stale_checkpoint_revision_atomically():
    owner_id, task_id = _seed_task()
    _source, response, _background = await _queue_question_preparation(
        owner_id,
        task_id,
    )
    operation = _claim(owner_id, response["job_id"], "worker-one")
    stale_checkpoint_revision = operation.checkpoint_revision
    await operation.checkpoint(
        stage="questions_extracted",
        checkpoint={"stage": "questions_extracted"},
        artifact_refs=[],
    )

    with pytest.raises(VersionConflict) as raised:
        task_facade._replace_draft_questions(
            task_id,
            owner_id,
            _questions(1),
            "questions.txt",
            expected_workflow_revision=1,
            operation_id=operation.operation_id,
            expected_operation_attempt=operation.attempt,
            expected_lease_token=operation.lease_token,
            expected_checkpoint_revision=stale_checkpoint_revision,
            expected_active_operation="question_preparation",
            operation_checkpoint=operation.checkpoint_data,
            operation_checkpoint_stage="question_packages_committed",
            recognition_provider_id="test-provider",
        )
    assert raised.value.code == "stale_checkpoint_revision"
    persisted = workflow_repository.get_operation(
        operation.operation_id,
        owner_id=owner_id,
    )
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    assert persisted.status == "running"
    assert persisted.checkpoint_revision == operation.checkpoint_revision
    assert assignment_repository.list_questions(task_id, teacher_id=owner_id) == []
    assert workflow.active_job_id == operation.operation_id


@pytest.mark.asyncio
async def test_final_commit_rejects_replaced_active_job_atomically():
    owner_id, task_id = _seed_task()
    _source, response, _background = await _queue_question_preparation(
        owner_id,
        task_id,
    )
    operation = _claim(owner_id, response["job_id"], "worker-one")
    with session_scope() as session:
        session.execute(
            update(workflow_repository.AssignmentWorkflowRecord)
            .where(
                workflow_repository.AssignmentWorkflowRecord.assignment_id
                == task_id
            )
            .values(
                active_operation="material_import",
                active_job_id="replacement-operation",
            )
        )

    with pytest.raises(VersionConflict) as raised:
        task_facade._replace_draft_questions(
            task_id,
            owner_id,
            _questions(1),
            "questions.txt",
            expected_workflow_revision=1,
            operation_id=operation.operation_id,
            expected_operation_attempt=operation.attempt,
            expected_lease_token=operation.lease_token,
            expected_checkpoint_revision=operation.checkpoint_revision,
            expected_active_operation="question_preparation",
            operation_checkpoint=operation.checkpoint_data,
            operation_checkpoint_stage="question_packages_committed",
            recognition_provider_id="test-provider",
        )
    assert raised.value.code == "stale_revision"
    persisted = workflow_repository.get_operation(
        operation.operation_id,
        owner_id=owner_id,
    )
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    assert persisted.status == "running"
    assert assignment_repository.list_questions(task_id, teacher_id=owner_id) == []
    assert workflow.active_operation == "material_import"
    assert workflow.active_job_id == "replacement-operation"


@pytest.mark.asyncio
async def test_legacy_expired_preparing_row_republishes_a_fresh_attempt():
    owner_id, task_id = _seed_task()
    registry = _RecoveryRegistry()
    source, first_response, _background = await _queue_question_preparation(
        owner_id,
        task_id,
        registry=registry,
    )
    request = task_preparation.StartQuestionPreparationRequest(
        source_tokens=[source["source_token"]],
        expected_workflow_revision=0,
        score_policy=QuestionScorePolicy(
            mode="uniform",
            uniform_max_score=10,
        ),
        recognition_provider_id="test-provider",
    )
    with session_scope() as session:
        session.execute(
            update(workflow_repository.WorkflowOperationRecord)
            .where(
                workflow_repository.WorkflowOperationRecord.id
                == first_response["job_id"]
            )
            .values(status="preparing", expires_at=time.time() - 1)
        )
        session.execute(
            update(workflow_repository.AssignmentWorkflowRecord)
            .where(
                workflow_repository.AssignmentWorkflowRecord.assignment_id
                == task_id
            )
            .values(
                workflow_revision=0,
                presentation_status="draft",
                active_operation=None,
                active_job_id=None,
                extract_job_id=None,
            )
        )

    preparing = workflow_repository.get_operation(
        first_response["job_id"],
        owner_id=owner_id,
    )
    assert preparing.status == "preparing"
    assert preparing.attempt == 1
    assert preparing.checkpoint_stage == "sources_validated"
    background = _BackgroundTasks()
    resumed = await task_preparation._start_question_preparation(
        task_id=task_id,
        request=request,
        background_tasks=background,
        current=SimpleNamespace(id=owner_id),
        registry=registry,
        allow_prepared_source_reuse=False,
    )
    published = workflow_repository.get_operation(
        preparing.id,
        owner_id=owner_id,
    )
    assert resumed["status"] == "started"
    assert resumed["job_id"] == preparing.id
    assert published.status == "pending"
    assert published.attempt == 2
    assert published.checkpoint_stage == "sources_validated"
    assert background.calls == []


def test_start_request_rejects_duplicate_source_tokens():
    with pytest.raises(PydanticValidationError):
        task_preparation.StartQuestionPreparationRequest(
            source_tokens=["source-one", "source-one"],
            expected_workflow_revision=0,
        )


def test_question_preparation_hash_preserves_source_order():
    common = {
        "logical_input_revision": 3,
        "replace_confirmed": False,
        "generation_policy": "complete_required_materials",
        "score_policy": {"mode": "uniform", "uniform_max_score": 10},
        "recognition_provider_id": "provider-one",
        "provider_configuration_fingerprint": "f" * 64,
    }
    first_then_second = [
        {
            "source_token": "source-one",
            "source_fingerprint": "a" * 64,
            "role": "problem",
        },
        {
            "source_token": "source-two",
            "source_fingerprint": "b" * 64,
            "role": "rubric",
        },
    ]
    assert task_preparation._question_preparation_input_hash(
        ordered_source_inputs=first_then_second,
        **common,
    ) != task_preparation._question_preparation_input_hash(
        ordered_source_inputs=list(reversed(first_then_second)),
        **common,
    )


def test_maximum_major_question_checkpoint_fits_bounded_contract():
    question_ids = [f"q{index}" for index in range(1, 201)]
    artifact_ids = {
        q_id: f"file_{index:032x}"
        for index, q_id in enumerate(question_ids, start=1)
    }
    checkpoint = {
        "stage": "solution_units_generated",
        "question_ids": question_ids,
        "generation_question_ids": question_ids,
        "completed_question_ids": question_ids,
        "failed_question_ids": [],
        "provider_inflight_question_ids": [],
        "question_error_codes": {},
        "question_artifact_ids": artifact_ids,
        "artifact_attempts": {
            artifact_id: 1 for artifact_id in artifact_ids.values()
        },
        "retry_frozen_contract": {
            "contract_version": 1,
            "operation_id": "op-maximum-checkpoint",
            "from_attempt": 1,
            "to_attempt": 2,
            "input_hash": "a" * 64,
            "provider_record_id": "provider-one",
            "source_content_hashes": {"source-one": "b" * 64},
            "source_text_hashes": {"source-one": "c" * 64},
        },
    }
    assert workflow_repository._validate_json_object(
        checkpoint,
        field="checkpoint",
        max_bytes=workflow_repository.MAX_OPERATION_CHECKPOINT_BYTES,
    ) == checkpoint
    assert workflow_repository._validate_artifact_refs(
        list(artifact_ids.values())
    ) == list(artifact_ids.values())


@pytest.mark.parametrize("requested_provider_id", [None, "test-provider"])
@pytest.mark.asyncio
async def test_safe_retry_inherits_frozen_provider_and_reuses_operation_id(
    requested_provider_id,
):
    owner_id, task_id = _seed_task()
    source, response, _background = await _queue_question_preparation(
        owner_id,
        task_id,
    )
    first = _claim(owner_id, response["job_id"], "worker-safe-failure")
    assert task_facade._fail_operation(
        task_id,
        owner_id,
        first.operation_id,
        first.attempt,
        "provider_rate_limited",
        expected_lease_token=first.lease_token,
    )
    with session_scope() as session:
        session.execute(
            update(workflow_repository.WorkflowOperationRecord)
            .where(
                workflow_repository.WorkflowOperationRecord.id
                == source["source_token"]
            )
            .values(expires_at=time.time() - 1)
        )
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)

    retried = await task_preparation.retry_question_preparation(
        task_id=task_id,
        job_id=first.operation_id,
        request=task_preparation.RetryQuestionPreparationRequest(
            expected_workflow_revision=workflow.workflow_revision,
            recognition_provider_id=requested_provider_id,
        ),
        background_tasks=_BackgroundTasks(),
        current=SimpleNamespace(id=owner_id),
        registry=_RecoveryRegistry(),
    )

    persisted = workflow_repository.get_operation(
        first.operation_id,
        owner_id=owner_id,
    )
    assert retried["status"] == "started"
    assert retried["job_id"] == first.operation_id
    assert retried["recognition_provider_id"] == "test-provider"
    assert persisted.attempt == first.attempt + 1
    assert persisted.status == "pending"
    assert persisted.payload["requested_workflow_revision"] == 0
    assert persisted.payload["base_workflow_revision"] == 1
    assert persisted.payload["claimed_workflow_revision"] == 2


@pytest.mark.asyncio
async def test_safe_retry_rejects_explicit_provider_change_before_publication():
    owner_id, task_id = _seed_task()
    _source, response, _background = await _queue_question_preparation(
        owner_id,
        task_id,
    )
    first = _claim(owner_id, response["job_id"], "worker-provider-failure")
    assert task_facade._fail_operation(
        task_id,
        owner_id,
        first.operation_id,
        first.attempt,
        "provider_rate_limited",
        expected_lease_token=first.lease_token,
    )
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)

    rejected = await task_preparation.retry_question_preparation(
        task_id=task_id,
        job_id=first.operation_id,
        request=task_preparation.RetryQuestionPreparationRequest(
            expected_workflow_revision=workflow.workflow_revision,
            recognition_provider_id="different-provider",
        ),
        background_tasks=_BackgroundTasks(),
        current=SimpleNamespace(id=owner_id),
        registry=_RecoveryRegistry(),
    )

    persisted = workflow_repository.get_operation(first.operation_id, owner_id=owner_id)
    assert json.loads(rejected.body)["error"]["code"] == (
        "question_preparation_provider_configuration_changed"
    )
    assert persisted.status == "error"
    assert persisted.attempt == first.attempt
    assert workflow_repository.get_workflow(
        task_id,
        owner_id=owner_id,
    ).workflow_revision == workflow.workflow_revision


@pytest.mark.asyncio
async def test_safe_retry_rejects_frozen_provider_configuration_change():
    owner_id, task_id = _seed_task()
    _source, response, _background = await _queue_question_preparation(
        owner_id,
        task_id,
        registry=_RecoveryRegistry(model="model-before"),
    )
    first = _claim(owner_id, response["job_id"], "worker-provider-config")
    assert task_facade._fail_operation(
        task_id,
        owner_id,
        first.operation_id,
        first.attempt,
        "provider_rate_limited",
        expected_lease_token=first.lease_token,
    )
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)

    rejected = await task_preparation.retry_question_preparation(
        task_id=task_id,
        job_id=first.operation_id,
        request=task_preparation.RetryQuestionPreparationRequest(
            expected_workflow_revision=workflow.workflow_revision,
        ),
        background_tasks=_BackgroundTasks(),
        current=SimpleNamespace(id=owner_id),
        registry=_RecoveryRegistry(model="model-after"),
    )

    persisted = workflow_repository.get_operation(first.operation_id, owner_id=owner_id)
    assert json.loads(rejected.body)["error"]["code"] == (
        "question_preparation_provider_configuration_changed"
    )
    assert persisted.status == "error"
    assert persisted.attempt == first.attempt


def test_stale_worker_cannot_publish_discoverable_preparation_artifact(
    tmp_path,
):
    owner_id, task_id = _seed_task()
    _source, response, _background = asyncio.run(
        _queue_question_preparation(owner_id, task_id)
    )
    stale = _claim(owner_id, response["job_id"], "worker-stale-artifact")
    _expire_lease(stale.operation_id)
    current = _claim(owner_id, stale.operation_id, "worker-current-artifact")
    storage = LocalStorage(tmp_path)
    context = {
        "owner_id": owner_id,
        "task_id": task_id,
        "operation_id": stale.operation_id,
        "attempt": stale.attempt,
        "input_hash": stale.input_hash,
        "provider_record_id": stale.payload["recognition_provider_id"],
        "stage": task_preparation.QUESTIONS_EXTRACTED_STAGE,
    }

    with pytest.raises(LeaseLost):
        task_preparation.save_base_preparation_artifact(
            problem_data=_questions(1),
            issues={},
            storage=storage,
            operation_lease_token=stale.lease_token,
            **context,
        )
    assert task_preparation.find_base_preparation_artifact(
        storage=storage,
        **context,
    ) is None
    assert not any(path.is_file() for path in tmp_path.rglob("*"))

    stored = task_preparation.save_base_preparation_artifact(
        problem_data=_questions(1),
        issues={},
        storage=storage,
        operation_lease_token=current.lease_token,
        **context,
    )
    assert task_preparation.find_base_preparation_artifact(
        storage=storage,
        **context,
    ).id == stored.id


@pytest.mark.asyncio
async def test_safe_retry_reuses_four_successful_questions_and_calls_only_q5(
    monkeypatch,
):
    owner_id, task_id = _seed_task()
    _source, response, _background = await _queue_question_preparation(
        owner_id,
        task_id,
        source_text="\n".join(f"{index}. Question {index}." for index in range(1, 6)),
    )
    provider_calls: list[str] = []
    monkeypatch.setattr(
        task_facade,
        "_registry_for_owner",
        lambda _owner: _RecoveryRegistry(),
    )
    monkeypatch.setattr(
        task_preparation,
        "prepare_question_packages",
        _fake_preparer(
            provider_calls,
            question_count=5,
            known_failure_question="q5",
            record_base_provider_calls=True,
        ),
    )
    first = _claim(owner_id, response["job_id"], "worker-first-attempt")
    await task_preparation.run_durable_question_preparation(first)

    failed = workflow_repository.get_operation(first.operation_id, owner_id=owner_id)
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    assert failed.status == "error"
    assert failed.error_code == "provider_rate_limited"
    assert failed.checkpoint["completed_question_ids"] == [
        "q1", "q2", "q3", "q4",
    ]
    assert failed.checkpoint["failed_question_ids"] == ["q5"]
    assert provider_calls == [
        "questions_extracted",
        "uploaded_materials_aligned",
        "q1", "q2", "q3", "q4", "q5",
    ]

    retried = await task_preparation.retry_question_preparation(
        task_id=task_id,
        job_id=failed.id,
        request=task_preparation.RetryQuestionPreparationRequest(
            expected_workflow_revision=workflow.workflow_revision,
            recognition_provider_id=None,
        ),
        background_tasks=_BackgroundTasks(),
        current=SimpleNamespace(id=owner_id),
        registry=_RecoveryRegistry(),
    )
    pending = workflow_repository.get_operation(failed.id, owner_id=owner_id)
    assert retried["status"] == "started"
    assert pending.attempt == failed.attempt + 1
    assert pending.checkpoint["completed_question_ids"] == [
        "q1", "q2", "q3", "q4",
    ]
    assert pending.checkpoint["failed_question_ids"] == []
    assert pending.checkpoint["retry_frozen_contract"]["from_attempt"] == 1
    assert set(pending.checkpoint["artifact_attempts"].values()) == {1}

    monkeypatch.setattr(
        task_preparation,
        "prepare_question_packages",
        _fake_preparer(
            provider_calls,
            question_count=5,
            record_base_provider_calls=True,
        ),
    )
    await task_preparation.run_durable_question_preparation(
        _claim(owner_id, pending.id, "worker-second-attempt")
    )

    completed = workflow_repository.get_operation(pending.id, owner_id=owner_id)
    assert completed.status == "done"
    assert completed.attempt == 2
    assert completed.checkpoint["completed_question_ids"] == [
        "q1", "q2", "q3", "q4", "q5",
    ]
    assert provider_calls == [
        "questions_extracted",
        "uploaded_materials_aligned",
        "q1", "q2", "q3", "q4", "q5",
        "q5",
    ]
    artifact_attempts = completed.checkpoint["artifact_attempts"]
    inherited_question_ids = failed.checkpoint["question_artifact_ids"]
    assert {
        artifact_attempts[artifact_id]
        for artifact_id in inherited_question_ids.values()
    } == {1}
    assert artifact_attempts[
        completed.checkpoint["question_artifact_ids"]["q5"]
    ] == 2
    assert artifact_attempts[completed.checkpoint["final_artifact_id"]] == 2


@pytest.mark.asyncio
async def test_multiple_safe_retries_keep_mixed_attempt_artifact_lineage(
    monkeypatch,
):
    owner_id, task_id = _seed_task()
    _source, response, _background = await _queue_question_preparation(
        owner_id,
        task_id,
        source_text="\n".join(f"{index}. Question {index}." for index in range(1, 6)),
    )
    provider_calls: list[str] = []
    monkeypatch.setattr(
        task_facade,
        "_registry_for_owner",
        lambda _owner: _RecoveryRegistry(),
    )
    monkeypatch.setattr(
        task_preparation,
        "prepare_question_packages",
        _fake_preparer(
            provider_calls,
            question_count=5,
            known_failure_question="q3",
            record_base_provider_calls=True,
        ),
    )
    await task_preparation.run_durable_question_preparation(
        _claim(owner_id, response["job_id"], "worker-attempt-one")
    )
    attempt_one = workflow_repository.get_operation(
        response["job_id"], owner_id=owner_id
    )
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    await task_preparation.retry_question_preparation(
        task_id=task_id,
        job_id=attempt_one.id,
        request=task_preparation.RetryQuestionPreparationRequest(
            expected_workflow_revision=workflow.workflow_revision,
        ),
        background_tasks=_BackgroundTasks(),
        current=SimpleNamespace(id=owner_id),
        registry=_RecoveryRegistry(),
    )

    monkeypatch.setattr(
        task_preparation,
        "prepare_question_packages",
        _fake_preparer(
            provider_calls,
            question_count=5,
            known_failure_question="q5",
            record_base_provider_calls=True,
        ),
    )
    await task_preparation.run_durable_question_preparation(
        _claim(owner_id, attempt_one.id, "worker-attempt-two")
    )
    attempt_two = workflow_repository.get_operation(
        attempt_one.id, owner_id=owner_id
    )
    assert attempt_two.status == "error"
    assert attempt_two.attempt == 2
    assert attempt_two.checkpoint["completed_question_ids"] == [
        "q1", "q2", "q3", "q4",
    ]
    attempts_after_two = attempt_two.checkpoint["artifact_attempts"]
    question_artifacts_after_two = attempt_two.checkpoint[
        "question_artifact_ids"
    ]
    assert {
        q_id: attempts_after_two[artifact_id]
        for q_id, artifact_id in question_artifacts_after_two.items()
    } == {"q1": 1, "q2": 1, "q3": 2, "q4": 2}

    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    await task_preparation.retry_question_preparation(
        task_id=task_id,
        job_id=attempt_two.id,
        request=task_preparation.RetryQuestionPreparationRequest(
            expected_workflow_revision=workflow.workflow_revision,
        ),
        background_tasks=_BackgroundTasks(),
        current=SimpleNamespace(id=owner_id),
        registry=_RecoveryRegistry(),
    )
    monkeypatch.setattr(
        task_preparation,
        "prepare_question_packages",
        _fake_preparer(
            provider_calls,
            question_count=5,
            record_base_provider_calls=True,
        ),
    )
    await task_preparation.run_durable_question_preparation(
        _claim(owner_id, attempt_two.id, "worker-attempt-three")
    )

    completed = workflow_repository.get_operation(attempt_two.id, owner_id=owner_id)
    assert completed.status == "done"
    assert completed.attempt == 3
    assert provider_calls == [
        "questions_extracted",
        "uploaded_materials_aligned",
        "q1", "q2", "q3",
        "q3", "q4", "q5",
        "q5",
    ]
    final_attempts = completed.checkpoint["artifact_attempts"]
    final_question_artifacts = completed.checkpoint["question_artifact_ids"]
    assert {
        q_id: final_attempts[artifact_id]
        for q_id, artifact_id in final_question_artifacts.items()
    } == {"q1": 1, "q2": 1, "q3": 2, "q4": 2, "q5": 3}


@pytest.mark.asyncio
async def test_safe_retry_rejects_corrupt_inherited_question_artifact(
    monkeypatch,
):
    owner_id, task_id = _seed_task()
    _source, response, _background = await _queue_question_preparation(
        owner_id,
        task_id,
        source_text="1. Question one.\n2. Question two.",
    )
    provider_calls: list[str] = []
    monkeypatch.setattr(
        task_facade,
        "_registry_for_owner",
        lambda _owner: _RecoveryRegistry(),
    )
    monkeypatch.setattr(
        task_preparation,
        "prepare_question_packages",
        _fake_preparer(
            provider_calls,
            question_count=2,
            known_failure_question="q2",
        ),
    )
    first = _claim(owner_id, response["job_id"], "worker-corrupt-first")
    await task_preparation.run_durable_question_preparation(first)
    failed = workflow_repository.get_operation(first.operation_id, owner_id=owner_id)
    q1_artifact_id = failed.checkpoint["question_artifact_ids"]["q1"]
    stored = file_repository.get_file(
        file_id=q1_artifact_id,
        owner_id=owner_id,
    )
    get_storage().save(stored.storage_key, b"{}")
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)

    await task_preparation.retry_question_preparation(
        task_id=task_id,
        job_id=failed.id,
        request=task_preparation.RetryQuestionPreparationRequest(
            expected_workflow_revision=workflow.workflow_revision,
        ),
        background_tasks=_BackgroundTasks(),
        current=SimpleNamespace(id=owner_id),
        registry=_RecoveryRegistry(),
    )
    pending = workflow_repository.get_operation(failed.id, owner_id=owner_id)
    calls_before_retry_worker = list(provider_calls)
    monkeypatch.setattr(
        task_preparation,
        "prepare_question_packages",
        _fake_preparer(provider_calls, question_count=2),
    )
    await task_preparation.run_durable_question_preparation(
        _claim(owner_id, pending.id, "worker-corrupt-second")
    )

    rejected = workflow_repository.get_operation(pending.id, owner_id=owner_id)
    assert rejected.status == "error"
    assert rejected.error_code == "question_preparation_artifact_invalid"
    assert provider_calls == calls_before_retry_worker


@pytest.mark.parametrize("tamper_mode", ["changed", "missing"])
@pytest.mark.asyncio
async def test_safe_retry_rejects_tampered_frozen_lineage_before_provider(
    monkeypatch,
    tamper_mode,
):
    owner_id, task_id = _seed_task()
    _source, response, _background = await _queue_question_preparation(
        owner_id,
        task_id,
    )
    first = _claim(owner_id, response["job_id"], "worker-frozen-first")
    assert task_facade._fail_operation(
        task_id,
        owner_id,
        first.operation_id,
        first.attempt,
        "provider_rate_limited",
        expected_lease_token=first.lease_token,
    )
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    await task_preparation.retry_question_preparation(
        task_id=task_id,
        job_id=first.operation_id,
        request=task_preparation.RetryQuestionPreparationRequest(
            expected_workflow_revision=workflow.workflow_revision,
        ),
        background_tasks=_BackgroundTasks(),
        current=SimpleNamespace(id=owner_id),
        registry=_RecoveryRegistry(),
    )
    pending = workflow_repository.get_operation(first.operation_id, owner_id=owner_id)
    tampered_checkpoint = dict(pending.checkpoint)
    if tamper_mode == "changed":
        tampered_contract = dict(tampered_checkpoint["retry_frozen_contract"])
        tampered_contract["provider_record_id"] = "other-provider"
        tampered_checkpoint["retry_frozen_contract"] = tampered_contract
    else:
        tampered_checkpoint.pop("retry_frozen_contract")
    with session_scope() as session:
        session.execute(
            update(workflow_repository.WorkflowOperationRecord)
            .where(
                workflow_repository.WorkflowOperationRecord.id == pending.id
            )
            .values(checkpoint=tampered_checkpoint)
        )

    provider_called = False

    async def forbidden_prepare(*_args, **_kwargs):
        nonlocal provider_called
        provider_called = True
        raise AssertionError("tampered retry lineage must fail before provider")

    monkeypatch.setattr(
        task_facade,
        "_registry_for_owner",
        lambda _owner: _RecoveryRegistry(),
    )
    monkeypatch.setattr(
        task_preparation,
        "prepare_question_packages",
        forbidden_prepare,
    )
    await task_preparation.run_durable_question_preparation(
        _claim(owner_id, pending.id, "worker-frozen-second")
    )

    rejected = workflow_repository.get_operation(pending.id, owner_id=owner_id)
    assert rejected.status == "error"
    assert rejected.error_code == "question_preparation_contract_invalid"
    assert provider_called is False


def test_atomic_publish_same_hash_has_one_publisher_and_one_replay():
    owner_id, task_id = _seed_task()
    input_hash = "a" * 64
    call = lambda: _publish_atomic_for_test(
        owner_id=owner_id,
        task_id=task_id,
        input_hash=input_hash,
    )

    with _force_two_publishers_to_reach_sqlite_write_gate() as hits:
        results = _run_publish_pair(call, call)

    assert hits == {"operation_gate": 2}
    assert all(result["kind"] == "ok" for result in results), results
    assert sorted(result["published"] for result in results) == [False, True]
    assert len({result["id"] for result in results}) == 1
    with session_scope() as session:
        rows = session.scalars(
            select(workflow_repository.WorkflowOperationRecord).where(
                workflow_repository.WorkflowOperationRecord.assignment_id
                == task_id
            )
        ).all()
        workflow = session.get(
            workflow_repository.AssignmentWorkflowRecord,
            task_id,
        )
    assert len(rows) == 1
    assert rows[0].status == "pending"
    assert rows[0].attempt == 1
    assert workflow is not None
    assert workflow.workflow_revision == 1
    assert workflow.active_job_id == rows[0].id


def test_atomic_publish_different_hash_same_revision_has_no_orphan():
    owner_id, task_id = _seed_task()
    first = lambda: _publish_atomic_for_test(
        owner_id=owner_id,
        task_id=task_id,
        input_hash="b" * 64,
    )
    second = lambda: _publish_atomic_for_test(
        owner_id=owner_id,
        task_id=task_id,
        input_hash="c" * 64,
    )

    with _force_two_publishers_to_reach_sqlite_write_gate() as hits:
        results = _run_publish_pair(first, second)

    assert hits == {"operation_gate": 2}
    assert sum(
        result["kind"] == "ok" and result["published"]
        for result in results
    ) == 1
    assert [
        result for result in results if result["kind"] == "domain_error"
    ] == [{"kind": "domain_error", "code": "workflow_busy"}]
    assert not [
        result for result in results if result["kind"] == "unexpected"
    ], results
    with session_scope() as session:
        rows = session.scalars(
            select(workflow_repository.WorkflowOperationRecord).where(
                workflow_repository.WorkflowOperationRecord.assignment_id
                == task_id
            )
        ).all()
        workflow = session.get(
            workflow_repository.AssignmentWorkflowRecord,
            task_id,
        )
    assert len(rows) == 1
    assert workflow is not None
    assert workflow.workflow_revision == 1
    assert workflow.active_job_id == rows[0].id


def test_atomic_publish_uncertain_double_click_never_resets_attempt():
    owner_id, task_id = _seed_task()
    input_hash = "d" * 64
    first, published, claimed_revision = _publish_atomic_for_test(
        owner_id=owner_id,
        task_id=task_id,
        input_hash=input_hash,
    )
    assert published is True
    with session_scope() as session:
        session.execute(
            update(workflow_repository.WorkflowOperationRecord)
            .where(workflow_repository.WorkflowOperationRecord.id == first.id)
            .values(
                status="error",
                error_code="provider_submit_uncertain",
                expires_at=None,
            )
        )
        session.execute(
            update(workflow_repository.AssignmentWorkflowRecord)
            .where(
                workflow_repository.AssignmentWorkflowRecord.assignment_id
                == task_id
            )
            .values(
                presentation_status="error",
                active_operation=None,
                active_job_id=None,
            )
        )

    call = lambda: _publish_atomic_for_test(
        owner_id=owner_id,
        task_id=task_id,
        input_hash=input_hash,
        expected_revision=claimed_revision,
        retry_id=first.id,
        retry_attempt=first.attempt,
    )
    with _force_two_publishers_to_reach_sqlite_write_gate() as hits:
        results = _run_publish_pair(call, call)

    assert hits == {"operation_gate": 2}
    assert all(
        result["kind"] == "ok" and result["published"] is False
        for result in results
    ), results
    persisted = workflow_repository.get_operation(first.id, owner_id=owner_id)
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    assert persisted.attempt == 1
    assert persisted.status == "error"
    assert persisted.error_code == "provider_submit_uncertain"
    assert persisted.checkpoint_revision == 1
    assert workflow.workflow_revision == claimed_revision
    assert workflow.active_job_id is None
