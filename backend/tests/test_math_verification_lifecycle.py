"""Admission, execution budgets and cleanup of real fixed math workers."""
import asyncio
import json

import pytest

from backend.tools import math_verification as mv, sandbox_runtime


def request():
    return mv.MathVerificationRequest(student_expression="1", reference_expression="1")


@pytest.mark.asyncio
async def test_queued_real_math_worker_gets_full_execution_window(monkeypatch):
    slots = asyncio.Semaphore(sandbox_runtime.DEFAULT_SANDBOX_CONCURRENCY)
    monkeypatch.setattr(sandbox_runtime, "_SEMAPHORE", slots)
    for _ in range(sandbox_runtime.DEFAULT_SANDBOX_CONCURRENCY):
        await slots.acquire()
    original = asyncio.create_subprocess_exec
    children = []

    async def launch(*args, **kwargs):
        child = await original(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    task = asyncio.create_task(mv.verify_math(request()))
    try:
        # Actual default 5s limit, actual 8-slot gate, actual arithmetic worker.
        await asyncio.sleep(6)
        queued = not task.done()
        assert not children
    finally:
        for _ in range(sandbox_runtime.DEFAULT_SANDBOX_CONCURRENCY):
            slots.release()
    result = await asyncio.wait_for(task, 10)
    assert queued, result
    assert result.status == "supported", result
    assert len(children) == 1 and children[0].returncode == 0
    assert slots._value == sandbox_runtime.DEFAULT_SANDBOX_CONCURRENCY


@pytest.mark.asyncio
async def test_slow_process_creation_does_not_spend_calculation_window(monkeypatch):
    original = asyncio.create_subprocess_exec
    payload = json.dumps({"status": "supported", "method": "fixture", "reason": "ready"})

    async def launch(*args, **kwargs):
        await asyncio.sleep(.15)
        return await original(args[0], "-I", "-c", f"print({payload!r})", **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(mv, "WALL_SECONDS", .1)
    result = await mv.verify_math(request())
    assert result.status == "supported", result


@pytest.mark.asyncio
async def test_cancel_during_launch_reaps_late_child_before_releasing_slot(monkeypatch):
    slots = asyncio.Semaphore(1)
    monkeypatch.setattr(sandbox_runtime, "_SEMAPHORE", slots)
    original = asyncio.create_subprocess_exec
    created, return_handle = asyncio.Event(), asyncio.Event()
    children = []

    async def launch(*args, **kwargs):
        child = await original(args[0], "-I", "-c", "import time; time.sleep(60)", **kwargs)
        children.append(child)
        created.set()
        await return_handle.wait()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    task = asyncio.create_task(mv.verify_math(request()))
    try:
        await asyncio.wait_for(created.wait(), 3)
        task.cancel()
        await asyncio.sleep(.02)
        task.cancel()
        await asyncio.sleep(.02)
        retained = slots.locked() and not task.done()
        return_handle.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert retained
        assert children[0].returncode is not None
        assert slots._value == 1
    finally:
        return_handle.set()
        # Clean up even against the unfixed implementation, which loses ownership.
        for child in children:
            if child.returncode is None:
                child.kill()
            await child.communicate()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_slot_is_retained_through_reaping_and_repeated_cancel(monkeypatch, cancel):
    slots = asyncio.Semaphore(1)
    monkeypatch.setattr(sandbox_runtime, "_SEMAPHORE", slots)
    original = asyncio.create_subprocess_exec
    created, reaping, finish_reaping = asyncio.Event(), asyncio.Event(), asyncio.Event()
    children = []

    async def launch(*args, **kwargs):
        child = await original(args[0], "-I", "-c", "while True: pass", **kwargs)
        original_wait = child.wait

        async def wait():
            reaping.set()
            await finish_reaping.wait()
            return await original_wait()

        child.wait = wait
        children.append(child)
        created.set()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(mv, "WALL_SECONDS", .1)
    task = asyncio.create_task(mv.verify_math(request()))
    try:
        await asyncio.wait_for(created.wait(), 3)
        if cancel:
            task.cancel()
        await asyncio.wait_for(reaping.wait(), 3)
        if cancel:
            task.cancel()
            await asyncio.sleep(.02)
        retained = slots.locked() and not task.done()
        finish_reaping.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
        else:
            result = await asyncio.wait_for(task, 3)
            assert result.status == "unknown" and result.method == "timeout"
        assert retained
        assert children[0].returncode is not None
        assert slots._value == 1
    finally:
        finish_reaping.set()
        for child in children:
            if child.returncode is None:
                child.kill()
            await child.communicate()


@pytest.mark.asyncio
async def test_cancel_while_queued_never_launches_or_spends_budget(monkeypatch):
    slots = asyncio.Semaphore(0)
    monkeypatch.setattr(sandbox_runtime, "_SEMAPHORE", slots)
    budget = mv.VerificationBudget(20)

    async def forbidden(*args, **kwargs):
        pytest.fail("Cancelled queue entry must not launch a worker")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    task = asyncio.create_task(mv.verify_math(request(), budget=budget))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert budget.remaining_seconds == 20 and slots._value == 0
    assert not slots._waiters


@pytest.mark.asyncio
async def test_startup_timeout_reaps_late_child_and_exhausted_budget_never_queues(monkeypatch):
    slots = asyncio.Semaphore(1)
    monkeypatch.setattr(sandbox_runtime, "_SEMAPHORE", slots)
    original = asyncio.create_subprocess_exec
    children = []
    budget = mv.VerificationBudget(.05)

    async def launch(*args, **kwargs):
        child = await original(args[0], "-I", "-c", "import time; time.sleep(60)", **kwargs)
        children.append(child)
        await asyncio.sleep(.15)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    result = await asyncio.wait_for(mv.verify_math(request(), budget=budget), 3)
    assert result.status == "unknown" and result.method == "timeout"
    assert children[0].returncode is not None and slots._value == 1
    assert budget.remaining_seconds == 0
    await slots.acquire()
    try:
        result = await asyncio.wait_for(mv.verify_math(request(), budget=budget), .5)
    finally:
        slots.release()
    assert result.status == "unknown" and result.method == "timeout"
    assert len(children) == 1


@pytest.mark.asyncio
async def test_per_call_guard_stops_hang_without_spending_next_calls_budget(monkeypatch):
    slots = asyncio.Semaphore(1)
    monkeypatch.setattr(sandbox_runtime, "_SEMAPHORE", slots)
    monkeypatch.setattr(mv, "CALL_WALL_SECONDS", .1)
    monkeypatch.setattr(mv, "WALL_SECONDS", 5)
    original = asyncio.create_subprocess_exec
    children = []

    async def launch(*args, **kwargs):
        child = await original(args[0], "-I", "-c", "while True: pass", **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    budget = mv.VerificationBudget(2)
    for _ in range(2):
        result = await asyncio.wait_for(mv.verify_math(request(), budget=budget), 3)
        assert result.status == "unknown" and result.method == "timeout"
    assert 0 < budget.remaining_seconds < 1.9
    assert len(children) == 2 and all(child.returncode is not None for child in children)
    assert slots._value == 1


@pytest.mark.asyncio
async def test_launch_failure_returns_unknown_and_releases_capacity(monkeypatch):
    slots = asyncio.Semaphore(1)
    monkeypatch.setattr(sandbox_runtime, "_SEMAPHORE", slots)

    async def fail(*args, **kwargs):
        raise OSError("cannot create child")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail)
    result = await mv.verify_math(request())
    assert result.status == "unknown" and result.method == "unavailable"
    assert slots._value == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_error", [OSError, TimeoutError])
async def test_cleanup_failure_does_not_swallow_parent_cancellation(monkeypatch, cleanup_error):
    slots = asyncio.Semaphore(1)
    monkeypatch.setattr(sandbox_runtime, "_SEMAPHORE", slots)
    original = asyncio.create_subprocess_exec
    communicating = asyncio.Event()
    children = []

    async def launch(*args, **kwargs):
        child = await original(args[0], "-I", "-c", "import time; time.sleep(60)", **kwargs)
        original_communicate = child.communicate

        async def communicate(input=None):
            if input is not None:
                communicating.set()
                return await original_communicate(input)
            await original_communicate()
            raise cleanup_error("pipe cleanup failed")

        child.communicate = communicate
        children.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    task = asyncio.create_task(mv.verify_math(request()))
    await asyncio.wait_for(communicating.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 3)
    assert children[0].returncode is not None and slots._value == 1


@pytest.mark.asyncio
async def test_parallel_checks_respect_shared_capacity_and_independent_budgets(monkeypatch):
    slots = asyncio.Semaphore(2)
    monkeypatch.setattr(sandbox_runtime, "_SEMAPHORE", slots)
    original = asyncio.create_subprocess_exec
    children = []
    pairs_ready = [asyncio.Event() for _ in range(4)]
    peak = 0
    payload = json.dumps({"status": "supported", "method": "fixture", "reason": "ready"})

    async def launch(*args, **kwargs):
        nonlocal peak
        child = await original(args[0], "-I", "-c",
            f"import sys; sys.stdin.read(); print({payload!r})", **kwargs)
        children.append(child)
        peak = max(peak, sum(child.returncode is None for child in children))
        pair = pairs_ready[(len(children) - 1) // 2]
        if len(children) % 2 == 0:
            pair.set()
        await pair.wait()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    budgets = [mv.VerificationBudget(1) for _ in range(8)]
    results = await asyncio.wait_for(asyncio.gather(*(
        mv.verify_math(request(), budget=budget) for budget in budgets
    )), 5)
    assert all(result.status == "supported" for result in results)
    assert peak == 2 and slots._value == 2
    assert all(child.returncode == 0 for child in children)
    assert all(0 < budget.remaining_seconds < 1 for budget in budgets)
