"""Public recognition progress must remain factual, scoped and safe."""
import asyncio
import json
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest

from backend.models import ProgressEvent
from backend.progress.recognition_activity import observe_recognition, public_progress, report_provider_state
from backend.progress.tracker import get_or_create_reporter, remove_reporter


def private_snapshot():
    return {
        "job_id": "recognition-test", "phase": "parsing", "workflow": "question_preparation",
        "total_students": 10, "completed_units": 3, "total_steps": 8, "completed_steps": 3,
        "current_step": "recognition_cache_lookup", "stage_sequence": ["generating_solutions", "PRIVATE_SKILL"],
        "stage_metrics": {"files_total": 10, "files_processed": 3, "PRIVATE_COUNTER": 9},
        "active": [{"skill": "PRIVATE_SKILL", "student_id": "PRIVATE_STUDENT"}],
        "messages": [{"ts": 1, "message": "PRIVATE_PROMPT", "level": "info", "unit": {"skill": "PRIVATE_SKILL"}},
                     {"ts": 2, "message": "Reading source 1/2: PRIVATE_FILENAME", "level": "info"},
                     {"ts": 3, "message": "activity_retrying", "recognition": {"state": "retrying", "reason": "timeout", "attempt": 2, "max_attempts": 3, "retry_at": 10, "prompt": "PRIVATE_PROMPT"}}],
        "recognition_requests": [{"state": "responding", "since": 1, "api_key": "PRIVATE_KEY"}],
        "error_detail": "PRIVATE_ERROR", "trace": "PRIVATE_TRACE",
        "question_labels": {"q1": "1.1"}, "active_question_ids": ["q1"],
        "failed_question_ids": ["q2"], "question_error_codes": {"q2": "provider_timeout"},
        "recognition_failure": {"failed_pages": [2], "processed_pages": [1], "prompt": "PRIVATE_PROMPT"},
    }


def test_legacy_projection_filters_every_nested_public_field_without_losing_recovery():
    result = public_progress(private_snapshot())
    assert "PRIVATE" not in json.dumps(result)
    assert result["completed_units"] == result["stage_metrics"]["files_processed"] == 3
    assert result["total_steps"] == 8 and result["completed_steps"] == 3
    assert result["current_step"] == "source_prepared"
    assert result["messages"][0]["message"] == "activity_reading"
    assert result["messages"][1]["recognition"]["reason"] == "timeout"
    assert result["failed_question_ids"] == ["q2"]
    assert result["question_error_codes"] == {"q2": "provider_timeout"}
    assert result["recognition_failure"] == {"failed_pages": [2], "processed_pages": [1]}
    assert public_progress(result) == result  # sync and async API projection
    assert public_progress(private_snapshot(), terminal=True)["recognition_requests"] == []


def test_old_waits_are_preserved_but_resumed_live_requests_do_not_reappear_as_waits():
    old = {"phase": "parsing", "model_waits": [{"model": "Gemini", "reason": "provider_rate_limited", "attempt": 2, "max_attempts": 3, "retry_at": 200}]}
    assert public_progress(old)["recognition_requests"][0]["state"] == "retrying"
    old["recognition_requests"] = [{"state": "responding"}]
    assert public_progress(old)["recognition_requests"] == [{"state": "responding"}]


def test_invalid_legacy_fields_are_omitted_instead_of_returned_or_crashing():
    result = public_progress({"phase": [], "stage_sequence": None, "messages": [{"message": "activity_retrying", "ts": 1, "recognition": {"state": [], "reason": []}}],
        "recognition_requests": [{"state": "responding", "since": float("nan"), "reason": []}], "question_labels": "PRIVATE", "failed_question_ids": None})
    assert result["recognition_requests"] == [{"state": "responding"}]
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.asyncio
async def test_saved_counts_and_events_survive_later_provider_updates():
    from backend.progress.tracker import ProgressReporter
    reporter = ProgressReporter("incremental-counts")
    await reporter.set_preparation_counts(questions=3, stems=3, reference_answers=1, rubrics=2)
    await reporter._emit_message("Saved 3 items from PRIVATE.pdf, part 1")
    await reporter._emit_message("Preparing missing materials for question PRIVATE")
    await reporter.set_current_step("recognition_read", message="Reading source evidence")
    result = public_progress((await reporter.snapshot()).model_dump(mode="json"))
    assert result["preparation_counts"] == dict(questions=3, stems=3, reference_answers=1, rubrics=2)
    assert {"activity_saved", "activity_materials"} <= {event["message"] for event in result["messages"]}
    assert "PRIVATE" not in json.dumps(result)
    assert "preparation_counts" not in public_progress({})
    assert "preparation_counts" not in public_progress({"preparation_counts": {"questions": 3}})


@pytest.mark.asyncio
async def test_large_batch_telemetry_stays_bounded_without_losing_request_counts():
    from backend.progress.recognition_activity import RecognitionActivity
    reporter = get_or_create_reporter("bounded-progress")
    observer = RecognitionActivity(Obj())
    class Pending:
        def done(self):
            return False
    pending = [Pending() for _ in range(1000)]
    for task in pending:
        observer.requests[task] = {"state": "retrying", "reason": "timeout", "attempt": 2, "max_attempts": 3, "retry_at": 200}
    try:
        for _ in range(200):
            await reporter._emit(ProgressEvent(message="activity_retrying", recognition={"state": "retrying", "reason": "timeout", "attempt": 2, "max_attempts": 3, "retry_at": 200}))
        reporter.recognition_activity = observer
        snapshot = (await reporter.snapshot()).model_dump(mode="json")
        assert snapshot["recognition_requests"][0]["count"] == 1000
        assert len(snapshot["recognition_requests"]) == 1
        assert 0 < len(snapshot["messages"]) <= 200
        assert len(json.dumps(snapshot).encode()) < 20 * 1024
        assert snapshot["completed_units"] == 0
    finally:
        remove_reporter("bounded-progress")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_capacity_failure_clears_waiting_response_without_success_event(monkeypatch, failure):
    from contextlib import asynccontextmanager
    from backend.llm import providers
    from backend.models import ProviderConfig
    op = Obj(operation_id="recognition-test", operation_type="question_preparation", update_progress=AsyncMock())
    reporter = get_or_create_reporter(op.operation_id)
    provider = Obj(config=ProviderConfig(provider_type="openai", model="synthetic", api_key="private"), _endpoint_breaker=lambda: Obj(is_open=False))
    @asynccontextmanager
    async def lease(**_):
        yield
    monkeypatch.setattr(providers, "get_scheduler", lambda: Obj(lease=lease))
    try:
        async with observe_recognition(op):
            with pytest.raises(failure):
                async with providers.BaseProvider._call_capacity(provider, []):
                    assert (await reporter.snapshot()).recognition_requests[0]["state"] == "responding"
                    raise failure()
            assert (await reporter.snapshot()).recognition_requests == []
            assert op.update_progress.call_args.args[0]["recognition_requests"] == []
            assert not any(e.message == "activity_response_received" for e in (await reporter.snapshot()).messages)
    finally:
        remove_reporter(op.operation_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["question_preparation", "submission_recognition", "problem_extraction"])
async def test_parallel_requests_keep_completed_progress_and_do_not_fake_completed_units(kind):
    op = Obj(operation_id="recognition-test", operation_type=kind, update_progress=AsyncMock())
    reporter = get_or_create_reporter(op.operation_id)
    reporter._progress.completed_units = 3
    reporter._progress.total_students = 10
    ready = asyncio.Event()
    release = asyncio.Event()
    async def slow():
        await report_provider_state(None, "queued")
        await report_provider_state(None, "responding")
        ready.set()
        await release.wait()
        await report_provider_state(None, "processing")
    try:
        async with observe_recognition(op):
            task = asyncio.create_task(slow())
            await ready.wait()
            await report_provider_state(None, "retrying", reason="timeout", attempt=2, max_attempts=3, retry_at=100)
            snapshot = (await reporter.snapshot()).model_dump(mode="json")
            assert {row["state"] for row in snapshot["recognition_requests"]} == {"responding", "retrying"}
            assert snapshot["completed_units"] == 3
            assert op.update_progress.call_args.args[0]["completed_units"] == 3
            release.set()
            await task
            await report_provider_state(None, "responding")
            await report_provider_state(None, "processing")
            snapshot = (await reporter.snapshot()).model_dump(mode="json")
            assert snapshot["recognition_requests"] == []
            assert snapshot["completed_units"] == 3
            assert snapshot["messages"][-1]["message"] == "activity_response_received"
        # No observer crosses into another operation or an unrelated call.
        count = op.update_progress.call_count
        await report_provider_state(None, "responding")
        assert op.update_progress.call_count == count
    finally:
        release.set()
        remove_reporter(op.operation_id)


@pytest.mark.asyncio
async def test_observability_failure_and_nonrecognition_scope_do_not_change_execution():
    op = Obj(operation_id="recognition-test", operation_type="question_preparation", update_progress=AsyncMock(side_effect=RuntimeError("DB unavailable")))
    reporter = get_or_create_reporter(op.operation_id)
    try:
        async with observe_recognition(op):
            await reporter.set_phase("parsing")
            await report_provider_state(None, "responding")
            await reporter._emit(ProgressEvent(message="PRIVATE_UNKNOWN_MESSAGE"))
            assert "PRIVATE" not in json.dumps((await reporter.snapshot()).model_dump(mode="json"))
            with pytest.raises(asyncio.CancelledError):
                raise asyncio.CancelledError
        op.operation_type = "source_cleanup"
        op.update_progress.reset_mock()
        async with observe_recognition(op):
            await report_provider_state(None, "responding")
        op.update_progress.assert_not_called()
    finally:
        remove_reporter(op.operation_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["rate_limit", "timeout", "unavailable"])
async def test_existing_retry_hook_reports_actual_reason_without_raw_error(reason):
    from backend.tools.structured_llm import _before_retry_sleep, RateLimitError, TransientLLMError
    from backend.llm.providers import ProviderRequestError
    op = Obj(operation_id="recognition-test", operation_type="submission_recognition", update_progress=AsyncMock())
    reporter = get_or_create_reporter(op.operation_id)
    error = RateLimitError("PRIVATE_ERROR") if reason == "rate_limit" else TransientLLMError("PRIVATE_ERROR")
    if reason == "timeout":
        error.__cause__ = ProviderRequestError("provider_timeout")
    try:
        async with observe_recognition(op):
            await _before_retry_sleep(Obj(args=[None], outcome=Obj(exception=lambda: error), next_action=Obj(sleep=1), attempt_number=1))
        snapshot = op.update_progress.call_args.args[0]
        assert snapshot["recognition_requests"][0]["reason"] == reason
        assert snapshot["recognition_requests"][0]["attempt"] == 2
        assert "PRIVATE" not in json.dumps(snapshot)
    finally:
        remove_reporter(op.operation_id)


@pytest.mark.asyncio
async def test_task_state_filters_both_persisted_and_live_recognition_and_retains_owner_boundary():
    from backend.tests.test_task_background_workflows import _seed_task
    from backend.db import workflow_repository
    from backend.services import task_facade
    from backend.domain.errors import NotFound
    owner, task_id = _seed_task()
    op, _ = workflow_repository.create_operation(assignment_id=task_id, owner_id=owner, operation_type="question_preparation", input_hash="synthetic", progress=private_snapshot())
    workflow_repository.update_workflow(task_id, owner_id=owner, bump_revision=False,
        presentation_status="extracting_problems", active_operation="question_preparation", active_job_id=op.id, extract_job_id=op.id)
    state = task_facade.task_state(task_id=task_id, owner_id=owner)
    assert "PRIVATE" not in json.dumps(state["progress"])
    reporter = get_or_create_reporter(op.id)
    try:
        await reporter.set_phase("parsing")
        await reporter._emit_message("PRIVATE_LIVE_PROMPT")
        state = await task_facade.async_task_state(task_id=task_id, owner_id=owner)
        assert "PRIVATE" not in json.dumps(state["progress"])
        with pytest.raises(NotFound):
            await task_facade.async_task_state(task_id=task_id, owner_id="another-owner")
    finally:
        remove_reporter(op.id)
