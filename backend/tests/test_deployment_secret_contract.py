from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _job_section(workflow_text: str, job_id: str) -> str:
    lines = workflow_text.splitlines()
    start = next(
        index for index, line in enumerate(lines)
        if line.rstrip() == f"  {job_id}:"
    )
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if (
            line.startswith("  ")
            and not line.startswith("   ")
            and line.rstrip().endswith(":")
        ):
            end = index
            break
    return "\n".join(lines[start:end])


def test_render_uses_canonical_production_secret_contract():
    render_config = (REPO_ROOT / "backend" / "render.yaml").read_text(
        encoding="utf-8"
    )

    assert "- key: SMARTAI_RUNTIME_ENVIRONMENT\n        value: production" in render_config
    assert "- key: SMARTAI_JWT_SECRET\n        generateValue: true" in render_config
    assert not re.search(r"^\s*- key: JWT_SECRET\s*$", render_config, re.MULTILINE)
    assert "- key: SMARTAI_PROVIDER_ENCRYPTION_KEY\n        sync: false" in render_config


def test_env_example_never_commits_secret_placeholders_as_values():
    env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")

    assert env_example.count("# SMARTAI_PROVIDER_ENCRYPTION_KEY=") == 1
    assert env_example.count("# SMARTAI_JWT_SECRET=") == 1
    assert not re.search(
        r"^SMARTAI_(?:PROVIDER_ENCRYPTION_KEY|JWT_SECRET)=.+$",
        env_example,
        re.MULTILINE,
    )
    assert "smartai-dev-provider-key-change-in-prod" not in env_example
    assert "replace-with-a-long-random-secret" not in env_example


def test_ci_backend_jobs_use_explicit_independent_test_secrets():
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    for job_id in ("backend-sqlite", "backend-postgres", "e2e"):
        section = _job_section(workflow, job_id)
        assert 'SMARTAI_RUNTIME_ENVIRONMENT: "test"' in section
        provider_match = re.search(
            r'SMARTAI_PROVIDER_ENCRYPTION_KEY: "([^"]+)"', section
        )
        jwt_match = re.search(r'SMARTAI_JWT_SECRET: "([^"]+)"', section)
        assert provider_match is not None
        assert jwt_match is not None
        provider_key = provider_match.group(1)
        jwt_secret = jwt_match.group(1)
        assert len(provider_key.encode("utf-8")) >= 32
        assert len(jwt_secret.encode("utf-8")) >= 32
        assert provider_key != jwt_secret

    assert "SMARTAI_PROVIDER_ENCRYPTION_KEY: ci-provider-master-key\n" not in workflow
    assert "SMARTAI_PROVIDER_ENCRYPTION_KEY=e2e-master-key" not in workflow


def test_accidental_root_curl_file_is_absent():
    assert not (REPO_ROOT / "curl").exists()
