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


def _seed_figma_grading_task(owner_id: str):
    from backend.db import (
        assignment_repository,
        course_repository,
        source_outcome_repository,
        workflow_repository,
    )
    from backend.db.file_repository import save_file
    from backend.db.provider_repository import upsert_provider_config
    from backend.db.workflow_repository import ensure_workflow, update_workflow
    from backend.db.models import UserRecord
    from backend.db.session import session_scope
    from backend.models import ProviderConfig, TaskGradingSetup
    from backend.services import task_facade
    from backend.storage import get_storage

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
    workflow = ensure_workflow(assignment_id=assignment.id, owner_id=owner_id)
    operation, _ = workflow_repository.create_operation(
        assignment_id=assignment.id,
        owner_id=owner_id,
        operation_type="submission_recognition",
        input_hash="f" * 64,
    )
    workflow_repository.update_operation(
        operation.id,
        owner_id=owner_id,
        expected_attempt=operation.attempt,
        status="running",
    )
    workflow = update_workflow(
        assignment.id,
        owner_id=owner_id,
        parse_job_id=operation.id,
        active_operation="submission_recognition",
        active_job_id=operation.id,
    )
    stored = save_file(
        storage=get_storage(),
        owner_id=owner_id,
        kind="submission_source",
        original_name="student.txt",
        content=b"answer",
        content_type="text/plain",
        assignment_id=assignment.id,
    )
    source, _ = source_outcome_repository.register_source(
        owner_id=owner_id,
        assignment_id=assignment.id,
        operation_id=operation.id,
        expected_attempt=operation.attempt,
        order_index=0,
        stored_file_id=stored.id,
    )
    source_outcome_repository.record_outcome(
        source_id=source.id,
        owner_id=owner_id,
        status="parsed",
        student_candidate="S001",
        matched_answer_count=1,
        unknown_question_ids=[],
        stable_error_code=None,
        failure_phase=None,
        retryable=False,
    )
    task_facade._commit_imported_submissions(
        task_id=assignment.id,
        owner_id=owner_id,
        course_id=course.id,
        students=[{
            "stu_id": "S001",
            "stu_name": "Student One",
            "source_id": source.id,
            "source_filename": "student.txt",
            "identity_match_method": "filename",
            "identity_status": "matched",
            "stu_ans": [{
                "q_id": "q1",
                "number": "1",
                "type": "short",
                "content": "answer",
                "flag": [],
            }],
        }],
        expected_workflow_revision=workflow.workflow_revision,
        operation_id=operation.id,
        expected_operation_attempt=operation.attempt,
        submission_file_name="student.txt",
    )
    workflow = update_workflow(
        assignment.id, owner_id=owner_id,
        grading_setup=setup.model_dump(mode="json"),
        grading_setup_fingerprint="teacher-approved",
    )
    return assignment, question, workflow


def test_figma_grading_run_freezes_full_questions_and_provider_configuration():
    from backend.db.workflow_repository import get_run_setup
    from backend.services import task_facade

    owner_id = "grading-input-owner"
    assignment, question, workflow = _seed_figma_grading_task(owner_id)

    started = task_facade.start_task_grading(
        task_id=assignment.id,
        owner_id=owner_id,
        expected_workflow_revision=workflow.workflow_revision,
    )
    frozen = get_run_setup(started["job_id"])

    assert frozen is not None
    manifest = frozen.input_manifest
    assert len(manifest["provider_configuration_fingerprint"]) == 64
    assert "provider-secret" not in str(manifest)
    assert manifest["questions"] == [question.model_dump(mode="json")]


def test_atomic_grading_start_rolls_back_run_and_workflow_together(monkeypatch):
    from backend.db import grading_repository, submission_repository, workflow_repository

    owner_id = "grading-race-owner"
    assignment, _question, workflow = _seed_figma_grading_task(owner_id)
    revisions = [
        item.current_revision_id
        for item in submission_repository.list_submissions(
            assignment.id,
            actor_id=owner_id,
        )
        if item.current_revision_id is not None
    ]

    def fail_before_commit(_record):
        raise RuntimeError("injected_precommit_failure")

    real_run_to_dto = grading_repository._run_to_dto
    monkeypatch.setattr(grading_repository, "_run_to_dto", fail_before_commit)

    with pytest.raises(RuntimeError, match="injected_precommit_failure"):
        grading_repository.create_run_bundle(
            assignment.id,
            teacher_id=owner_id,
            revision_ids=revisions,
            setup=dict(workflow.grading_setup or {}),
            setup_fingerprint="atomic-test",
            input_manifest={},
            workflow_expected_revision=workflow.workflow_revision,
        )
    monkeypatch.setattr(grading_repository, "_run_to_dto", real_run_to_dto)

    persisted_workflow = workflow_repository.get_workflow(
        assignment.id, owner_id=owner_id,
    )
    runs = grading_repository.list_runs_for_assignment(
        assignment.id, actor_id=owner_id,
    )
    assert runs == []
    assert persisted_workflow.workflow_revision == workflow.workflow_revision
    assert persisted_workflow.active_job_id is None
    assert persisted_workflow.grading_job_id is None


def test_grading_start_repairs_a_legacy_active_run_without_a_workflow_pointer():
    from backend.db import workflow_repository
    from backend.services import grading_runs, task_facade

    owner_id = "grading-repair-owner"
    assignment, _question, workflow = _seed_figma_grading_task(owner_id)
    run = grading_runs.start_run(
        assignment_id=assignment.id,
        teacher_id=owner_id,
        grading_setup=dict(workflow.grading_setup or {}),
        setup_fingerprint="legacy-window",
        input_manifest=None,
    )

    started = task_facade.start_task_grading(
        task_id=assignment.id,
        owner_id=owner_id,
        expected_workflow_revision=workflow.workflow_revision,
    )
    repaired = workflow_repository.get_workflow(
        assignment.id,
        owner_id=owner_id,
    )

    assert started == {
        "status": "already_running",
        "task_id": assignment.id,
        "job_id": run.id,
    }
    assert repaired.grading_job_id == run.id
    assert repaired.active_operation == "grading"
    assert repaired.active_job_id == run.id
    assert repaired.workflow_revision == workflow.workflow_revision


def test_grading_start_rejects_an_active_non_grading_operation():
    from backend.db import grading_repository, workflow_repository
    from backend.domain.errors import InvalidTransition
    from backend.services import task_facade

    owner_id = "grading-busy-owner"
    assignment, _question, workflow = _seed_figma_grading_task(owner_id)
    operation, _created = workflow_repository.create_operation(
        assignment_id=assignment.id,
        owner_id=owner_id,
        operation_type="submission_recognition",
        input_hash="active-recognition",
    )
    workflow_repository.update_workflow(
        assignment.id,
        owner_id=owner_id,
        bump_revision=False,
        active_operation=operation.operation_type,
        active_job_id=operation.id,
    )

    with pytest.raises(InvalidTransition) as busy:
        task_facade.start_task_grading(
            task_id=assignment.id,
            owner_id=owner_id,
            expected_workflow_revision=workflow.workflow_revision,
        )

    assert busy.value.code == "workflow_busy"
    assert grading_repository.list_runs_for_assignment(
        assignment.id, actor_id=owner_id,
    ) == []
