"""Exercise the Lightsail release transaction with disposable directories."""

from pathlib import Path
import subprocess
import sys

import pytest


DEPLOY_SCRIPT = Path(__file__).resolve().parents[2] / "scripts/ops/aws/smartai-deploy"
OLD_IMAGE = "sha256:" + "a" * 64
NEW_IMAGE = "sha256:" + "b" * 64


def run_deploy_functions(shell, *args, check=True):
    return subprocess.run(
        ["bash", "-c", 'source "$1"; shift\n' + shell, "test-deploy", DEPLOY_SCRIPT, *args],
        check=check,
        capture_output=True,
        text=True,
        timeout=15,
    )


@pytest.mark.parametrize("outcome", ["build", "reuse", "build_failure", "probe_failure", "smoke_failure"])
def test_candidate_preflight(tmp_path, outcome):
    runner = tmp_path / "tools/runner_spike"
    runner.mkdir(parents=True)
    for name in ("Dockerfile", "worker.py", ".dockerignore"):
        (runner / name).touch()

    result = run_deploy_functions(
        r"""
release="$1"
expected_image="$2"
outcome="$3"
log() { printf '%s\n' "$*"; }
if ! command -v sha256sum >/dev/null; then
    sha256sum() { shasum -a 256 "$@"; }
fi
if [[ "$OSTYPE" == darwin* ]]; then
    # The deployed host uses GNU tar. This test targets preflight sequencing.
    tar() { command tar -cf - .; }
fi
runner_as_service() {
    printf '%s:%s:%s\n' "$1" "$2" "$3" >> "$release/calls"
    case "$1:$2:$3" in
        podman:image:exists) [[ "$outcome" == reuse ]] ;;
        podman:build:--tag) [[ "$outcome" != build_failure ]] ;;
        podman:image:inspect) printf '%s\n' "${expected_image#sha256:}" ;;
        .venv/bin/python:tools/runner_spike/harness.py:probe) [[ "$outcome" != probe_failure ]] ;;
        .venv/bin/python:-:*) cat >/dev/null; [[ "$outcome" != smoke_failure ]] ;;
        *) return 1 ;;
    esac
}
if candidate="$(prepare_runner "$release")"; then
    [[ "$outcome" == build || "$outcome" == reuse ]]
    printf '%s\n' "$candidate"
else
    [[ "$outcome" == *_failure && -z "$candidate" ]]
fi
""",
        str(tmp_path),
        NEW_IMAGE,
        outcome,
    )
    assert result.stdout.strip() == ("" if outcome.endswith("_failure") else NEW_IMAGE)
    calls = (tmp_path / "calls").read_text().splitlines()
    assert calls[0] == "podman:image:exists"
    assert ("podman:build:--tag" in calls) == (outcome != "reuse")
    last_command = {
        "build_failure": "podman:build:--tag",
        "probe_failure": ".venv/bin/python:tools/runner_spike/harness.py:probe",
    }.get(outcome, f".venv/bin/python:-:{NEW_IMAGE}")
    assert calls[-1] == last_command


@pytest.mark.parametrize("result", ["healthy", "restart_failure", "wrong_live_image", "migration"])
def test_activate_and_recover_backend_and_image(tmp_path, result):
    previous = tmp_path / "previous"
    candidate = tmp_path / "candidate"
    current = tmp_path / "current"
    for release, image in ((previous, OLD_IMAGE), (candidate, NEW_IMAGE)):
        release.mkdir()
        (release / ".runner.env").write_text(f"SMARTAI_PROGRAM_RUNNER_IMAGE={image}\n")
    current.symlink_to(previous, target_is_directory=True)

    run_deploy_functions(
        r"""
BASE="$1"
OLD_RUNNER_IMAGE="$2"
RUNNER_IMAGE="$3"
result="$4"
python="$5"
CURRENT="$BASE/current"
OLD_RELEASE="$BASE/previous"
RELEASE="$BASE/candidate"
SHA=new
MIGRATION_NEEDED=0
MIGRATION_PHASE=none
BACKUP_FILE=previous.dump
if [[ "$result" == migration ]]; then
    MIGRATION_NEEDED=1
    MIGRATION_PHASE=migrating
fi
log() { printf '%s\n' "$*"; }
sleep() { :; }
curl() { :; }
if [[ "$OSTYPE" == darwin* ]]; then
    mv() {
        [[ "$1" == -Tf && "$2" == -- ]]
        "$python" -c 'import os, sys; os.replace(*sys.argv[1:])' "$3" "$4"
    }
fi
systemctl() {
    [[ "$2" == smartai && "$3" == smartai-admin ]] || return 1
    local target
    target="$(readlink "$CURRENT")"
    if [[ "$target" == "$RELEASE" ]]; then
        [[ "$result" != restart_failure && "$result" != migration ]] || return 1
    fi
    printf '%s\n' "$target" >> "$BASE/starts"
}
live_runner_image() {
    if [[ "$result" == wrong_live_image && "$1" == "$RUNNER_IMAGE" ]]; then
        return 1
    fi
    grep -Fxq "SMARTAI_PROGRAM_RUNNER_IMAGE=$1" "$CURRENT/.runner.env"
}
if activate_release; then
    [[ "$result" == healthy ]]
elif recover_release; then
    [[ "$result" == restart_failure || "$result" == wrong_live_image ]]
else
    [[ "$result" == migration ]]
fi
""",
        str(tmp_path),
        OLD_IMAGE,
        NEW_IMAGE,
        result,
        sys.executable,
    )
    active = candidate if result in {"healthy", "migration"} else previous
    assert current.resolve() == active
    starts = tmp_path / "starts"
    if result in {"restart_failure", "wrong_live_image"}:
        assert starts.read_text().splitlines()[-1] == str(previous)
    elif result == "migration":
        assert not starts.exists()
    else:
        assert starts.read_text().splitlines() == [str(candidate)]


@pytest.mark.parametrize("outcome", ["stop_failure", "backup_failure", "backup_signal", "migration_failure",
                                     "migration_signal", "success"])
def test_migration_recovery_only_restarts_the_previous_services_before_alembic(tmp_path, outcome):
    previous = tmp_path / "previous"
    candidate = tmp_path / "candidate"
    for release in (previous, candidate):
        release.mkdir()
    (tmp_path / "current").symlink_to(previous, target_is_directory=True)

    result = run_deploy_functions(
        r"""
BASE="$1"
outcome="$2"
CURRENT="$BASE/current"
RELEASE="$BASE/candidate"
OLD_RUNNER_IMAGE=old-image
BACKUPS="$BASE"
SHA=example
TARGET_RELEASE_CREATED=1
TARGET_ENV_CREATED=0
DEPLOY_OK=0
ACTIVATION_STARTED=0
MIGRATION_PHASE=none
BACKUP_FILE=""
log() { echo "$*"; }
systemctl() {
    echo "$*" >> "$BASE/service-actions"
    [[ "$1" != stop || "$outcome" != stop_failure ]]
}
wait_for_release() { echo "$1" >> "$BASE/verified-images"; }
runuser() {
    if [[ "$2" == postgres ]]; then
        echo "example backup" > "${!#}"
        if [[ "$outcome" == backup_signal ]]; then kill -TERM "$$"; fi
        [[ "$outcome" != backup_failure ]]
    else
        [[ "$2" == smartai && -f "$BACKUP_FILE" ]] || return 2
        echo attempted > "$BASE/migration"
        if [[ "$outcome" == migration_signal ]]; then kill -TERM "$$"; fi
        [[ "$outcome" != migration_failure ]]
    fi
}
trap cleanup_on_exit EXIT
trap 'exit 143' TERM
migrate_database
DEPLOY_OK=1
""",
        str(tmp_path), outcome, check=False,
    )
    expected_status = 143 if outcome.endswith("_signal") else 0 if outcome == "success" else 1
    assert result.returncode == expected_status, result.stderr
    reached_migration = (tmp_path / "migration").exists()
    assert reached_migration == (outcome.startswith("migration_") or outcome == "success")
    assert bool(list(tmp_path.glob("*.dump"))) == reached_migration
    assert not list(tmp_path.glob("*.partial"))
    assert candidate.exists() == reached_migration
    assert (tmp_path / "current").resolve() == previous
    actions = (tmp_path / "service-actions").read_text().splitlines()
    assert actions == ["stop smartai smartai-admin"] + ([] if reached_migration else ["start smartai smartai-admin"])
    if reached_migration:
        assert ("require manual review" in result.stdout) == (outcome != "success")
    else:
        assert (tmp_path / "verified-images").read_text().strip() == "old-image"
