"""Exercise the saved-source worker, PDF batches and durable child together."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
import hashlib
import time

import pytest

from backend.api import task_preparation
from backend.db import file_repository
from backend.domain.errors import RecognitionError
from backend.models import ProblemSourceDraft
from backend.progress.tracker import ProgressReporter
from backend.services import question_sources
from backend.services.stage_provider_routing import StageProviderRoute
from backend.tests.test_recognition_v2_artifacts import setup


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_attempt,new_parent", [(2, False), (1, True)])
async def test_saved_pdf_explicit_retry_reaches_failed_leaf(tmp_path, monkeypatch, retry_attempt, new_parent):
    agent, request, data, results = setup(tmp_path, pdf=True, pages=2, initial=[
        "Q1. x = -2", RecognitionError("provider_upstream_unavailable", submission_may_exist=True),
    ])
    agent.engine.decision = "keep_visual"
    storage = results.store.storage
    source = file_repository.save_file(
        storage=storage, owner_id=request.source.owner_id, assignment_id=request.source.business_id,
        kind="problem_source", original_name="questions.pdf", content=data, content_type="application/pdf",
    )
    monkeypatch.setattr(task_preparation, "get_storage", lambda: storage)
    monkeypatch.setattr(question_sources, "get_storage", lambda: storage)

    @asynccontextmanager
    async def engine(**_kwargs):
        yield agent.engine

    monkeypatch.setattr(question_sources, "recognition_engine", engine)
    draft = ProblemSourceDraft(
        source_token="synthetic", task_id=request.source.business_id, owner_id=request.source.owner_id,
        source_kind="upload", structure_mode="organized", filename="questions.pdf",
        content_type="application/pdf", size_bytes=len(data), content_sha256=hashlib.sha256(data).hexdigest(),
        expires_at=time.time() + 600,
    )
    sources = [(draft, "", {"recognition_pending": True, "source_ref": {"stored_file_id": source.id}})]
    operation = SimpleNamespace(owner_id=draft.owner_id, assignment_id=draft.task_id,
                                operation_id="parent", attempt=1, payload={}, update_progress=AsyncMock())
    route = StageProviderRoute(route_id="selected", kind="llm", provider=SimpleNamespace())

    async def read():
        return await task_preparation._recognize_saved_question_sources(
            sources, operation=operation, route=route, registry=None, reporter=ProgressReporter("synthetic-retry"))

    with pytest.raises(RecognitionError) as first:
        await read()
    assert first.value.code == "provider_submit_uncertain"
    before = len(agent.engine.reads)
    assert before > 0
    with pytest.raises(RecognitionError):
        await read()
    assert len(agent.engine.reads) == before
    agent.engine.initial[1] = "Q2. x = -2"
    operation.attempt = retry_attempt
    if new_parent:
        operation.operation_id = "acknowledged-parent"
        operation.payload = {"retry_origin_id": "parent", "acknowledged_restart_from": "parent:1"}
    recovered = await read()
    assert "Q1" in recovered[0][1] and "Q2" in recovered[0][1]
    assert recovered[0][2]["recognition"]["coverage"]["processed_pages"] == [1, 2]
    assert all(call.page_number == 2 for call in agent.engine.reads[before:])
    assert len(agent.engine.reads) == before + 1
    await read()
    assert len(agent.engine.reads) == before + 1
