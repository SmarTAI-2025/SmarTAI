"""Real asyncio scheduling with a virtual clock and a controlled OCI boundary."""
import asyncio

import pytest

from backend.models import TestCase as Case
from backend.tools import code_interpreter as runner
from backend.tools.oci_grading_runner import IsolatedRunnerUnavailable


def cases(count):
    return [Case(input=str(i), expected_output=str(i)) for i in range(count)]


def result(case):
    return runner.TestResult(case, True, case.expected_output, "", 1,
                             execution_id="run-" + case.input,
                             request_digest="a" * 64, exit_reason="success")


@pytest.fixture
def two_slots(monkeypatch):
    monkeypatch.setattr(runner.settings, "program_runner_mode", "oci")
    monkeypatch.setattr(runner.settings, "program_runner_max_concurrent", 2)


@pytest.mark.asyncio
async def test_fifty_healthy_cases_at_two_slots_survive_over_sixty_seconds(two_slots, monkeypatch):
    """25 waves × four virtual seconds. Every case is under its own 10s limit."""
    loop = asyncio.get_running_loop()
    now, elapsed = loop.time, 0
    monkeypatch.setattr(loop, "time", lambda: now() + elapsed)
    started = asyncio.Queue()
    physical = asyncio.Semaphore(2)
    waiting_for_os = peak = 0

    async def controlled(code, case, timeout, memory):
        nonlocal waiting_for_os, peak
        waiting_for_os += 1
        peak = max(peak, waiting_for_os)
        async with physical:
            waiting_for_os -= 1
            done = loop.create_future()
            started.put_nowait(done)
            await done
            return result(case)

    monkeypatch.setattr(runner, "_run_case", controlled)
    batch = asyncio.create_task(runner.run_sandbox("# fixture", cases(50), per_case_timeout=10))
    try:
        for wave in range(25):
            permits = []
            for _ in range(2):
                admitted = asyncio.create_task(started.get())
                completed, _ = await asyncio.wait((admitted, batch), return_when=asyncio.FIRST_COMPLETED)
                if batch in completed:
                    admitted.cancel()
                    await asyncio.gather(admitted, return_exceptions=True)
                    await batch  # Raise the old aggregate timeout at the failing wave.
                    pytest.fail("Batch ended before admitting all cases")
                permits.append(admitted.result())
            elapsed += 4
            for permit in permits:
                permit.set_result(None)
        report = await batch
    finally:
        batch.cancel()
        await asyncio.gather(batch, return_exceptions=True)
    assert report.passed_count == report.total_count == 50
    assert [r.case_index for r in report.results] == list(range(50))
    assert peak <= 2


@pytest.mark.asyncio
async def test_concurrent_students_share_admission_before_os_slots(two_slots, monkeypatch):
    started = asyncio.Queue()

    async def controlled(code, case, timeout, memory):
        release = asyncio.Event()
        started.put_nowait((code, release))
        await release.wait()
        return result(case)

    monkeypatch.setattr(runner, "_run_case", controlled)
    a = asyncio.create_task(runner.run_sandbox("A", cases(8)))
    first_wave = [await started.get(), await started.get()]
    b = asyncio.create_task(runner.run_sandbox("B", cases(2)))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert started.empty()
    for label, release in first_wave:
        assert label == "A"
        release.set()
    second_wave = [await started.get(), await started.get()]
    for label, release in second_wave:
        assert label == "B"
        release.set()
    for _ in range(6):
        _, release = await started.get()
        release.set()
    assert (await a).total_count == 8 and (await b).total_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_completed_case_checkpoint_survives_platform_failure_or_cancel(two_slots, monkeypatch, cancel):
    notices = []
    saving, allow_save, failure, cleaned = (asyncio.Event() for _ in range(4))
    async def controlled(code, case, timeout, memory):
        if case.input == "0":
            return result(case)
        try:
            await failure.wait()
            raise IsolatedRunnerUnavailable()
        finally:
            cleaned.set()
    async def checkpoint(item):
        saving.set()
        await allow_save.wait()
        notices.append((item.case_index, item.case_id, item.execution_id))
    monkeypatch.setattr(runner, "_run_case", controlled)
    batch = asyncio.create_task(runner.run_sandbox("A", cases(4), on_case_complete=checkpoint))
    await saving.wait()
    if cancel:
        batch.cancel()
    else:
        failure.set()
    await cleaned.wait()
    assert not batch.done()
    allow_save.set()
    with pytest.raises(asyncio.CancelledError if cancel else IsolatedRunnerUnavailable):
        await batch
    assert notices == [(0, "case-1", "run-0")]


@pytest.mark.asyncio
async def test_checkpoint_failure_is_an_operation_failure_and_stops_sibling(two_slots, monkeypatch):
    active, cleaned = asyncio.Event(), asyncio.Event()
    async def controlled(code, case, timeout, memory):
        if case.input == "0":
            await active.wait()
            return result(case)
        active.set()
        try:
            await asyncio.Future()
        finally:
            cleaned.set()
    async def failed(_item):
        raise OSError("checkpoint unavailable")
    monkeypatch.setattr(runner, "_run_case", controlled)
    with pytest.raises(OSError, match="checkpoint unavailable"):
        await runner.run_sandbox("A", cases(4), on_case_complete=failed)
    assert cleaned.is_set()
