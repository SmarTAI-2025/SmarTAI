import asyncio
import time

from backend.progress.tracker import ProgressReporter


def test_grading_phase_records_a_factual_start_time():
    reporter = ProgressReporter("c03-start", total_students=4, total_questions=3)
    before = time.time()

    asyncio.run(reporter.set_phase("grading"))
    snapshot = asyncio.run(reporter.snapshot())

    assert snapshot.phase == "grading"
    assert snapshot.started_at is not None
    assert before <= snapshot.started_at <= time.time()


def test_progress_reporter_counts_completed_units_and_active_work():
    async def exercise():
        reporter = ProgressReporter("c03-queue", total_students=2, total_questions=2)
        await reporter.set_phase("grading")
        async with reporter.step("anonymous-1", "q1", skill="ConceptSkill"):
            active = await reporter.snapshot()
            await reporter.increment_completed()
        finished = await reporter.snapshot()
        return active, finished

    active, finished = asyncio.run(exercise())

    assert active.total_students == 2
    assert active.total_questions == 2
    assert len(active.active) == 1
    assert finished.completed_units == 1
    assert finished.active == []


def test_failed_grading_state_overrides_stale_done_reporter(monkeypatch):
    from backend.services import task_facade

    monkeypatch.setattr(
        task_facade,
        "task_state",
        lambda **_kwargs: {
            "status": "error",
            "grading_job_id": "run-failed",
            "active_job_id": "run-failed",
            "active_operation": "grading",
            "progress": {"phase": "done", "error_detail": None},
            "error": None,
        },
    )
    monkeypatch.setattr(task_facade, "get_reporter", lambda _job_id: None)
    monkeypatch.setattr(
        task_facade,
        "_grading_progress",
        lambda _run_id, _owner_id: {
            "phase": "error",
            "error_detail": "grading_failed",
        },
    )

    snapshot = asyncio.run(
        task_facade.async_task_state(task_id="task-1", owner_id="teacher-1")
    )

    assert snapshot["progress"]["phase"] == "error"
    assert snapshot["progress"]["error_detail"] == "grading_failed"
    assert snapshot["error"] == "grading_failed"


def test_grading_state_exposes_live_active_units(monkeypatch):
    """The durable projection hardcodes ``active: []``; the live reporter's
    in-flight units must be merged in so the progress page's "running" count
    is not stuck at 0 (2026-08-28 fix)."""
    from backend.progress import tracker
    from backend.services import task_facade

    monkeypatch.setattr(
        task_facade,
        "task_state",
        lambda **_kwargs: {
            "status": "grading",
            "grading_job_id": "run-live",
            "active_job_id": None,
            "active_operation": None,
            "progress": None,
            "error": None,
        },
    )
    monkeypatch.setattr(
        task_facade,
        "_grading_progress",
        lambda _run_id, _owner_id: {
            "phase": "grading",
            "completed_units": 0,
            "active": [],
            "messages": [],
        },
    )

    async def exercise():
        # A real reporter in the shared registry — get_reporter() (unpatched)
        # must find it, exercising the merge path in async_task_state.
        reporter = tracker.get_or_create_reporter("run-live", 2, 2)
        await reporter.set_phase("grading")
        await reporter.increment_completed()
        step = reporter.step("anonymous-1", "q2", skill="ConceptSkill")
        await step.__aenter__()
        try:
            return await task_facade.async_task_state(
                task_id="task-1", owner_id="teacher-1"
            )
        finally:
            await step.__aexit__(None, None, None)
            tracker.remove_reporter("run-live")

    snapshot = asyncio.run(exercise())
    progress = snapshot["progress"]

    # Durable fields stay durable; live fields are borrowed from the reporter.
    assert progress["phase"] == "grading"
    assert progress["completed_units"] == 1
    assert len(progress["active"]) == 1
    assert progress["active"][0]["student_id"] == "anonymous-1"
    assert progress["active"][0]["q_id"] == "q2"
