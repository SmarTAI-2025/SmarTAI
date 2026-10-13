from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock
import pytest
from backend.services import task_facade


@pytest.mark.parametrize("kind,comment", [
    ("programming_execution_unavailable", "localized error"),
    ("general", "该题未执行：生产环境禁止宿主代码执行。隔离执行尚未接通，请人工复核。"),
])
def test_new_and_persisted_execution_errors_retain_specific_reason(monkeypatch, kind, comment):
    run = Obj(id="run", status="partial_failed", error_message=None)
    monkeypatch.setattr(task_facade.grading_repository, "list_results_for_run", lambda _: [Obj(effective_score=None, ai_expert_results=[{"error_kind":kind,"comment":comment}])])
    assert task_facade._grading_failure_code(run) == "programming_execution_unavailable"


@pytest.mark.asyncio
async def test_partial_grading_failure_replaces_stale_submission_progress(monkeypatch):
    monkeypatch.setattr(task_facade, "task_state", lambda **_: {"status":"error", "grading_job_id":"run", "last_failed_job_id":"run", "progress":{"phase":"consolidating_submission_results"}})
    monkeypatch.setattr(task_facade, "get_reporter", lambda _: None)
    monkeypatch.setattr(task_facade, "_grading_progress", lambda *_: {"phase":"done", "current_step":"completed", "error_detail":"programming_execution_unavailable"})
    result = await task_facade.async_task_state(task_id="task", owner_id="owner")
    assert result["progress"]["error_detail"] == "programming_execution_unavailable"
    assert result["progress"]["phase"] != "consolidating_submission_results"
