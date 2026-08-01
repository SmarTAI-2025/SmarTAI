import pytest
from types import SimpleNamespace


def _seed_assignment_and_run(*, owner_id: str, assignment_id: str):
    from backend.db import assignment_repository, grading_repository
    from backend.db.models import AssignmentRecord, CourseRecord, UserRecord
    from backend.db.session import session_scope

    with session_scope() as session:
        session.add(UserRecord(
            id=owner_id, username=owner_id, password_hash="hash",
            role="teacher", is_active=True,
        ))
        session.flush()
        session.add(CourseRecord(
            id=f"{assignment_id}-course", name="Course", teacher_id=owner_id,
        ))
        session.flush()
        session.add(AssignmentRecord(
            id=assignment_id, course_id=f"{assignment_id}-course",
            teacher_id=owner_id, name="Assignment", status="draft", version=1,
        ))
    assignment_repository.add_question(
        assignment_id=assignment_id,
        teacher_id=owner_id,
        q_id="q1",
        order_index=0,
        type="short",
        stem="Question",
        max_score=10,
    )
    return grading_repository.create_run(
        assignment_id, teacher_id=owner_id, total_submissions=0,
    )


def test_formal_artifact_version_is_append_only_and_idempotent():
    from backend.db import workflow_repository
    from backend.domain.errors import VersionConflict

    owner_id = "artifact-owner"
    assignment_id = "artifact-assignment"
    run = _seed_assignment_and_run(
        owner_id=owner_id, assignment_id=assignment_id,
    )
    original, created = workflow_repository.save_artifact_manifest(
        assignment_id=assignment_id,
        grading_run_id=run.id,
        owner_id=owner_id,
        result_version=1,
        result_fingerprint="fingerprint-v1",
        manifest={"task_name": "Original", "files": []},
    )
    repeated, repeated_created = workflow_repository.save_artifact_manifest(
        assignment_id=assignment_id,
        grading_run_id=run.id,
        owner_id=owner_id,
        result_version=1,
        result_fingerprint="fingerprint-v1",
        manifest={"task_name": "Mutated", "files": [{"artifact_id": "changed"}]},
    )

    assert created is True
    assert repeated_created is False
    assert repeated.id == original.id
    assert repeated.generated_at == original.generated_at
    assert repeated.manifest == {"task_name": "Original", "files": []}

    with pytest.raises(VersionConflict):
        workflow_repository.save_artifact_manifest(
            assignment_id=assignment_id,
            grading_run_id=run.id,
            owner_id=owner_id,
            result_version=1,
            result_fingerprint="different-source",
            manifest={"task_name": "Other", "files": []},
        )


def test_final_result_release_and_version_are_atomic_and_idempotent():
    from backend.db import grading_repository, workflow_repository

    owner_id = "atomic-final-owner"
    assignment_id = "atomic-final-assignment"
    run = _seed_assignment_and_run(
        owner_id=owner_id, assignment_id=assignment_id,
    )
    workflow_repository.ensure_workflow(
        assignment_id=assignment_id, owner_id=owner_id,
    )
    grading_repository.claim_lease(
        run_id=run.id, worker_id="atomic-worker", lease_seconds=60,
    )
    grading_repository.mark_completed(
        run_id=run.id, worker_id="atomic-worker", completed=0, failed=0,
    )

    workflow, released_at, changed = workflow_repository.confirm_final_result_atomic(
        assignment_id=assignment_id,
        owner_id=owner_id,
        grading_run_id=run.id,
        expected_revision=0,
    )
    replay, replay_released_at, replay_changed = (
        workflow_repository.confirm_final_result_atomic(
            assignment_id=assignment_id,
            owner_id=owner_id,
            grading_run_id=run.id,
            # Simulate retrying the exact request after its HTTP response was
            # lost: the client still has the old revision.
            expected_revision=0,
        )
    )

    assert changed is True
    assert workflow.presentation_status == "finalized"
    assert workflow.final_result_version == 1
    assert workflow.workflow_revision == 1
    assert replay_changed is False
    assert replay.final_result_version == 1
    assert replay.workflow_revision == 1
    assert replay_released_at == released_at


def test_grading_uses_full_frozen_question_snapshot_after_later_edit():
    from backend.db import assignment_repository
    from backend.db.models import AssignmentRecord, CourseRecord, UserRecord
    from backend.db.session import session_scope
    from backend.services.grading_runs import _questions_for_run

    owner_id = "question-snapshot-owner"
    assignment_id = "question-snapshot-assignment"
    with session_scope() as session:
        session.add(UserRecord(
            id=owner_id, username=owner_id, password_hash="hash",
            role="teacher", is_active=True,
        ))
        session.flush()
        session.add(CourseRecord(
            id="question-snapshot-course", name="Course", teacher_id=owner_id,
        ))
        session.flush()
        session.add(AssignmentRecord(
            id=assignment_id, course_id="question-snapshot-course",
            teacher_id=owner_id, name="Assignment", status="draft", version=1,
        ))
    original = assignment_repository.add_question(
        assignment_id=assignment_id, teacher_id=owner_id,
        q_id="q1", order_index=0, type="short", stem="original",
        max_score=10,
    )
    frozen_setup = SimpleNamespace(input_manifest={
        "questions": [original.model_dump(mode="json")],
    })
    assignment_repository.update_question(
        assignment_id, teacher_id=owner_id, q_id="q1",
        expected_version=original.version, stem="later edit",
    )

    questions = _questions_for_run(
        assignment_id=assignment_id, frozen_setup=frozen_setup,
    )

    assert questions[0].stem == "original"
    assert questions[0].version == original.version


def test_legacy_minimal_question_manifest_fails_closed_after_edit():
    from backend.db import assignment_repository
    from backend.db.models import AssignmentRecord, CourseRecord, UserRecord
    from backend.db.session import session_scope
    from backend.domain.errors import VersionConflict
    from backend.services.grading_runs import _questions_for_run

    owner_id = "legacy-question-owner"
    assignment_id = "legacy-question-assignment"
    with session_scope() as session:
        session.add(UserRecord(
            id=owner_id, username=owner_id, password_hash="hash",
            role="teacher", is_active=True,
        ))
        session.flush()
        session.add(CourseRecord(
            id="legacy-question-course", name="Course", teacher_id=owner_id,
        ))
        session.flush()
        session.add(AssignmentRecord(
            id=assignment_id, course_id="legacy-question-course",
            teacher_id=owner_id, name="Assignment", status="draft", version=1,
        ))
    original = assignment_repository.add_question(
        assignment_id=assignment_id, teacher_id=owner_id,
        q_id="q1", order_index=0, type="short", stem="original",
        max_score=10,
    )
    legacy_setup = SimpleNamespace(input_manifest={
        "questions": [{
            "id": original.id, "q_id": original.q_id,
            "version": original.version,
        }],
    })
    assignment_repository.update_question(
        assignment_id, teacher_id=owner_id, q_id="q1",
        expected_version=original.version, stem="later edit",
    )

    with pytest.raises(VersionConflict):
        _questions_for_run(
            assignment_id=assignment_id, frozen_setup=legacy_setup,
        )


def test_artifact_manifest_keeps_confirmation_time_and_csv_is_formula_safe():
    import hashlib

    from backend.services.result_artifacts import (
        build_artifact_bundle,
        build_artifact_files,
        build_artifact_manifest,
    )

    snapshot = {
        "version": 2,
        "fingerprint": "formal-v2",
        "created_at": 1234.5,
        "payload": {
            "problem_data": {
                "=2+2": {
                    "q_id": "=2+2", "number": "", "type": "short",
                },
            },
            "results": [{
                "student_id": "=HYPERLINK(\"https://example.invalid\")",
                "student_name": "+SUM(1,1)",
                "corrections": [],
            }],
        },
    }
    manifest = build_artifact_manifest(
        task_id="artifact-safe", task_name="Safe", snapshot=snapshot,
        generated_at=2345.6,
    )
    files = build_artifact_files(
        task_id="artifact-safe", task_name="Safe", snapshot=snapshot,
        generated_at=2345.6,
    )
    csv_body = next(item.content for item in files if item.artifact_id == "grades_csv")

    assert manifest["confirmed_at"] == 1234.5
    assert b"'=HYPERLINK" in csv_body
    assert b"'+SUM" in csv_body
    assert b"'=2+2_score" in csv_body
    metadata = {item["artifact_id"]: item for item in manifest["files"]}
    assert all(
        hashlib.sha256(item.content).hexdigest()
        == metadata[item.artifact_id]["sha256"]
        for item in files
    )
    assert build_artifact_bundle(files, manifest) == build_artifact_bundle(
        files, manifest
    )


def test_figma_grading_run_freezes_full_questions_and_provider_configuration():
    from backend.db import assignment_repository, course_repository
    from backend.db.provider_repository import upsert_provider_config
    from backend.db.workflow_repository import ensure_workflow, get_run_setup, update_workflow
    from backend.db.models import UserRecord
    from backend.db.session import session_scope
    from backend.models import ProviderConfig, TaskGradingSetup
    from backend.services import task_facade

    owner_id = "grading-input-owner"
    with session_scope() as session:
        session.add(UserRecord(
            id=owner_id, username=owner_id, password_hash="hash",
            role="teacher", is_active=True,
        ))
    course = course_repository.create_course(
        teacher_id=owner_id, name="Frozen Inputs"
    )
    assignment = assignment_repository.create_assignment(
        teacher_id=owner_id, course_id=course.id, name="Frozen Inputs"
    )
    question = assignment_repository.add_question(
        assignment_id=assignment.id, teacher_id=owner_id,
        q_id="q1", order_index=0, type="short", stem="original stem",
        criterion="rubric", reference_answer="answer", max_score=10,
    )
    assignment_repository.publish(
        assignment_id=assignment.id, teacher_id=owner_id,
        expected_version=assignment.version,
    )
    provider = upsert_provider_config(
        owner_id,
        ProviderConfig(
            provider_type="openai", api_key="provider-secret",
            model="gpt-test", base_url="https://api.openai.com/v1",
        ),
        master_key="test-suite-provider-master-key",
    )
    setup = TaskGradingSetup(
        selected_provider_ids=[provider.id],
        primary_provider_id=provider.id,
        knowledge_scope="none",
    )
    ensure_workflow(assignment_id=assignment.id, owner_id=owner_id)
    update_workflow(
        assignment.id, owner_id=owner_id,
        grading_setup=setup.model_dump(mode="json"),
        grading_setup_fingerprint="teacher-approved",
    )

    started = task_facade.start_task_grading(
        task_id=assignment.id, owner_id=owner_id
    )
    frozen = get_run_setup(started["job_id"])

    assert frozen is not None
    manifest = frozen.input_manifest
    assert len(manifest["provider_configuration_fingerprint"]) == 64
    assert "provider-secret" not in str(manifest)
    assert manifest["questions"] == [question.model_dump(mode="json")]
