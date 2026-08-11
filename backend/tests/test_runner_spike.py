from __future__ import annotations

import os
from pathlib import Path

from backend.tools.grading_runner import RunnerResult
from tools.runner_spike.harness import (
    DEFAULT_PIDS_LIMIT,
    build_container_command,
)
from tools.runner_spike.worker import execute_request


def _request(code: str, *, max_output_chars: int = 4096) -> dict:
    return {
        "schema_version": 1,
        "execution_id": "runner-spike-test",
        "task_type": "sympy",
        "language": "python",
        "code": code,
        "test_cases": [],
        "limits": {
            "timeout_seconds": 1.0,
            "memory_mb": 256,
            "max_output_chars": max_output_chars,
        },
    }


def test_oci_command_contains_every_week_one_isolation_control(tmp_path: Path):
    request_file = tmp_path / "request.json"
    request_file.write_text("{}", encoding="utf-8")

    command = build_container_command(
        runtime="docker",
        image="runner:test",
        request_file=request_file,
        container_name="runner-spike-test",
        memory_mb=256,
    )

    assert "--network=none" in command
    assert "--read-only" in command
    assert "--cap-drop=ALL" in command
    assert "--security-opt=no-new-privileges=true" in command
    assert f"--pids-limit={DEFAULT_PIDS_LIMIT}" in command
    assert "--memory=256m" in command
    assert "--cpus=0.5" in command
    assert "--user=65534:65534" in command
    assert any(
        item.startswith("--tmpfs=/work:rw,nosuid,nodev,noexec") for item in command
    )
    mount = next(item for item in command if item.startswith("--mount="))
    assert "target=/input/request.json" in mount
    assert mount.endswith(",readonly")
    assert not any(item.startswith("--env-file") for item in command)
    assert not any("SMARTAI_" in item or "DATABASE_URL" in item for item in command)


def test_worker_clears_launcher_environment_and_matches_runner_contract(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("SMARTAI_SPIKE_PRODUCTION_SECRET", "do-not-leak")
    request = _request(
        "import os\n"
        "print(os.environ.get('SMARTAI_SPIKE_PRODUCTION_SECRET', 'missing'))"
    )

    raw_result = execute_request(request, workdir=tmp_path)
    result = RunnerResult.model_validate(raw_result)

    assert os.environ["SMARTAI_SPIKE_PRODUCTION_SECRET"] == "do-not-leak"
    assert result.exit_reason.value == "success"
    assert result.stdout.strip() == "missing"
    assert result.isolation_mode == "oci_container"
    assert not (tmp_path / "main.py").exists()


def test_worker_enforces_wall_timeout_without_returning_a_host_path(tmp_path: Path):
    request = _request("while True:\n    pass\n")
    request["limits"]["timeout_seconds"] = 0.1

    result = RunnerResult.model_validate(execute_request(request, workdir=tmp_path))

    assert result.exit_reason.value == "timeout"
    assert result.resource_status.value == "timeout"
    assert str(tmp_path) not in result.stderr


def test_worker_bounds_output_before_serializing_it(tmp_path: Path):
    result = RunnerResult.model_validate(
        execute_request(
            _request("print('x' * 10000)", max_output_chars=256),
            workdir=tmp_path,
        )
    )

    assert result.exit_reason.value == "output_limit"
    assert result.stdout_truncated is True
    assert len(result.stdout) <= 256
