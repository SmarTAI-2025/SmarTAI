"""Regression tests for the GitHub Actions CI workflow (Task 1 CI failures).

The CI workflow in ``.github/workflows/ci.yml`` had three failure modes that
these tests pin down so they cannot silently regress:

* The SQLite backend job installed only ``render-requirements.txt`` (no
  ``pytest``), so the test step aborted with ``No module named pytest``.
* The E2E job used ``wait-on http://127.0.0.1:8000/ready``. ``wait-on`` issues
  an HTTP ``HEAD`` for plain HTTP URL resources, while FastAPI's ``/ready``
  route is GET-only, so the readiness wait timed out with 405s.

These tests inspect the workflow and execute the aggregate check's shell gate
against success, failure, cancellation and skipped results. GitHub runner and
matrix behavior still require an actual CI run.
"""
from __future__ import annotations

from pathlib import Path
import os
import subprocess

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"


@pytest.fixture(scope="module")
def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_sqlite_backend_job_installs_pytest(workflow_text: str) -> None:
    """The SQLite backend job must install ``pytest`` before invoking it.

    The original job only ran ``pip install -r render-requirements.txt``; that
    file does not include ``pytest``, so ``python -m pytest backend/tests -q``
    failed with ``No module named pytest`` in GitHub Actions. The fix is to
    install a test lock that includes ``pytest`` before the test step.
    """
    assert "name: Backend (SQLite)" in workflow_text, "SQLite backend job must exist"

    sqlite_section = _job_section(workflow_text, "backend-sqlite-tests")
    assert "python -m pytest" in sqlite_section, (
        "SQLite backend job must still run pytest"
    )
    _assert_locked_test_dependency(sqlite_section, "pytest")


def test_e2e_readiness_probe_uses_http_get(workflow_text: str) -> None:
    """The E2E readiness probe must use HTTP GET against ``/ready``.

    ``wait-on`` issues ``HEAD`` for HTTP URL resources, but the ``/ready``
    route is GET-only and returns 405 for HEAD, so the original
    ``wait-on http://127.0.0.1:8000/ready`` invocation timed out after 30s.
    The fix is to probe ``/ready`` with an explicit GET (e.g. ``curl -f``)
    rather than broadening the application endpoint to accept HEAD solely to
    satisfy ``wait-on``.
    """
    assert "name: E2E (Playwright)" in workflow_text, "E2E job must exist"

    e2e_section = _job_section(workflow_text, "e2e")
    # The old HEAD-based wait-on command must not be the readiness gate. We
    # match the actual command invocation (`npx ... wait-on` or a bare
    # `wait-on` token on a command line), not the word in comments, so a
    # comment explaining why we removed it is allowed.
    command_lines = [
        line.strip()
        for line in e2e_section.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert not any("wait-on" in line for line in command_lines), (
        "E2E job must not invoke wait-on for /ready (its default HEAD probe "
        "gets 405 from the GET-only route)"
    )
    # An explicit GET probe against /ready must be present so the readiness
    # wait works with the GET-only route.
    assert "curl" in e2e_section, (
        "E2E job must use an explicit curl GET probe for /ready"
    )
    assert "/ready" in e2e_section, "E2E job must still probe the /ready endpoint"


def test_sqlite_backend_job_installs_pytest_asyncio(workflow_text: str) -> None:
    """The SQLite backend job must install ``pytest-asyncio`` before tests.

    The async test functions are decorated with ``@pytest.mark.asyncio``. The
    mark is only recognized when ``pytest-asyncio`` is installed; without it
    every async test fails with
    ``async def functions are not natively supported`` plus a
    ``PytestUnknownMarkWarning`` for ``pytest.mark.asyncio``.
    ``render-requirements.txt`` is the deployment pin list and does not declare
    test-only dependencies, so the job must install ``pytest-asyncio``
    through the test lock alongside ``pytest``.
    """
    assert "name: Backend (SQLite)" in workflow_text, "SQLite backend job must exist"

    sqlite_section = _job_section(workflow_text, "backend-sqlite-tests")
    _assert_locked_test_dependency(sqlite_section, "pytest-asyncio")


def test_postgres_backend_job_installs_pytest_asyncio(workflow_text: str) -> None:
    """The PostgreSQL backend job must install ``pytest-asyncio`` before tests.

    The PostgreSQL integration job runs ``python -m pytest
    backend/tests/test_postgres_integration.py``. Even though that file is
    currently synchronous, the job shares the test environment with the async
    test suite and must install ``pytest-asyncio`` so the async test
    infrastructure (and the ``@pytest.mark.asyncio`` mark handling) is
    available. ``render-requirements.txt`` does not declare it, so it must be
    installed through the test lock, mirroring the SQLite backend job.
    """
    assert "name: Backend (PostgreSQL)" in workflow_text, (
        "PostgreSQL backend job must exist"
    )

    postgres_section = _job_section(workflow_text, "backend-postgres")
    _assert_locked_test_dependency(postgres_section, "pytest-asyncio")


@pytest.mark.parametrize("result", ["success", "failure", "cancelled", "skipped", ""])
def test_sqlite_required_check_rejects_incomplete_shards(workflow_text, result):
    jobs = yaml.safe_load(workflow_text)["jobs"]
    gate = jobs["backend-sqlite"]
    assert gate["name"] == "Backend (SQLite)"
    assert gate["needs"] == "backend-sqlite-tests"
    assert gate["if"] == "always()"
    step, = gate["steps"]
    assert step["env"]["SHARD_RESULT"] == "${{ needs.backend-sqlite-tests.result }}"
    completed = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        env={**os.environ, "SHARD_RESULT": result}, check=False,
    )
    assert (completed.returncode == 0) is (result == "success")


def test_sqlite_matrix_covers_every_shard_and_retains_diagnostics(workflow_text):
    workflow = yaml.safe_load(workflow_text)
    job = workflow["jobs"]["backend-sqlite-tests"]
    assert job["strategy"] == {"fail-fast": False, "matrix": {"shard": [1, 2, 3, 4]}}
    assert not job.get("continue-on-error", False)
    test = next(step for step in job["steps"] if step.get("name") == "Test (SQLite)")
    assert "backend/tests" in test["run"]
    assert "--test-shard=${{ matrix.shard }}/4" in test["run"]
    assert "--durations=" in test["run"] and "--junitxml=" in test["run"]
    assert not test.get("continue-on-error", False)
    artifact = next(step for step in job["steps"] if step.get("name") == "Preserve SQLite test timings")
    assert artifact["if"] == "always()"
    assert "${{ matrix.shard }}" in artifact["with"]["name"]
    assert workflow["concurrency"]["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
    assert "github.run_id" in workflow["concurrency"]["group"]


def _assert_locked_test_dependency(job_text: str, package: str) -> None:
    install_step = _install_step(job_text)
    assert "-r requirements-dev.txt" in install_step
    assert "--require-hashes" in install_step
    locked = {
        canonicalize_name(requirement.name): requirement
        for line in (WORKFLOW.parents[2] / "requirements-dev.txt").read_text().splitlines()
        if line.strip() and not line.strip().startswith(("#", "-"))
        for requirement in [Requirement(line.removesuffix("\\").strip())]
    }
    requirement = locked[canonicalize_name(package)]
    pin, = requirement.specifier
    assert pin.operator == "==", f"{package} must be pinned in the CI test lock"
    assert requirement.marker is None, f"{package} must be installed on every CI platform"


def _job_section(workflow_text: str, job_id: str) -> str:
    """Return the YAML text for a single named job.

    Jobs are top-level keys under ``jobs:``; we slice from the job's key to the
    next top-level job key (or end of file). This is intentionally a simple
    text slice rather than a YAML walker because we only need to grep inside
    one job's steps.
    """
    lines = workflow_text.splitlines()
    start = None
    for idx, line in enumerate(lines):
        if line.rstrip() == f"  {job_id}:":
            start = idx
            break
    assert start is not None, f"job {job_id!r} not found in workflow"
    end = len(lines)
    for idx in range(start + 1, len(lines)):
        line = lines[idx]
        if line.startswith("  ") and not line.startswith("   ") and line.rstrip().endswith(":"):
            end = idx
            break
    return "\n".join(lines[start:end])


def _install_step(job_section: str) -> str:
    """Return the text of the ``Install dependencies`` step within a job."""
    lines = job_section.splitlines()
    in_install = False
    out: list[str] = []
    for line in lines:
        if "name: Install dependencies" in line:
            in_install = True
            out.append(line)
            continue
        if in_install:
            if line.startswith("      - name:") or line.startswith("      - uses:"):
                break
            out.append(line)
    return "\n".join(out)
