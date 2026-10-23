from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from backend.api import task_preparation
from backend.agents.ingest_agent import (
    AICompletionCandidateOutput,
    MaterialImportCandidateOutput,
)
from backend.db import assignment_repository, file_repository, workflow_repository
from backend.db.models import AssignmentQuestionRecord
from backend.db.session import session_scope
from backend.domain.errors import LeaseLost
from backend.services import task_facade
from backend.services.workflow_worker import LeasedOperation
from backend.storage import get_storage
from backend.services.source_files import persist_problem_source
from backend.tests.test_task_background_workflows import _BackgroundTasks, _Registry, _seed_task


@pytest.mark.asyncio
async def test_ai_completion_queues_for_durable_worker():
    owner_id, task_id = _seed_task(with_question=True, criterion="")
    background = _BackgroundTasks()
    response = await task_preparation.confirm_ai_completion(
        task_id=task_id,
        request=task_preparation.ConfirmAICompletionRequest(
            target_ids=["q1:criterion"], expected_workflow_revision=0,
        ),
        background_tasks=background,
        current=SimpleNamespace(id=owner_id), registry=_Registry(),
    )
    assert response["status"] == "started"
    assert background.calls == []
    assert workflow_repository.get_operation(
        response["job_id"], owner_id=owner_id
    ).status == "pending"


@pytest.mark.asyncio
async def test_ai_completion_is_not_claimable_before_workflow_activation(monkeypatch):
    owner_id, task_id = _seed_task(with_question=True, criterion="")
    original_activate = task_facade.activate_workflow_operation_atomic
    observed = {}

    def activate(**kwargs):
        try:
            workflow_repository.claim_operation(
                kwargs["operation_id"], owner_id=owner_id,
                worker_id="racing-worker", lease_seconds=60,
            )
        except LeaseLost as exc:
            observed["before"] = exc.code
        return original_activate(**kwargs)

    monkeypatch.setattr(task_facade, "activate_workflow_operation_atomic", activate)
    response = await task_preparation.confirm_ai_completion(
        task_id=task_id,
        request=task_preparation.ConfirmAICompletionRequest(
            target_ids=["q1:criterion"], expected_workflow_revision=0,
        ), background_tasks=_BackgroundTasks(),
        current=SimpleNamespace(id=owner_id), registry=_Registry(),
    )

    assert response["status"] == "started"
    assert observed["before"] == "operation_not_claimable"
    assert workflow_repository.get_operation(
        response["job_id"], owner_id=owner_id
    ).status == "pending"


def test_ai_completion_commit_is_lease_fenced():
    owner_id, task_id = _seed_task(with_question=True)
    job, _ = workflow_repository.create_operation(
        assignment_id=task_id, owner_id=owner_id,
        operation_type="ai_completion", input_hash="a" * 64,
        payload={}, expires_at=10**12,
    )
    task_facade.claim_workflow_operation_atomic(
        task_id=task_id, owner_id=owner_id, operation_id=job.id,
        expected_operation_attempt=job.attempt, expected_workflow_revision=0,
        workflow_changes={"active_operation": "ai_completion", "active_job_id": job.id},
    )
    old = workflow_repository.claim_operation(
        job.id, owner_id=owner_id, worker_id="old", lease_seconds=60,
    )
    workflow_repository.release_operation(
        old.id, owner_id=owner_id, worker_id="old", lease_token=old.lease_token,
    )
    current = workflow_repository.claim_operation(
        job.id, owner_id=owner_id, worker_id="new", lease_seconds=60,
    )

    with pytest.raises(LeaseLost):
        task_facade.apply_question_patches_atomic(
            task_id=task_id, owner_id=owner_id,
            expected_workflow_revision=1, patches=[], operation_id=job.id,
            expected_operation_attempt=job.attempt,
            expected_lease_token=old.lease_token,
            required_operation_status="running", final_operation_status="done",
            operation_payload={},
        )
    assert workflow_repository.get_operation(
        job.id, owner_id=owner_id
    ).lease_token == current.lease_token


def _claim_auxiliary_operation(owner_id, operation_id, *, worker_id="fresh-worker"):
    claimed = workflow_repository.claim_operation(
        operation_id, owner_id=owner_id, worker_id=worker_id, lease_seconds=60,
    )
    return LeasedOperation(claimed, worker_id=worker_id, lease_seconds=60)


@pytest.mark.asyncio
async def test_fresh_material_worker_reads_artifact_and_keeps_text_out_of_payload(monkeypatch):
    owner_id, task_id = _seed_task(with_question=True)
    original, _ = persist_problem_source(storage=get_storage(), owner_id=owner_id, task_id=task_id,
        original_name="rubric.txt", content=b"rubric text", content_type="text/plain")
    text_artifact = file_repository.save_file(
        storage=get_storage(), owner_id=owner_id, kind="material_import_text",
        original_name="material-source.txt", content=b"rubric text",
        content_type="text/plain", assignment_id=task_id,
    )
    source, _ = workflow_repository.create_operation(
        assignment_id=task_id, owner_id=owner_id,
        operation_type="material_source", input_hash="b" * 64,
        payload={
            "text_artifact_id": text_artifact.id, "filename": "rubric.txt",
            "original_stored_file_id": original.id, "original_sha256": original.sha256,
            "targets": ["criterion"], "structure_mode": "organized",
            "extraction_hint": "", "base_workflow_revision": 0,
        }, expires_at=10**12,
    )
    job, _ = workflow_repository.create_operation(
        assignment_id=task_id, owner_id=owner_id,
        operation_type="material_import", input_hash="c" * 64,
        payload={"source_token": source.id, "base_workflow_revision": 0},
        expires_at=10**12,
    )
    task_facade.claim_workflow_operation_atomic(
        task_id=task_id, owner_id=owner_id, operation_id=job.id,
        expected_operation_attempt=job.attempt, expected_workflow_revision=0,
        workflow_changes={"active_operation": "material_import", "active_job_id": job.id},
    )

    async def fake_candidates(*, text, **kwargs):
        assert text == "rubric text"
        return [MaterialImportCandidateOutput(
            q_id="q1", target="criterion", text_value="100% correct",
            confidence=1.0, match_status="exact",
        )]

    monkeypatch.setattr(task_preparation, "parse_material_import_to_candidates", fake_candidates)
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _owner: _Registry())
    await task_preparation.run_durable_material_import(
        _claim_auxiliary_operation(owner_id, job.id)
    )

    completed = workflow_repository.get_operation(job.id, owner_id=owner_id)
    assert completed.status == "ready"
    assert "text" not in completed.payload
    assert completed.payload["text_artifact_id"] == text_artifact.id
    assert completed.payload["candidates"][0]["text_value"] == "100% correct"


@pytest.mark.asyncio
@pytest.mark.parametrize("unresolved_stem", [False, True])
async def test_fresh_ai_worker_applies_generated_patch_once(monkeypatch, unresolved_stem):
    owner_id, task_id = _seed_task(with_question=True, criterion="")
    question = assignment_repository.list_questions(task_id, teacher_id=owner_id)[0]
    with session_scope() as session:
        row = session.get(AssignmentQuestionRecord, question.id)
        row.source = {"presentation": {"preparation_issues": [
            {"issue_id": "rubric-failed", "q_id": "q1", "field": "rubric", "code": "generation_failed",
             "severity": "blocking", "status": "open", "details": {"target": "criterion"} if unresolved_stem else {}},
            {"issue_id": "manual", "q_id": "q1", "field": "source", "code": "manual_completion_required",
             "severity": "warning", "status": "open", "details": {"required_fields": ["criterion", *(["stem"] if unresolved_stem else [])]}},
            *([{"issue_id": "stem-page", "q_id": "q1", "field": "stem", "code": "recognition_partial",
                "severity": "blocking", "status": "open", "details": {"coverage": {"failed_pages": [2]}}}]
              if unresolved_stem else []),
        ]}}
    assert "question_review_blocked" in task_facade.grading_readiness(task_id=task_id, owner_id=owner_id)["blocking_issues"]
    response = await task_preparation.confirm_ai_completion(
        task_id=task_id,
        request=task_preparation.ConfirmAICompletionRequest(
            target_ids=["q1:criterion"], expected_workflow_revision=0,
        ), background_tasks=_BackgroundTasks(),
        current=SimpleNamespace(id=owner_id), registry=_Registry(),
    )

    async def fake_generate(**kwargs):
        return [AICompletionCandidateOutput(
            target_id="q1:criterion", q_id="q1", target="criterion",
            text_value="100% correct",
        )]

    monkeypatch.setattr(task_preparation, "generate_missing_question_materials", fake_generate)
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _owner: _Registry())
    await task_preparation.run_durable_ai_completion(
        _claim_auxiliary_operation(owner_id, response["job_id"])
    )

    completed = workflow_repository.get_operation(response["job_id"], owner_id=owner_id)
    question = task_facade.assignment_repository.list_questions(
        task_id, teacher_id=owner_id
    )[0]
    assert completed.status == "done"
    assert question.criterion == "100% correct"
    problem = task_facade.get_task(task_id=task_id, owner_id=owner_id, full=True)["problem_data"]["q1"]
    issues = {issue["issue_id"]: issue for issue in problem["preparation_issues"]}
    assert issues["rubric-failed"]["status"] == "resolved"
    assert issues["manual"]["details"]["required_fields"] == (["stem"] if unresolved_stem else [])
    blockers = task_facade.grading_readiness(task_id=task_id, owner_id=owner_id)["blocking_issues"]
    assert ("question_review_blocked" in blockers) is unresolved_stem
    assert ("question_manual_completion_required" in blockers) is unresolved_stem
    if unresolved_stem:
        assert issues["stem-page"]["details"]["coverage"]["failed_pages"] == [2]
        assert issues["stem-page"]["status"] == "open"
    else:
        task_facade.update_problem(task_id=task_id, owner_id=owner_id, q_id="q1", patch={"review_status": "confirmed"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation_type", "artifact_kind", "artifact_suffix", "artifact_body"),
    [
        (
            "material_import", "material_import_result", "material-candidates",
            [{
                "q_id": "q1", "target": "criterion", "text_value": "100% durable",
                "test_cases": None, "confidence": 1.0, "match_status": "exact",
                "source_excerpt": "", "source_location": "", "reason": "",
            }],
        ),
        (
            "ai_completion", "ai_completion_result", "ai-candidates",
            [{
                "target_id": "q1:criterion", "q_id": "q1",
                "target": "criterion", "text_value": "100% durable",
                "test_cases": None,
            }],
        ),
    ],
)
async def test_auxiliary_worker_discovers_result_saved_before_checkpoint(
    monkeypatch, operation_type, artifact_kind, artifact_suffix, artifact_body,
):
    owner_id, task_id = _seed_task(with_question=True, criterion="")
    if operation_type == "material_import":
        original, _ = persist_problem_source(storage=get_storage(), owner_id=owner_id, task_id=task_id,
            original_name="rubric.txt", content=b"rubric text", content_type="text/plain")
        text_artifact = file_repository.save_file(
            storage=get_storage(), owner_id=owner_id, kind="material_import_text",
            original_name="material-source.txt", content=b"rubric text",
            content_type="text/plain", assignment_id=task_id,
        )
        source, _ = workflow_repository.create_operation(
            assignment_id=task_id, owner_id=owner_id,
            operation_type="material_source", input_hash="f" * 64,
            payload={
                "text_artifact_id": text_artifact.id, "filename": "rubric.txt",
                "original_stored_file_id": original.id, "original_sha256": original.sha256,
                "targets": ["criterion"], "structure_mode": "organized",
                "extraction_hint": "", "base_workflow_revision": 0,
            }, expires_at=10**12,
        )
        job_payload = {"source_token": source.id, "base_workflow_revision": 0}
    else:
        job_payload = {
            "target_ids": ["q1:criterion"], "test_case_count": 5,
            "base_workflow_revision": 0,
        }
    job, _ = workflow_repository.create_operation(
        assignment_id=task_id, owner_id=owner_id,
        operation_type=operation_type,
        input_hash=("1" if operation_type == "material_import" else "2") * 64,
        payload=job_payload, expires_at=10**12,
    )
    task_facade.claim_workflow_operation_atomic(
        task_id=task_id, owner_id=owner_id, operation_id=job.id,
        expected_operation_attempt=job.attempt, expected_workflow_revision=0,
        workflow_changes={"active_operation": operation_type, "active_job_id": job.id},
    )
    artifact = file_repository.save_file(
        storage=get_storage(), owner_id=owner_id, kind=artifact_kind,
        original_name=(
            f"{job.id}-attempt-{job.attempt}-{artifact_suffix}.json"
        ),
        content=json.dumps(artifact_body).encode(),
        content_type="application/json", assignment_id=task_id,
    )

    async def unexpected(**kwargs):
        raise AssertionError("completed provider stage was repeated")

    monkeypatch.setattr(task_preparation, "parse_material_import_to_candidates", unexpected)
    monkeypatch.setattr(task_preparation, "generate_missing_question_materials", unexpected)
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _owner: _Registry())
    context = _claim_auxiliary_operation(owner_id, job.id)
    if operation_type == "material_import":
        await task_preparation.run_durable_material_import(context)
    else:
        await task_preparation.run_durable_ai_completion(context)

    completed = workflow_repository.get_operation(job.id, owner_id=owner_id)
    assert completed.status == ("ready" if operation_type == "material_import" else "done")
    assert completed.checkpoint["result_artifact_id"] == artifact.id
    assert completed.artifact_refs == [artifact.id]


@pytest.mark.parametrize("operation_type", ["material_import", "ai_completion"])
def test_auxiliary_stale_lease_cannot_finish_or_fail(operation_type):
    owner_id, task_id = _seed_task(with_question=True)
    job, _ = workflow_repository.create_operation(
        assignment_id=task_id, owner_id=owner_id,
        operation_type=operation_type, input_hash=("d" if operation_type == "material_import" else "e") * 64,
        payload={}, expires_at=10**12,
    )
    task_facade.claim_workflow_operation_atomic(
        task_id=task_id, owner_id=owner_id, operation_id=job.id,
        expected_operation_attempt=job.attempt, expected_workflow_revision=0,
        workflow_changes={"active_operation": operation_type, "active_job_id": job.id},
    )
    old = workflow_repository.claim_operation(
        job.id, owner_id=owner_id, worker_id="old", lease_seconds=60,
    )
    workflow_repository.release_operation(
        old.id, owner_id=owner_id, worker_id="old", lease_token=old.lease_token,
    )
    current = workflow_repository.claim_operation(
        job.id, owner_id=owner_id, worker_id="current", lease_seconds=60,
    )

    with pytest.raises(LeaseLost):
        if operation_type == "material_import":
            task_facade.complete_planning_operation_atomic(
                task_id=task_id, owner_id=owner_id, expected_workflow_revision=1,
                operation_id=job.id, expected_operation_attempt=job.attempt,
                expected_lease_token=old.lease_token, payload={}, progress={},
            )
        else:
            task_facade.apply_question_patches_atomic(
                task_id=task_id, owner_id=owner_id, expected_workflow_revision=1,
                patches=[], operation_id=job.id,
                expected_operation_attempt=job.attempt,
                expected_lease_token=old.lease_token,
                required_operation_status="running", final_operation_status="done",
                operation_payload={},
            )
    with pytest.raises(LeaseLost):
        task_facade._fail_operation(
            task_id, owner_id, job.id, job.attempt, f"{operation_type}_failed",
            expected_lease_token=old.lease_token,
        )
    assert workflow_repository.get_operation(
        job.id, owner_id=owner_id
    ).lease_token == current.lease_token
