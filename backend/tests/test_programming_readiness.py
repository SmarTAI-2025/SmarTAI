from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.tools import programming_readiness as probe


@pytest.mark.asyncio
async def test_readiness_only_advertises_modes_observed_in_the_pinned_runtime(monkeypatch):
    digest = "sha256:" + "a" * 64
    monkeypatch.setattr(probe.settings, "program_runner_image", "registry/runner@" + digest)
    monkeypatch.setattr(probe.settings, "runtime_environment", "production")
    monkeypatch.setattr(probe.settings, "program_runner_mode", "disabled")
    execute = AsyncMock(side_effect=[
        SimpleNamespace(passed=True, stdout="5\n", isolation_mode="oci_container"),
        SimpleNamespace(passed=True, return_value="5", isolation_mode="oci_container"),
        probe.IsolatedRunnerUnavailable()])
    monkeypatch.setattr(probe, "run_python_oci", execute)
    native = AsyncMock(side_effect=[SimpleNamespace(passed=True, stdout="5\n", isolation_mode="oci_container"),
                                    probe.IsolatedRunnerUnavailable()])
    monkeypatch.setattr(probe, "run_program_oci", native)
    assert (await probe.capabilities())["status"] == "disabled"
    execute.assert_not_awaited()
    monkeypatch.setattr(probe.settings, "program_runner_mode", "oci")
    ready = await probe.capabilities()
    assert (ready["status"], ready["image_digest"], ready["languages"], ready["modes"]) == (
        "ready", digest, ["python", "c"], ["stdin", "function"])
    unavailable = await probe.capabilities()
    assert unavailable["status"] == "unavailable" and not unavailable["languages"]
