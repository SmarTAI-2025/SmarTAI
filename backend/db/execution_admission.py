"""One active teacher workload across recognition/preparation and grading."""
import hashlib
import time

from sqlalchemy import exists, func, select, text, union_all, update
from sqlalchemy.orm import aliased

from backend.domain.errors import LeaseLost

WORKLOAD_TYPES = ("problem_extraction", "submission_recognition", "material_import",
                  "ai_completion", "question_preparation")


def owner_running_predicate(owner_id, *, exclude_id: str = "", now: float | None = None):
    from backend.db.workflow_repository import WorkflowOperationRecord
    from backend.db.grading_repository import GradingRunRecord
    Op, Run = aliased(WorkflowOperationRecord), aliased(GradingRunRecord)
    if now is None:
        now = time.time()
    return exists(select(Op.id).where(
        Op.owner_id == owner_id, Op.id != exclude_id,
        Op.operation_type.in_(WORKLOAD_TYPES), Op.status == "running",
        Op.lease_owner.is_not(None), Op.lease_expires_at > now,
    )).correlate_except(Op) | exists(select(Run.id).where(
        Run.teacher_id == owner_id, Run.id != exclude_id, Run.status == "running",
        Run.lease_owner.is_not(None), Run.lease_expiry > now,
    )).correlate_except(Run)


def owner_is_running(session, owner_id: str, *, exclude_id: str = "") -> bool:
    return bool(session.scalar(select(owner_running_predicate(owner_id, exclude_id=exclude_id))))


def _earlier_workload_is_waiting(session, workload_id: str, now: float) -> bool:
    from backend.db.models import AssignmentRecord as Task, GradingRunRecord as Run
    from backend.db.workflow_repository import AssignmentWorkflowRecord as Workflow, WorkflowOperationRecord as Op

    # Only published workflow jobs participate. Publication and explicit
    # retries refresh updated_at, while grading retries create a new run.
    operations = (
        select(Op.id, Op.owner_id, Op.updated_at.label("queued_at"))
        .join(Workflow, (Workflow.assignment_id == Op.assignment_id)
              & (Workflow.owner_id == Op.owner_id) & (Workflow.active_job_id == Op.id))
        .join(Task, (Task.id == Op.assignment_id) & (Task.teacher_id == Op.owner_id))
        .where(Task.deletion_requested_at.is_(None), Op.operation_type.in_(WORKLOAD_TYPES),
               Op.status.in_(("pending", "running")),
               Op.lease_owner.is_(None) | (Op.lease_expires_at < now))
    )
    # The standalone grading API can queue a run without a workflow pointer.
    grading = (
        select(Run.id, Run.teacher_id, Run.created_at)
        .join(Task, (Task.id == Run.assignment_id) & (Task.teacher_id == Run.teacher_id))
        .where(Task.deletion_requested_at.is_(None),
               Run.status.in_(("queued", "running")),
               ((Run.status == "queued") & Run.lease_owner.is_(None)) | (Run.lease_expiry < now))
    )
    queue = union_all(operations, grading).cte("queued_workloads")
    # Live lease renewals and non-queue claims keep their existing path.
    queued_at = select(queue.c.queued_at).where(queue.c.id == workload_id).scalar_subquery()
    return bool(session.scalar(select(exists().where(
        queue.c.queued_at < queued_at,
        ~owner_running_predicate(queue.c.owner_id, now=now),
    ))))


def admit_owner(session, owner_id: str, *, exclude_id: str) -> None:
    # Serialize the two independent worker claim paths across processes. No
    # lease is held by a browser session and no user/account state is changed.
    if session.bind.dialect.name == "postgresql":
        key = int.from_bytes(hashlib.sha256(b"smartai-workload-admission").digest()[:8],
                             "big", signed=True)
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
    else:
        from backend.db.models import UserRecord
        # SQLite serializes writes; do this before reading active leases.
        session.execute(update(UserRecord).where(UserRecord.id == owner_id).values(id=UserRecord.id))
    if owner_is_running(session, owner_id, exclude_id=exclude_id):
        raise LeaseLost("Another task for this user is running.", code="owner_task_running")
    from backend.config import settings
    from backend.db.workflow_repository import WorkflowOperationRecord as Op
    from backend.db.grading_repository import GradingRunRecord as Run
    now = time.time()
    count = session.scalar(select(func.count()).select_from(Op).where(
        Op.id != exclude_id, Op.operation_type.in_(WORKLOAD_TYPES), Op.status == "running",
        Op.lease_owner.is_not(None), Op.lease_expires_at > now,
    )) + session.scalar(select(func.count()).select_from(Run).where(
        Run.id != exclude_id, Run.status == "running", Run.lease_owner.is_not(None), Run.lease_expiry > now,
    ))
    if count >= max(1, settings.workload_max_in_flight):
        raise LeaseLost("The server's workload slots are occupied.", code="server_capacity_full")
    if _earlier_workload_is_waiting(session, exclude_id, now):
        raise LeaseLost("An earlier queued workload has priority.", code="server_capacity_full")
