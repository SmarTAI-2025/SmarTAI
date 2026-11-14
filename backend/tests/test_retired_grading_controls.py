"""Old client parameters cannot change the frozen scoring policy."""
import pytest
from pydantic import ValidationError
from backend.models import TaskGradingSetup
from backend.db import workflow_repository
from backend.tests.test_task_stage_contract import _client
from backend.tests.test_grading_source_readiness import _seed_legacy_structured_case


@pytest.mark.parametrize("retired,value", [("allow_partial_credit", False), ("strictness", 100)])
def test_direct_setup_request_rejects_retired_controls_without_mutation(retired, value):
    seed = _seed_legacy_structured_case()
    owner, task = seed["owner_id"], seed["assignment_id"]
    before = workflow_repository.get_live_workflow(task, owner_id=owner)
    client = _client(owner)
    current = client.get(f"/tasks/{task}/grading-setup").json()["grading_setup"]
    assert retired not in current
    response = client.put(f"/tasks/{task}/grading-setup", json={
        "expected_workflow_revision": before.workflow_revision,
        "grading_setup": {**current, retired: value}})
    assert response.status_code == 422
    after = workflow_repository.get_live_workflow(task, owner_id=owner)
    assert after.workflow_revision == before.workflow_revision and after.grading_setup == before.grading_setup


def test_historical_config_can_be_read_but_not_reenabled_as_new_input():
    old = {"selected_provider_ids":["mock:a"], "primary_provider_id":"mock:a",
           "allow_partial_credit":False, "strictness":100}
    read = TaskGradingSetup.from_saved(old)
    assert not {"allow_partial_credit", "strictness"}.intersection(read.model_dump())
    assert old["allow_partial_credit"] is False
    with pytest.raises(ValidationError):
        TaskGradingSetup.model_validate(old)


@pytest.mark.parametrize("notes", ["Questions 2 and 3: 其他正确解法最多1分", "#2 and #3: 其他正确解法最多1分"])
def test_unresolved_note_scope_is_rejected_before_setup_save(notes):
    seed = _seed_legacy_structured_case()
    owner, task = seed["owner_id"], seed["assignment_id"]
    before = workflow_repository.get_live_workflow(task, owner_id=owner)
    client = _client(owner)
    current = client.get(f"/tasks/{task}/grading-setup").json()["grading_setup"]
    response = client.put(f"/tasks/{task}/grading-setup", json={
        "expected_workflow_revision": before.workflow_revision,
        "grading_setup": {**current, "teacher_notes": notes}})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_teacher_note_scope"
    after = workflow_repository.get_live_workflow(task, owner_id=owner)
    assert after.workflow_revision == before.workflow_revision and after.grading_setup == before.grading_setup
