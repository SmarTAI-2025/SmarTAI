from __future__ import annotations

import csv
from io import StringIO

import pytest


def _prepared_task():
    from backend.db import (
        assignment_repository,
        course_repository,
        grading_repository,
        submission_repository,
    )
    from backend.db.models import UserRecord
    from backend.db.session import session_scope
    from backend.domain import education
    from backend.services import grading_runs, task_facade

    owner_id = "finalization-owner"
    student_id = "finalization-student"
    with session_scope() as session:
        session.add_all([
            UserRecord(
                id=owner_id, username=owner_id, password_hash="hash",
                role="teacher", is_active=True,
            ),
            UserRecord(
                id=student_id, username=student_id, password_hash="hash",
                role="student", is_active=True,
            ),
        ])
    task = task_facade.create_task(
        owner_id=owner_id,
        name="Formal result",
        semester_id=None,
        course_id=None,
        idempotency_key="formal-result-task",
    )
    task_id = task["task_id"]
    assignment = assignment_repository.get_assignment(task_id, actor_id=owner_id)
    course_repository.enroll(course_id=assignment.course_id, student_id=student_id)
    questions = [
        assignment_repository.add_question(
            assignment_id=task_id, teacher_id=owner_id,
            q_id=q_id, order_index=index, type="short", stem=q_id,
            max_score=10,
        )
        for index, q_id in enumerate(("q-required", "q-optional"))
    ]
    assignment_repository.publish(
        assignment_id=task_id,
        teacher_id=owner_id,
        expected_version=assignment.version,
    )
    submission = submission_repository.create_submission(
        assignment_id=task_id, student_id=student_id,
    )
    revision = submission_repository.add_revision(
        submission_id=submission.id,
        student_id=student_id,
        source=education.SubmissionRevisionSource.ONLINE.value,
        answers=[{
            "question_id": question.id,
            "q_id": question.q_id,
            "type": question.type,
            "content": "answer",
        } for question in questions],
    )
    run = grading_runs.create_run(teacher_id=owner_id, assignment_id=task_id)
    grading_repository.claim_lease(
        run_id=run.id, worker_id="finalization-worker", lease_seconds=60,
    )
    required = education.GradeResultDTO(
        id="",
        grading_run_id=run.id,
        submission_revision_id=revision.id,
        question_id=questions[0].id,
        student_id=student_id,
        q_id=questions[0].q_id,
        ai_score=6,
        ai_max_score=10,
        requires_review=True,
        review_reasons=["low_confidence"],
        result_status=education.GradeResultStatus.NEEDS_REVIEW.value,
        created_at=0,
        updated_at=0,
    )
    optional = education.GradeResultDTO(
        id="",
        grading_run_id=run.id,
        submission_revision_id=revision.id,
        question_id=questions[1].id,
        student_id=student_id,
        q_id=questions[1].q_id,
        ai_score=9,
        ai_max_score=10,
        requires_review=False,
        result_status=education.GradeResultStatus.GRADED.value,
        created_at=0,
        updated_at=0,
    )
    required_row = grading_repository.upsert_result(
        run.id, worker_id="finalization-worker", grade_result=required,
    )
    optional_row = grading_repository.upsert_result(
        run.id, worker_id="finalization-worker", grade_result=optional,
    )
    grading_repository.mark_completed(
        run.id, worker_id="finalization-worker", completed=1, failed=0,
    )
    return {
        "owner_id": owner_id,
        "student_id": student_id,
        "task_id": task_id,
        "run_id": run.id,
        "required_result_id": required_row.id,
        "optional_result_id": optional_row.id,
    }


def test_required_review_audit_counts_only_originally_required_results():
    from backend.db import grading_repository
    from backend.services import task_facade

    seeded = _prepared_task()
    before = task_facade.finalization(
        task_id=seeded["task_id"], owner_id=seeded["owner_id"],
    )
    assert (
        before["required_review_count"],
        before["confirmed_required_count"],
        before["remaining_review_count"],
    ) == (1, 0, 0)

    grading_repository.add_teacher_review(
        seeded["required_result_id"],
        teacher_id=seeded["owner_id"],
        new_score=7,
        new_comment="required confirmed",
        confirm=True,
    )
    grading_repository.add_teacher_review(
        seeded["optional_result_id"],
        teacher_id=seeded["owner_id"],
        new_score=9.5,
        new_comment="optional override",
        confirm=True,
    )
    after = task_facade.finalization(
        task_id=seeded["task_id"], owner_id=seeded["owner_id"],
    )
    assert (
        after["required_review_count"],
        after["confirmed_required_count"],
        after["remaining_review_count"],
    ) == (1, 1, 0)


def test_missing_ai_score_repair_still_requires_explicit_confirmation():
    from backend.db import grading_repository
    from backend.db.models import GradeResultRecord
    from backend.db.session import session_scope
    from backend.domain.errors import ResultNotReleasable
    from backend.services import task_facade

    seeded = _prepared_task()
    args = {"task_id": seeded["task_id"], "owner_id": seeded["owner_id"]}
    with session_scope() as session:
        row = session.get(GradeResultRecord, seeded["required_result_id"])
        row.ai_score = None
        row.result_status = "failed"
        row.initial_review_reasons = ["llm_failed"]
        row.review_reasons = ["llm_failed"]
    grading_repository.add_teacher_review(
        seeded["required_result_id"], teacher_id=seeded["owner_id"],
        new_score=7, new_comment="Repaired score", confirm=False,
    )
    pending = task_facade.finalization(**args)
    assert pending["ready_for_confirmation"] is False
    assert pending["remaining_review_count"] == 1
    with pytest.raises(ResultNotReleasable):
        task_facade.generate_artifacts(**args, expected_revision=pending["workflow_revision"])
    assert grading_repository.get_run(seeded["run_id"], actor_id=seeded["owner_id"]).released_at is None
    grading_repository.add_teacher_review(
        seeded["required_result_id"], teacher_id=seeded["owner_id"],
        new_score=7, new_comment="Repaired score", confirm=True,
    )
    confirmed = task_facade.finalization(**args)
    assert confirmed["ready_for_confirmation"] is True
    assert confirmed["remaining_review_count"] == 0
    task_facade.generate_artifacts(**args, expected_revision=confirmed["workflow_revision"])
    assert grading_repository.get_run(seeded["run_id"], actor_id=seeded["owner_id"]).released_at is not None


def test_soft_review_ai_score_is_publishable_and_exported_by_default():
    from backend.services import task_facade

    seeded = _prepared_task()
    final = task_facade.generate_artifacts(
        task_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        expected_revision=0,
    )
    assert final["status"] == "ok"
    assert final["required_review_count"] == 1
    assert final["confirmed_required_count"] == 0
    assert final["remaining_review_count"] == 0

    snapshot = task_facade.result_snapshot(
        task_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        result_version=1,
    )
    corrections = snapshot["payload"]["results"][0]["corrections"]
    by_question = {row["q_id"]: row for row in corrections}
    assert by_question["q-required"]["score"] == 6
    assert by_question["q-required"]["teacher_score"] is None
    assert by_question["q-required"]["review_status"] == "pending"

    body, media_type, _filename = task_facade.artifact_bytes(
        task_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        version=1,
        artifact_id="grades_csv",
    )
    assert media_type == "text/csv; charset=utf-8"
    rows = list(csv.DictReader(StringIO(body.decode("utf-8-sig"))))
    assert len(rows) == 1
    assert rows[0]["total_score"] == "15"
    assert rows[0]["q-required_score"] == "6"
    assert rows[0]["q-optional_score"] == "9"


def test_same_score_individual_confirmation_survives_result_reload():
    from backend.services import task_facade

    seeded = _prepared_task()
    args = {"task_id": seeded["task_id"], "owner_id": seeded["owner_id"]}
    saved = task_facade.update_correction_review(
        **args, display_student_id=seeded["student_id"], q_id="q-required",
        teacher_score=6, teacher_comment="", confirm=True, expected_revision=0,
    )
    assert saved["unchanged"] is True
    rows = {item["q_id"]: item for item in task_facade.task_results(**args)["results"][0]["corrections"]}
    assert rows["q-required"]["teacher_score"] == 6
    assert rows["q-required"]["review_status"] == "confirmed"
    assert rows["q-required"]["reviewed_at"] is not None
    assert rows["q-optional"]["teacher_score"] is None


def test_finalization_artifacts_and_dirty_state_are_idempotent():
    from backend.db import grading_repository
    from backend.services import task_facade

    seeded = _prepared_task()
    grading_repository.add_teacher_review(
        seeded["required_result_id"],
        teacher_id=seeded["owner_id"],
        new_score=7,
        new_comment="confirmed",
        confirm=True,
    )
    first = task_facade.generate_artifacts(
        task_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        expected_revision=0,
    )
    replay = task_facade.generate_artifacts(
        task_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        expected_revision=0,
    )
    assert first["status"] == "ok"
    assert first["final_result_version"] == 1
    assert replay["status"] == "already_done"
    assert replay["workflow_revision"] == first["workflow_revision"] == 1

    assert first["artifacts"]["versions"][0]["status"] == "ready"

    bundle, media_type, filename = task_facade.artifact_bytes(
        task_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        version=1,
        artifact_id="bundle",
    )
    assert bundle.startswith(b"PK")
    assert media_type == "application/zip"
    assert filename.endswith(".zip")

    edited = task_facade.update_correction_review(
        task_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        display_student_id=seeded["student_id"],
        q_id="q-optional",
        teacher_score=8.5,
        teacher_comment="new draft",
        confirm=False,
        expected_revision=first["workflow_revision"],
    )
    assert edited["workflow_revision"] == 2
    stale_index = task_facade.artifact_index(
        task_id=seeded["task_id"], owner_id=seeded["owner_id"],
    )
    assert stale_index["versions"][0]["status"] == "stale"


@pytest.mark.parametrize("with_json_metadata", [False, True])
def test_paused_json_exports_keep_current_format_reports_downloadable(monkeypatch, with_json_metadata):
    import hashlib
    from io import BytesIO
    from zipfile import ZipFile

    from backend.domain.errors import NotFound
    from backend.services import result_artifacts, task_facade

    seeded = _prepared_task()
    args = {"task_id": seeded["task_id"], "owner_id": seeded["owner_id"]}
    monkeypatch.setattr(result_artifacts, "JSON_EXPORT_ENABLED", with_json_metadata)
    generated = task_facade.generate_artifacts(**args, expected_revision=0)
    original_files = generated["artifacts"]["versions"][0]["files"]
    assert len(original_files) == (5 if with_json_metadata else 4)
    monkeypatch.setattr(result_artifacts, "JSON_EXPORT_ENABLED", False)

    original_dumps = result_artifacts.json.dumps

    def reject_json_export(value, *args, **kwargs):
        assert not (isinstance(value, dict) and "result" in value and "task_id" in value)
        return original_dumps(value, *args, **kwargs)

    monkeypatch.setattr(result_artifacts.json, "dumps", reject_json_export)
    index = task_facade.artifact_index(**args)
    assert index["versions"][0]["status"] == "ready"
    files = index["versions"][0]["files"]
    assert {item["artifact_id"] for item in files} == {
        "grades_csv", "learning_report_md", "published_answers_md", "published_answers_tex",
    }
    with pytest.raises(NotFound):
        task_facade.artifact_bytes(**args, version=1, artifact_id="formal_result_json")
    bundle = task_facade.artifact_bytes(**args, version=1, artifact_id="bundle")[0]
    with ZipFile(BytesIO(bundle)) as archive:
        assert set(archive.namelist()) == {item["filename"] for item in files}
        assert not any(name.endswith(".json") for name in archive.namelist())
        for item in files:
            content = task_facade.artifact_bytes(**args, version=1, artifact_id=item["artifact_id"])[0]
            assert archive.read(item["filename"]) == content
            assert hashlib.sha256(content).hexdigest() == item["sha256"]

    # Pausing JSON does not invalidate the current report's other files.
    regenerated = task_facade.generate_artifacts(**args, expected_revision=1)
    assert regenerated["status"] == "already_done"
    assert regenerated["final_result_version"] == 1
    assert task_facade.generate_artifacts(**args, expected_revision=1)["status"] == "already_done"
    assert task_facade.artifact_bytes(**args, version=1, artifact_id="bundle")[0] == bundle


@pytest.mark.parametrize("schema_version", [None, 1, 999])
def test_retired_report_formats_are_hidden_blocked_and_never_reused(monkeypatch, schema_version):
    from fastapi.testclient import TestClient
    from backend.auth import create_token
    from backend.db import workflow_repository
    from backend.db.session import session_scope
    from backend.main import app
    from backend.services import result_artifacts, task_facade

    seeded = _prepared_task()
    args = {"task_id": seeded["task_id"], "owner_id": seeded["owner_id"]}
    generated = task_facade.generate_artifacts(**args, expected_revision=0)
    record = workflow_repository.list_artifact_manifests(seeded["task_id"], owner_id=seeded["owner_id"])[0]
    # Represent a pre-upgrade/unsupported report without keeping its renderer.
    with session_scope() as session:
        stored = session.get(workflow_repository.ResultArtifactManifestRecord, record.id)
        stored.manifest = {**stored.manifest, "schema_version": schema_version}

    client = TestClient(app)
    base = f"/tasks/{seeded['task_id']}"
    headers = {"Authorization": "Bearer " + create_token(seeded["owner_id"], "teacher")}
    result_before = client.get(base + "/result", headers=headers).json()
    index = client.get(base + "/artifacts", headers=headers).json()
    assert not any(item["files"] for item in index["versions"])
    assert index["analysis_status"] == "not_generated"
    state = client.get(base + "/finalization", headers=headers).json()
    assert state["available_result_versions"] == 0
    assert state["analysis_status"] == "not_generated"

    def unexpected_render(*args, **kwargs):
        raise AssertionError("Retired reports must be rejected before rendering")

    with monkeypatch.context() as patch:
        patch.setattr(task_facade, "build_artifact_files", unexpected_render)
        patch.setattr(task_facade, "result_snapshot", unexpected_render)
        for artifact_id in [item["artifact_id"] for item in record.manifest["files"]] + ["bundle", "formal_result_json"]:
            assert client.get(base + f"/artifacts/1/{artifact_id}", headers=headers).status_code == 404
        assert client.delete(base + "/artifacts/1", headers=headers).status_code == 404

    refreshed = client.post(base + "/artifacts/generate", headers=headers,
                            json={"expected_workflow_revision": generated["workflow_revision"], "schema_version": schema_version})
    assert refreshed.status_code == 200, refreshed.text
    state = refreshed.json()
    assert state["status"] == "ok"
    assert state["available_result_versions"] == 1
    assert [item["version"] for item in state["artifacts"]["versions"]] == [2]
    records = workflow_repository.list_artifact_manifests(seeded["task_id"], owner_id=seeded["owner_id"])
    assert records[0].manifest["schema_version"] == result_artifacts.ARTIFACT_SCHEMA_VERSION
    assert client.get(base + "/artifacts/1/bundle", headers=headers).status_code == 404
    assert client.get(base + "/artifacts/2/bundle", headers=headers).status_code == 200
    assert task_facade.generate_artifacts(**args, expected_revision=state["workflow_revision"])["status"] == "already_done"
    assert client.get(base + "/result", headers=headers).json() == result_before


@pytest.mark.parametrize("field,error_code", [
    ("sha256", "artifact_renderer_changed"),
    ("title", "artifact_renderer_changed"),
    ("artifact_fingerprint", "artifact_manifest_changed"),
])
def test_paused_json_exports_preserve_download_integrity_checks(field, error_code):
    from copy import deepcopy
    from sqlalchemy import select

    from backend.db.session import session_scope
    from backend.db.workflow_repository import ResultArtifactManifestRecord
    from backend.domain.errors import VersionConflict
    from backend.services import task_facade

    seeded = _prepared_task()
    args = {"task_id": seeded["task_id"], "owner_id": seeded["owner_id"]}
    task_facade.generate_artifacts(**args, expected_revision=0)
    task_facade.generate_artifacts(**args, expected_revision=1)
    with session_scope() as session:
        record = session.scalar(select(ResultArtifactManifestRecord).where(
            ResultArtifactManifestRecord.assignment_id == seeded["task_id"],
        ))
        manifest = deepcopy(record.manifest)
        if field == "artifact_fingerprint":
            manifest[field] = "changed"
        else:
            manifest["files"][0][field] = "changed"
        record.manifest = manifest
    with pytest.raises(VersionConflict, match=error_code):
        task_facade.artifact_bytes(**args, version=1, artifact_id="bundle")


def test_upstream_answer_edit_marks_detached_formal_result_and_artifacts_stale():
    from backend.services import task_facade

    seeded = _prepared_task()
    task_facade.generate_artifacts(
        task_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        expected_revision=0,
    )
    generated = task_facade.generate_artifacts(
        task_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        expected_revision=1,
    )

    task_facade.update_student_answer(
        task_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        display_student_id=seeded["student_id"],
        q_id="q-required",
        patch={"content": "corrected after release", "review_status": "confirmed"},
        expected_revision=generated["workflow_revision"],
    )

    task = task_facade.get_task(
        task_id=seeded["task_id"], owner_id=seeded["owner_id"], full=False,
    )
    finalization = task_facade.finalization(
        task_id=seeded["task_id"], owner_id=seeded["owner_id"],
    )
    artifacts = task_facade.artifact_index(
        task_id=seeded["task_id"], owner_id=seeded["owner_id"],
    )

    assert task["status"] == "submissions_ready"
    assert task["grading_job_id"] is None
    assert task["final_result_dirty"] is True
    assert finalization["final_result_dirty"] is True
    assert artifacts["versions"][0]["status"] == "stale"


@pytest.mark.parametrize("generate_latest", [False, True])
def test_history_download_and_delete_preserve_scores_and_latest_report(generate_latest):
    from fastapi.testclient import TestClient
    from backend.auth import create_token
    from backend.db import workflow_repository
    from backend.domain.errors import VersionConflict
    from backend.main import app
    from backend.services import task_facade

    seeded = _prepared_task()
    args = {"task_id": seeded["task_id"], "owner_id": seeded["owner_id"]}
    task_facade.generate_artifacts(**args, expected_revision=0)
    client = TestClient(app)
    base = f"/tasks/{seeded['task_id']}/artifacts"
    headers = {"Authorization": "Bearer " + create_token(seeded["owner_id"], "teacher")}
    original = client.get(base + "/1/bundle", headers=headers).content
    assert original.startswith(b"PK")
    # The latest unchanged report is protected; deletion requires ownership.
    assert client.delete(base + "/1").status_code == 401
    assert client.delete(base + "/1", headers=headers).status_code == 409
    other = {"Authorization": "Bearer " + create_token(seeded["student_id"], "student")}
    assert client.delete(base + "/1", headers=other).status_code == 403

    edited = task_facade.update_correction_review(
        **args, display_student_id=seeded["student_id"], q_id="q-optional",
        teacher_score=8.5, teacher_comment="keep this review", confirm=True, expected_revision=1,
    )
    # A stale deletion decision must not race a workflow change.
    with pytest.raises(VersionConflict):
        workflow_repository.delete_artifact_manifest_atomic(
            assignment_id=seeded["task_id"], owner_id=seeded["owner_id"],
            result_version=1, expected_revision=1,
        )
    latest = None
    if generate_latest:
        prepared = task_facade.generate_artifacts(**args, expected_revision=edited["workflow_revision"])
        task_facade.generate_artifacts(**args, expected_revision=prepared["workflow_revision"])
        latest = client.get(base + "/2/bundle", headers=headers).content
        assert latest.startswith(b"PK") and latest != original
    # Both before and after new report generation, historical bytes stay frozen.
    assert client.get(base + "/1/bundle", headers=headers).content == original
    before = client.get(f"/tasks/{seeded['task_id']}/result", headers=headers).json()
    removed = client.delete(base + "/1", headers=headers)
    assert removed.status_code == 200, removed.text
    assert not any(row["version"] == 1 and row["files"] for row in removed.json()["versions"])
    assert client.get(base + "/1/bundle", headers=headers).status_code == 404
    assert client.get(f"/tasks/{seeded['task_id']}/result", headers=headers).json() == before
    if generate_latest:
        assert client.get(base + "/2/bundle", headers=headers).content == latest
    else:
        # Deleting the last historical report does not block new generation.
        prepared = task_facade.generate_artifacts(**args, expected_revision=edited["workflow_revision"])
        generated = task_facade.generate_artifacts(**args, expected_revision=prepared["workflow_revision"])
        assert generated["artifacts"]["versions"][0]["version"] == 2


@pytest.mark.parametrize("generated_first", [False, True])
def test_legacy_unexported_snapshots_do_not_consume_report_versions(generated_first):
    from backend.db import workflow_repository
    from backend.db.models import GradingRunRecord
    from backend.db.session import session_scope
    from backend.services import task_facade

    seeded = _prepared_task()
    args = {"task_id": seeded["task_id"], "owner_id": seeded["owner_id"]}
    if generated_first:
        state = task_facade.generate_artifacts(**args, expected_revision=0)
        task_facade.update_correction_review(
            **args, display_student_id=seeded["student_id"], q_id="q-optional",
            teacher_score=8, teacher_comment="", confirm=True,
            expected_revision=state["workflow_revision"],
        )
    # Reproduce an old snapshot directly; the retired API can no longer create it.
    with session_scope() as session:
        workflow = session.get(workflow_repository.AssignmentWorkflowRecord, seeded["task_id"])
        run = session.get(GradingRunRecord, workflow.grading_job_id or seeded["run_id"])
        run.released_at = 1234.5
        workflow.final_result_version = 8
        workflow.final_result_updated_at = run.released_at
        workflow.presentation_status = "finalized"
    state = task_facade.finalization(**args)
    expected_version = 2 if generated_first else 1
    generated = task_facade.generate_artifacts(**args, expected_revision=state["workflow_revision"])
    current = next(item for item in generated["artifacts"]["versions"] if item["current"])
    assert current["version"] == expected_version
    _, _, filename = task_facade.artifact_bytes(**args, version=expected_version, artifact_id="bundle")
    assert f"_v{expected_version}_" in filename
    replay = task_facade.generate_artifacts(**args, expected_revision=state["workflow_revision"])
    assert replay["unchanged"] is True
    assert replay["final_result_version"] == expected_version


def test_history_deletion_is_owner_scoped():
    from backend.domain.errors import NotFound
    from backend.services import task_facade

    seeded = _prepared_task()
    args = {"task_id": seeded["task_id"], "owner_id": seeded["owner_id"]}
    task_facade.generate_artifacts(**args, expected_revision=0)
    task_facade.generate_artifacts(**args, expected_revision=1)
    with pytest.raises(NotFound):
        task_facade.delete_historical_artifact(task_id=seeded["task_id"], owner_id="another-teacher", version=1)
    assert task_facade.artifact_bytes(**args, version=1, artifact_id="bundle")[0].startswith(b"PK")
