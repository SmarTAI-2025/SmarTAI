"""Resource admission and cancellation keep execution clocks and ownership honest."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.services.submission_work_budget import SubmissionWorkBudget, _current
from backend.tools import code_interpreter as code, math_verification as math
from backend.tools import pdf_worker_lifecycle as lifecycle, sandbox_runtime as slots
from backend.tools import oci_grading_runner as oci


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["math", "code"])
async def test_sandbox_queue_has_own_deadline_without_charging_work(monkeypatch, kind):
    monkeypatch.setattr(slots, "_SEMAPHORE", asyncio.Semaphore(0))
    monkeypatch.setattr(slots, "SANDBOX_ADMISSION_WAIT_SECONDS", .02)
    monkeypatch.setattr(code.settings, "program_runner_mode", "subprocess")
    monkeypatch.setattr(code.settings, "runtime_environment", "test")
    launch = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    budget = SubmissionWorkBudget("queued", limit_seconds=1)
    token = _current.set(budget)
    try:
        if kind == "math":
            result = await asyncio.wait_for(math.verify_math(math.MathVerificationRequest(
                student_expression="1", reference_expression="1")), 1)
            assert result.status == "unknown" and result.method == "timeout"
        else:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(code.run_python_subprocess("pass"), 1)
        assert budget.elapsed == 0 and budget.active == 0
        launch.assert_not_called()
    finally:
        _current.reset(token)
        budget.close()


@pytest.mark.asyncio
async def test_host_cancel_kills_and_reaps_before_releasing_slot(monkeypatch):
    semaphore = asyncio.Semaphore(1)
    monkeypatch.setattr(slots, "_SEMAPHORE", semaphore)
    monkeypatch.setattr(code.settings, "program_runner_mode", "subprocess")
    monkeypatch.setattr(code.settings, "runtime_environment", "test")
    entered = asyncio.Event()
    class Process:
        returncode = None
        killed = False
        waited = False
        def kill(self): self.killed = True; self.returncode = -9
        async def communicate(self, data=None):
            if data is not None:
                entered.set()
                await asyncio.Event().wait()
            return b"", b""
        async def wait(self): self.waited = True; return self.returncode
    child = Process()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=child))
    budget = SubmissionWorkBudget("working", limit_seconds=1)
    token = _current.set(budget)
    try:
        task = asyncio.create_task(code.run_python_subprocess("pass"))
        await entered.wait()
        assert budget.active == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert child.killed and child.waited
        assert semaphore._value == 1 and budget.active == 0 and budget.elapsed > 0
    finally:
        _current.reset(token)
        budget.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["math", "code"])
async def test_stuck_reaper_is_bounded_and_holds_slot_until_safe(monkeypatch, kind):
    semaphore = asyncio.Semaphore(1)
    monkeypatch.setattr(slots, "_SEMAPHORE", semaphore)
    monkeypatch.setattr(lifecycle, "PDF_CLEANUP_SECONDS", .02)
    monkeypatch.setattr(code.settings, "program_runner_mode", "subprocess")
    monkeypatch.setattr(code.settings, "runtime_environment", "test")
    release = asyncio.Event()
    waiting = asyncio.Event()
    payload = json.dumps(dict(status="supported", method="fixture", reason="done")).encode()
    async def wait():
        waiting.set()
        await release.wait()
        return 0
    child = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(payload, b"")), wait=wait)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=child))
    try:
        if kind == "math":
            result = await asyncio.wait_for(math.verify_math(math.MathVerificationRequest(
                student_expression="1", reference_expression="1")), 1)
            assert result.status == "unknown"
        else:
            with pytest.raises(lifecycle.PdfWorkerCleanupTimeout):
                await asyncio.wait_for(code.run_python_subprocess("pass"), 1)
        assert waiting.is_set() and semaphore.locked()
    finally:
        release.set()
        for _ in range(20):
            if semaphore._value == 1:
                break
            await asyncio.sleep(0)
        assert semaphore._value == 1


@pytest.mark.asyncio
async def test_parallel_admitted_sandbox_work_counts_once(monkeypatch):
    monkeypatch.setattr(slots, "_SEMAPHORE", asyncio.Semaphore(2))
    budget = SubmissionWorkBudget("parallel", limit_seconds=1)
    token = _current.set(budget)
    try:
        started = asyncio.get_running_loop().time()
        async with slots.sandbox_slot():
            async with slots.sandbox_slot():
                assert budget.active == 2
                await asyncio.sleep(.02)
        elapsed = asyncio.get_running_loop().time() - started
        assert budget.active == 0 and .015 <= budget.elapsed <= elapsed + .005
    finally:
        _current.reset(token)
        budget.close()


@pytest.mark.asyncio
async def test_oci_command_startup_timeout_still_reaps_late_cli(monkeypatch):
    gate, launched = asyncio.Event(), asyncio.Event()
    child = SimpleNamespace(returncode=None, communicate=AsyncMock(return_value=(b"", b"")), wait=AsyncMock(return_value=0))
    def kill(): child.returncode = -9
    child.kill = kill
    async def launch(*args, **kwargs):
        launched.set()
        await gate.wait()
        return child
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(lifecycle, "PDF_CLEANUP_SECONDS", .02)
    pending = []
    token = oci._command_reaps.set(pending)
    try:
        task = asyncio.create_task(oci._command(["docker", "info"], timeout=.01, env={}))
        await launched.wait()
        with pytest.raises(lifecycle.PdfWorkerCleanupTimeout):
            await asyncio.wait_for(task, 1)
        assert len(pending) == 1 and not pending[0].done()
        gate.set()
        await asyncio.wait_for(asyncio.shield(pending[0]), 1)
        assert child.returncode == -9
        child.wait.assert_awaited_once()
    finally:
        gate.set()
        oci._command_reaps.reset(token)
