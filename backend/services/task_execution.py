"""Teacher-owned, atomic stop of exactly the run shown in the progress page."""
import time

from sqlalchemy import select

from backend.db import workflow_repository as workflows
from backend.db.grading_repository import GradingRunRecord
from backend.db.session import session_scope
from backend.domain.errors import NotFound, VersionConflict


def continue_auxiliary_run(*, task_id: str, owner_id: str, job_id: str, expected_revision: int) -> dict:
    """Republish a stopped material/AI job with its saved inputs and a new queue time."""
    with session_scope() as session:
        row = session.scalar(select(workflows.WorkflowOperationRecord).where(
            workflows.WorkflowOperationRecord.id == job_id,
            workflows.WorkflowOperationRecord.assignment_id == task_id,
            workflows.WorkflowOperationRecord.owner_id == owner_id,
            workflows.WorkflowOperationRecord.operation_type.in_(("material_import", "ai_completion")),
        ).with_for_update())
        if row is None:
            raise NotFound("task_run")
        workflow = session.scalar(select(workflows.AssignmentWorkflowRecord).where(
            workflows.AssignmentWorkflowRecord.assignment_id == task_id,
            workflows.AssignmentWorkflowRecord.owner_id == owner_id,
        ).with_for_update())
        workflows._lock_live_assignment(session, assignment_id=task_id, owner_id=owner_id)
        if workflow and workflow.active_job_id == job_id and row.status in {"pending", "running"}:
            return {"status": "already_running", "job_id": job_id}
        if (workflow is None or workflow.active_job_id or workflow.workflow_revision != expected_revision
                or workflow.last_failed_job_id != job_id or row.error_code != "operation_cancelled"
                or (row.payload or {}).get("stopped_workflow_revision") != workflow.workflow_revision):
            raise VersionConflict("The stopped run changed.", code="workflow_revision_conflict")
        row.payload = {**(row.payload or {}), "base_workflow_revision": workflow.workflow_revision,
                       "resume_saved_results": True}
        row.status, row.error_code, row.completed_at = "pending", None, None
        row.attempt += 1
        row.updated_at = time.time()  # list_claimable_operations uses this new submission time.
        row.progress = {**(row.progress or {}), "phase": "init", "model_waits": [], "error_detail": None}
        row.terminal_summary = None
        workflow.active_job_id, workflow.active_operation = job_id, row.operation_type
        workflow.presentation_status, workflow.error_code = "problems_ready", None
        workflow.last_failed_job_id = None
        workflow.workflow_revision += 1
        workflow.updated_at = row.updated_at
        return {"status": "started", "job_id": job_id}


def stop_task_run(*, task_id: str, owner_id: str, job_id: str, expected_revision: int) -> dict:
    now = time.time()
    with session_scope() as session:
        # Match existing writer lock order: operation/run -> workflow -> task.
        run = session.scalar(select(GradingRunRecord).where(
            GradingRunRecord.id == job_id, GradingRunRecord.assignment_id == task_id,
            GradingRunRecord.teacher_id == owner_id,
        ).with_for_update())
        operation = None if run is not None else session.scalar(select(workflows.WorkflowOperationRecord).where(
            workflows.WorkflowOperationRecord.id == job_id,
            workflows.WorkflowOperationRecord.assignment_id == task_id,
            workflows.WorkflowOperationRecord.owner_id == owner_id,
        ).with_for_update())
        row = run if run is not None else operation
        if row is None:
            raise NotFound("task_run")
        workflow = session.scalar(select(workflows.AssignmentWorkflowRecord).where(
            workflows.AssignmentWorkflowRecord.assignment_id == task_id,
            workflows.AssignmentWorkflowRecord.owner_id == owner_id,
        ).with_for_update())
        if workflow is None:
            raise NotFound("workflow")
        workflows._lock_live_assignment(session, assignment_id=task_id, owner_id=owner_id)
        code = row.error_message if run is not None else row.error_code
        if row.status not in {"pending", "queued", "running"}:
            return {"status": "stopped" if code == "operation_cancelled" else "already_finished", "job_id": job_id}
        if workflow.active_job_id != job_id or workflow.workflow_revision != expected_revision:
            raise VersionConflict("The active run changed.", code="workflow_revision_conflict")
        if run is not None:
            run.status = "cancelled"
            run.error_message = "operation_cancelled"
            run.lease_owner = None
            run.lease_expiry = None
        else:
            operation.status = "error"
            operation.error_code = "operation_cancelled"
            operation.lease_owner = operation.lease_token = None
            operation.lease_expires_at = operation.lease_heartbeat_at = None
            operation.updated_at = now
            operation.terminal_summary = {"error_code": "operation_cancelled"}
            operation.progress = {**(operation.progress or {}), "phase": "error",
                                  "error_detail": "operation_cancelled", "model_waits": []}
        row.completed_at = now
        workflow.active_job_id = workflow.active_operation = None
        workflow.presentation_status = "error"
        workflow.error_code = "operation_cancelled"
        workflow.last_failed_job_id = job_id
        workflow.workflow_revision += 1
        workflow.updated_at = now
        if operation is not None and operation.operation_type in {"material_import", "ai_completion"}:
            operation.payload = {**(operation.payload or {}), "stopped_workflow_revision": workflow.workflow_revision}
        return {"status": "stopped", "job_id": job_id}
