"""Keep source timeout changes scoped to new, durably frozen parent work.

These tests exercise real publication/retry transactions and stop at a mocked
source reader. They never submit OCR or structure requests to a model.
"""
from __future__ import annotations

import hashlib
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.api import task_preparation, tasks
from backend.db import file_repository, workflow_repository
from backend.domain.errors import RecognitionError
from backend.models import ProblemSourceDraft, QuestionScorePolicy
from backend.progress.tracker import ProgressReporter
from backend.services import task_facade
from backend.services.question_sources import QuestionSourceRead
from backend.services.stage_provider_routing import StageProviderRoute
from backend.storage import get_storage
from backend.tests.test_question_preparation_recovery import (
    _RecoveryRegistry,
    _claim,
    _queue_question_preparation,
)
from backend.tests.test_task_background_workflows import (
    _BackgroundTasks,
    _Registry,
    _seed_task,
)


PROFILE_KEY = "recognition_timeout_profile"
NEW_PROFILE = "source-response-300-v1"
PR245_PROFILE = "source-300-v1"
TIMEOUT_PROFILES = pytest.mark.parametrize(
    "profile", [NEW_PROFILE, None, PR245_PROFILE], ids=["new", "pre245", "pr245"],
)


def _freeze_parent(owner_id, operation_id, profile):
    operation = workflow_repository.get_operation(operation_id, owner_id=owner_id)
    assert operation.payload[PROFILE_KEY] == NEW_PROFILE
    if profile != NEW_PROFILE:
        # Simulate an already-published job from before this rollout, without
        # changing its input hash, sources, checkpoint or source artifacts.
        payload = dict(operation.payload)
        if profile is None:
            payload.pop(PROFILE_KEY)
        else:
            payload[PROFILE_KEY] = profile
        operation = workflow_repository.update_operation(
            operation.id,
            owner_id=owner_id,
            expected_attempt=operation.attempt,
            payload=payload,
        )
    return operation


def _assert_frozen_profile(operation, profile):
    # Missing and explicit None both keep the oldest policy. A PR245 parent
    # keeps its original profile even when its retry is published by new code.
    assert operation.payload.get(PROFILE_KEY) == profile


def _fail_claimed(owner_id, task_id, job_id, error_code):
    operation = _claim(owner_id, job_id, "timeout-profile-test")
    assert task_facade._fail_operation(
        task_id,
        owner_id,
        operation.operation_id,
        operation.attempt,
        error_code,
        expected_lease_token=operation.lease_token,
    )
    return workflow_repository.get_operation(job_id, owner_id=owner_id)


@pytest.mark.asyncio
@TIMEOUT_PROFILES
async def test_question_start_freezes_profile_without_upgrading_idempotent_replay(profile):
    owner, task = _seed_task()
    source, queued, _ = await _queue_question_preparation(owner, task)
    original = _freeze_parent(owner, queued["job_id"], profile)
    source_operation = workflow_repository.get_operation(source["source_token"], owner_id=owner)
    assert PROFILE_KEY not in source_operation.payload

    replay = await task_preparation._start_question_preparation(
        task_id=task,
        request=task_preparation.StartQuestionPreparationRequest(
            source_tokens=[source["source_token"]],
            expected_workflow_revision=0,
            score_policy=QuestionScorePolicy(mode="uniform", uniform_max_score=10),
            recognition_provider_id="test-provider",
        ),
        background_tasks=_BackgroundTasks(),
        current=SimpleNamespace(id=owner),
        registry=_RecoveryRegistry(),
        allow_prepared_source_reuse=False,
    )
    assert isinstance(replay, dict), getattr(replay, "body", replay)
    assert replay["job_id"] == original.id
    after = workflow_repository.get_operation(original.id, owner_id=owner)
    _assert_frozen_profile(after, profile)
    assert after.input_hash == original.input_hash
    assert after.attempt == original.attempt
    assert after.payload == original.payload
    assert after.checkpoint == original.checkpoint


@pytest.mark.asyncio
@TIMEOUT_PROFILES
@pytest.mark.parametrize("retry_kind", ["same_operation", "new_configuration", "uncertain"])
async def test_question_retry_inherits_timeout_profile_and_preserves_uncertainty(profile, retry_kind):
    owner, task = _seed_task()
    _, queued, _ = await _queue_question_preparation(owner, task)
    _freeze_parent(owner, queued["job_id"], profile)
    uncertain = retry_kind == "uncertain"
    failed = _fail_claimed(
        owner, task, queued["job_id"],
        "provider_submit_uncertain" if uncertain else "provider_rate_limited",
    )
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    kwargs = dict(
        task_id=task,
        job_id=failed.id,
        background_tasks=_BackgroundTasks(),
        current=SimpleNamespace(id=owner),
        registry=_RecoveryRegistry("changed-model" if retry_kind == "new_configuration" else "test-model"),
    )
    if uncertain:
        denied = await task_preparation.retry_question_preparation(
            **kwargs,
            request=task_preparation.RetryQuestionPreparationRequest(
                expected_workflow_revision=workflow.workflow_revision,
            ),
        )
        assert denied.status_code == 409
        assert json.loads(denied.body)["error"]["code"] == "provider_submit_uncertain"

    retried = await task_preparation.retry_question_preparation(
        **kwargs,
        request=task_preparation.RetryQuestionPreparationRequest(
            expected_workflow_revision=workflow.workflow_revision,
            use_current_configuration=retry_kind == "new_configuration",
            acknowledge_possible_duplicate_call=uncertain,
        ),
    )
    assert isinstance(retried, dict), getattr(retried, "body", retried)
    resumed = workflow_repository.get_operation(retried["job_id"], owner_id=owner)
    _assert_frozen_profile(resumed, profile)
    if retry_kind == "same_operation":
        assert resumed.id == failed.id
        assert resumed.input_hash == failed.input_hash
        assert resumed.attempt == failed.attempt + 1
    else:
        assert resumed.id != failed.id
        assert resumed.payload["retry_origin_id"] == failed.id
        original = workflow_repository.get_operation(failed.id, owner_id=owner)
        assert original.input_hash == failed.input_hash
        assert original.payload == failed.payload
        assert original.checkpoint == failed.checkpoint
        assert original.error_code == failed.error_code


@pytest.mark.asyncio
@TIMEOUT_PROFILES
async def test_saved_question_and_reference_sources_use_the_parent_frozen_profile(monkeypatch, profile):
    owner, task = _seed_task()
    sources = []
    for role in ("problem", "reference_answer", "rubric", "programming_tests"):
        content = f"Synthetic {role} source".encode()
        filename = f"{role}.txt"
        stored = file_repository.save_file(
            storage=get_storage(), owner_id=owner, assignment_id=task,
            kind="problem_source", original_name=filename,
            content=content, content_type="text/plain",
        )
        draft = ProblemSourceDraft(
            source_token=f"source-{role}", task_id=task, owner_id=owner,
            role=role, source_kind="upload", structure_mode="organized",
            filename=filename, content_type="text/plain", size_bytes=len(content),
            content_sha256=hashlib.sha256(content).hexdigest(), expires_at=time.time() + 600,
        )
        sources.append((draft, "", {
            "recognition_pending": True,
            "source_ref": {"stored_file_id": stored.id},
            "enable_material_ocr": True,
        }))

    read = AsyncMock(side_effect=lambda **kw: QuestionSourceRead(
        "1. Synthetic recognized source.", None, kw["stored_file_id"],
    ))
    monkeypatch.setattr(task_preparation, "read_question_source", read)
    operation = SimpleNamespace(
        owner_id=owner, assignment_id=task, operation_id="timeout-profile-parent",
        attempt=1, payload={} if profile is None else {PROFILE_KEY: profile},
        update_progress=AsyncMock(),
    )
    result = await task_preparation._recognize_saved_question_sources(
        sources,
        operation=operation,
        route=StageProviderRoute(route_id="test-provider", kind="llm", provider=SimpleNamespace()),
        registry=None,
        reporter=ProgressReporter("timeout-profile-parent"),
    )
    assert len(result) == 4
    assert [call.kwargs["purpose"] for call in read.await_args_list] == [
        "problems", "reference", "rubric", "test_cases",
    ]
    assert [call.kwargs["timeout_profile"] for call in read.await_args_list] == [
        profile,
    ] * 4


@pytest.mark.asyncio
@TIMEOUT_PROFILES
async def test_submission_configuration_restart_uses_new_profile_and_execution_scope(monkeypatch, profile):
    owner, task = _seed_task(with_question=True)
    registry = _Registry()
    kwargs = dict(task_id=task, owner_id=owner, filename="student.txt",
                  content=b"Synthetic student answer", content_type="text/plain", registry=registry)
    queued = task_facade.queue_task_submission_parsing(**kwargs)
    _freeze_parent(owner, queued["job_id"], profile)
    failed = _fail_claimed(owner, task, queued["job_id"], "provider_rate_limited")

    replay = task_facade.queue_task_submission_parsing(**kwargs)
    resumed = workflow_repository.get_operation(replay["job_id"], owner_id=owner)
    _assert_frozen_profile(resumed, NEW_PROFILE)
    assert resumed.id != failed.id
    assert resumed.input_hash != failed.input_hash
    assert resumed.attempt == 1
    assert resumed.payload["recognition_execution_scope"] != failed.payload["recognition_execution_scope"]
    assert resumed.payload["input_file_id"] == failed.payload["input_file_id"]

    read = AsyncMock(side_effect=RecognitionError("provider_rate_limited"))
    monkeypatch.setattr(task_facade, "read_question_source", read)
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _owner: registry)
    await task_facade.run_durable_submission_recognition(_claim(owner, resumed.id, "replayed-reader"))
    read.assert_awaited_once()
    assert read.await_args.kwargs["timeout_profile"] == NEW_PROFILE


@pytest.mark.asyncio
@TIMEOUT_PROFILES
@pytest.mark.parametrize("uncertain", [False, True], ids=["safe_retry", "acknowledged_retry"])
async def test_submission_retry_from_inherits_profile_without_bypassing_ack(monkeypatch, profile, uncertain):
    owner, task = _seed_task(with_question=True)
    registry = _Registry()
    queued = task_facade.queue_task_submission_parsing(
        task_id=task, owner_id=owner, filename="student.txt", content=b"Synthetic answer",
        content_type="text/plain", registry=registry,
    )
    _freeze_parent(owner, queued["job_id"], profile)
    failed = _fail_claimed(
        owner, task, queued["job_id"],
        "provider_submit_uncertain" if uncertain else "provider_rate_limited",
    )
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    read = AsyncMock(side_effect=RecognitionError("provider_rate_limited"))
    monkeypatch.setattr(task_facade, "read_question_source", read)
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _owner: registry)
    kwargs = dict(task_id=task, job_id=failed.id, background_tasks=_BackgroundTasks(),
                  current=SimpleNamespace(id=owner), registry=registry)
    if uncertain:
        denied = await tasks.retry_submission_recognition_endpoint(
            **kwargs,
            request=tasks.RetrySubmissionRecognitionRequest(
                expected_workflow_revision=workflow.workflow_revision,
            ),
        )
        assert denied.status_code == 409
        assert json.loads(denied.body)["error"]["code"] == "provider_submit_uncertain"
        read.assert_not_awaited()

    retried = await tasks.retry_submission_recognition_endpoint(
        **kwargs,
        request=tasks.RetrySubmissionRecognitionRequest(
            expected_workflow_revision=workflow.workflow_revision,
            acknowledge_possible_duplicate_call=uncertain,
        ),
    )
    assert isinstance(retried, dict), getattr(retried, "body", retried)
    resumed = workflow_repository.get_operation(retried["job_id"], owner_id=owner)
    _assert_frozen_profile(resumed, profile)
    assert resumed.id != failed.id
    assert resumed.payload["retry_from"] == {"operation_id": failed.id, "attempt": failed.attempt}
    original = workflow_repository.get_operation(failed.id, owner_id=owner)
    assert original.payload == failed.payload
    assert original.input_hash == failed.input_hash
    assert original.checkpoint == failed.checkpoint
    read.assert_not_awaited()

    await task_facade.run_durable_submission_recognition(_claim(owner, resumed.id, "retry-reader"))
    read.assert_awaited_once()
    assert read.await_args.kwargs["timeout_profile"] == profile


@pytest.mark.asyncio
@TIMEOUT_PROFILES
@pytest.mark.parametrize("error_code", ["provider_submit_uncertain", "operation_cancelled"])
async def test_submission_saved_upload_restart_inherits_profile_without_retry_from(monkeypatch, profile, error_code):
    owner, task = _seed_task(with_question=True)
    registry = _Registry()
    queued = task_facade.queue_task_submission_parsing(
        task_id=task, owner_id=owner, filename="student.txt", content=b"Saved student answer",
        content_type="text/plain", registry=registry,
    )
    _freeze_parent(owner, queued["job_id"], profile)
    failed = _fail_claimed(owner, task, queued["job_id"], error_code)
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    read = AsyncMock(side_effect=RecognitionError("provider_rate_limited"))
    monkeypatch.setattr(task_facade, "read_question_source", read)
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _owner: registry)

    # This upload form is a second supported restart entry point. It supplies
    # acknowledged_restart_from, whereas the /retry endpoint supplies retry_from.
    restarted = await tasks.parse_submissions_endpoint(
        task_id=task, file=None, stored_file_id=failed.payload["input_file_id"],
        identity_mode="filename", roster_file=None, recognition_provider_id="test-provider",
        replace_confirmed=False, expected_workflow_revision=workflow.workflow_revision,
        acknowledge_possible_duplicate_call=True, background_tasks=_BackgroundTasks(),
        current=SimpleNamespace(id=owner), registry=registry,
    )
    assert isinstance(restarted, dict), getattr(restarted, "body", restarted)
    resumed = workflow_repository.get_operation(restarted["job_id"], owner_id=owner)
    _assert_frozen_profile(resumed, profile)
    assert resumed.id != failed.id
    assert resumed.payload["acknowledged_restart_from"] == f"{failed.id}:{failed.attempt}"
    original = workflow_repository.get_operation(failed.id, owner_id=owner)
    assert original.payload == failed.payload
    assert original.input_hash == failed.input_hash
    assert original.checkpoint == failed.checkpoint
    read.assert_not_awaited()

    await task_facade.run_durable_submission_recognition(_claim(owner, resumed.id, "upload-restart-reader"))
    read.assert_awaited_once()
    assert read.await_args.kwargs["timeout_profile"] == profile
