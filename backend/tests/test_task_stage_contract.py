"""Task stages come from persisted workflow facts, identically for both lists."""
from types import SimpleNamespace
from typing import get_args

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import tasks
from backend.auth import create_token
from backend.db import grading_repository, workflow_repository
from backend.models import TaskStatus
from backend.services import task_facade
from backend.services.task_execution import stop_task_run
from backend.tests.test_workflow_worker import _seed_operation
from backend.tests.test_task_execution_control import seed_grade
from backend.tests.test_grading_source_readiness import (
    _seed_legacy_structured_case,
    _seed_source_case,
)
from backend.tests.test_workflow_facade_integrity import _seed_assignment_and_run


def _client(owner):
    app = FastAPI()
    app.include_router(tasks.router)
    return TestClient(app, headers={"Authorization": f"Bearer {create_token(owner, 'teacher')}"})


@pytest.mark.parametrize("needs_review", [False, True])
def test_only_formally_saved_setup_changes_stage_in_both_lists(needs_review):
    seeded = _seed_legacy_structured_case(recognition_needs_review=needs_review)
    owner, task_id = seeded["owner_id"], seeded["assignment_id"]
    saved_setup = seeded["workflow"].grading_setup
    workflow = workflow_repository.update_workflow(
        task_id, owner_id=owner, grading_setup=None, grading_setup_fingerprint=None,
    )
    client = _client(owner)

    def assert_stage(expected):
        detail = client.get(f"/tasks/{task_id}").json()
        dashboard = client.get("/tasks/").json()[task_id]
        history = client.get("/tasks/?page=1&page_size=25").json()["items"][0]
        state = client.get(f"/tasks/{task_id}/state").json()
        assert {item["status"] for item in (detail, dashboard, history, state)} == {expected}
        assert client.get("/tasks/", params={"page": 1, "statuses": expected}).json()["total"] == 1

    assert_stage("submissions_ready")
    suggested = client.get(f"/tasks/{task_id}/grading-setup").json()
    assert suggested["suggested_setup"] is not None
    assert suggested["configured"] is False
    assert_stage("submissions_ready")
    response = client.put(f"/tasks/{task_id}/grading-setup", json={
        "expected_workflow_revision": workflow.workflow_revision,
        "grading_setup": saved_setup,
    })
    assert response.status_code == 200, response.text
    assert response.json()["task_status"] == "grading_setup"
    assert "invalid_state" not in response.json()["readiness"]["blocking_issues"]
    assert_stage("grading_setup")
    task_facade.start_task_grading(
        task_id=task_id, owner_id=owner,
        expected_workflow_revision=response.json()["workflow_revision"],
    )
    assert_stage("grading")


@pytest.mark.parametrize("source_status", ["parse_failed", "pending"])
def test_saved_setup_does_not_hide_incomplete_recognition(source_status):
    seeded = _seed_source_case(source_status)
    task = task_facade.get_task(task_id=seeded["assignment_id"], owner_id=seeded["owner_id"])
    assert task["status"] == "error"


@pytest.mark.parametrize("legacy", ["review_confirmed", "generating_analysis"])
def test_historical_stage_is_projected_from_real_run_instead_of_reappearing(legacy):
    owner, task_id = "stage-owner", "stage-task"
    run = _seed_assignment_and_run(owner_id=owner, assignment_id=task_id)
    grading_repository.claim_lease(run_id=run.id, worker_id="stage-worker", lease_seconds=60)
    grading_repository.mark_completed(run_id=run.id, worker_id="stage-worker", completed=0, failed=0)
    workflow_repository.ensure_workflow(assignment_id=task_id, owner_id=owner)
    workflow_repository.update_workflow(task_id, owner_id=owner, presentation_status=legacy, grading_job_id=None)
    client = _client(owner)
    assert client.get(f"/tasks/{task_id}").json()["status"] == "graded"
    assert client.get("/tasks/").json()[task_id]["status"] == "graded"
    assert client.get("/tasks/?page=1").json()["items"][0]["status"] == "graded"
    assert legacy not in get_args(TaskStatus)


@pytest.mark.parametrize("status, released, expected", [
    ("queued", None, "grading"), ("running", None, "grading"),
    ("completed", None, "graded"), ("partial_failed", None, "graded"),
    ("failed", None, "error"), ("completed", 123, "finalized"),
])
def test_actual_run_takes_precedence_over_saved_setup(status, released, expected):
    workflow = SimpleNamespace(presentation_status="submissions_ready", grading_setup={})
    run = SimpleNamespace(status=status, released_at=released)
    assert task_facade._presentation_status(workflow, [object()], [object()], run) == expected


@pytest.mark.parametrize("status", ["extracting_problems", "parsing_submissions", "error"])
def test_active_recognition_or_error_takes_precedence_over_saved_setup(status):
    workflow = SimpleNamespace(presentation_status=status, grading_setup={})
    assert task_facade._presentation_status(workflow, [object()], [object()], None) == status


@pytest.mark.parametrize("query, expected", [
    ("校对题目", "problems_ready"), ("校对作答", "submissions_ready"),
    ("批改配置", "grading_setup"), ("Grading setup", "grading_setup"),
    ("题目识别暂停", "problems_paused"), ("Question recognition paused", "problems_paused"),
    ("作答识别暂停", "submissions_paused"), ("Submission recognition paused", "submissions_paused"),
    ("批改暂停", "grading_paused"), ("Grading paused", "grading_paused"),
])
def test_history_interpreter_uses_current_stage_names(query, expected):
    from backend.agents.history_query_agent import deterministic_history_query
    result = deterministic_history_query(query, semesters=[], courses=[], tags=[])
    assert result["filters"].statuses == [expected]


@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize("kind,pointer,active_status,paused_status", [
    ("question_preparation", "extract_job_id", "extracting_problems", "problems_paused"),
    ("submission_recognition", "parse_job_id", "parsing_submissions", "submissions_paused"),
    ("grading", "grading_job_id", "grading", "grading_paused"),
])
def test_manual_stop_projects_pause_consistently_without_public_error(kind, pointer, active_status, paused_status, running):
    owner, task_id, operation = _seed_operation(kind if kind != "grading" else "question_preparation")
    job_id = seed_grade(owner, task_id) if kind == "grading" else operation.id
    if running:
        if kind == "grading":
            grading_repository.claim_lease(job_id, worker_id="paused-test", lease_seconds=60)
        else:
            workflow_repository.claim_operation(job_id, owner_id=owner, worker_id="paused-test", lease_seconds=60)
    if kind != "grading":
        operation = workflow_repository.get_operation(job_id, owner_id=owner)
        workflow_repository.update_operation(job_id, owner_id=owner,
            expected_attempt=operation.attempt, expected_lease_token=operation.lease_token,
            progress={"job_id": job_id, "completed_units": 1, "total_questions": 2})
    workflow = workflow_repository.update_workflow(task_id, owner_id=owner,
        active_job_id=job_id, active_operation=kind, presentation_status=active_status,
        **{pointer: job_id})
    stop_task_run(task_id=task_id, owner_id=owner, job_id=job_id, expected_revision=workflow.workflow_revision)
    # Existing stored cancellation records need no migration; their public stage changes.
    stored = workflow_repository.get_workflow(task_id, owner_id=owner)
    assert stored.presentation_status == "error" and stored.error_code == "operation_cancelled"
    client = _client(owner)
    detail = client.get(f"/tasks/{task_id}").json()
    dashboard = client.get("/tasks/").json()[task_id]
    history = client.get("/tasks/?page=1").json()["items"][0]
    state = client.get(f"/tasks/{task_id}/state").json()
    for item in (detail, dashboard, history, state):
        assert item["status"] == paused_status
        assert item["error"] is None and item["needs_attention"] is False
        assert item["last_failed_job_id"] == job_id
    assert state["active_job_id"] is None and state["queue_reason"] is None
    assert state["progress"]["phase"] == "paused"
    assert state["progress"]["error_detail"] is None
    if kind != "grading":
        assert state["progress"]["completed_units"] == 1
    assert client.get("/tasks/", params={"page": 1, "statuses": paused_status}).json()["total"] == 1
    # A real provider failure still uses the error recovery path.
    workflow_repository.update_workflow(task_id, owner_id=owner, error_code="provider_unreachable")
    assert client.get(f"/tasks/{task_id}").json()["status"] == "error"
