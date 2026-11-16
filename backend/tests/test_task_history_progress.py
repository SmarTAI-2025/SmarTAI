import asyncio
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import tasks
from backend.domain.errors import NotFound
from backend.models import User
from backend.services import task_facade, task_history_progress


def _grading(**changes):
    return {"phase": "grading", "total_students": 4, "total_questions": 5,
            "completed_units": 4, "started_at": 100, **changes}


@pytest.mark.parametrize("status,progress,expected", [
    ("draft", None, (0, None)),
    ("graded", None, (100, 0)),
    ("grading_setup", None, (None, None)),
    ("finalized", None, (100, 0)),
    ("problems_ready", None, (None, None)),
    ("submissions_ready", None, (None, None)),
    ("error", _grading(phase="error"), (None, None)),
    ("grading", None, (None, None)),
    ("grading", _grading(), (20, None)),
    ("grading", _grading(completed_units=15), (75, None)),
    ("grading", _grading(completed_units=20), (99, None)),
    ("grading", _grading(completed_units=0), (0, None)),
    ("grading", _grading(total_questions=0), (None, None)),
    ("grading", _grading(started_at=None), (20, None)),
    ("grading", _grading(started_at=201), (20, None)),
    ("grading", _grading(phase="error"), (20, None)),
    ("grading", _grading(error_detail="grading_failed"), (20, None)),
    ("parsing_submissions", _grading(phase="parsing", completed_units=2), (50, None)),
    ("extracting_problems", {"total_steps": 8, "completed_steps": 1}, (13, None)),
    ("extracting_problems", {"phase": "done"}, (100, None)),
])
def test_progress_uses_real_units_and_eta_requires_a_measured_grading_rate(status, progress, expected):
    fields = task_history_progress.progress_fields(status, progress)
    assert (fields["progress_percent"], fields["eta_seconds"]) == expected


@pytest.mark.parametrize("eta,expected", [(123, 123), (None, None), (-1, None), (float("nan"), None)])
def test_eta_uses_whole_grading_pipeline_without_initial_only_or_elapsed_fallback(eta, expected):
    fields = task_history_progress.progress_fields(
        "grading", _grading(grading_eta_seconds=eta, model_eta_seconds=1,
                            model_progress=[{"total": 20, "succeeded": 20, "failed": 0}]),
    )
    assert fields == {"progress_percent": 60, "eta_seconds": expected}


@pytest.mark.parametrize("completed,phase,expected", [(216, "grading", 99), (217, "grading", 99), (217, "done", 99)])
@pytest.mark.parametrize("with_models", [False, True])
def test_unfinished_grading_never_rounds_up_to_complete(completed, phase, expected, with_models):
    models = [{"total": 217, "succeeded": 217, "failed": 0}] * 2 if with_models else []
    snapshot = _grading(total_students=31, total_questions=7, completed_units=completed,
                        phase=phase, model_progress=models)
    assert task_history_progress.progress_fields("grading", snapshot)["progress_percent"] == expected
    assert task_history_progress.progress_fields("graded", snapshot) == {"progress_percent": 100, "eta_seconds": 0}


def test_initial_work_and_finalization_use_the_same_fixed_denominator():
    snapshot = _grading(total_students=31, total_questions=7, completed_units=4,
        model_progress=[{"total": 217, "succeeded": 80, "failed": 0},
                        {"total": 217, "succeeded": 199, "failed": 1}])
    assert task_history_progress.progress_fields("grading", snapshot)["progress_percent"] == 44


@pytest.mark.parametrize("done,failed,expected", [(0, 0, 38), (1, 0, 39), (1, 1, 39), (7, 0, 50)])
def test_question_generation_keeps_per_question_progress(done, failed, expected):
    snapshot = {"total_steps": 8, "completed_steps": 3, "current_step": "generating_solutions",
        "stage_metrics": {"solution_total_questions": 7, "solution_completed_questions": done,
                          "solution_failed_questions": failed}}
    assert task_history_progress.progress_fields("extracting_problems", snapshot)["progress_percent"] == expected


@pytest.fixture
def history(monkeypatch):
    owner = User(id="owner", username="teacher", role="teacher", password_hash="test")
    app = FastAPI()
    app.include_router(tasks.router)
    app.dependency_overrides[tasks.require_teacher] = lambda: owner
    items = {key: {"task_id": key, "name": key, "status": status} for key, status in [
        ("slow", "grading"), ("fast", "grading"), ("complete", "graded"),
        ("draft", "draft"), ("unknown", "error"),
    ]}
    state_calls = []

    def own_tasks(*, owner_id):
        assert owner_id == owner.id
        return items

    async def own_state(*, task_id, owner_id):
        assert owner_id == owner.id
        state_calls.append(task_id)
        return {"status": "grading", "progress": _grading(completed_units=4 if task_id == "slow" else 15, grading_eta_seconds=400 if task_id == "slow" else 33)}

    monkeypatch.setattr(task_facade, "list_tasks", own_tasks)
    monkeypatch.setattr(task_facade, "async_task_state", own_state)
    monkeypatch.setattr(tasks, "_history_facets", lambda items, owner_id: {})
    return TestClient(app), items, state_calls


@pytest.mark.parametrize("sort,expected", [
    ("progress_asc", ["draft", "slow", "fast", "complete", "unknown"]),
    ("progress_desc", ["complete", "fast", "slow", "draft", "unknown"]),
    ("eta_asc", ["complete", "fast", "slow", "draft", "unknown"]),
    ("eta_desc", ["slow", "fast", "complete", "draft", "unknown"]),
])
def test_history_sorts_all_owned_tasks_before_pagination_with_unknowns_last(history, sort, expected):
    client, _, calls = history
    found = []
    for page in (1, 2, 3):
        response = client.get("/tasks/", params={"page": page, "page_size": 2, "sort": sort})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"] == 5
        found.extend(item["task_id"] for item in body["items"])
    assert found == expected
    assert set(calls) == {"slow", "fast"}


def test_dashboard_mapping_contract_does_not_fetch_progress(history):
    client, items, calls = history
    response = client.get("/tasks/")
    assert response.status_code == 200
    assert response.json() == items
    assert calls == []


def test_state_polling_uses_the_same_numeric_progress_and_eta_as_history(history):
    client, _, _ = history
    rows = client.get("/tasks/", params={"page": 1}).json()["items"]
    for row in rows:
        if row["status"] != "grading":
            continue
        response = client.get(f"/tasks/{row['task_id']}/state")
        assert response.status_code == 200, response.text
        for field in ("progress_percent", "eta_seconds"):
            assert response.json()[field] == row[field]


@pytest.mark.parametrize("status,phase,completed,expected", [
    ("grading", "grading", 4, 44),
    ("grading", "grading", 216, 99),
    ("grading", "done", 217, 99),
    ("graded", "done", 217, 100),
])
def test_history_and_live_state_share_grading_boundary(history, monkeypatch, status, phase, completed, expected):
    client, _, _ = history
    async def snapshot(**kwargs):
        early = completed == 4
        return {"status": status, "progress": _grading(phase=phase,
            total_students=31, total_questions=7, completed_units=completed,
            grading_eta_seconds=900 if early else None, model_eta_seconds=1,
            model_progress=[{"total": 217, "succeeded": 80 if early else 217, "failed": 0},
                            {"total": 217, "succeeded": 199 if early else 217, "failed": 1 if early else 0}])}
    monkeypatch.setattr(task_facade, "async_task_state", snapshot)
    row = next(row for row in client.get("/tasks/", params={"page": 1}).json()["items"] if row["task_id"] == "slow")
    state = client.get("/tasks/slow/state").json()
    assert row["status"] == state["status"] == status
    assert row["progress_percent"] == state["progress_percent"] == expected
    assert row["eta_seconds"] == state["eta_seconds"] == (900 if completed == 4 else 0 if status == "graded" else None)


@pytest.mark.parametrize("status,progress,expected", [
    ("grading_paused", _grading(phase="paused", grading_eta_seconds=900,
        model_progress=[{"total": 20, "succeeded": 20, "failed": 0}]), 60),
    ("submissions_paused", _grading(phase="paused", completed_units=2), 50),
    ("problems_paused", {"phase": "paused", "total_steps": 8, "completed_steps": 3}, 38),
])
def test_paused_history_fetches_saved_owner_scoped_progress(history, monkeypatch, status, progress, expected):
    client, items, _ = history
    items["slow"]["status"] = status
    async def snapshot(*, task_id, owner_id):
        assert owner_id == "owner"
        return {"status": status, "progress": progress}
    monkeypatch.setattr(task_facade, "async_task_state", snapshot)
    row = next(row for row in client.get("/tasks/", params={"page": 1}).json()["items"] if row["task_id"] == "slow")
    state = client.get("/tasks/slow/state").json()
    assert row["status"] == state["status"] == status
    assert row["progress_percent"] == state["progress_percent"] == expected
    assert row["eta_seconds"] == state["eta_seconds"] is None


@pytest.mark.parametrize("status", ["grading", "grading_paused"])
def test_unavailable_progress_stays_unknown_without_exposing_errors(monkeypatch, status):
    async def missing(*, task_id, owner_id):
        assert task_id == "owned-task" and owner_id == "owner"
        raise NotFound("private-detail")
    monkeypatch.setattr(task_facade, "async_task_state", missing)
    rows = asyncio.run(task_history_progress.enrich_history_progress(
        [{"task_id": "owned-task", "status": status}], owner_id="owner",
    ))
    assert rows == [{"task_id": "owned-task", "status": status, "progress_percent": None, "eta_seconds": None}]


def test_task_state_keeps_reporter_on_event_loop_and_database_off_loop(monkeypatch):
    threads = {}

    def database_state(*, task_id, owner_id):
        assert task_id == "owned-task" and owner_id == "owner"
        threads["database"] = threading.get_ident()
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        return {"status": "extracting_problems", "active_job_id": "job", "progress": None}

    async def snapshot():
        threads["reporter"] = threading.get_ident()
        asyncio.get_running_loop()
        return SimpleNamespace(model_dump=lambda **_: {"total_steps": 4, "completed_steps": 1, "internal_trace": "PRIVATE"})

    monkeypatch.setattr(task_facade, "task_state", database_state)
    monkeypatch.setattr(task_facade, "get_reporter", lambda _: SimpleNamespace(snapshot=snapshot))
    app = FastAPI()
    app.include_router(tasks.router)
    app.dependency_overrides[tasks.require_teacher] = lambda: SimpleNamespace(id="owner")
    response = TestClient(app).get("/tasks/owned-task/state")
    assert response.status_code == 200, response.text
    assert response.json()["progress"]["total_steps"] == 4
    assert response.json()["progress"]["completed_steps"] == 1
    assert "internal_trace" not in response.json()["progress"]
    assert response.json()["progress_percent"] == 25
    assert response.json()["eta_seconds"] is None
    assert threads["database"] != threads["reporter"]


@pytest.mark.parametrize("status,durable_phase", [("grading", "grading"), ("error", "error")])
def test_grading_state_merges_live_counts_and_preserves_durable_phase_off_loop(monkeypatch, status, durable_phase):
    threads = {}
    active = [{"student_id": "student-1", "q_id": "q2", "skill": "ConceptSkill"}]

    def database_state(*, task_id, owner_id):
        assert (task_id, owner_id) == ("owned-task", "owner")
        threads["state"] = threading.get_ident()
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        return {"status": status, "grading_job_id": "run", "active_job_id": "run" if status == "grading" else None,
                "last_failed_job_id": "run" if status == "error" else None, "error": None}

    def database_progress(run_id, owner_id):
        assert (run_id, owner_id) == ("run", "owner")
        threads["grading"] = threading.get_ident()
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        return _grading(job_id="run", phase=durable_phase, completed_units=1, active=[],
                        model_progress=[{"id": "model-1", "label": "Synthetic", "responding": 1}],
                        activity_latest={"sequence": 1, "ts": 100, "code": "responding", "model_id": "model-1"},
                        error_detail="grading_failed" if status == "error" else None)

    async def snapshot():
        threads["reporter"] = threading.get_ident()
        asyncio.get_running_loop()
        return SimpleNamespace(model_dump=lambda **_: {
            "phase": "done", "completed_units": 2, "active": active, "error_detail": None,
        })

    monkeypatch.setattr(task_facade, "task_state", database_state)
    monkeypatch.setattr(task_facade, "_grading_progress", database_progress)
    monkeypatch.setattr(task_facade, "get_reporter", lambda job_id: SimpleNamespace(snapshot=snapshot) if job_id == "run" else None)
    app = FastAPI()
    app.include_router(tasks.router)
    app.dependency_overrides[tasks.require_teacher] = lambda: SimpleNamespace(id="owner")
    response = TestClient(app).get("/tasks/owned-task/state")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["progress"]["completed_units"] == 2
    assert body["progress"]["active"] == []
    assert body["progress"].get("running_units", 0) == (1 if status == "grading" else 0)
    assert "ConceptSkill" not in response.text
    assert body["progress"]["phase"] == durable_phase
    # The first active-run poll must keep public activity even when a live
    # reporter snapshot (which has no model summary) is available.
    assert body["progress"]["job_id"] == "run"
    assert body["progress"]["model_progress"][0]["label"] == "Synthetic"
    assert body["progress"]["activity_latest"]["code"] == "responding"
    assert body["error"] == ("grading_failed" if status == "error" else None)
    assert body["progress_percent"] == (10 if status == "grading" else None)
    assert threads["state"] != threads["reporter"]
    assert threads["grading"] != threads["reporter"]
