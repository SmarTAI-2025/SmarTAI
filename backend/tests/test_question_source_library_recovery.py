"""Library retention must not invalidate an uploaded preparation source."""

import io
from types import SimpleNamespace

import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers

from backend.api import task_preparation
from backend.db import workflow_repository
from backend.models import QuestionScorePolicy
from backend.services import task_facade
from backend.tests.test_question_preparation_recovery import (
    _RecoveryRegistry, _claim, _fake_preparer,
)
from backend.tests.test_task_background_workflows import _BackgroundTasks, _seed_task


@pytest.mark.asyncio
@pytest.mark.parametrize("save_to_library,retry_existing", [(False, False), (True, False), (True, True)])
async def test_uploaded_source_runs_after_optional_library_save(monkeypatch, save_to_library, retry_existing):
    owner_id, task_id = _seed_task()
    registry = _RecoveryRegistry()
    source = await task_preparation.preflight_problem_source(
        task_id=task_id,
        file=UploadFile(io.BytesIO(b"1. Explain A."), filename="questions.txt",
                        headers=Headers({"content-type": "text/plain"})),
        library_material_id=None, stored_file_id=None, inline_text=None,
        structure_mode="organized", role="problem", extraction_hint="",
        save_to_library=save_to_library, recognition_provider_id="test-provider",
        current=SimpleNamespace(id=owner_id), registry=registry,
    )
    assert isinstance(source, dict), source
    assert bool(source["source"]["library_material_id"]) is save_to_library
    queued = await task_preparation._start_question_preparation(
        task_id=task_id,
        request=task_preparation.StartQuestionPreparationRequest(
            source_tokens=[source["source_token"]], expected_workflow_revision=0,
            score_policy=QuestionScorePolicy(mode="uniform", uniform_max_score=10),
            recognition_provider_id="test-provider",
        ),
        background_tasks=_BackgroundTasks(), current=SimpleNamespace(id=owner_id),
        registry=registry, allow_prepared_source_reuse=False,
    )
    calls = []
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: registry)
    monkeypatch.setattr(task_preparation, "prepare_question_packages", _fake_preparer(calls, question_count=1))
    if retry_existing:
        # Persist the old failure without modifying the prepared source or hash.
        claimed = _claim(owner_id, queued["job_id"], "old-worker")
        task_facade._fail_operation(task_id, owner_id, claimed.operation_id, claimed.attempt,
                                    "question_preparation_source_unavailable",
                                    expected_lease_token=claimed.lease_token)
        workflow = workflow_repository.get_live_workflow(task_id, owner_id=owner_id)
        retried = await task_preparation.retry_question_preparation(
            task_id=task_id, job_id=queued["job_id"],
            request=task_preparation.RetryQuestionPreparationRequest(
                expected_workflow_revision=workflow.workflow_revision,
                recognition_provider_id="test-provider",
            ),
            background_tasks=_BackgroundTasks(), current=SimpleNamespace(id=owner_id), registry=registry,
        )
        assert retried["job_id"] == queued["job_id"]
    await task_preparation.run_durable_question_preparation(_claim(owner_id, queued["job_id"], "worker"))
    operation = workflow_repository.get_operation(queued["job_id"], owner_id=owner_id)
    assert operation.status == "done", operation.error_code
    assert calls


def test_library_selected_source_keeps_material_identity_in_fingerprint():
    source = {"source_kind": "library", "library_material_id": "material-one", "sha256": "same-bytes"}
    assert task_preparation._source_fingerprint(source) != task_preparation._source_fingerprint(
        {**source, "library_material_id": "material-two"}
    )
