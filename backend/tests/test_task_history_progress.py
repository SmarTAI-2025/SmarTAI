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
    ("review_confirmed", None, (100, 0)),
    ("finalized", None, (100, 0)),
    ("problems_ready", None, (None, None)),
    ("submissions_ready", None, (None, None)),
    ("error", _grading(phase="error"), (None, None)),
    ("grading", None, (None, None)),
    ("grading", _grading(), (20, 400)),
    ("grading", _grading(completed_units=15), (75, 33)),
    ("grading", _grading(completed_units=20), (100, 0)),
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
    fields = task_history_progress.progress_fields(status, progress, now=200)
    assert (fields["progress_percent"], fields["eta_seconds"]) == expected


def test_eta_accepts_millisecond_start_timestamps():
    fields = task_history_progress.progress_fields(
        "grading", _grading(started_at=1_800_000_000_000), now=1_800_000_100,
    )
    assert fields == {"progress_percent": 20, "eta_seconds": 400}


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
        return {"status": "grading", "progress": _grading(completed_units=4 if task_id == "slow" else 15)}

    monkeypatch.setattr(task_facade, "list_tasks", own_tasks)
    monkeypatch.setattr(task_facade, "async_task_state", own_state)
    monkeypatch.setattr(tasks, "_history_facets", lambda items, owner_id: {})
    monkeypatch.setattr(task_history_progress.time, "time", lambda: 200)
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


def test_unavailable_progress_stays_unknown_without_exposing_errors(monkeypatch):
    async def missing(*, task_id, owner_id):
        assert task_id == "owned-task" and owner_id == "owner"
        raise NotFound("private-detail")
    monkeypatch.setattr(task_facade, "async_task_state", missing)
    rows = asyncio.run(task_history_progress.enrich_history_progress(
        [{"task_id": "owned-task", "status": "grading"}], owner_id="owner",
    ))
    assert rows == [{"task_id": "owned-task", "status": "grading", "progress_percent": None, "eta_seconds": None}]


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
        return SimpleNamespace(model_dump=lambda **_: {"total_steps": 4, "completed_steps": 1})

    monkeypatch.setattr(task_facade, "task_state", database_state)
    monkeypatch.setattr(task_facade, "get_reporter", lambda _: SimpleNamespace(snapshot=snapshot))
    app = FastAPI()
    app.include_router(tasks.router)
    app.dependency_overrides[tasks.require_teacher] = lambda: SimpleNamespace(id="owner")
    response = TestClient(app).get("/tasks/owned-task/state")
    assert response.status_code == 200, response.text
    assert response.json()["progress"] == {"total_steps": 4, "completed_steps": 1}
    assert response.json()["progress_percent"] == 25
    assert response.json()["eta_seconds"] is None
    assert threads["database"] != threads["reporter"]
