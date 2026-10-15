from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import uuid

import pytest
from pydantic import ValidationError as SchemaError

from backend.api.tasks import UpdateTaskRequest
from backend.db import workflow_repository
from backend.db.models import UserRecord
from backend.db.session import session_scope
from backend.domain.errors import NotFound, ValidationError, VersionConflict
from backend.services import task_facade


def seed():
    owner = f"metadata-{uuid.uuid4().hex[:12]}"
    with session_scope() as session:
        session.add(UserRecord(id=owner, username=owner, password_hash="hash", role="teacher", is_active=True))
    task = task_facade.create_task(owner_id=owner, name="Original", semester_id="fall", course_id=None, tag_ids=[], idempotency_key=f"create-{owner}")
    return owner, task, {key: task[key] for key in ("name", "semester_id", "course_id", "tag_ids")}


def test_metadata_api_requires_a_baseline():
    with pytest.raises(SchemaError):
        UpdateTaskRequest(name="Unprotected")


def test_stale_metadata_is_rejected_without_overwriting_any_field():
    owner, task, baseline = seed()
    args = dict(owner_id=owner, task_id=task["task_id"], expected_metadata=baseline)
    task_facade.update_task(**args, name="New title")
    with pytest.raises(VersionConflict) as failure:
        task_facade.update_task(**args, name="Old page", semester_id="spring")
    assert failure.value.code == "task_metadata_conflict"
    current = task_facade.get_task(task_id=task["task_id"], owner_id=owner)
    assert (current["name"], current["semester_id"]) == ("New title", "fall")
    assert current["workflow_revision"] == task["workflow_revision"]


def test_unrelated_workflow_and_metadata_changes_do_not_block_other_fields():
    owner, task, baseline = seed()
    args = dict(owner_id=owner, task_id=task["task_id"], expected_metadata=baseline)
    task_facade.update_task(**args, name="New title")
    workflow_repository.update_workflow(task["task_id"], owner_id=owner, presentation_status="problems_ready")
    updated = task_facade.update_task(**args, semester_id="spring")
    assert (updated["name"], updated["semester_id"]) == ("New title", "spring")


def test_invalid_tags_roll_back_name_and_semester_and_enforce_owner():
    owner, task, baseline = seed()
    with pytest.raises(ValidationError):
        task_facade.update_task(owner_id=owner, task_id=task["task_id"], expected_metadata=baseline,
                                name="Must not persist", semester_id="spring", tag_ids=["missing"])
    current = task_facade.get_task(task_id=task["task_id"], owner_id=owner)
    assert all(current[key] == value for key, value in baseline.items())
    with pytest.raises(NotFound):
        task_facade.update_task(owner_id="someone-else", task_id=task["task_id"], expected_metadata=baseline, name="Forbidden")


def test_simultaneous_metadata_updates_have_exactly_one_winner():
    owner, task, baseline = seed()
    barrier = Barrier(2)

    def save(name):
        barrier.wait(timeout=5)
        try:
            task_facade.update_task(owner_id=owner, task_id=task["task_id"], expected_metadata=baseline, name=name)
            return "saved"
        except VersionConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(save, ["First", "Second"])) == ["conflict", "saved"]
