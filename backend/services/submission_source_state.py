"""Small parent checkpoints referencing immutable per-source diagnostic state."""
import hashlib
import json

from sqlalchemy import select

from backend.db import source_outcome_repository, workflow_repository
from backend.db.session import session_scope


OPERATION_TYPE = "submission_source_state_v2"


def save_source_state(*, owner_id, task_id, operation_id, attempt, lease_token, state):
    source_outcome_repository.assert_source_write_fence(owner_id=owner_id, assignment_id=task_id,
        operation_id=operation_id, expected_attempt=attempt, expected_lease_token=lease_token)
    payload = {"parent_operation_id": operation_id, "parent_attempt": attempt, **state}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
        separators=(",", ":")).encode()).hexdigest()
    row, _ = workflow_repository.create_operation(owner_id=owner_id, assignment_id=task_id,
        operation_type=OPERATION_TYPE, input_hash=digest, payload=payload,
        initial_status="preparing", retry_existing=False)
    if row.status != "done":
        workflow_repository.save_operation_checkpoint(row.id, owner_id=owner_id,
            expected_attempt=row.attempt, expected_checkpoint_revision=row.checkpoint_revision,
            stage="source_state_saved", checkpoint={}, artifact_refs=[],
            terminal_status="done", terminal_summary={"stored_file_id": state["stored_file_id"]})
    return row.id


def load_source_states(operation):
    """Read all file states in one owner/task-scoped DB query, never S3 polls."""
    refs = (operation.checkpoint or {}).get("source_state_refs", {})
    if not refs:
        return {}
    manual = operation.operation_type == "submission_manual_review"
    parent_id = (operation.payload or {}).get("source_origin_id") if manual else operation.id
    attempt = ((operation.payload or {}).get("retry_from") or {}).get("attempt") if manual else operation.attempt
    with session_scope() as session:
        rows = session.scalars(select(workflow_repository.WorkflowOperationRecord).where(
            workflow_repository.WorkflowOperationRecord.id.in_(list(refs.values())),
            workflow_repository.WorkflowOperationRecord.owner_id == operation.owner_id,
            workflow_repository.WorkflowOperationRecord.assignment_id == operation.assignment_id,
            workflow_repository.WorkflowOperationRecord.operation_type == OPERATION_TYPE,
            workflow_repository.WorkflowOperationRecord.status == "done",
        )).all()
        return {row.payload["stored_file_id"]: dict(row.payload) for row in rows
            if row.payload.get("parent_operation_id") == parent_id
            and row.payload.get("parent_attempt") == attempt
            and refs.get(row.payload.get("stored_file_id")) == row.id}
