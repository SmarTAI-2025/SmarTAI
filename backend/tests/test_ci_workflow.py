"""Regression tests for the GitHub Actions CI workflow (Task 1 CI failures).

The CI workflow in ``.github/workflows/ci.yml`` had three failure modes that
these tests pin down so they cannot silently regress:

* The SQLite backend job installed only ``render-requirements.txt`` (no
  ``pytest``), so the test step aborted with ``No module named pytest``.
* The E2E job used ``wait-on http://127.0.0.1:8000/ready``. ``wait-on`` issues
  an HTTP ``HEAD`` for plain HTTP URL resources, while FastAPI's ``/ready``
  route is GET-only, so the readiness wait timed out with 405s.

These tests read the workflow YAML as text and assert the fixes stay in place.
They do not execute the workflow; they are static guards against regression.
"""
from __future__ import annotations

from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"


@pytest.fixture(scope="module")
def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_sqlite_backend_job_installs_pytest(workflow_text: str) -> None:
    """The SQLite backend job must install ``pytest`` before invoking it.

    The original job only ran ``pip install -r render-requirements.txt``; that
    file does not include ``pytest``, so ``python -m pytest backend/tests -q``
    failed with ``No module named pytest`` in GitHub Actions. The fix is to
    install ``pytest`` explicitly in the job before the test step.
    """
    assert "name: Backend (SQLite)" in workflow_text, "SQLite backend job must exist"

    sqlite_section = _job_section(workflow_text, "backend-sqlite")
    assert "python -m pytest" in sqlite_section, (
        "SQLite backend job must still run pytest"
    )
    assert "pytest" in _install_step(sqlite_section), (
        "SQLite backend job's dependency install step must include pytest "
        "(render-requirements.txt does not declare it)"
    )


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
    explicitly alongside ``pytest``.
    """
    assert "name: Backend (SQLite)" in workflow_text, "SQLite backend job must exist"

    sqlite_section = _job_section(workflow_text, "backend-sqlite")
    install_step = _install_step(sqlite_section)
    assert "pytest-asyncio" in install_step, (
        "SQLite backend job's dependency install step must include "
        "pytest-asyncio (async tests use @pytest.mark.asyncio and fail with "
        "'async def functions are not natively supported' without it)"
    )


def test_postgres_backend_job_installs_pytest_asyncio(workflow_text: str) -> None:
    """The PostgreSQL backend job must install ``pytest-asyncio`` before tests.

    The PostgreSQL integration job runs ``python -m pytest
    backend/tests/test_postgres_integration.py``. Even though that file is
    currently synchronous, the job shares the test environment with the async
    test suite and must install ``pytest-asyncio`` so the async test
    infrastructure (and the ``@pytest.mark.asyncio`` mark handling) is
    available. ``render-requirements.txt`` does not declare it, so it must be
    installed explicitly, mirroring the SQLite backend job.
    """
    assert "name: Backend (PostgreSQL)" in workflow_text, (
        "PostgreSQL backend job must exist"
    )

    postgres_section = _job_section(workflow_text, "backend-postgres")
    install_step = _install_step(postgres_section)
    assert "pytest-asyncio" in install_step, (
        "PostgreSQL backend job's dependency install step must include "
        "pytest-asyncio so the async test infrastructure is available"
    )


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
