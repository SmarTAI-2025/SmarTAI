"""Existing sandbox admission and per-case timeouts retain their semantics."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from backend.domain.errors import DomainError
from backend.services import workflow_deadlines as limits, submission_work_budget as clocks
from backend.tests.test_oci_grading_runner import request_from, worker_result
from backend.tools import code_interpreter, math_verification, oci_grading_runner, sandbox_runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["host", "math", "oci"])
async def test_only_admitted_computation_spends_work_budget(route, monkeypatch):
    gate = asyncio.Semaphore(0)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    monkeypatch.setattr(sandbox_runtime, "_SEMAPHORE", gate)
    monkeypatch.setattr(code_interpreter.settings, "runtime_environment", "test")
    monkeypatch.setattr(code_interpreter.settings, "program_runner_mode", "oci" if route == "oci" else "subprocess")

    async def computing():
        calls.append(True)
        assert clocks.current_budget().active == 1
        entered.set()
        await release.wait()

    if route == "host":
        async def launch(*args, **kwargs):
            await computing()
            async def communicate(data):
                return b"ok", b""
            return SimpleNamespace(returncode=0, communicate=communicate)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
        async def invoke():
            return await code_interpreter.run_python_subprocess("print('ok')", timeout=7)
    elif route == "math":
        async def work(request):
            await computing()
            return math_verification._unknown("fixture")
        monkeypatch.setattr(math_verification, "_run_worker", work)
        async def invoke():
            return await math_verification.verify_math(math_verification.MathVerificationRequest(
                student_expression="1", reference_expression="1"))
    else:
        @asynccontextmanager
        async def slot(*_args):
            async with gate:
                yield
        async def check(*args):
            pass
        async def command(args, **kwargs):
            if args[1] == "rm":
                return b""
            await computing()
            assert kwargs["timeout"] == 19  # Original per-case + launcher allowance.
            return worker_result(request_from(args))
        monkeypatch.setattr(oci_grading_runner, "_host_slot", slot)
        monkeypatch.setattr(oci_grading_runner, "_check_runtime", check)
        monkeypatch.setattr(oci_grading_runner, "_command", command)
        monkeypatch.setattr(oci_grading_runner.shutil, "which", lambda name: "/usr/bin/docker")
        monkeypatch.setattr(oci_grading_runner.settings, "program_runner_image", "sha256:" + "a" * 64)
        async def invoke():
            return await code_interpreter.run_python_subprocess("print('ok')", timeout=7)

    with pytest.raises(DomainError, match="grading_wall_time_limit_reached"):
        async with limits.processing_budget("run", kind="grading") as budget:
            task = asyncio.create_task(invoke())
            try:
                await asyncio.sleep(.02)
                assert budget.active == 0 and calls == []
                gate.release()
                await asyncio.wait_for(entered.wait(), 1)
                await asyncio.sleep(.02)
                assert budget.active == 1
                release.set()
                await task
                assert budget.active == 0
                budget.expire()
                try:
                    await invoke()
                except Exception:
                    pass  # Existing math/OCI APIs may report their typed failure.
                assert calls == [True]
                assert budget.blocked
            finally:
                release.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    assert gate._value == 1
