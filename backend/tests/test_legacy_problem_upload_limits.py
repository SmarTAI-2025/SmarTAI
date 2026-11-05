"""Compatibility extraction rejects invalid byte counts before queue writes."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import BackgroundTasks, HTTPException

from backend.api import tasks
from backend.api.task_preparation import MAX_SOURCE_BYTES


async def invoke(file):
    return await tasks.extract_problems_endpoint(
        task_id="task", background_tasks=BackgroundTasks(), file=file,
        source_token=None, confirmed_candidate_ids="[]", replace_confirmed=False,
        current=SimpleNamespace(id="teacher"), registry=object(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("content,status,code", [
    (b"", 400, "source_empty"),
    (b"x" * (MAX_SOURCE_BYTES + 1), 413, "source_too_large"),
], ids=["empty", "oversized"])
async def test_invalid_bytes_rejected_before_queue(monkeypatch, content, status, code):
    queue = Mock(side_effect=AssertionError("must not write or enqueue"))
    monkeypatch.setattr(tasks.task_facade, "queue_task_problem_extraction", queue)
    file = SimpleNamespace(filename="questions.csv", content_type="text/csv", read=AsyncMock(return_value=content))
    with pytest.raises(HTTPException) as caught:
        await invoke(file)
    assert caught.value.status_code == status
    assert caught.value.detail["code"] == code
    if status == 413:
        assert caught.value.detail["max_bytes"] == MAX_SOURCE_BYTES
    file.read.assert_awaited_once_with(MAX_SOURCE_BYTES + 1)
    queue.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("filename,content_type", [("questions.csv", "text/csv"), ("questions.rst", "text/plain"), ("questions.pdf", "application/pdf")])
async def test_small_legacy_format_still_reaches_queue(monkeypatch, filename, content_type):
    content = b"legacy source bytes"
    queue = Mock(return_value={"status": "queued"})
    monkeypatch.setattr(tasks.task_facade, "queue_task_problem_extraction", queue)
    file = SimpleNamespace(filename=filename, content_type=content_type, read=AsyncMock(return_value=content))
    assert await invoke(file) == {"status": "queued"}
    file.read.assert_awaited_once_with(MAX_SOURCE_BYTES + 1)
    assert queue.call_args.kwargs["filename"] == filename
    assert queue.call_args.kwargs["content"] == content
    assert queue.call_args.kwargs["content_type"] == content_type
