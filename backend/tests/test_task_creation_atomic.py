from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import func, select


def _seed_teacher(owner_id: str) -> None:
    from backend.db.models import UserRecord
    from backend.db.session import session_scope

    with session_scope() as session:
        session.add(UserRecord(
            id=owner_id,
            username=owner_id,
            password_hash="hash",
            role="teacher",
            is_active=True,
        ))


def _create(owner_id: str, *, key: str, request_hash: str):
    from backend.services.task_creation import create_task_bundle

    return create_task_bundle(
        owner_id=owner_id,
        name="Atomic task",
        semester_id="semester-1",
        course_id=None,
        tag_ids=[],
        idempotency_key=key,
        request_hash=request_hash,
        system_course_code="__smartai_tasks__",
        system_course_name="SmarTAI Tasks",
    )


def test_task_creation_replay_is_one_atomic_bundle():
    from backend.db.models import AssignmentRecord
    from backend.db.session import session_scope
    from backend.db.workflow_repository import (
        AssignmentWorkflowRecord,
        TaskCreateIdempotencyRecord,
    )

    owner_id = "atomic-create-owner"
    _seed_teacher(owner_id)
    first_id, first_created = _create(
        owner_id, key="create-key", request_hash="same-hash",
    )
    replay_id, replay_created = _create(
        owner_id, key="create-key", request_hash="same-hash",
    )

    assert (replay_id, replay_created) == (first_id, False)
    assert first_created is True
    with session_scope() as session:
        assert session.scalar(select(func.count()).select_from(
            AssignmentRecord
        ).where(AssignmentRecord.teacher_id == owner_id)) == 1
        assert session.scalar(select(func.count()).select_from(
            AssignmentWorkflowRecord
        ).where(AssignmentWorkflowRecord.owner_id == owner_id)) == 1
        assert session.scalar(select(func.count()).select_from(
            TaskCreateIdempotencyRecord
        ).where(TaskCreateIdempotencyRecord.owner_id == owner_id)) == 1


def test_task_facade_uses_atomic_creation_bundle():
    from backend.db.models import AssignmentRecord
    from backend.db.session import session_scope
    from backend.services import task_facade

    owner_id = "facade-atomic-create-owner"
    _seed_teacher(owner_id)
    request = dict(
        owner_id=owner_id,
        name="Facade atomic task",
        semester_id=None,
        course_id=None,
        idempotency_key="facade-shared-key",
        tag_ids=[],
    )
    first = task_facade.create_task(**request)
    replay = task_facade.create_task(**request)

    assert replay["task_id"] == first["task_id"]
    with session_scope() as session:
        assert session.scalar(select(func.count()).select_from(
            AssignmentRecord
        ).where(AssignmentRecord.teacher_id == owner_id)) == 1


def test_concurrent_same_key_creates_one_assignment():
    from backend.db.models import AssignmentRecord
    from backend.db.session import session_scope

    owner_id = "concurrent-create-owner"
    _seed_teacher(owner_id)
    barrier = Barrier(2)

    def invoke():
        barrier.wait(timeout=5)
        return _create(owner_id, key="shared-key", request_hash="shared-hash")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = [future.result() for future in (
            pool.submit(invoke), pool.submit(invoke)
        )]

    assert first[0] == second[0]
    assert sorted([first[1], second[1]]) == [False, True]
    with session_scope() as session:
        assert session.scalar(select(func.count()).select_from(
            AssignmentRecord
        ).where(AssignmentRecord.teacher_id == owner_id)) == 1


def test_reused_key_with_different_payload_is_rejected_without_orphan():
    from backend.db.models import AssignmentRecord
    from backend.db.session import session_scope
    from backend.domain.errors import VersionConflict

    owner_id = "conflicting-create-owner"
    _seed_teacher(owner_id)
    _create(owner_id, key="reused-key", request_hash="first-hash")

    with pytest.raises(VersionConflict):
        _create(owner_id, key="reused-key", request_hash="different-hash")
    with session_scope() as session:
        assert session.scalar(select(func.count()).select_from(
            AssignmentRecord
        ).where(AssignmentRecord.teacher_id == owner_id)) == 1
