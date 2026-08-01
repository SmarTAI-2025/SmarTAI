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
