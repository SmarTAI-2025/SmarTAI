"""Contracts reproduced against the official providers on 2026-10-04."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage

from backend.llm.image_capability import explicitly_rejects_images
from backend.llm.providers import LLMResponse, SafeRelayProvider, _response_metadata
from backend.models import ProviderConfig
from backend.services.background_errors import classify_background_error
from backend.services.question_sources import question_recognition_options
from backend.tests.test_recognition_reader import llm, request
from backend.tools.structured_llm import PermanentLLMError, ainvoke_with_retry
from backend.tools.structured_llm import extract_and_parse_json
from pydantic import BaseModel


@pytest.mark.asyncio
async def test_upload_saves_original_before_recognition_and_worker_reports_failure(monkeypatch):
    from io import BytesIO
    from fastapi import UploadFile
    from backend.api import task_preparation as api
    from backend.db import workflow_repository
    from backend.domain.errors import RecognitionError
    from backend.services import task_facade
    from backend.tests.test_question_preparation_recovery import _RecoveryRegistry, _claim
    from backend.tests.test_task_background_workflows import _BackgroundTasks, _seed_task
    from backend.tests.test_recognition_agent import pdf
    owner, task = _seed_task()
    registry = _RecoveryRegistry()
    read = AsyncMock(side_effect=RecognitionError("provider_recitation_blocked", failed_pages=[1]))
    monkeypatch.setattr(api, "read_question_source", read)
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: registry)
    source = await api.preflight_problem_source(
        task_id=task, file=UploadFile(filename="test.pdf", file=BytesIO(pdf(["Exercise 1. Explain A."]))),
        library_material_id=None, stored_file_id=None, inline_text=None, role="problem",
        structure_mode="organized", extraction_hint="", save_to_library=False,
        recognition_provider_id="test-provider", defer_recognition=True,
        current=SimpleNamespace(id=owner), registry=registry,
    )
    assert source["status"] == "ready" and source["source"]["stored_file_id"]
    read.assert_not_awaited()
    started = await api.start_question_preparation(task_id=task,
        request=api.StartQuestionPreparationRequest(source_tokens=[source["source_token"]],
            expected_workflow_revision=0, recognition_provider_id="test-provider"),
        background_tasks=_BackgroundTasks(), current=SimpleNamespace(id=owner), registry=registry)
    read.assert_not_awaited()
    await api.run_durable_question_preparation(_claim(owner, started["job_id"], "regression-worker"))
    read.assert_awaited_once()
    job = workflow_repository.get_operation(started["job_id"], owner_id=owner)
    assert job.error_code == "provider_recitation_blocked"
    assert job.progress["recognition_failure"]["failed_pages"] == [1]
    assert job.payload["source_refs"][0]["stored_file_id"] == source["source"]["stored_file_id"]
