"""Host-runner cancellation must reap real children without changing normal runs."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.tools import code_interpreter, sandbox_runtime


@pytest.fixture(autouse=True)
def host_runner(monkeypatch):
    monkeypatch.setattr(code_interpreter.settings, "runtime_environment", "test")
    monkeypatch.setattr(code_interpreter.settings, "program_runner_mode", "subprocess")
    monkeypatch.setattr(sandbox_runtime, "_SEMAPHORE", asyncio.Semaphore(1))


@pytest.mark.asyncio
@pytest.mark.parametrize("during_launch", [False, True])
async def test_cancelled_host_runner_reaps_real_child(monkeypatch, during_launch):
    create_process = asyncio.create_subprocess_exec
    launched, release_launch = asyncio.Event(), asyncio.Event()
    processes, paths = [], []

    async def launch(*args, **kwargs):
        process = await create_process(*args, **kwargs)
        processes.append(process)
        paths.append(Path(args[1]))
        launched.set()
        if during_launch:
            await release_launch.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    request = asyncio.create_task(code_interpreter.run_python_subprocess(
        "import time\ntime.sleep(60)", timeout=30,
    ))
    try:
        await asyncio.wait_for(launched.wait(), 5)
        # Let the runner enter communicate() when the launch is not gated.
        await asyncio.sleep(0)
        request.cancel()
        if during_launch:
            await asyncio.sleep(0)
            assert not request.done()
            assert sandbox_runtime.get_sandbox_semaphore().locked()
            assert paths[0].exists()
            # Repeated cancellation must not discard the late process handle.
            request.cancel()
            release_launch.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(request, 5)
        assert processes[0].returncode is not None
        assert await asyncio.wait_for(processes[0].wait(), 1) != 0
        assert not paths[0].exists()
        assert sandbox_runtime.get_sandbox_semaphore()._value == 1
    finally:
        release_launch.set()
        if not request.done():
            request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        # The control checkout leaks the child. Never leave it behind on failure.
        for process in processes:
            if process.returncode is None:
                process.kill()
            await process.communicate()
            await process.wait()


@pytest.mark.asyncio
async def test_cancelled_sandbox_queue_does_not_start_child(monkeypatch):
    semaphore = asyncio.Semaphore(0)
    monkeypatch.setattr(sandbox_runtime, "_SEMAPHORE", semaphore)

    async def forbidden(*args, **kwargs):
        raise AssertionError("Queued cancellation must not launch a process")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    request = asyncio.create_task(code_interpreter.run_python_subprocess("pass"))
    await asyncio.sleep(0)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert semaphore._value == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("code,input_text,expected,reason", [
    ("print(input().upper())", "hello", "HELLO\n", "success"),
    ("raise ValueError('fixture')", "", "", "runtime_error"),
])
async def test_normal_host_execution_is_unchanged(code, input_text, expected, reason):
    result = await code_interpreter.run_python_subprocess(code, input_text)
    assert result.actual_output == expected
    assert result.exit_reason == reason
    assert result.passed is (reason == "success")
    assert sandbox_runtime.get_sandbox_semaphore()._value == 1


@pytest.mark.asyncio
async def test_startup_still_does_not_consume_execution_allowance(monkeypatch):
    create_process = asyncio.create_subprocess_exec

    async def launch(*args, **kwargs):
        await asyncio.sleep(.15)
        return await create_process(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    result = await code_interpreter.run_python_subprocess("print('ok')", timeout=.1)
    assert result.passed and result.actual_output == "ok\n"
    assert result.duration_ms >= 150


@pytest.mark.asyncio
async def test_existing_execution_timeout_still_kills_real_child(monkeypatch):
    create_process = asyncio.create_subprocess_exec
    processes = []

    async def launch(*args, **kwargs):
        process = await create_process(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    result = await code_interpreter.run_python_subprocess("import time; time.sleep(60)", timeout=.1)
    assert result.exit_reason == "timeout" and result.error == "Timeout after 0.1s"
    assert processes[0].returncode is not None
    assert sandbox_runtime.get_sandbox_semaphore()._value == 1
