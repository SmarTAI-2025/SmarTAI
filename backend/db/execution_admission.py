"""One active teacher workload across recognition/preparation and grading."""
import hashlib
import time

from sqlalchemy import exists, func, select, text, update
from sqlalchemy.orm import aliased

from backend.domain.errors import LeaseLost

WORKLOAD_TYPES = ("problem_extraction", "submission_recognition", "material_import",
                  "ai_completion", "question_preparation")


def owner_running_predicate(owner_id, *, exclude_id: str = ""):
    from backend.db.workflow_repository import WorkflowOperationRecord
    from backend.db.grading_repository import GradingRunRecord
    Op, Run = aliased(WorkflowOperationRecord), aliased(GradingRunRecord)
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
