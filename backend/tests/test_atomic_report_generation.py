"""Report creation is the sole public snapshot mutation, with rollback/replay."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.tests.test_task_finalization_contract import _prepared_task


@pytest.mark.parametrize("failure_stage", ["render", "persist"])
def test_generation_failure_rolls_back_snapshot_version_and_manifest(monkeypatch, failure_stage):
    from backend.db import grading_repository, workflow_repository
    from backend.services import task_facade

    seeded = _prepared_task()
    args = {"task_id": seeded["task_id"], "owner_id": seeded["owner_id"]}

    def fail(*args, **kwargs):
        raise RuntimeError("report generation interrupted")

    with monkeypatch.context() as patch:
        if failure_stage == "render":
            patch.setattr(task_facade, "build_artifact_manifest", fail)
        else:
            # Fails after the report and run have been flushed, before commit.
            patch.setattr(workflow_repository, "_detach_artifact", fail)
        with pytest.raises(RuntimeError, match="interrupted"):
            task_facade.generate_artifacts(**args, expected_revision=0)
    run = grading_repository.get_run(seeded["run_id"], actor_id=seeded["owner_id"])
    state = task_facade.finalization(**args)
    assert run.released_at is None
    assert state["final_result_version"] == state["workflow_revision"] == 0
    assert state["available_result_versions"] == 0
    assert task_facade.generate_artifacts(**args, expected_revision=0)["final_result_version"] == 1


def test_concurrent_generation_and_replay_only_create_one_report(monkeypatch):
    from backend.services import task_facade

    seeded = _prepared_task()
    args = {"task_id": seeded["task_id"], "owner_id": seeded["owner_id"], "expected_revision": 0}
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: task_facade.generate_artifacts(**args), range(2)))
    assert sorted(item["status"] for item in responses) == ["already_done", "ok"]

    def unexpected_render(*args, **kwargs):
        raise AssertionError("An exact replay must not render files again")

    monkeypatch.setattr(task_facade, "build_artifact_manifest", unexpected_render)
    replay = task_facade.generate_artifacts(**args)
    assert replay["final_result_version"] == replay["workflow_revision"] == 1
    assert replay["available_result_versions"] == 1


def test_retired_snapshot_routes_and_read_only_pages_cannot_create_versions(monkeypatch):
    from fastapi.testclient import TestClient
    from backend.auth import create_token
    from backend.main import app
    from backend.db import workflow_repository
    from backend.services import result_artifacts, task_facade

    seeded = _prepared_task()
    client = TestClient(app)
    headers = {"Authorization": "Bearer " + create_token(seeded["owner_id"], "teacher")}
    base = "/tasks/" + seeded["task_id"]
    assert "/tasks/{task_id}/finalization/confirm" not in app.openapi()["paths"]
    for route in (base + "/finalization/confirm", "/grading-runs/" + seeded["run_id"] + "/release"):
        assert client.post(route, headers=headers, json={"expected_workflow_revision": 0}).status_code == 404
    for route in ("/finalization", "/result", "/state", "/artifacts"):
        assert client.get(base + route, headers=headers).status_code == 200
    state = task_facade.finalization(task_id=seeded["task_id"], owner_id=seeded["owner_id"])
    assert state["final_result_version"] == state["workflow_revision"] == 0
    assert state["available_result_versions"] == 0

    def retired_rule(*args):
        raise AssertionError("New reports must never use historical review rules")

    monkeypatch.setattr(result_artifacts, "_legacy_needs_review", retired_rule)
    generated = client.post(base + "/artifacts/generate", headers=headers,
                            json={"expected_workflow_revision": 0, "schema_version": 1})
    assert generated.status_code == 200, generated.text
    record = workflow_repository.list_artifact_manifests(
        seeded["task_id"], owner_id=seeded["owner_id"],
    )[0]
    assert record.manifest["schema_version"] == result_artifacts.ARTIFACT_SCHEMA_VERSION == 2


def test_confirmation_changes_new_report_count_without_rewriting_history():
    from backend.db.models import GradeResultRecord
    from backend.db.session import session_scope
    from backend.services import task_facade

    seeded = _prepared_task()
    args = {"task_id": seeded["task_id"], "owner_id": seeded["owner_id"]}
    with session_scope() as session:
        for result_id in (seeded["required_result_id"], seeded["optional_result_id"]):
            row = session.get(GradeResultRecord, result_id)
            row.ai_confidence = .95
            row.requires_review = False
            row.review_reasons = []
        row = session.get(GradeResultRecord, seeded["optional_result_id"])
        row.ai_expert_results = [{"score": 10}, {"score": 7}]
    first = task_facade.generate_artifacts(**args, expected_revision=0)
    original = task_facade.artifact_bytes(**args, version=1, artifact_id="learning_report_md")[0]
    assert next(line for line in original.decode().splitlines() if line.startswith("| q-optional |")).endswith("| 1 |")
    edited = task_facade.update_correction_review(
        **args, display_student_id=seeded["student_id"], q_id="q-optional",
        teacher_score=9, teacher_comment="", confirm=True,
        expected_revision=first["workflow_revision"],
    )
    task_facade.generate_artifacts(**args, expected_revision=edited["workflow_revision"])
    current = task_facade.artifact_bytes(**args, version=2, artifact_id="learning_report_md")[0]
    assert next(line for line in current.decode().splitlines() if line.startswith("| q-optional |")).endswith("| 0 |")
    assert task_facade.artifact_bytes(**args, version=1, artifact_id="learning_report_md")[0] == original
